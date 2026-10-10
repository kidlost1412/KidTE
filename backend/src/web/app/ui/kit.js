import {h} from '../core/dom.js';
import {icon} from '../core/icons.js';
import {initials, hashHue} from '../core/format.js';

export function btn(label, {kind = '', size = '', icon: iconName = '', onClick, title, type = 'button', disabled, kbd, attrs = {}} = {}) {
  const classes = ['btn', kind, size, !label && iconName ? 'icon' : ''].filter(Boolean).join(' ');
  return h('button', {class: classes, type, title: title || (label ? null : attrs['aria-label']), disabled, onClick, ...attrs},
    iconName ? icon(iconName) : null,
    label ? h('span', {}, label) : null,
    kbd ? h('span', {class: 'kbd'}, kbd) : null);
}

export function iconBtn(iconName, label, onClick, {kind = 'ghost', size = '', disabled, attrs = {}} = {}) {
  return h('button', {class: ['btn', 'icon', kind, size].filter(Boolean).join(' '), type: 'button', title: label, 'aria-label': label, disabled, onClick, ...attrs}, icon(iconName));
}

export function chip(label, tone = '', {iconName, dot = false, title, size = ''} = {}) {
  return h('span', {class: ['chip', tone, size].filter(Boolean).join(' '), title},
    dot ? h('i', {class: 'dot'}) : null, iconName ? icon(iconName) : null, label);
}

export function bagColor(index) {
  const n = Number(index);
  if (!Number.isFinite(n) || n <= 0) return 'var(--bag-none)';
  return `var(--bag-${((n - 1) % 10) + 1})`;
}

export function bag(index, {size = '', ghost = false, title} = {}) {
  const label = Number(index) > 0 ? `#${index}` : '—';
  return h('span', {class: ['bag', size, ghost ? 'ghost' : ''].filter(Boolean).join(' '), style: {'--bag': bagColor(index)}, title: title || (Number(index) > 0 ? `Giỏ số ${index}` : 'Không ghim sản phẩm')}, label);
}

/** Ảnh sản phẩm; mất ảnh thì hiện chữ viết tắt trên nền màu của số giỏ. */
export function productAvatar(product, size = 40, {showBag = true} = {}) {
  const bagIndex = product && product.bag_index;
  const wrap = h('span', {class: 'pavatar' + (product ? '' : ' none'), style: {'--size': size + 'px', '--bag': bagColor(bagIndex)}});
  const fallback = () => h('span', {class: 'pavatar-fallback'}, product ? initials(product.name || product.title || 'SP') : '');
  if (product && product.image_url) {
    const img = h('img', {src: product.image_url, alt: '', loading: 'lazy', decoding: 'async', draggable: 'false'});
    img.addEventListener('error', () => img.replaceWith(fallback()), {once: true});
    wrap.append(img);
  } else {
    wrap.append(fallback());
  }
  if (showBag && product && Number(bagIndex) > 0 && size >= 34) wrap.append(h('span', {class: 'pavatar-bag'}, String(bagIndex)));
  return wrap;
}

export function personAvatar(name, key, size = 32) {
  const hue = hashHue(key || name);
  return h('span', {class: 'avatar', style: {width: size + 'px', height: size + 'px', background: `hsl(${hue} 55% 46%)`, fontSize: Math.round(size * 0.4) + 'px'}}, initials(name));
}

export function empty(title, text = '', {iconName = 'inbox', action} = {}) {
  return h('div', {class: 'empty'}, h('span', {class: 'empty-icon'}, icon(iconName)), h('strong', {}, title), text ? h('p', {}, text) : null, action || null);
}

export function errorBox(text, retry) {
  return h('div', {class: 'error-box'}, icon('alert'), h('div', {class: 'grow'}, h('strong', {}, 'Không tải được dữ liệu'), h('p', {class: 'fs-sm'}, text)), retry ? btn('Thử lại', {size: 'sm', onClick: retry}) : null);
}

export function loading(text = 'Đang tải…') {
  return h('div', {class: 'loading', role: 'status'}, h('span', {class: 'spinner'}), text);
}

export function skeletonRows(n = 6, height = 44) {
  return h('div', {class: 'col', style: {padding: '12px 16px', gap: '10px'}}, Array.from({length: n}, () => h('div', {class: 'skeleton', style: {height: height + 'px'}})));
}

