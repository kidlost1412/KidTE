import test from 'node:test';
import assert from 'node:assert/strict';
import {pickMode, loadMode, saveMode, orderSteps, currentStep, groupSteps, bannerModel, actionView,
  quickToggles, toggleRequest, readinessOf, MODE_KEY} from '../../src/web/app/pages/live-wizard-model.js';

const S = (id, group, state, extra = {}) => ({id, group, state, title: id, why: '', detail: '', required: true, action: null, ...extra});

test('pickMode: stored wins, else new when done<=2', () => {
  assert.equal(pickMode('', {done: 2}), 'new');
  assert.equal(pickMode('', {done: 3}), 'pro');
  assert.equal(pickMode('new', {done: 9}), 'new');
  assert.equal(pickMode('junk', null), 'new');
});

test('loadMode/saveMode survive throwing storage', () => {
  const bad = {getItem() { throw new Error('x'); }, setItem() { throw new Error('x'); }};
  assert.equal(loadMode(bad), '');
  assert.doesNotThrow(() => saveMode(bad, 'pro'));
  const mem = {v: {}, getItem(k) { return this.v[k] ?? null; }, setItem(k, v) { this.v[k] = v; }};
  saveMode(mem, 'pro');
  assert.equal(mem.v[MODE_KEY], 'pro');
  assert.equal(loadMode(mem), 'pro');
});

test('orderSteps groups once -> session -> live; currentStep prefers required error', () => {
  const steps = [S('C1', 'live', 'todo', {required: false}), S('B1', 'session', 'todo'), S('A1', 'once', 'done'), S('A3', 'once', 'todo'), S('B4', 'session', 'error')];
  assert.deepEqual(orderSteps(steps).map(s => s.id), ['A1', 'A3', 'B1', 'B4', 'C1']);
  assert.equal(currentStep(steps).id, 'B4');
  assert.equal(currentStep([S('A1', 'once', 'done'), S('C1', 'live', 'todo', {required: false})]).id, 'C1');
  assert.equal(currentStep([S('A1', 'once', 'done')]), null);
});

test('groupSteps: pro mode hides done/optional/blocked, new shows all but na', () => {
  const steps = [S('A1', 'once', 'done'), S('A2', 'once', 'na'), S('A3', 'once', 'todo'), S('A5', 'once', 'blocked'), S('A8', 'once', 'optional')];
  const pro = groupSteps(steps, 'pro')[0];
  assert.deepEqual(pro.items.map(s => s.id), ['A3']);
  assert.equal(pro.total, 4);
  assert.equal(groupSteps(steps, 'new')[0].items.length, 5);
});

test('bannerModel and readinessOf', () => {
  assert.match(bannerModel({blocking: 3, done: 1, total: 10}).title, /Còn 3 việc bắt buộc/);
  assert.equal(bannerModel({blocking: 0}).tone, 'ok');
  assert.equal(readinessOf({readiness: {ok: false}}), null);
  assert.equal(readinessOf({}), null);
});

test('actionView maps navigate/post/toggle', () => {
  assert.deepEqual(actionView(S('A3', 'once', 'todo', {action: {kind: 'navigate', page: 'system', sub: 'setup'}})),
    {kind: 'navigate', label: 'Mở', page: 'system', sub: 'setup', arg: ''});
  const a6 = actionView(S('A6', 'once', 'todo', {action: {kind: 'post', endpoint: '/api/mapping/auto-match', body: {}, label: 'Tự khớp'}}));
  assert.equal(a6.endpoint, '/api/mapping/auto-match');
  assert.deepEqual(a6.then, {page: 'catalog', sub: 'mapping'});
  assert.equal(actionView(S('A3', 'once', 'done', {action: {kind: 'navigate', page: 'x'}})), null);
  assert.equal(actionView(S('B4', 'session', 'done', {action: {kind: 'toggle', target: 'sync', on: false, label: 'Tắt'}})).on, false);
});

