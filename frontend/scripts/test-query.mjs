import test, { afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { QueryObserver } from '@tanstack/react-query';
import { resetApiTransport, setApiTransport } from '../src/api.ts';
import { apiQueryKey, apiQueryOptions, createQueryClient } from '../src/shared/api/query.ts';

globalThis.window = new EventTarget();
globalThis.window.location = { origin: 'https://publisher.test' };
globalThis.location = globalThis.window.location;
afterEach(resetApiTransport);

test('observers share a request and one addressed invalidation refreshes both', async () => {
  let calls = 0;
  let finishFirst;
  const pending = new Promise(resolve => { finishFirst = resolve; });
  setApiTransport({ async request() {
    calls++;
    if (calls === 1) await pending;
    return { status: 200, body: { revision: calls } };
  } });
  const client = createQueryClient();
  const options = apiQueryOptions('number:1', '/channels');
  const first = new QueryObserver(client, options);
  const second = new QueryObserver(client, options);
  const stopFirst = first.subscribe(() => {});
  const stopSecond = second.subscribe(() => {});
  try {
    assert.equal(calls, 1);
    const initial = first.refetch();
    finishFirst();
    await initial;
    assert.equal(calls, 1);
    assert.deepEqual(second.getCurrentResult().data, { revision: 1 });
    await client.invalidateQueries({ queryKey: apiQueryKey('number:1', '/channels'), exact: true });
    assert.equal(calls, 2);
    assert.deepEqual(first.getCurrentResult().data, { revision: 2 });
    assert.deepEqual(second.getCurrentResult().data, { revision: 2 });
  } finally { stopFirst(); stopSecond(); client.clear(); }
});

test('switching search cancels the old read and cannot display its stale result', async () => {
  let oldSignal;
  let oldStarted;
  const started = new Promise(resolve => { oldStarted = resolve; });
  setApiTransport({ request({ path, signal }) {
    if (path.includes('old')) {
      oldSignal = signal;
      oldStarted();
      return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true }));
    }
    return Promise.resolve({ status: 200, body: { items: ['new result'] } });
  } });
  const client = createQueryClient();
  const observer = new QueryObserver(client, apiQueryOptions('number:1', '/publications?search=old'));
  const stop = observer.subscribe(() => {});
  try {
    await started;
    observer.setOptions(apiQueryOptions('number:1', '/publications?search=new'));
    assert.equal(oldSignal.aborted, true);
    await observer.refetch();
    assert.deepEqual(observer.getCurrentResult().data, { items: ['new result'] });
    assert.equal(client.getQueryData(apiQueryKey('number:1', '/publications?search=old')), undefined);
  } finally { stop(); client.clear(); }
});

test('cache keys isolate accounts and clearing removes their data', () => {
  const client = createQueryClient();
  client.setQueryData(apiQueryKey('number:1', '/channels'), { private: 'first' });
  assert.equal(client.getQueryData(apiQueryKey('number:2', '/channels')), undefined);
  client.clear();
  assert.equal(client.getQueryData(apiQueryKey('number:1', '/channels')), undefined);
});
