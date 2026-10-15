// Trợ lý chuẩn bị LIVE: khối đầu trang Trung tâm LIVE (overview.js).
// Logic ở live-wizard-model.js; file này chỉ vẽ và gọi endpoint có sẵn.
import {h, clear} from '../core/dom.js';
import {icon} from '../core/icons.js';
import {request, friendlyError} from '../core/api.js';
import {btn, chip, notice, toggle, segmented} from '../ui/kit.js';
import {toast, confirmDialog} from '../ui/overlays.js';
import {LIVE_REASONS} from '../ui/live-status.js';
import {readinessOf, loadMode, saveMode, pickMode, groupSteps, currentStep, bannerModel, chipRow,
  actionView, quickToggles, toggleRequest, STATE_VIEW} from './live-wizard-model.js';

const store = () => { try { return globalThis.localStorage; } catch (_) { return null; } };

export function liveWizard(ctx) {
  const root = h('section', {class: 'card live-wizard', 'aria-label': 'Trợ lý chuẩn bị LIVE'});
  let mode = loadMode(store());
  let dash = null;
  let busy = '';
  let lastKey = '';

  async function run(id, fn) {
    if (busy) return;
    busy = id; draw(true);
    try { await fn(); }
    catch (error) { toast(friendlyError(error), 'danger'); }
    finally { busy = ''; draw(true); }
  }

  function doAction(step) {
    const a = actionView(step);
    if (!a) return;
    if (a.kind === 'navigate') { ctx.navigate(a.page, a.sub, a.arg); return; }
    if (a.kind === 'post') {
      run(step.id, async () => {
        const out = await request(a.endpoint, {method: 'POST', body: a.body});
        toast(out && out.ok === false ? (out.error || 'Chưa làm được') : 'Đã gửi lệnh', out && out.ok === false ? 'warn' : 'ok');
        if (a.then && step.id === 'A6') ctx.navigate(a.then.page, a.then.sub);
      });
      return;
    }
    if (a.kind === 'toggle') flip(a.target, a.on);
  }

  async function flip(id, on) {
    const live = !!(dash && dash.extension && dash.extension.connected && dash.extension.live_running);
    const req = toggleRequest(id, on, {live});
    if (!req) return;
    if (req.confirm && !(await confirmDialog(req.confirm))) { draw(true); return; }
    run(id, async () => {
      const out = await request(req.endpoint, {method: 'POST', body: req.body});
      if (out && out.ok === false) toast(out.error || 'Chưa làm được', 'warn');
      else toast(id === 'captcha' && on ? 'Đã gửi lệnh khởi chạy — bấm Có khi Windows hỏi quyền' : 'Đã cập nhật', 'ok');
      ctx.store.refresh && ctx.store.refresh();
    });
  }

  function stepRow(step, current, faded) {
    const v = STATE_VIEW[step.state] || STATE_VIEW.todo;
    const a = actionView(step);
    const detail = step.id === 'C2' && step.detail ? (LIVE_REASONS[step.detail] || step.detail) : step.detail;
    const actionBtn = a ? btn(a.label, {size: current ? '' : 'sm', kind: current ? 'primary' : 'ghost',
      icon: a.kind === 'navigate' ? 'arrowRight' : a.kind === 'toggle' ? 'zap' : 'play', disabled: !!busy,
      onClick: () => doAction(step)}) : null;
    return h('div', {class: 'wz-step ' + step.state + (current ? ' current' : '') + (faded ? ' faded' : '')},
      h('span', {class: 'wz-ck ' + v.tone}, icon(v.icon)),
      h('div', {class: 'wz-body'},
        h('div', {class: 'wz-title'}, h('strong', {}, step.title), step.required ? null : h('span', {class: 'fs-xs muted'}, ' · không bắt buộc')),
        current || step.state === 'error' ? h('div', {class: 'fs-sm soft'}, step.why) : null,
        detail ? h('div', {class: 'fs-xs muted'}, detail) : null),
      h('div', {class: 'wz-act'}, chip(v.label, v.tone, {size: 'sm'}), actionBtn));
  }

  function togglesBlock() {
    const items = quickToggles(dash);
    if (!items.length) return null;
    return h('div', {class: 'wz-toggles'}, h('h3', {class: 'wz-h'}, 'Bật/tắt nhanh'),
      h('div', {class: 'wz-tgrid'}, items.map(t => h('div', {class: 'wz-tg' + (t.pending ? ' pending' : '')},
        t.mode === 'info'
          ? h('div', {class: 'row', style: {gap: '8px'}}, chip(t.on ? 'Đang bật' : 'Đang tắt', t.on ? 'ok' : 'neutral', {dot: true}), h('strong', {class: 'fs-sm'}, t.label))
          : toggle(t.label, !!t.on, val => flip(t.id, val), {disabled: !!busy}),
        h('div', {class: 'fs-xs muted'}, t.note || ''),
        t.howTo ? btn('Cách bật', {size: 'sm', kind: 'ghost', icon: 'help', onClick: () => toast(t.howTo, 'info', {duration: 8000})}) : null))));
  }

  function draw(force = false) {
    const r = readinessOf(dash);
    const toggles = quickToggles(dash);
    const key = JSON.stringify([r, toggles, mode, busy]);
    if (!force && key === lastKey) return;
    lastKey = key;
    if (!r) { clear(root); root.hidden = true; return; }
    root.hidden = false;
    const m = pickMode(mode, r.summary);
    const cur = currentStep(r.steps);
    const b = bannerModel(r.summary);
    const head = h('header', {class: 'card-head'},
      h('div', {class: 'grow'}, h('h2', {}, 'Trợ lý chuẩn bị LIVE'), h('div', {class: 'sub'}, m === 'new' ? 'Làm lần lượt từ trên xuống, mỗi bước một nút.' : 'Chỉ hiện việc còn thiếu.')),
      segmented([['new', 'Người mới'], ['pro', 'Đã quen']], m, id => { mode = id; saveMode(store(), id); draw(true); }, {label: 'Chế độ trợ lý'}));
    const body = [notice(b.tone, b.title, b.text)];
    if (m === 'pro') {
      body.push(h('div', {class: 'wz-chips'}, chipRow(r.steps).map(c => chip(c.label, c.tone, {dot: true, size: 'sm', title: STATE_VIEW[c.state]?.label}))));
    }
    let passed = false;
    for (const g of groupSteps(r.steps, m)) {
      if (!g.items.length) continue;
      body.push(h('div', {class: 'wz-group'},
        h('div', {class: 'wz-gh'}, h('h3', {class: 'wz-h'}, g.title), h('span', {class: 'fs-xs muted'}, `${g.done}/${g.total} · ${g.sub}`)),
        g.items.map(s => {
          const isCur = cur && s.id === cur.id;
          const row = stepRow(s, isCur, m === 'new' && passed && s.state !== 'done');
          if (isCur) passed = true;
          return row;
        })));
    }
    body.push(togglesBlock());
    clear(root, head, h('div', {class: 'wz-wrap'}, body));
  }

  return {el: root, update(next) { dash = next; draw(); }};
}
