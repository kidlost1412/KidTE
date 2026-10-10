import {h} from '../core/dom.js';
import {icon} from '../core/icons.js';
import {btn} from './kit.js';

// ---------------- Toast ----------------
let toastHost = null;
function host() {
  if (!toastHost || !toastHost.isConnected) {
    toastHost = h('div', {class: 'toasts', role: 'region', 'aria-label': 'Thông báo', 'aria-live': 'polite'});
    document.body.append(toastHost);
  }
  return toastHost;
}

/** toast('Đã lưu', 'ok', {action: 'Hoàn tác', onAction}) */
export function toast(message, tone = 'info', {action, onAction, duration} = {}) {
  const item = h('div', {class: `toast ${tone}`, role: tone === 'danger' ? 'alert' : 'status'});
  const close = () => { item.classList.add('leaving'); setTimeout(() => item.remove(), 200); };
  item.append(h('div', {class: 'toast-body'}, message));
  if (action && onAction) item.append(h('button', {class: 'toast-action', type: 'button', onClick: () => { close(); onAction(); }}, action));
  item.append(h('button', {class: 'toast-close', type: 'button', 'aria-label': 'Đóng', onClick: close}, icon('x')));
  host().append(item);
  const ms = duration ?? (action ? 9000 : tone === 'danger' ? 8000 : 4500);
  let timer = setTimeout(close, ms);
  item.addEventListener('mouseenter', () => clearTimeout(timer));
  item.addEventListener('mouseleave', () => { timer = setTimeout(close, 2500); });
  return close;
}

