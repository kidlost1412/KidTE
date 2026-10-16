// Trợ lý chuẩn bị LIVE: khối đầu trang Trung tâm LIVE (overview.js).
// Logic ở live-wizard-model.js; file này chỉ vẽ và gọi endpoint có sẵn.
import {h, clear} from '../core/dom.js';
import {icon} from '../core/icons.js';
import {request, friendlyError} from '../core/api.js';
import {btn, toggle, segmented} from '../ui/kit.js';
import {toast, confirmDialog} from '../ui/overlays.js';
import {LIVE_REASONS} from '../ui/live-status.js';
import {readinessOf, loadMode, saveMode, pickMode, actionView, quickToggles, toggleRequest, STATE_VIEW,
  loadPlatform, savePlatform, choosePlatforms, applyPlatforms, nextStep, stages, stageRows, phaseOf,
  summaryLine, liveStrip, PLATFORM_CHOICES} from './live-wizard-model.js';

const store = () => { try { return globalThis.localStorage; } catch (_) { return null; } };

export function liveWizard(ctx) {
  const root = h('section', {class: 'card live-wizard', 'aria-label': 'Trợ lý chuẩn bị LIVE'});
  let mode = loadMode(store());
  let pchoice = loadPlatform(store());
  let dash = null;
  let busy = '';
  let lastKey = '';
  let openStage = null;          // null = tự chọn; '' = gộp hết; 'once' | 'session' | 'live'
  const doneOpen = new Set();
  let names = {};

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

  let isLive = false;
  async function flip(id, on) {
    const req = toggleRequest(id, on, {live: isLive});
    if (!req) return;
    if (req.confirm && !(await confirmDialog(req.confirm))) { draw(true); return; }
    run(id, async () => {
      const out = await request(req.endpoint, {method: 'POST', body: req.body});
      if (out && out.ok === false) toast(out.error || 'Chưa làm được', 'warn');
      else toast(id === 'captcha' && on ? 'Đã gửi lệnh khởi chạy — bấm Có khi Windows hỏi quyền' : 'Đã cập nhật', 'ok');
      ctx.store.refresh && ctx.store.refresh();
    });
  }

  const detailOf = s => s.id === 'C2' && s.detail ? (LIVE_REASONS[s.detail] || s.detail) : s.detail;

  function row(step, m) {
    const v = STATE_VIEW[step.state] || STATE_VIEW.todo;
    const a = actionView(step);
    return h('div', {class: 'wz-row ' + step.state},
      h('span', {class: 'wz-dot ' + v.tone, title: v.label}),
      h('div', {class: 'wz-line'},
        h('strong', {}, step.title),
        detailOf(step) ? h('span', {class: 'muted'}, ' · ' + detailOf(step)) : null,
        step.required ? null : h('span', {class: 'muted'}, ' · tuỳ chọn'),
        m === 'new' && step.state !== 'done' && step.why ? h('div', {class: 'wz-why'}, step.why) : null),
      a ? btn(a.label, {size: 'sm', kind: 'ghost', icon: a.kind === 'navigate' ? 'arrowRight' : a.kind === 'toggle' ? 'zap' : 'play',
        disabled: !!busy, onClick: () => doAction(step)}) : null);
  }

  function nextCard(step, m) {
    const v = STATE_VIEW[step.state] || STATE_VIEW.todo;
    const a = actionView(step);
    return h('div', {class: 'wz-next ' + v.tone},
      h('div', {class: 'wz-next-body'},
        h('div', {class: 'wz-kicker'}, 'Việc tiếp theo · ' + v.label),
        h('strong', {class: 'wz-next-title'}, step.title),
        h('div', {class: 'fs-sm soft'}, m === 'new' ? step.why : (detailOf(step) || step.why)),
        m === 'new' && detailOf(step) ? h('div', {class: 'fs-xs muted'}, detailOf(step)) : null),
      a ? btn(a.label, {kind: 'primary', icon: a.kind === 'navigate' ? 'arrowRight' : a.kind === 'toggle' ? 'zap' : 'play',
        disabled: !!busy, onClick: () => doAction(step)}) : null);
  }

  function stagePanel(steps, g, m, exclude) {
    const {open, done} = stageRows(steps, g, exclude);
    if (!open.length && !done.length) return null;
    const expanded = doneOpen.has(g);
    return h('div', {class: 'wz-panel'},
      open.map(s => row(s, m)),
      done.length ? h('button', {type: 'button', class: 'wz-done-toggle', 'aria-expanded': String(expanded),
        onClick: () => { expanded ? doneOpen.delete(g) : doneOpen.add(g); draw(true); }},
        icon('check'), ` ${done.length} bước đã xong `, h('span', {class: 'wz-caret'}, expanded ? '▾' : '▸')) : null,
      done.length ? h('div', {class: 'wz-done' + (expanded ? ' open' : '')}, expanded ? done.map(s => row(s, m)) : null) : null);
  }

  function stageBar(list, active) {
    return h('div', {class: 'wz-stages', role: 'tablist'}, list.map(st => h('button', {
      type: 'button', role: 'tab', class: 'wz-stage ' + st.tone + (active === st.id ? ' active' : ''),
      'aria-selected': String(active === st.id),
      onClick: () => { openStage = active === st.id ? '' : st.id; draw(true); }},
    h('span', {class: 'wz-dot ' + st.tone}), h('span', {class: 'wz-stage-t'}, st.title),
    h('span', {class: 'wz-stage-n'}, `${st.done}/${st.total}`))));
  }

  function liveBlock(use) {
    return h('div', {class: 'wz-live'}, liveStrip(dash, use, names).map(x => h('div', {class: 'wz-live-row' + (x.live ? ' on' : '')},
      h('span', {class: 'wz-dot ' + (x.live ? 'danger pulse' : 'neutral')}),
      h('strong', {}, x.label), h('span', {class: 'muted'}, x.live ? 'Đang LIVE' : 'Chưa LIVE'),
      h('span', {class: 'wz-live-v', title: x.video}, icon('video'), ' ', x.video, x.time ? ' · ' + x.time : '', x.match ? ' · ' + x.match : ''),
      h('span', {class: 'wz-pill ' + (x.ai ? 'ok' : '')}, 'AI ' + (x.ai ? 'bật' : 'tắt')),
      h('span', {class: 'wz-pill ' + (x.pin ? 'ok' : '')}, 'Tự ghim ' + (x.pin ? 'chạy' : 'dừng')),
      x.queue != null ? h('span', {class: 'wz-pill'}, `Hàng chờ ${x.queue}`) : null)));
  }

  function togglesBlock(use) {
    const items = quickToggles(dash, use);
    if (!items.length) return null;
    return h('div', {class: 'wz-toggles'}, h('h3', {class: 'wz-h'}, 'Bật/tắt nhanh'),
      h('div', {class: 'wz-tgrid'}, items.map(t => h('div', {class: 'wz-tg' + (t.pending ? ' pending' : ''), title: t.note || ''},
        t.mode === 'info'
          ? h('div', {class: 'wz-tg-info'}, h('span', {class: 'wz-dot ' + (t.on ? 'ok' : 'neutral')}), h('strong', {}, t.label),
            btn('', {size: 'sm', kind: 'ghost', icon: 'help', title: 'Cách bật', onClick: () => toast(t.howTo, 'info', {duration: 8000})}))
          : toggle(t.label, !!t.on, val => flip(t.id, val), {disabled: !!busy}),
        h('div', {class: 'wz-tg-note'}, h('span', {class: 'wz-dot ' + (t.error ? 'danger' : t.pending ? 'warn' : t.on ? 'ok' : 'neutral')}), t.note || '')))));
  }

  function draw(force = false) {
    const r = readinessOf(dash);
    const use = r ? choosePlatforms(pchoice, r) : [];
    const strip = r ? liveStrip(dash, use, names) : null;
    const key = JSON.stringify([r, quickToggles(dash, use), strip, mode, pchoice, busy, openStage, [...doneOpen]]);
    if (!force && key === lastKey) return;
    lastKey = key;
    if (!r) { clear(root); root.hidden = true; return; }
    root.hidden = false;
    const {steps, summary} = applyPlatforms(r, use);
    const m = pickMode(mode, summary);
    const phase = phaseOf(r, use, summary);
    isLive = phase === 'live';
    const nxt = phase === 'prep' ? nextStep(steps) : null;
    const st = stages(steps);
    // Mặc định: Người mới mở chặng của việc tiếp theo; Đã quen và lúc LIVE thì gộp hết.
    const active = openStage !== null ? openStage : (m === 'new' && nxt ? nxt.group : '');
    root.className = 'card live-wizard phase-' + phase + ' mode-' + m;
    const head = h('header', {class: 'wz-head'},
      h('div', {class: 'wz-title'}, h('h2', {}, phase === 'live' ? 'Đang LIVE' : 'Trợ lý LIVE'),
        h('span', {class: 'wz-sum ' + (phase === 'prep' ? 'warn' : 'ok')}, summaryLine(summary, phase))),
      h('div', {class: 'wz-ctrls'},
        segmented(PLATFORM_CHOICES, pchoice, id => { pchoice = id; savePlatform(store(), id); draw(true); }, {label: 'Hôm nay LIVE'}),
        segmented([['new', 'Người mới'], ['pro', 'Đã quen']], m, id => { mode = id; saveMode(store(), id); draw(true); }, {label: 'Chế độ trợ lý'})));
    const body = [stageBar(st, active)];
    if (phase === 'live') body.push(liveBlock(use));
    if (phase === 'ready') body.push(h('div', {class: 'wz-ready'}, icon('check'), ' Sẵn sàng LIVE ✓', h('span', {class: 'muted'}, ' · bấm phát trên sàn là Kid TE tự nhận phòng')));
    if (nxt) body.push(nextCard(nxt, m));
    if (active) body.push(stagePanel(steps, active, m, nxt ? nxt.id : ''));
    body.push(togglesBlock(use));
    clear(root, head, h('div', {class: 'wz-wrap'}, body));
  }

  return {el: root, update(next, videoNames) { dash = next; if (videoNames) names = videoNames; draw(); }};
}
