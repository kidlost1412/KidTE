import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile, readdir} from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {createServer} from 'node:http';
const require = createRequire(import.meta.url);
let playwright;
try {playwright = require('playwright');} catch {
  const root = path.join(process.env.LOCALAPPDATA, 'OpenAI', 'Codex', 'runtimes', 'cua_node');
  playwright = require(path.join(root, (await readdir(root)).sort().at(-1), 'bin', 'node_modules', 'playwright'));
}
const web = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\/(\w:)/, '$1')), '../../src/web');

test('Studio keeps edits on failed save and awaits a slow save before leaving', async () => {
  const server = createServer(async (request, response) => {
    try {
      if (request.url === '/') {response.setHeader('Content-Type', 'text/html'); response.end('<!doctype html><main id="host"></main>'); return;}
      const target = path.resolve(web, '.' + request.url);
      if (!target.startsWith(web + path.sep)) {response.writeHead(403).end(); return;}
      response.setHeader('Content-Type', 'text/javascript');
      response.end(await readFile(target));
    } catch (_) {response.writeHead(404).end();}
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  const calls = [];
  let fail = true;
  let release;
  let revision = 0;
  const timeline = {video_id: 'v', title: 'Original', cycle_ms: 60000, repin_every_ms: 20000,
    products: {sku_1: {product_id: '1', bag_index: 1, short_name: 'TC01', include_all: ['tc01']}},
    events: [{at_ms: 0, product: 'sku_1'}], script_contexts: []};
  try {
    browser = await playwright.chromium.launch({headless: true,
      executablePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'});
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      let payload = {ok: true};
      if (url.pathname === '/api/video/catalog') payload.products = [{product_id: '1', bag_index: 1, name: 'TC01', title: 'TC01'}];
      else if (url.pathname === '/api/video/assets') payload.state = 'missing_media';
      else if (url.pathname.endsWith('/draft')) {
        calls.push(route.request().postDataJSON());
        if (fail) {await route.fulfill({status: 500, json: {ok: false, error: 'internal_error'}}); return;}
        await new Promise(resolve => {release = resolve;});
        payload = {ok: true, saved_at: 'now', draft_revision: ++revision};
      } else if (url.pathname === '/api/workspace/videos/v') payload = {ok: true, revision: 'live',
        data: {timeline, draft_revision: revision}};
      await route.fulfill({json: payload});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.evaluate(async () => {
      const {mountStudio} = await import('/app/pages/video/studio.js');
      window.studio = mountStudio({navigate() {}, shell: {showShortcuts() {}},
        store: {get: () => ({}), subscribe: () => () => {}}}, document.querySelector('#host'), {kind: 'library', id: 'v'});
    });
    const title = page.getByRole('textbox', {name: 'Tên video'});
    await title.waitFor();
    await title.fill('Changed');
    await title.blur();
    await page.waitForFunction(() => document.querySelector('.save-state').textContent.includes('Lưu lỗi'));
    assert.equal(await page.evaluate(() => studio.isDirty()), true);
    const rows = await page.evaluate(async () => (await (await import('/app/pages/video/recovery.js')).listRecovery('library:v')).map(row => row.document.title));
    assert.ok(rows.includes('Changed'));
    fail = false;
    await page.evaluate(() => {window.leaveFinished = false; window.leaveTask = studio.beforeLeave().then(ok => {window.leaveFinished = ok;});});
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(() => leaveFinished), false);
    assert.equal(calls.at(-1).expected_draft_revision, 0);
    release();
    await page.waitForFunction(() => leaveFinished === true);
    assert.equal(await page.evaluate(() => studio.isDirty()), false);
    await page.evaluate(() => studio.unmount());
    assert.deepEqual(errors, []);
  } finally {
    if (release) release();
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
});
