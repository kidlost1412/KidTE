import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const rd = rel => readFile(new URL('../../src/web/app/' + rel, import.meta.url), 'utf8');
const load = async rel => import('data:text/javascript;base64,' + Buffer.from(await rd(rel)).toString('base64'));
const {makeGuardedDismiss, createDirtyTracker, DISCARD_TITLE} = await load('core/dirty-guard.js');

test('không thay đổi thì đóng ngay, không hỏi', async () => {
  let asked = 0, closed = 0;
  const dismiss = makeGuardedDismiss({isDirty: () => false, ask: () => { asked++; return Promise.resolve(true); }, proceed: () => { closed++; }});
  await dismiss();
  assert.deepEqual([asked, closed], [0, 1]);
});

test('có thay đổi: hỏi; từ chối thì ở lại, đồng ý thì đóng; Esc+nền liên tiếp chỉ hỏi 1 lần', async () => {
  let asked = 0, closed = 0, answer = false, release;
  const dismiss = makeGuardedDismiss({isDirty: () => true, proceed: () => { closed++; },
    ask: () => { asked++; return new Promise(r => { release = () => r(answer); }); }});
  const first = dismiss();
  const second = dismiss();
  assert.equal(asked, 1);
  assert.equal(await second, false);
  release(); assert.equal(await first, false);
  assert.equal(closed, 0);
  answer = true;
  const third = dismiss(); release(); await third;
  assert.equal(closed, 1);
  assert.match(DISCARD_TITLE, /chưa lưu/);
});

test('isDirty ném lỗi thì coi là có thay đổi (an toàn)', async () => {
  let asked = 0;
  await makeGuardedDismiss({isDirty: () => { throw new Error('x'); }, ask: () => { asked++; return false; }, proceed() {}})();
  assert.equal(asked, 1);
});

test('tracker: sạch trước khi tải, dirty khi khác bản đã chụp, sạch lại sau reset', () => {
  let value = 'a';
  const t = createDirtyTracker(() => ({value}));
  value = 'b';
  assert.equal(t.isDirty(), false);
  t.reset();
  assert.equal(t.isDirty(), false);
  value = 'c';
  assert.equal(t.isDirty(), true);
  t.reset();
  assert.equal(t.isDirty(), false);
});

test('overlays nối isDirty cho modal và ngăn kéo; các trang đã cài isDirty', async () => {
  const ov = await rd('ui/overlays.js');
  assert.match(ov, /openModal\(\{[^}]*isDirty/);
  assert.match(ov, /openDrawer\(\{[^}]*isDirty/);
  assert.equal((ov.match(/makeGuardedDismiss\(/g) || []).length, 2);
  for (const f of ['pages/catalog.js', 'pages/product-ingest-ui.js']) assert.match(await rd(f), /isDirty:/, f);
  for (const f of ['pages/shop-info.js', 'pages/playbook.js', 'pages/system.js', 'pages/automation.js']) assert.match(await rd(f), /isDirty:/, f);
});
