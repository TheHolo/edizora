// Browser-only integration test. Every API response is synthetic and all
// non-local requests are blocked; never point this test at a real profile.
import test from 'node:test';
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { preview } from 'vite';

test('web login, shared queries, logout and account switch', { timeout: 60_000 }, async () => {
  const executablePath = process.env.PLAYWRIGHT_BROWSER_EXECUTABLE;
  if (!executablePath) throw new Error('Set PLAYWRIGHT_BROWSER_EXECUTABLE to an installed Chromium-based browser.');
  const server = await preview({ logLevel: 'silent', preview: { host: '127.0.0.1', port: 0, strictPort: true } });
  const address = server.httpServer.address();
  if (!address || typeof address === 'string') throw new Error('The isolated preview server did not start.');
  const origin = `http://127.0.0.1:${address.port}`;
  let browser;
  try {
    browser = await chromium.launch({ executablePath, headless: true, args: ['--disable-background-networking', '--no-proxy-server'] });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    let account = null;
    const counts = new Map();
    const unexpected = [];
    const logoutTokens = [];
    const uploads = [];
    await context.route('**/*', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin !== origin) { await route.abort(); return; }
      if (!url.pathname.startsWith('/api/v1/')) { await route.continue(); return; }
      const path = url.pathname.slice('/api/v1'.length);
      counts.set(`${account}:${path}`, (counts.get(`${account}:${path}`) || 0) + 1);
      let status = 200;
      let body;
      if (path === '/auth/session') {
        status = 401; body = { error: { code: 'unauthorized', message: 'Sign in' } };
      } else if (path === '/auth/password') {
        const input = request.postDataJSON();
        account = Number(input.telegram_id);
        if (![111, 222].includes(account) || input.password !== 'test-only-password') throw new Error('Unexpected synthetic login');
        body = { user: { id: account, name: `Fixture ${account}` }, csrf_token: `csrf-${account}` };
      } else if (path === '/auth/logout') {
        logoutTokens.push(request.headers()['x-csrf-token']); account = null;
        status = 204;
      } else if (!account) {
        status = 401; body = { error: { code: 'unauthorized', message: 'Sign in' } };
      } else if (path === '/assets/uploads' && request.method() === 'POST') {
        uploads.push({ csrf: request.headers()['x-csrf-token'], type: request.headers()['content-type'], body: request.postDataBuffer()?.toString('utf8') });
        status = 201; body = { id: 'browser-fixture-asset' };
      } else if (/^\/assets\/fixture(?:111|222)\/thumbnail$/.test(path)) {
        const pixel = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');
        await route.fulfill({ status: 200, body: pixel, contentType: 'image/png' });
        return;
      } else if (path === '/channels') {
        body = { items: [{ id: account, version: 1, title: `Fixture channel ${account}`, telegram_chat_id: `-${account}`, is_enabled: true, is_paused: false }] };
      } else if (path === '/settings') body = { timezone: 'UTC' };
      else if (path === '/desktop/status') body = { telegram_configured: true };
      else if (path === '/deployment/current') body = { role: 'active', hostname: 'Fixture host' };
      else if (path === '/publication-control') body = { publication_allowed: true, blocks: {} };
      else if (path === '/publications') {
        const title = account === 111 ? 'First account draft' : 'Second account draft';
        body = { items: [{ id: account, version: 1, title, status: 'waiting_confirmation', content_kind: 'text', album: { cover: `/api/v1/assets/fixture${account}/thumbnail`, total: 1, selected: 1 } }], total: 1 };
      } else if (path === '/operations') body = { items: [], total: 0 };
      else { unexpected.push(`${request.method()} ${path}`); status = 500; body = { error: { code: 'unexpected_fixture_request', message: 'Unexpected fixture request' } }; }
      await route.fulfill(status === 204 ? { status } : { status, json: body });
    });
    const page = await context.newPage();
    const pageErrors = [];
    page.on('pageerror', error => pageErrors.push(error.message));
    await page.goto(origin, { waitUntil: 'domcontentloaded' });
    async function login(id) {
      await page.locator('input[autocomplete=username]').fill(String(id));
      await page.locator('input[autocomplete=current-password]').fill('test-only-password');
      await page.locator('form button.primary').click();
    }
    await login(111);
    await page.getByText('First account draft', { exact: true }).waitFor();
    await page.waitForFunction(() => [...document.querySelectorAll('#main img.thumbnail')].some(image => image.naturalWidth === 1));
    assert.equal(counts.get('111:/channels'), 1);
    await page.locator('aside a[href="/channels"]').click();
    await page.locator('#main').getByText('Fixture channel 111', { exact: true }).first().waitFor();
    assert.equal(counts.get('111:/channels'), 1, 'the channels page should reuse the active query');
    await page.locator('.channel-select select').selectOption('111');
    await page.getByRole('button', { name: 'Sign out' }).click();
    await page.locator('input[autocomplete=username]').waitFor();
    await login(222);
    await page.locator('#main').getByText('Fixture channel 222', { exact: true }).first().waitFor();
    assert.equal(await page.locator('.channel-select select').inputValue(), '');
    assert.equal(await page.getByText('Fixture channel 111', { exact: true }).count(), 0);
    assert.equal(counts.get('222:/channels'), 1);
    await page.locator('aside a.nav-item[href="/publications"]').click();
    await page.getByText('Second account draft', { exact: true }).waitFor();
    assert.equal(await page.getByText('First account draft', { exact: true }).count(), 0);

    // A link and browser Back must both protect a draft that is still local.
    await page.locator('#main a[href="/publications/new"]').click();
    const draftTitle = page.locator('#main form input').first();
    await draftTitle.fill('Unsent fixture');
    await page.locator('#main input[type=file]').setInputFiles({ name: 'browser-fixture.png', mimeType: 'image/png', buffer: Buffer.from('synthetic image') });
    await page.getByText('browser-fixture.png', { exact: true }).waitFor();
    let dialogPromise = page.waitForEvent('dialog', { timeout: 5000 });
    let click = page.locator('aside a.nav-item[href="/channels"]').click();
    let dialog = await dialogPromise;
    await dialog.dismiss(); await click;
    assert.equal(new URL(page.url()).pathname, '/publications/new');
    assert.equal(await draftTitle.inputValue(), 'Unsent fixture');

    dialogPromise = page.waitForEvent('dialog', { timeout: 5000 });
    await page.evaluate(() => history.back());
    dialog = await dialogPromise;
    await dialog.dismiss();
    await page.waitForURL('**/publications/new');
    assert.equal(await draftTitle.inputValue(), 'Unsent fixture');

    dialogPromise = page.waitForEvent('dialog', { timeout: 5000 });
    click = page.locator('aside a.nav-item[href="/channels"]').click();
    dialog = await dialogPromise;
    await dialog.accept(); await click;
    await page.locator('#main').getByText('Fixture channel 222', { exact: true }).first().waitFor();
    assert.deepEqual(logoutTokens, ['csrf-111']);
    assert.equal(uploads.length, 1);
    assert.equal(uploads[0].csrf, 'csrf-222');
    assert.match(uploads[0].type, /^multipart\/form-data; boundary=/);
    assert.match(uploads[0].body, /browser-fixture\.png/);
    assert.deepEqual(unexpected, []);
    assert.deepEqual(pageErrors, []);
    await context.close();
  } finally {
    await browser?.close();
    await new Promise((resolve, reject) => server.httpServer.close(error => error ? reject(error) : resolve()));
  }
});
