import test, { afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { api, ApiError, clearSession, pickNativeFiles, releaseNativeFiles, resetApiTransport, setApiTransport, setSession, upload } from '../src/api.ts';
import { DesktopTransport } from '../src/platform/transport.ts';

globalThis.window = new EventTarget();
globalThis.window.location = { origin: 'http://tauri.localhost' };
globalThis.location = globalThis.window.location;
afterEach(() => { clearSession(); resetApiTransport(); });

test('desktop JSON uses the bridge without sending renderer credentials', async () => {
  const calls = [];
  setSession({ user: { id: 1 }, csrf_token: 'private-csrf', token: 'private-bearer' });
  setApiTransport(new DesktopTransport(async (command, args) => {
    calls.push({ command, args });
    return { status: 200, body: { id: 'fixture' }, request_id: 'fixture-request' };
  }));
  assert.deepEqual(await api('/channels', { method: 'POST', body: { title: 'Synthetic channel' } }), { id: 'fixture' });
  assert.equal(calls[0].command, 'desktop_request');
  assert.equal(calls[0].args.path, '/api/v1/channels');
  assert.equal(calls[0].args.method, 'POST');
  assert.match(calls[0].args.key, /^[0-9a-f-]{36}$/);
  assert.doesNotMatch(JSON.stringify(calls[0]), /private-csrf|private-bearer|Authorization/);
});

test('aborting a desktop mutation preserves the key if the server may finish it', async () => {
  const calls = [];
  let firstReceived;
  const received = new Promise(resolve => { firstReceived = resolve; });
  let finishFirst;
  setApiTransport(new DesktopTransport(async (_command, args) => {
    calls.push(args);
    if (calls.length === 1) {
      firstReceived();
      return await new Promise(resolve => { finishFirst = resolve; });
    }
    return { status: 200, body: { accepted: true } };
  }));
  const controller = new AbortController();
  const input = { method: 'POST', body: { title: 'pending desktop draft' }, signal: controller.signal };
  const first = api('/publications/text-draft', input);
  await received;
  controller.abort();
  await assert.rejects(first, error => error instanceof DOMException && error.name === 'AbortError');
  finishFirst({ status: 200, body: { accepted: true } });
  assert.deepEqual(await api('/publications/text-draft', { ...input, signal: undefined }), { accepted: true });
  assert.equal(calls[0].key, calls[1].key);
  await api('/publications/text-draft', { ...input, signal: undefined });
  assert.notEqual(calls[1].key, calls[2].key);
});

test('desktop errors keep the common reauthentication behavior', async () => {
  let prompts = 0;
  const listener = () => { prompts++; };
  window.addEventListener('publisher-reauth-needed', listener);
  setApiTransport(new DesktopTransport(async () => ({ status: 403, body: { error: { code: 'reauth_required', message: 'Confirm identity' } } })));
  await assert.rejects(api('/deployment/plans', { method: 'POST', body: {} }), error => error instanceof ApiError && error.data.code === 'reauth_required');
  assert.equal(prompts, 1);
  window.removeEventListener('publisher-reauth-needed', listener);
});

test('pending mutation keys are scoped to a user across session changes', async () => {
  const calls = [];
  setApiTransport(new DesktopTransport(async (_command, args) => {
    calls.push(args);
    if (calls.length === 1) throw new Error('response lost');
    return { status: 200, body: {} };
  }));
  const input = { method: 'POST', body: { title: 'same draft' } };
  setSession({ user: { id: 111 } });
  await assert.rejects(api('/publications/text-draft', input));
  setSession({ user: { id: 222 } });
  await api('/publications/text-draft', input);
  setSession({ user: { id: 111 } });
  await api('/publications/text-draft', input);
  assert.notEqual(calls[0].key, calls[1].key);
  assert.equal(calls[0].key, calls[2].key);
});

test('a second concurrent response lost after success retains the same mutation key', async () => {
  const calls = [];
  const completions = [];
  let bothReceived;
  const received = new Promise(resolve => { bothReceived = resolve; });
  setApiTransport(new DesktopTransport(async (_command, args) => {
    calls.push(args);
    if (calls.length <= 2) {
      const result = new Promise((resolve, reject) => { completions.push({ resolve, reject }); });
      if (calls.length === 2) bothReceived();
      return await result;
    }
    return { status: 200, body: { accepted: true } };
  }));
  const input = { method: 'POST', body: { title: 'concurrent desktop draft' } };
  const first = api('/publications/text-draft', input);
  const second = api('/publications/text-draft', input);
  await received;
  completions[0].resolve({ status: 200, body: { accepted: true } });
  await first;
  completions[1].reject(new Error('response lost'));
  await assert.rejects(second, error => error instanceof ApiError && error.status === 0);
  await api('/publications/text-draft', input);
  assert.equal(calls[0].key, calls[1].key);
  assert.equal(calls[1].key, calls[2].key);
});

test('an already cancelled desktop request never reaches the bridge', async () => {
  let calls = 0;
  setApiTransport(new DesktopTransport(async () => { calls++; return { status: 200, body: {} }; }));
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(api('/channels', { signal: controller.signal }), error => error instanceof DOMException && error.name === 'AbortError');
  assert.equal(calls, 0);
});

test('desktop media references are opaque and revoked if the view goes away', async () => {
  const calls = [];
  let deliver;
  const transport = new DesktopTransport(async (command, args) => {
    calls.push({ command, args });
    if (command === 'desktop_media') return await new Promise(resolve => { deliver = resolve; });
    return undefined;
  });
  const controller = new AbortController();
  const media = transport.media('/api/v1/assets/fixture/thumbnail', controller.signal);
  controller.abort();
  deliver('http://edizora-media.localhost/opaque-fixture');
  await assert.rejects(media, error => error instanceof DOMException && error.name === 'AbortError');
  assert.deepEqual(calls.map(call => call.command), ['desktop_media', 'desktop_revoke_media']);
  assert.equal(calls[1].args.url, 'http://edizora-media.localhost/opaque-fixture');
});

test('desktop selection and streaming uploads pass handles rather than file contents', async () => {
  const calls = [];
  setApiTransport(new DesktopTransport(async (command, args) => {
    calls.push({ command, args });
    if (command === 'desktop_pick_files') return [{ id: 'fixture-handle', name: 'fixture.png', size: 32 }];
    if (command === 'desktop_upload') return { status: 201, body: { id: 'fixture-asset' } };
    return undefined;
  }));
  const files = await pickNativeFiles();
  assert.deepEqual(files, [{ kind: 'desktop', id: 'fixture-handle', name: 'fixture.png', size: 32 }]);
  assert.deepEqual(await upload(files[0]), { id: 'fixture-asset' });
  await releaseNativeFiles(files);
  assert.deepEqual(calls.map(call => call.command), ['desktop_pick_files', 'desktop_upload', 'desktop_release_files']);
  assert.deepEqual(calls[2].args.ids, ['fixture-handle']);
  assert.equal(calls[1].args.id, 'fixture-handle');
  assert.match(calls[1].args.requestId, /^[0-9a-f-]{36}$/);
  assert.equal('body' in calls[1].args, false);
});