export function notice(tone, title, text, action) {
  const iconName = tone === 'danger' ? 'alert' : tone === 'warn' ? 'alert' : tone === 'ok' ? 'checkCircle' : 'info';
  return h('div', {class: `notice ${tone}`, role: tone === 'danger' ? 'alert' : 'status'}, icon(iconName),
    h('div', {class: 'notice-body'}, title ? h('div', {class: 'notice-title'}, title) : null, text ? h('div', {}, text) : null),
    action || null);
}

export function field(label, control, {hint, error, id} = {}) {
  const controlId = id || control.id || ('f' + Math.random().toString(36).slice(2, 8));
  if (!control.id) control.id = controlId;
  return h('div', {class: 'field'}, h('label', {for: controlId}, label), control,
    hint ? h('div', {class: 'field-hint'}, hint) : null, error ? h('div', {class: 'field-error'}, error) : null);
}

export function input(props = {}) { return h('input', {class: 'input', type: 'text', ...props}); }
export function textarea(props = {}) { return h('textarea', {class: 'textarea', ...props}); }
export function select(options, value, props = {}) {
  const node = h('select', {class: 'select', ...props});
  options.forEach(opt => {
    const [val, label] = Array.isArray(opt) ? opt : [opt.value, opt.label];
    node.append(h('option', {value: val, selected: String(val) === String(value)}, label));
  });
  return node;
}

export function toggle(label, checked, onChange, {disabled} = {}) {
  const inputNode = h('input', {type: 'checkbox', checked, disabled, onChange: e => onChange && onChange(e.target.checked)});
  return h('label', {class: 'switch'}, inputNode, h('span', {class: 'switch-track'}), h('span', {}, label));
}

export function tabs(items, active, onSelect) {
  return h('div', {class: 'tabs', role: 'tablist'}, items.map(item => h('button', {
    class: 'tab', role: 'tab', type: 'button', 'aria-selected': String(item.id === active),
    onClick: () => onSelect(item.id),
  }, item.icon ? icon(item.icon, {size: 16}) : null, item.label,
  item.count !== undefined && item.count !== null ? h('span', {class: 'count' + (item.alert ? ' alert' : '')}, String(item.count)) : null)));
}

export function segmented(items, active, onSelect, {label} = {}) {
  return h('div', {class: 'segmented', role: 'group', 'aria-label': label}, items.map(([id, text]) => h('button', {
    type: 'button', 'aria-pressed': String(id === active), onClick: () => onSelect(id),
  }, text)));
}

export function stat(label, value, hint, {tone} = {}) {
  return h('div', {class: 'stat'}, h('span', {class: 'stat-label'}, label),
    h('span', {class: 'stat-value', style: tone ? {color: `var(--${tone}-text)`} : null}, value),
    hint ? h('span', {class: 'stat-hint'}, hint) : null);
}

export function progress(value, {indeterminate = false, tone} = {}) {
  const bar = h('i', {style: {width: Math.max(0, Math.min(100, Number(value) || 0)) + '%', background: tone ? `var(--${tone})` : null}});
  return h('div', {class: 'progress' + (indeterminate ? ' indeterminate' : ''), role: 'progressbar', 'aria-valuenow': String(Math.round(value || 0)), 'aria-valuemin': '0', 'aria-valuemax': '100'}, bar);
}

export function kv(rows) {
  return h('dl', {class: 'kv'}, rows.filter(Boolean).flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v === null || v === undefined || v === '' ? '—' : v)]));
}

export function card(title, body, {sub, actions, className = '', footer} = {}) {
  return h('section', {class: 'card ' + className},
    title ? h('header', {class: 'card-head'}, h('div', {class: 'grow'}, h('h2', {}, title), sub ? h('div', {class: 'sub'}, sub) : null), actions ? h('div', {class: 'row'}, actions) : null) : null,
    body, footer ? h('footer', {class: 'card-foot'}, footer) : null);
}

export function pageHead(title, lede, actions, crumbs) {
  return h('header', {class: 'page-head'},
    h('div', {class: 'grow'},
      crumbs ? h('nav', {class: 'crumbs', 'aria-label': 'Vị trí'}, crumbs) : null,
      h('h1', {}, title), lede ? h('p', {class: 'lede'}, lede) : null),
    actions ? h('div', {class: 'actions'}, actions) : null);
}
