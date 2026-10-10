// T03 - UI screenshot/overflow harness. Usage: node backend/tools/ui_screens.mjs [--only substring]
// Serves backend/src/web against a stub API, captures every route at 3 resolutions x 3 DPI
// into test-artifacts/ui/<route>-<w>x<h>@<scale>.png and writes test-artifacts/ui/report.json.
import {readFile, readdir, mkdir, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {createServer} from 'node:http';
import {fileURLToPath} from 'node:url';

const require = createRequire(import.meta.url);
export const here = path.dirname(fileURLToPath(import.meta.url));
export const web = path.resolve(here, '../src/web');
export const outDir = path.resolve(here, '../../test-artifacts/ui');
export const CHROME = process.env.KIDTE_CHROME || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';

export async function loadPlaywright() {
  try { return require('playwright'); } catch {
    const root = path.join(process.env.LOCALAPPDATA || '', 'OpenAI', 'Codex', 'runtimes', 'cua_node');
    return require(path.join(root, (await readdir(root)).sort().at(-1), 'bin', 'node_modules', 'playwright'));
  }
}

export const PAGES = ['overview', 'inbox', 'brain', 'video', 'catalog', 'customers', 'captcha', 'automation', 'system'];
export const ROUTES = [
  'overview', 'inbox', 'inbox/unanswered',
  'brain/knowledge', 'brain/docs', 'brain/try', 'brain/style', 'brain/relearn',
  'video', 'video/studio/vid-1',
  'catalog/products', 'catalog/profiles', 'catalog/mapping',
  'customers/customers', 'customers/facts', 'customers/sessions',
  'captcha',
  'automation', 'automation/chinh-sach-gia', 'automation/chon-san-pham', 'automation/lieu-dung',
  'automation/model-deepseek', 'automation/thu-du-lieu', 'automation/dong-bo-video',
  'system/setup', 'system/live', 'system/audio', 'system/whisper', 'system/updates', 'system/diagnostics',
  'system/backups', 'system/trash', 'system/logs', 'system/appearance',
];
export const SIZES = [[1366, 768], [1920, 1080], [2560, 1440]];
export const SCALES = [1, 1.5, 2];

const now = '2026-10-03T09:30:00Z';
const questions = ['Bình 25 lít pha bao nhiêu thuốc vậy shop?', 'Sản phẩm này còn hàng không?', 'Giá bao nhiêu ạ, ship về Đắk Lắk mất mấy ngày?'];
const comments = Array.from({length: 14}, (_, i) => ({
  id: `c${i}`, platform: i % 2 ? 'shopee' : 'tiktok', customer: `Khách hàng ${i + 1}`, author: `khach_${i + 1}`,
  text: questions[i % 3], answer: i % 4 ? 'Dạ bình 25 lít pha 50ml anh nhé.' : '', state: i % 4 ? 'answered' : 'unanswered',
  created_at: now, session_id: 's1', title: `Hội thoại ${i + 1}`, updated_at: now, message_count: 3,
}));
const products = Array.from({length: 12}, (_, i) => ({
  id: `p${i}`, sku: `SKU-${100 + i}`, name: `Phân bón lá Kid TE số ${i + 1} chai 500ml dùng cho cây sầu riêng và cà phê`,
  title: `Sản phẩm ${i + 1}`, price: 125000 + i * 5000, platform: i % 2 ? 'shopee' : 'tiktok', mapped: i % 3 !== 0, updated_at: now,
}));
const dashboard = {
  ok: true, ts: now,
  session: {live: true, todo: 3, unanswered: 3, total: 128, answered: 120, platform: 'tiktok', title: 'LIVE sáng thứ bảy'},
  learning: {suggested: 2, items: 41}, video_review: {pending_jobs: 1},
  catalog: {unmapped: 4, total: 12}, backup: {overdue: false, last_at: now},
  video: {video_id: 'vid-1', title: 'Video LIVE 20/09', position: 120, state: 'watching'},
  services: [{id: 'video-sync', label: 'Đồng bộ video', state: 'running'}], health: {ok: true, checks: []},
};
const video = {id: 'vid-1', video_id: 'vid-1', title: 'LIVE sầu riêng 20/09', duration: 5400, state: 'ready', created_at: now, status: 'ready', segments: []};
const processor = {id: 'chinh-sach-gia', title: 'Chính sách giá', enabled: true, settings: {}, schema: [], stats: {}, recent: []};

export function stubResponse(url) {
  const p = url.pathname;
  if (p === '/api/dashboard') return dashboard;
  if (p === '/api/settings') return {ok: true, deepseek_model: 'deepseek-chat', has_api_key: true, selected_audio_device: 'auto', shop: {name: 'Kid TE', hotline: '0900000000'},
    audio_devices: [{id: 'loop-1', name: 'Loa (Realtek Audio) - loopback', loopback: true}, {id: 'mic-1', name: 'Micro mảng (Realtek)', loopback: false}]};
  if (p === '/api/updates') return {ok: true, enabled: true, trusted: true, busy: false, error: '',
    channel: 'stable', installed: {core: '1.14.255', 'media-tools': '1.0.0', 'captcha-solver': '1.0.0'}, pending: {}, restart_required: false,
    available: [{id: 'core', version: '1.14.254', download_bytes: 265000000, unpacked_bytes: 955000000, dependencies: {}},
      {id: 'gpu-cuda12', version: '1.0.0', download_bytes: 1375876639, unpacked_bytes: 2102925408, dependencies: {core: '>=1.14.254,<2.0.0'}},
      ...['media-tools', 'captcha-solver', 'video-sync', 'video-analysis-cpu'].map(id => ({id, version: '1.0.0', download_bytes: 100000000, unpacked_bytes: 200000000, dependencies: {core: '>=1.14.255,<2.0.0'}}))]};
  if (p.startsWith('/api/workspace/videos')) return {ok: true, items: [video], total: 1};
  if (p.startsWith('/api/workspace/conversations')) return {ok: true, items: comments, total: comments.length};
  if (p.startsWith('/api/workspace/products') || p === '/api/video/catalog') return {ok: true, items: products, products, total: products.length};
  if (p.startsWith('/api/workspace/backups')) return {ok: true, items: [{id: 'b1', created_at: now, scope: 'full', bytes: 4500000, reason: 'manual'}], total: 1};
  if (p.startsWith('/api/workspace/')) return {ok: true, items: comments.slice(0, 6), total: 6};
  if (p === '/api/plugins') return {ok: true, plugins: [], items: []};
  if (p.startsWith('/api/system/processors/')) return {ok: true, ...processor, name: processor.title, version: '1.0', links: [], try: null,
    about: {how: ['Đọc câu hỏi', 'Kiểm tra giá'], what: 'Hướng dẫn AI báo đúng giá, tôn trọng giá khách thấy.', when: 'Mỗi lần soạn câu trả lời', limits: 'Không tự tính giá'},
    values: {}, changed: [], schema: [{key: 'max_len', label: 'Độ dài tối đa', type: 'number', default: 300, value: 300}], stats: {runs: 120, blocked: 3}};
  if (p === '/api/logs') return {ok: true, items: [{ts: now, level: 'warning', message: 'Mất kết nối OBS 3 giây', hint: 'Mở lại OBS'}]};
  if (p === '/api/trash') return {ok: true, items: [{id: 't1', resource: 'products', label: 'Sản phẩm cũ', deleted_at: now, expires_at: now}]};
  if (p === '/api/live/sources') {
    const ch = {settings: {mode: 'device', device_id: 'loop-1', delay_ms: 1500, bindings: {}}, extension: {connected: true}, state: 'watching', health: {inputs: []}};
    return {ok: true, sources: [], channels: {tiktok: ch, shopee: {...ch, settings: {mode: 'obs', delay_ms: 2500, bindings: {}}}}};
  }
  if (p === '/api/video/timelines') return {ok: true, timelines: {'vid-1': {title: video.title}}};
  return {ok: true, items: [], devices: [], models: [], parts: [], jobs: [], total: 0};
}

export async function startServer() {
  const server = createServer(async (req, res) => {
    try {
      const url = new URL(req.url, 'http://x');
      if (url.pathname.startsWith('/api/')) {
        res.setHeader('Content-Type', 'application/json');
        res.end(JSON.stringify(stubResponse(url))); return;
      }
      if (url.pathname === '/' || url.pathname === '/operator.html') {
        res.setHeader('Content-Type', 'text/html; charset=utf-8'); res.end(await readFile(path.join(web, 'operator.html'))); return;
      }
      const rel = url.pathname.replace(/^\/assets\//, '/');
      const target = path.resolve(web, '.' + rel);
      if (!target.startsWith(web + path.sep)) { res.writeHead(403).end(); return; }
      const types = {'.css': 'text/css', '.js': 'text/javascript', '.png': 'image/png'};
      res.setHeader('Content-Type', types[path.extname(target)] || 'application/octet-stream');
      res.end(await readFile(target));
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  return {server, origin: `http://127.0.0.1:${server.address().port}`, close: () => new Promise(r => server.close(r))};
}

export async function openRoute(page, origin, route) {
  await page.goto(`${origin}/#${route}`);
  await page.waitForSelector('main', {timeout: 10000}).catch(() => {});
  await page.waitForFunction(() => !document.querySelector('.loading, .skeleton'), null, {timeout: 8000}).catch(() => {});
  await page.waitForTimeout(250);
}

// Elements whose right edge exceeds the viewport and are not inside a scroll container.
export function measureOverflow(page) {
  return page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    const scrolls = el => {
      for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
        const s = getComputedStyle(a);
        if (/(auto|scroll|hidden|clip)/.test(s.overflowX) && a.scrollWidth > a.clientWidth) return true;
      }
      return false;
    };
    const clipped = [];
    for (const el of document.body.querySelectorAll('*')) {
      const s = getComputedStyle(el);
      if (s.display === 'none' || s.visibility === 'hidden' || el.classList.contains('skip-link')) continue;
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      if (r.right > vw + 1 && !scrolls(el)) {
        const cls = typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\s+/).join('.') : '';
        clipped.push(`${el.tagName.toLowerCase()}${cls}:right=${Math.round(r.right)}`);
      }
      if (clipped.length >= 15) break;
    }
    return {docOverflow: document.documentElement.scrollWidth > innerWidth + 1, viewport: vw, clipped};
  });
}

async function main() {
  const only = process.argv.includes('--only') ? process.argv[process.argv.indexOf('--only') + 1] : '';
  const playwright = await loadPlaywright();
  const {origin, close} = await startServer();
  await mkdir(outDir, {recursive: true});
  const browser = await playwright.chromium.launch({headless: true, executablePath: CHROME});
  const report = [];
  try {
    for (const [width, height] of SIZES) for (const scale of SCALES) {
      const context = await browser.newContext({viewport: {width, height}, deviceScaleFactor: scale});
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      for (const route of ROUTES.filter(r => r.includes(only))) {
        errors.length = 0;
        await openRoute(page, origin, route);
        const file = `${route.replace(/\//g, '-')}-${width}x${height}@${scale}.png`;
        await page.screenshot({path: path.join(outDir, file), fullPage: false});
        const overflow = await measureOverflow(page);
        report.push({route, width, height, scale, file, ...overflow, pageErrors: [...errors]});
      }
      await context.close();
    }
  } finally { await browser.close(); await close(); }
  await writeFile(path.join(outDir, 'report.json'), JSON.stringify(report, null, 1));
  const bad = report.filter(r => r.docOverflow || r.clipped.length || r.pageErrors.length);
  console.log(JSON.stringify({screenshots: report.length, problems: bad.length, bad}, null, 1));
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