test('toggleRequest endpoints and confirmations', () => {
  assert.deepEqual(toggleRequest('sync', true).body, {id: 'dong-bo-video', action: 'start'});
  assert.equal(toggleRequest('sync', false, {live: false}).confirm, null);
  assert.ok(toggleRequest('sync', false, {live: true}).confirm);
  assert.deepEqual(toggleRequest('src-shopee', false).body, {platform: 'shopee', settings: {enabled: false}});
  assert.ok(toggleRequest('src-tiktok', false, {live: true}).confirm);
  assert.equal(toggleRequest('captcha', true).endpoint, '/api/captcha/start');
  assert.equal(toggleRequest('captcha', true).confirm, null);
  assert.ok(toggleRequest('captcha', false).confirm);
  assert.deepEqual(toggleRequest('ai-tiktok', true).body, {platform: 'tiktok', ai_enabled: true});
  assert.deepEqual(toggleRequest('pin-tiktok', false).body, {platform: 'tiktok', auto_pin: false});
});

const baseDash = () => ({
  extension: {connected: true, live_running: true, ai_enabled: true, ai_ready: true},
  plugins: {plugins: [{id: 'dong-bo-video', runtime: {state: 'running'}}]},
  captcha: {configured: true, running: false},
  live_channels: {tiktok: {settings: {enabled: true}, position: {available: true}}, shopee: {settings: {enabled: false}}},
});

test('quickToggles without live_control: AI is info-only with how-to, no auto-pin', () => {
  const t = quickToggles(baseDash());
  const ids = t.map(x => x.id);
  assert.deepEqual(ids, ['sync', 'src-tiktok', 'src-shopee', 'captcha', 'ai-tiktok']);
  const ai = t.find(x => x.id === 'ai-tiktok');
  assert.equal(ai.mode, 'info');
  assert.equal(ai.on, true);
  assert.ok(ai.howTo);
  assert.match(t.find(x => x.id === 'captcha').note, /UAC/);
});

test('quickToggles with live_control: desired state, pending note, auto-pin', () => {
  const dash = {...baseDash(), live_channels: {...baseDash().live_channels, shopee: {settings: {enabled: true}, extension: {ai_enabled: false}}},
    live_control: {tiktok: {ai_enabled: false, auto_pin: null, applied: false}, shopee: {ai_enabled: null, applied: true}, queue_mode: false}};
  const t = quickToggles(dash);
  const ai = t.find(x => x.id === 'ai-tiktok');
  assert.equal(ai.mode, 'control');
  assert.equal(ai.on, false);
  assert.equal(ai.actual, true);
  assert.equal(ai.pending, true);
  assert.match(ai.note, /Đang chờ tiện ích nhận lệnh/);
  const sp = t.find(x => x.id === 'ai-shopee');
  assert.equal(sp.on, false);
  assert.equal(sp.pending, false);
  const pin = t.find(x => x.id === 'pin-tiktok');
  assert.equal(pin.on, true);       // null => theo thực tế
  assert.equal(pin.pending, false);
});

test('quickToggles: control_error shows real state + reason; queue toggle; pin from heartbeat', () => {
  const base = baseDash();
  const dash = {...base, extension: {...base.extension, pin_running: false},
    live_channels: {...base.live_channels, shopee: {settings: {enabled: true}, extension: {connected: true, ai_enabled: true}}},
    live_control: {tiktok: {ai_enabled: null, auto_pin: true, applied: true, control_error: ''},
      shopee: {ai_enabled: true, applied: true, control_error: 'DeepSeek chưa sẵn sàng'}, queue_mode: true}};
  const t = quickToggles(dash);
  const sp = t.find(x => x.id === 'ai-shopee');
  assert.equal(sp.actual, false, 'thiếu ai_ready thì Shopee AI chưa chạy');
  assert.equal(sp.on, false);
  assert.match(sp.note, /DeepSeek/);
  assert.equal(t.find(x => x.id === 'pin-tiktok').actual, false);
  const q = t.find(x => x.id === 'queue-mode');
  assert.equal(q.on, true);
  const r = toggleRequest('queue-mode', false, {live: true});
  assert.equal(r.endpoint, '/api/live/queue-mode');
  assert.deepEqual(r.body, {enabled: false});
  assert.ok(r.confirm && /chưa LIVE/.test(r.confirm.text));
});

