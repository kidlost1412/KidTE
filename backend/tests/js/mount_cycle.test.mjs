// E05: mỗi trang mount/unmount 30 lần -> không còn timer, listener, fetch đang treo.
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

const PAGES = [
  ['overview', ''], ['inbox', ''], ['inbox', 'unanswered'], ['brain', 'knowledge'], ['brain', 'docs'], ['brain', 'try'], ['brain', 'style'], ['brain', 'relearn'],
  ['customers', 'customers'], ['customers', 'facts'], ['customers', 'sessions'],
  ['catalog', 'products'], ['catalog', 'profiles'], ['catalog', 'mapping'],
  ['video', ''], ['video', 'studio', 'v1'], ['video', 'review', 'j1'],
  ['automation', ''], ['automation', 'chinh-sach-gia'], ['automation', 'dong-bo-video'],
  ['system', 'setup'], ['system', 'live'], ['system', 'audio'], ['system', 'whisper'], ['system', 'diagnostics'],
  ['system', 'backups'], ['system', 'trash'], ['system', 'logs'], ['system', 'appearance'],
  ['shop-info', ''], ['playbook', ''],
  ['captcha', ''],
];

const INIT = () => {
  const own = stack => !/\/(overlays|shell)\.js/.test(stack) && /\/app\//.test(stack);
  const timers = new Map(), intervals = new Map(), listeners = new Map(), pending = new Set();
  window.__probe = {timers, intervals, listeners, pending, fetchTotal: 0, fetchAborted: 0};
  const st = window.setTimeout, ct = window.clearTimeout, si = window.setInterval, ci = window.clearInterval;
  window.setTimeout = (fn, ms, ...a) => {
    const stack = new Error().stack, id = st(() => { timers.delete(id); fn(...a); }, ms);
    if (own(stack)) timers.set(id, stack);
    return id;
  };
  window.clearTimeout = id => { timers.delete(id); return ct(id); };
  window.setInterval = (fn, ms, ...a) => { const id = si(fn, ms, ...a); if (own(new Error().stack)) intervals.set(id, 1); return id; };
  window.clearInterval = id => { intervals.delete(id); return ci(id); };
  for (const target of [window, document]) {
    const add = target.addEventListener.bind(target), rem = target.removeEventListener.bind(target);
    const label = target === window ? 'window' : 'document';
    target.addEventListener = (type, fn, opts) => {
      const stack = new Error().stack;
      if (own(stack) && fn) { fn.__id ||= Math.random(); listeners.set(label + ':' + type + ':' + fn.__id, 1); }
      return add(type, fn, opts);
    };
    target.removeEventListener = (type, fn, opts) => { if (fn && fn.__id) listeners.delete(label + ':' + type + ':' + fn.__id); return rem(type, fn, opts); };
  }
  const realFetch = window.fetch.bind(window);
  window.fetch = (url, opts = {}) => {
    const entry = {url: String(url)};
    pending.add(entry);
    window.__probe.fetchTotal += 1;
    if (opts.signal) opts.signal.addEventListener('abort', () => { window.__probe.fetchAborted += 1; });
    return realFetch(url, opts).finally(() => pending.delete(entry));
  };
};

test('every page disposer clears timers, listeners and in-flight fetches over 30 mount cycles', async () => {
  const server = createServer(async (request, response) => {
    try {
      if (request.url === '/') {response.setHeader('Content-Type', 'text/html'); response.end('<!doctype html><main id="host"></main>'); return;}
      if (request.url.startsWith('/api/')) {
        await new Promise(resolve => setTimeout(resolve, 1500)); // giữ request treo đủ lâu để unmount cắt ngang
        response.setHeader('Content-Type', 'application/json'); response.end('{"ok":true,"items":[]}'); return;
      }
      const target = path.resolve(web, '.' + request.url.split('?')[0]);
      if (!target.startsWith(web + path.sep)) {response.writeHead(403).end(); return;}
      response.setHeader('Content-Type', target.endsWith('.css') ? 'text/css' : 'text/javascript');
      response.end(await readFile(target));
    } catch (_) {response.writeHead(404).end();}
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await playwright.chromium.launch({headless: true, executablePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'});
    const page = await browser.newPage();
    await page.addInitScript(INIT);
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    const report = await page.evaluate(async pages => {
      const api = await import('/app/core/api.js');
      const {createStore} = await import('/app/core/store.js');
      const modules = {};
      const out = [];
      const host = document.getElementById('host');
      for (const [name, sub, id] of pages) {
        modules[name] ||= (await import(`/app/pages/${name}${name === 'video' ? '/index' : ''}.js`)).default;
        let mountErrors = 0;
        for (let i = 0; i < 30; i += 1) {
          const store = createStore({dashboard: {}});
          const scope = api.beginPageScope();
          const ctx = {route: {page: name, sub, id: id || ''}, store, navigate() {}, replaceSilently() {},
            shell: {main: host, setFullBleed() {}, setRoute() {}}};
          let mounted = null;
          try { mounted = modules[name].mount(ctx); } catch (error) { mountErrors += 1; }
          await new Promise(resolve => setTimeout(resolve, 15));
          if (mounted && mounted.unmount) mounted.unmount();
          api.endPageScope(scope);
          host.replaceChildren();
        }
        await new Promise(resolve => setTimeout(resolve, 200));
        const p = window.__probe;
        out.push({key: `${name}/${sub}`, mountErrors, timers: p.timers.size, intervals: p.intervals.size, listeners: [...p.listeners.keys()], pending: p.pending.size,
          timerStacks: [...p.timers.values()].map(s => s.split('\n').slice(1, 4).join('|'))});
      }
      return {out, fetchTotal: window.__probe.fetchTotal, fetchAborted: window.__probe.fetchAborted};
    }, PAGES);
    for (const row of report.out) {
      assert.equal(row.mountErrors, 0, `${row.key}: mount ném lỗi`);
      assert.equal(row.timers, 0, `${row.key}: timer còn sống ${row.timerStacks.join(' // ')}`);
      assert.equal(row.intervals, 0, `${row.key}: interval còn sống`);
      assert.deepEqual(row.listeners, [], `${row.key}: listener window/document còn sống`);
      assert.equal(row.pending, 0, `${row.key}: fetch còn treo sau unmount`);
    }
    assert.ok(report.fetchTotal > 0 && report.fetchAborted > 0, 'phải có request bị abort khi rời trang');
  } finally {
    if (browser) await browser.close();
    server.close();
  }
});

test('request: rời trang thì abort, phản hồi muộn bị bỏ và không báo lỗi; timeout mặc định 15 giây', async () => {
  const src = await readFile(path.join(web, 'app/core/api.js'), 'utf8');
  assert.match(src, /timeoutMs \?\? 15000/);
  const api = await import('data:text/javascript;base64,' + Buffer.from(src).toString('base64'));
  let seenSignal = null;
  globalThis.fetch = (url, opts) => new Promise((resolve, reject) => {
    seenSignal = opts.signal;
    opts.signal.addEventListener('abort', () => reject(new Error('aborted')));
  });
  const scope = api.beginPageScope();
  let settled = false;
  const call = api.request('/api/x').then(() => { settled = true; }, () => { settled = true; });
  await new Promise(resolve => setTimeout(resolve, 5));
  assert.equal(api.pageGeneration(), scope.generation);
  api.endPageScope(scope);
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(seenSignal.aborted, true);
  assert.equal(settled, false, 'trang đã đóng: không resolve/reject (không toast lỗi muộn)');
  assert.equal(api.pageGeneration(), 0);
  void call;
});