// ---------------- Modal ----------------
const stack = [];
function trapKeys(event) {
  const top = stack[stack.length - 1];
  if (!top) return;
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); top.dismiss(); }
  if (event.key === 'Tab') {
    const focusables = [...top.node.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')].filter(el => !el.disabled && el.offsetParent !== null);
    if (!focusables.length) return;
    const first = focusables[0], last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
}
document.addEventListener('keydown', trapKeys, true);

export function isOverlayOpen() { return stack.length > 0; }

/** Mở modal; trả {close, node}. onDismiss gọi khi bấm nền / Esc. */
export function openModal({title, description, body, footer, size = '', onDismiss, dismissible = true, labelledBy} = {}) {
  const previous = document.activeElement;
  const titleId = labelledBy || ('m' + Math.random().toString(36).slice(2, 8));
  const modal = h('div', {class: `modal ${size}`, role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId},
    title ? h('header', {class: 'modal-head'}, h('div', {class: 'grow'}, h('h2', {id: titleId}, title), description ? h('p', {}, description) : null),
      dismissible ? h('button', {class: 'btn icon ghost', type: 'button', 'aria-label': 'Đóng', onClick: () => entry.dismiss()}, icon('x')) : null) : null,
    h('div', {class: 'modal-body'}, body),
    footer ? h('footer', {class: 'modal-foot'}, footer) : null);
  const overlay = h('div', {class: 'overlay'}, modal);
  const entry = {
    node: overlay,
    close() {
      const i = stack.indexOf(entry);
      if (i >= 0) stack.splice(i, 1);
      overlay.remove();
      if (previous && previous.focus) try { previous.focus({preventScroll: true}); } catch (_) { /* */ }
    },
    dismiss() { if (!dismissible) return; entry.close(); if (onDismiss) onDismiss(); },
  };
  overlay.addEventListener('mousedown', e => { if (e.target === overlay) entry.dismiss(); });
  stack.push(entry);
  document.body.append(overlay);
  requestAnimationFrame(() => {
    const auto = modal.querySelector('[autofocus], .modal-body input, .modal-body textarea, .modal-body select, .modal-foot .btn.primary, .modal-foot .btn.danger');
    (auto || modal).focus?.();
  });
  return entry;
}

/** Hộp xác nhận thay window.confirm (pywebview hiển thị confirm rất xấu). */
export function confirmDialog({title, text, confirm = 'Đồng ý', cancel = 'Huỷ', tone = 'primary', details} = {}) {
  return new Promise(resolve => {
    let done = false;
    const finish = value => { if (done) return; done = true; entry.close(); resolve(value); };
    const ok = btn(confirm, {kind: tone, onClick: () => finish(true)});
    const entry = openModal({
      title, body: h('div', {class: 'col gap-3'}, text ? h('p', {class: 'soft'}, text) : null, details || null),
      footer: [btn(cancel, {kind: 'ghost', onClick: () => finish(false)}), ok],
      onDismiss: () => { if (!done) { done = true; resolve(false); } },
    });
    requestAnimationFrame(() => ok.focus());
  });
}

export function promptDialog({title, text, label, value = '', placeholder = '', confirm = 'Lưu', multiline = false, validate} = {}) {
  return new Promise(resolve => {
    let done = false;
    const control = multiline
      ? h('textarea', {class: 'textarea', placeholder, rows: 4}, value)
      : h('input', {class: 'input', value, placeholder, autofocus: true});
    const err = h('div', {class: 'field-error'});
    const submit = () => {
      const v = control.value;
      const problem = validate ? validate(v) : '';
      if (problem) { err.textContent = problem; control.focus(); return; }
      done = true; entry.close(); resolve(v);
    };
    control.addEventListener('keydown', e => { if (e.key === 'Enter' && (!multiline || e.ctrlKey)) { e.preventDefault(); submit(); } });
    const entry = openModal({
      title, body: h('div', {class: 'col gap-3'}, text ? h('p', {class: 'soft'}, text) : null, h('div', {class: 'field'}, label ? h('label', {}, label) : null, control, err)),
      footer: [btn('Huỷ', {kind: 'ghost', onClick: () => { done = true; entry.close(); resolve(null); }}), btn(confirm, {kind: 'primary', onClick: submit})],
      onDismiss: () => { if (!done) { done = true; resolve(null); } },
    });
    requestAnimationFrame(() => { control.focus(); if (control.select) control.select(); });
  });
}

// ---------------- Drawer ----------------
export function openDrawer({title, sub, body, footer, width = 560, onClose} = {}) {
  const previous = document.activeElement;
  const shade = h('div', {class: 'drawer-overlay'});
  const panel = h('aside', {class: 'drawer', role: 'dialog', 'aria-modal': 'true', 'aria-label': title, style: {width: `min(${width}px, 96vw)`}},
    h('header', {class: 'card-head'}, h('div', {class: 'grow'}, h('h2', {}, title), sub ? h('div', {class: 'sub'}, sub) : null),
      h('button', {class: 'btn icon ghost', type: 'button', 'aria-label': 'Đóng', onClick: () => entry.dismiss()}, icon('x'))),
    h('div', {style: {flex: '1', overflow: 'auto'}}, body),
    footer ? h('footer', {class: 'card-foot'}, footer) : null);
  const entry = {
    node: panel,
    close() { const i = stack.indexOf(entry); if (i >= 0) stack.splice(i, 1); shade.remove(); panel.remove(); previous?.focus?.({preventScroll: true}); },
    dismiss() { entry.close(); if (onClose) onClose(); },
  };
  shade.addEventListener('mousedown', () => entry.dismiss());
  stack.push(entry);
  document.body.append(shade, panel);
  requestAnimationFrame(() => panel.querySelector('input, textarea, select, button')?.focus?.());
  return entry;
}

// ---------------- Menu ----------------
export function openMenu(anchor, items) {
  const rect = anchor.getBoundingClientRect();
  const menu = h('div', {class: 'menu', role: 'menu'});
  const close = () => { menu.remove(); document.removeEventListener('mousedown', outside, true); document.removeEventListener('keydown', esc, true); };
  const outside = e => { if (!menu.contains(e.target)) close(); };
  const esc = e => { if (e.key === 'Escape') { e.stopPropagation(); close(); anchor.focus(); } };
  items.forEach(item => {
    if (item === '-') { menu.append(h('hr')); return; }
    menu.append(h('button', {type: 'button', role: 'menuitem', class: item.danger ? 'danger' : '', disabled: item.disabled, onClick: () => { close(); item.onClick(); }},
      item.icon ? icon(item.icon) : null, h('span', {class: 'grow'}, item.label), item.kbd ? h('span', {class: 'kbd'}, item.kbd) : null));
  });
  document.body.append(menu);
  const w = menu.offsetWidth, hgt = menu.offsetHeight;
  menu.style.left = Math.min(window.innerWidth - w - 8, Math.max(8, rect.right - w)) + 'px';
  menu.style.top = (rect.bottom + hgt + 8 > window.innerHeight ? Math.max(8, rect.top - hgt - 6) : rect.bottom + 6) + 'px';
  setTimeout(() => { document.addEventListener('mousedown', outside, true); document.addEventListener('keydown', esc, true); });
  menu.querySelector('button')?.focus();
  return close;
}

// ---------------- Tooltip (dùng chung, 1 node) ----------------
let tipNode = null;
export function showTip(content, x, y) {
  if (!tipNode) { tipNode = h('div', {class: 'tooltip', role: 'tooltip'}); document.body.append(tipNode); }
  tipNode.replaceChildren(content instanceof Node ? content : document.createTextNode(String(content)));
  tipNode.hidden = false;
  const w = tipNode.offsetWidth, hh = tipNode.offsetHeight;
  tipNode.style.left = Math.min(window.innerWidth - w - 8, Math.max(8, x - w / 2)) + 'px';
  tipNode.style.top = (y - hh - 12 < 8 ? y + 18 : y - hh - 12) + 'px';
}
export function hideTip() { if (tipNode) tipNode.hidden = true; }
