// Chọn giỏ sản phẩm theo tài khoản LIVE (trang Sản phẩm + Studio Edit).
import {h, clear} from '../core/dom.js';
import {request, friendlyError} from '../core/api.js';
import {btn, select} from '../ui/kit.js';
import {toast, confirmDialog, promptDialog} from '../ui/overlays.js';
import {cartOptions, cartLockNote, deleteCartText} from './product-list-model.js';

export function loadCarts() { return request('/api/system/carts'); }

/**
 * Thanh giỏ của trang Sản phẩm: chọn giỏ đang dùng, đổi tên, xoá (có xác nhận).
 * onChange() chạy sau khi giỏ hoạt động đổi để trang tải lại danh mục.
 */
export function cartBar({onChange} = {}) {
  const host = h('div', {class: 'row', style: {gap: '8px', flexWrap: 'wrap', alignItems: 'center', marginBottom: '12px'}});
  let state = null;
  async function post(path, body, okText) {
    try {
      state = await request('/api/system/carts/' + path, {method: 'POST', body});
      if (okText) toast(okText, 'ok');
      render();
      if (onChange) await onChange(state);
    } catch (e) { toast(friendlyError(e), 'danger'); }
  }
  function render() {
    if (!state || !(state.carts || []).length) { clear(host); return; }
    const current = state.carts.find(c => c.key === state.selected) || state.carts.find(c => c.active) || state.carts[0];
    const picker = select(cartOptions(state), current.key, {'aria-label': 'Giỏ sản phẩm', onChange: e => post('select', {key: e.target.value})});
    const note = cartLockNote(state);
    clear(host,
      h('strong', {class: 'fs-sm'}, 'Giỏ:'), picker,
      btn('Đổi tên', {size: 'sm', kind: 'ghost', icon: 'edit', onClick: async () => {
        const name = await promptDialog({title: 'Đổi tên giỏ', text: 'Để trống để dùng lại tên tài khoản LIVE.', label: 'Tên giỏ', value: current.custom_name ? current.name : ''});
        if (name !== null && name !== undefined) post('rename', {key: current.key, name: String(name).trim()}, 'Đã đổi tên giỏ.');
      }}),
      btn('Xoá giỏ', {size: 'sm', kind: 'ghost', icon: 'trash', disabled: current.live, onClick: async () => {
        const {title, text} = deleteCartText(current);
        if (await confirmDialog({title, text, confirm: 'Xoá giỏ', tone: 'danger'})) post('delete', {key: current.key}, 'Đã xoá giỏ.');
      }}),
      note ? h('span', {class: 'fs-xs muted'}, note) : null);
  }
  loadCarts().then(s => { state = s; render(); }).catch(() => clear(host));
  return host;
}

/** Ô chọn giỏ cho Studio Edit (không đổi giỏ hoạt động của AI). onPick(key). */
export function cartSelect(value, onPick) {
  const host = h('span', {class: 'row', style: {gap: '6px', alignItems: 'center'}});
  loadCarts().then(state => {
    const options = cartOptions(state);
    if (options.length < 2) return; // một giỏ thì không cần chọn
    const chosen = value && options.some(o => o[0] === value) ? value : state.active;
    clear(host, h('span', {class: 'fs-xs muted'}, 'Giỏ:'),
      select(options, chosen, {'aria-label': 'Giỏ sản phẩm cho timeline', onChange: e => onPick(e.target.value)}));
  }).catch(() => {});
  return host;
}
