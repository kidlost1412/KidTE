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
