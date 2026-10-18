import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile, readdir, mkdir} from 'node:fs/promises';
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
const artifacts = path.resolve(web, '../../../test-artifacts/backup-ui');

test('backup UI previews deletion, waits for job and renders at three sizes/DPI', async () => {
  const server = createServer(async (request, response) => {
    try {
      if (request.url === '/') {
        response.end('<!doctype html><html lang="vi"><head>' +
          ['tokens','base','shell','components','pages'].map(name => `<link rel="stylesheet" href="/styles/${name}.css">`).join('') +
          '</head><body><main id="host" style="padding:24px"></main></body></html>'); return;
      }
      const target = path.resolve(web, '.' + request.url);
      if (!target.startsWith(web + path.sep)) {response.writeHead(403).end(); return;}
      response.setHeader('Content-Type', target.endsWith('.css') ? 'text/css' : 'text/javascript');
      response.end(await readFile(target));
    } catch {response.writeHead(404).end();}
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    await mkdir(artifacts, {recursive: true});
    browser = await playwright.chromium.launch({headless: true,
      executablePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'});
    for (const [width,height,dpi] of [[1366,768,1],[1920,1080,1.5],[2560,1440,2]]) {
      const context = await browser.newContext({viewport: {width,height}, deviceScaleFactor: dpi});
      const page = await context.newPage();
      const errors = [], calls = [];
      page.on('pageerror', error => errors.push(error.message));
      let deleted = false;
      await page.route('**/api/**', async route => {
        const url = new URL(route.request().url());
        let payload = {ok: true};
        if (url.pathname === '/api/workspace/backups') payload.items = deleted ? [] : [
          {id: 'fixture-one', created_at: '2026-10-02T00:00:00Z', scope: 'partial', parts: ['khach_hang'], bytes: 12345}];
        else if (url.pathname === '/api/system/backup-parts') payload.parts = [{id:'khach_hang', name:'Khách hàng', available:true}];
        else if (url.pathname.endsWith('/delete/preview')) {
          calls.push(['preview', route.request().postDataJSON()]);
          payload = {ok:true, ids:['fixture-one'], reclaimable_bytes:12345, preview_token:'verified'};
        } else if (url.pathname.endsWith('/delete')) {
          calls.push(['delete', route.request().postDataJSON()]);
          payload = {ok:true, accepted:true, job_id:'test-job'};
        } else if (url.pathname.endsWith('/jobs/test-job')) {
          deleted = true;
          payload = {ok:true, state:'completed', progress:1, result:{deleted_ids:['fixture-one']}};
        }
        await route.fulfill({status: payload.accepted ? 202 : 200, json:payload});
      });
      await page.goto(`http://127.0.0.1:${server.address().port}`);
      await page.evaluate(async () => {
        const {default: system} = await import('/app/pages/system.js');
        window.mounted = system.mount({route:{sub:'backups'}, shell:{main:document.querySelector('#host')},
          navigate(){}, store:{get:()=>({dashboard:{}})}});
      });
      const select = page.getByRole('checkbox', {name:'Chọn bản sao lưu fixture-one'});
      await select.waitFor();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({path:path.join(artifacts, `backups-${width}-dpi${dpi*100}.png`), fullPage:true});
      await select.check();
      await page.getByRole('button', {name:'Xoá đã chọn', exact:true}).click();
      await page.getByRole('dialog').waitFor();
      assert.equal(calls.length, 1, 'preview must not delete');
      await page.getByRole('dialog').getByRole('button', {name:'Xoá', exact:true}).click();
      await page.getByText('Chưa có bản sao lưu', {exact:true}).waitFor();
      assert.deepEqual(calls[1], ['delete', {ids:['fixture-one'], preview_token:'verified'}]);
      assert.deepEqual(errors, []);
      await page.evaluate(() => mounted.unmount());
      await context.close();
    }
  } finally {await browser?.close(); await new Promise(resolve => server.close(resolve));}
});
