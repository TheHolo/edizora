import test, { afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { api, ApiError, clearSession, mediaBlob, safeURL, setSession } from '../src/api.ts';

globalThis.window = new EventTarget();
globalThis.window.location = { origin: 'https://publisher.test' };
globalThis.location = globalThis.window.location;
const originalFetch = globalThis.fetch;
const response = value => new Response(JSON.stringify(value), { headers: { 'content-type': 'application/json' } });
afterEach(() => { globalThis.fetch = originalFetch; clearSession(); });

test('lost response retry keeps its idempotency key; next accepted command gets a new key', async () => {
  const attempts = [];
  globalThis.fetch = async (url, options) => {
    attempts.push({url, ...options});
    if (attempts.length === 1) throw new TypeError('test connection dropped');
    return response({ operation_id: 'accepted-operation' });
  };
  const request = { method: 'POST', body: { title: 'Тестовый черновик' } };
  await assert.rejects(api('/publications/text-draft', request), error => error instanceof ApiError && error.status === 0);
  const accepted = await api('/publications/text-draft', request);
  assert.equal(accepted.operation_id, 'accepted-operation');
  assert.equal(attempts[0].headers['Idempotency-Key'], attempts[1].headers['Idempotency-Key']);
  await api('/publications/text-draft', request);
  assert.notEqual(attempts[1].headers['Idempotency-Key'], attempts[2].headers['Idempotency-Key']);
});

test('different pending payloads never reuse one command key', async () => {
  const keys = [];
  globalThis.fetch = async (_url, options) => {
    keys.push(options.headers['Idempotency-Key']);
    if (keys.length === 1) throw new TypeError('test connection dropped');
    return response({});
  };
  await assert.rejects(api('/test/payload', { method: 'POST', body: { text: 'first' } }));
  await api('/test/payload', { method: 'POST', body: { text: 'second' } });
  assert.notEqual(keys[0], keys[1]);
});

test('cookie mutations send CSRF and Mini App calls send the memory bearer', async () => {
  setSession({user:{id:1},csrf_token:'test-csrf',token:'test-bearer'});
  let request;
  globalThis.fetch = async (_url, options) => { request = options; return response({}); };
  await api('/test/authenticated', {method:'PATCH', body:{expected_version:1}});
  assert.equal(request.headers['X-CSRF-Token'],'test-csrf');
  assert.equal(request.headers.Authorization,'Bearer test-bearer');
  assert.equal(request.credentials,'same-origin');
});

test('GET requests carry no mutation key and an empty 204 response is valid', async () => {
  let request;
  globalThis.fetch = async (_url, options) => { request = options; return new Response(null,{status:204}); };
  assert.equal(await api('/test/no-content'),null);
  assert.equal(request.headers['Idempotency-Key'],undefined);
  assert.equal(request.headers['X-CSRF-Token'],undefined);
});

test('expired privileged confirmation prompts reauthentication without retrying the action', async () => {
  let prompts=0;let calls=0;
  const listener=()=>prompts++;
  window.addEventListener('publisher-reauth-needed',listener);
  globalThis.fetch=async()=>{calls++;return new Response(JSON.stringify({error:{code:'reauth_required',message:'Подтвердите личность'}}),{status:403,headers:{'content-type':'application/json'}});};
  await assert.rejects(api('/deployment/plans',{method:'POST',body:{}}),error=>error instanceof ApiError&&error.data.code==='reauth_required');
  assert.equal(prompts,1);assert.equal(calls,1);
  window.removeEventListener('publisher-reauth-needed',listener);
});

test('bearer media stays on the application origin and external URLs never receive credentials', async () => {
  setSession({user:{id:1},token:'test-bearer'});
  let calls=0;
  globalThis.fetch=async(_url, options)=>{calls++;assert.equal(options.headers.Authorization,'Bearer test-bearer');return new Response('image',{headers:{'content-type':'image/png'}});};
  const signal=new AbortController().signal;
  await assert.rejects(mediaBlob('https://external.test/image.png',signal));
  assert.equal(calls,0);
  const blob=await mediaBlob('/api/v1/assets/test/thumbnail',signal);
  assert.equal(blob.type,'image/png');assert.equal(calls,1);
  assert.equal(safeURL('javascript:alert(1)'),undefined);
});

test('wire channel fields and controller collection arrays normalize for the same UI', async () => {
  globalThis.fetch=async()=>response({items:[{id:3,version:2,title:'Тестовый канал',telegram_chat_id:'-1000000',is_enabled:true,is_paused:false,publish_interval_seconds:600}]});
  const channels=await api('/channels');
  assert.equal(channels.items[0].name,'Тестовый канал');
  assert.equal(channels.items[0].interval_seconds,600);
  globalThis.fetch=async()=>response([{node_id:'test-node',role:'standby'}]);
  assert.equal((await api('/deployment/nodes')).items[0].node_id,'test-node');
});
