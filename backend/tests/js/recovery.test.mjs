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
  const latest = (await readdir(root)).sort().at(-1);
  playwright = require(path.join(root, latest, 'bin', 'node_modules', 'playwright'));
}

test('real IndexedDB retains 20 recovery points across reload and reports quota errors', async () => {
  const script = await readFile(new URL('../../src/web/app/pages/video/recovery.js', import.meta.url));
  const server = createServer((request, response) => {
    response.setHeader('Content-Type', request.url === '/recovery.js' ? 'text/javascript' : 'text/html');
    response.end(request.url === '/recovery.js' ? script : '<!doctype html><title>Isolated recovery test</title>');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await playwright.chromium.launch({headless: true,
      executablePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'});
    const page = await browser.newPage();
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    const result = await page.evaluate(async () => {
      await new Promise((resolve, reject) => {
        const open = indexedDB.open('kid-te-studio-recovery', 1);
        open.onupgradeneeded = () => {
          const store = open.result.createObjectStore('snapshots', {keyPath:'snapshot_id'});
          store.createIndex('document_key', 'document_key');
        };
        open.onerror = () => reject(open.error);
        open.onsuccess = () => {
          const db = open.result;
          const tx = db.transaction('snapshots', 'readwrite');
          tx.objectStore('snapshots').put({snapshot_id:'legacy', document_key:'library:other',
            saved_at_ms:Date.now(), document:{marker:'preserved'}});
          tx.oncomplete = () => {db.close(); resolve();};
          tx.onabort = () => reject(tx.error);
        };
      });
      const recovery = await import('/recovery.js');
      const oldGetAll = IDBObjectStore.prototype.getAll;
      IDBObjectStore.prototype.getAll = () => {throw new Error('Full recovery store scan is forbidden');};
      if ((await recovery.listRecovery('library:other'))[0].document.marker !== 'preserved') throw new Error('Migration lost recovery');
      const now = Date.now;
      let instant = now() - 31 * 86400000;
      Date.now = () => instant;
      await recovery.saveRecovery('library:v', {document: {marker: 'expired'}, revision: 'live'});
      instant = now();
      for (let i = 0; i < 25; i++) {
        instant++;
        await recovery.saveRecovery('library:v', {document: {marker: i}, revision: 'live', selected: i});
      }
      Date.now = now;
      const rows = await recovery.listRecovery('library:v');
      IDBObjectStore.prototype.getAll = oldGetAll;
      return rows.map(row => ({marker: row.document.marker, selected: row.selected, revision: row.revision}));
    });
    assert.equal(result.length, 20);
    assert.deepEqual(result[0], {marker: 24, selected: 24, revision: 'live'});
    assert.equal(result.at(-1).marker, 5);
    await page.reload();
    assert.equal(await page.evaluate(async () => (await (await import('/recovery.js')).listRecovery('library:v')).length), 20);
    const quota = await page.evaluate(async () => {
      const original = IDBDatabase.prototype.transaction;
      IDBDatabase.prototype.transaction = () => {throw new DOMException('full', 'QuotaExceededError');};
      try {await (await import('/recovery.js')).saveRecovery('library:v', {document: {}});}
      catch (error) {return error.name;}
      finally {IDBDatabase.prototype.transaction = original;}
    });
    assert.equal(quota, 'QuotaExceededError');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
});