// ---- v2 ----
import {loadPlatform, savePlatform, choosePlatforms, applyPlatforms, nextStep, stages, stageRows, phaseOf,
  summaryLine, liveStrip, PLATFORM_KEY} from '../../src/web/app/pages/live-wizard-model.js';

const realShopee = () => ({
  platforms: {tiktok: {active: false, connected: false, live: false}, shopee: {active: true, connected: true, live: true}},
  steps: [
    S('A1', 'once', 'na', {platform: 'tiktok', required: false, real: {state: 'todo', required: true, detail: 'x'}}),
    S('A2', 'once', 'done', {platform: 'shopee'}), S('A3', 'once', 'done'),
    S('B3', 'session', 'na', {platform: 'tiktok', required: false, real: {state: 'todo', required: true, detail: ''}}),
    S('B3S', 'session', 'done', {platform: 'shopee'}), S('B4', 'session', 'done'),
    S('C1', 'live', 'done', {required: false, detail: 'Đang LIVE: Shopee'})],
});

test('platform choice: auto from backend, stored override, throwing storage', () => {
  const r = realShopee();
  assert.deepEqual(choosePlatforms('auto', r), ['shopee']);
  assert.deepEqual(choosePlatforms('both', r), ['tiktok', 'shopee']);
  assert.deepEqual(choosePlatforms('auto', {}), ['tiktok']);
  const bad = {getItem() { throw new Error('x'); }, setItem() { throw new Error('x'); }};
  assert.equal(loadPlatform(bad), 'auto');
  assert.doesNotThrow(() => savePlatform(bad, 'both'));
  const mem = {v: {}, getItem(k) { return this.v[k] ?? null; }, setItem(k, v) { this.v[k] = v; }};
  savePlatform(mem, 'shopee');
  assert.equal(mem.v[PLATFORM_KEY], 'shopee');
  assert.equal(loadPlatform(mem), 'shopee');
});

test('applyPlatforms: Shopee only is ready; choosing both restores TikTok steps as required', () => {
  const r = realShopee();
  const only = applyPlatforms(r, ['shopee']);
  assert.equal(only.summary.blocking, 0);
  assert.equal(nextStep(only.steps), null);
  assert.equal(phaseOf(r, ['shopee'], only.summary), 'live');
  const both = applyPlatforms(r, ['tiktok', 'shopee']);
  assert.equal(both.summary.blocking, 2);
  assert.equal(nextStep(both.steps).id, 'A1');
  const tk = applyPlatforms(r, ['tiktok']);
  assert.equal(tk.steps.find(s => s.id === 'A2').state, 'na');
  assert.equal(phaseOf(r, ['tiktok'], tk.summary), 'prep');
  assert.match(summaryLine(tk.summary, 'prep'), /Còn 2 việc bắt buộc/);
});

test('ready phase, stages and collapsed done rows', () => {
  const r = {platforms: {tiktok: {active: true}}, steps: [S('A1', 'once', 'done'), S('A3', 'once', 'done'),
    S('A8', 'once', 'optional', {required: false}), S('B4', 'session', 'done'), S('C1', 'live', 'todo', {required: false})]};
  const {summary, steps} = applyPlatforms(r, ['tiktok']);
  assert.equal(phaseOf(r, ['tiktok'], summary), 'ready');
  assert.match(summaryLine(summary, 'ready'), /Sẵn sàng LIVE/);
  const st = stages(steps);
  assert.deepEqual(st.map(x => [x.id, x.done, x.total]), [['once', 2, 3], ['session', 1, 1], ['live', 0, 1]]);
  assert.equal(st[1].tone, 'ok');
  const rows = stageRows(steps, 'once');
  assert.deepEqual(rows.open.map(s => s.id), ['A8']);
  assert.deepEqual(rows.done.map(s => s.id), ['A1', 'A3']);
  assert.deepEqual(stageRows(steps, 'once', 'A8').open, []);
});

test('nextStep prefers required error, skips live group and optional', () => {
  const steps = [S('A3', 'once', 'todo'), S('B4', 'session', 'error'), S('C1', 'live', 'todo'), S('A8', 'once', 'error', {required: false})];
  assert.equal(nextStep(steps).id, 'B4');
});

test('liveStrip per platform and quickToggles hide unused platform', () => {
  const dash = {extension: {}, ai_queue: {pending: 3},
    live_channels: {shopee: {position: {available: true, video_id: 'v1', position_s: 75, match_label: 'Khop ro'},
      extension: {connected: true, live_running: true, ai_enabled: true, ai_ready: true, pin_running: null}}}};
  const [x] = liveStrip(dash, ['shopee'], {v1: 'Video A'});
  assert.deepEqual([x.label, x.live, x.video, x.time, x.ai, x.pin, x.queue], ['Shopee', true, 'Video A', '1:15', true, true, 3]);
  const ids = quickToggles({...baseDash(), live_channels: {...baseDash().live_channels, shopee: {settings: {enabled: true}}}}, ['shopee']).map(t => t.id);
  assert.ok(ids.includes('src-shopee') && ids.includes('sync'));
  assert.ok(!ids.includes('src-tiktok') && !ids.includes('ai-tiktok') && !ids.includes('captcha'));
});
test('modeView: Người mới có hướng dẫn + mô tả; Đã quen gọn, không giải thích', async () => {
  const {modeView, GUIDE_NEW} = await import('../../src/web/app/pages/live-wizard-model.js');
  const n = modeView('new'), p = modeView('pro');
  assert.equal(n.guide, GUIDE_NEW);
  assert.match(n.guide, /lần lượt từ trái sang phải/);
  assert.equal(n.why, true); assert.equal(n.notes, true); assert.equal(n.stepChips, false); assert.equal(n.nextSize, 'lg');
  assert.equal(p.guide, null); assert.equal(p.why, false); assert.equal(p.notes, false);
  assert.equal(p.stepChips, true); assert.equal(p.toggleChips, true); assert.equal(p.nextSize, 'sm');
});

test('toggleView: Đã quen ẩn mô tả dưới công tắc nhưng giữ tooltip + tông', async () => {
  const {toggleView} = await import('../../src/web/app/pages/live-wizard-model.js');
  const t = {id: 'sync', label: 'Đồng bộ video', on: true, note: 'Biết video đang tới đâu'};
  assert.equal(toggleView(t, 'new').note, 'Biết video đang tới đâu');
  const pv = toggleView(t, 'pro');
  assert.equal(pv.note, ''); assert.equal(pv.title, 'Biết video đang tới đâu'); assert.equal(pv.tone, 'ok');
  assert.equal(toggleView({...t, pending: true}, 'pro').tone, 'warn');
  assert.equal(toggleView({...t, error: 'x'}, 'pro').tone, 'danger');
});

test('stageChips: chỉ bước chưa xong của chặng, nhãn ngắn, bỏ bước chờ không bắt buộc', async () => {
  const {stageChips} = await import('../../src/web/app/pages/live-wizard-model.js');
  const steps = [S('A1', 'once', 'done'), S('A3', 'once', 'todo'), S('A7', 'once', 'blocked', {required: false}),
    S('A8', 'once', 'error'), S('B1', 'session', 'todo'), S('A2', 'once', 'na')];
  const c = stageChips(steps, 'once');
  assert.deepEqual(c.map(x => x.id), ['A3', 'A8']);
  assert.equal(c[0].label, 'AI'); assert.equal(c[1].tone, 'danger');
});
