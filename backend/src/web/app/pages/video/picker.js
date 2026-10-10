import {h, clear} from '../../core/dom.js';
import {icon} from '../../core/icons.js';
import {fold, clock} from '../../core/format.js';
import {btn, bag, productAvatar, chip} from '../../ui/kit.js';
import {openModal} from '../../ui/overlays.js';
import {keyForProductId, usedProductKeys} from './model.js';
import {resolveProduct} from './data.js';

/**
 * Chọn sản phẩm để ghim. Resolve: khoá timeline ('sku_<id>' / khoá cũ),
 * null = "không ghim", undefined = huỷ.
 */
export function pickProduct({doc, catalog, current = undefined, title = 'Chọn sản phẩm để ghim', atMs = null, allowNone = true}) {
  return new Promise(resolve => {
    let done = false;
    const finish = value => { if (done) return; done = true; entry.close(); resolve(value); };
    const used = new Set(usedProductKeys(doc));
    const products = (catalog.products || []).map(p => ({...p, key: keyForProductId(p.product_id)}));
    const legacy = usedProductKeys(doc).filter(key => !products.some(p => p.key === key)).map(key => ({...resolveProduct(doc, key, catalog), key}));
    const search = h('input', {class: 'input', type: 'search', placeholder: 'Gõ số giỏ (vd 3) hoặc tên: “đạm cá”, “tc01”…', 'aria-label': 'Tìm sản phẩm'});
    const grid = h('div', {class: 'col gap-4'});
    let flat = [];
    let cursor = 0;

    function item(p, isLegacy = false) {
      const node = h('button', {class: 'picker-item' + (p.key === current ? ' current' : ''), type: 'button', dataset: {key: p.key}, onClick: () => finish(p.key)},
        productAvatar(p, 52),
        h('span', {class: 'grow', style: {minWidth: '0'}},
          h('span', {class: 'row', style: {gap: '6px', marginBottom: '2px'}}, bag(p.bag_index, {size: 'lg'}), p.price ? h('small', {class: 'price'}, p.price) : null,
            used.has(p.key) ? chip('Đã dùng', 'info') : null, isLegacy ? chip('Không còn bán', 'warn') : null),
          h('strong', {class: 'clamp-2'}, p.name),
          h('small', {class: 'truncate', title: p.title}, p.profile_name || p.title)));
      return node;
    }
    function render() {
      const q = fold(search.value);
      const byBag = /^#?\d+$/.test(q.trim()) ? Number(q.replace('#', '')) : null;
      const match = p => !q || (byBag !== null ? Number(p.bag_index) === byBag : fold(`${p.name} ${p.title} ${p.profile_name || ''} ${p.bag_index}`).includes(q));
      const inVideo = products.filter(p => used.has(p.key) && match(p));
      const others = products.filter(p => !used.has(p.key) && match(p));
      const old = legacy.filter(match);
      flat = [];
      const sections = [];
      const section = (label, list, isLegacy) => {
        if (!list.length) return;
        flat.push(...list);
        sections.push(h('div', {}, h('div', {class: 'section-title', style: {margin: '4px 0 8px'}}, label), h('div', {class: 'picker-grid'}, list.map(p => item(p, isLegacy)))));
      };
      section('Đã có trong video này', inVideo);
      section(inVideo.length ? 'Các sản phẩm khác trong giỏ hôm nay' : 'Giỏ hàng hôm nay', others);
      section('Có trong kịch bản nhưng không còn trong giỏ hôm nay', old, true);
      if (allowNone && !q) sections.push(h('div', {}, h('div', {class: 'section-title', style: {margin: '4px 0 8px'}}, 'Khác'),
        h('button', {class: 'picker-item picker-none' + (current === null ? ' current' : ''), type: 'button', onClick: () => finish(null)},
          h('span', {class: 'pavatar none', style: {'--size': '52px'}}, icon('eyeOff')),
          h('span', {class: 'grow'}, h('strong', {}, 'Không ghim sản phẩm'), h('small', {}, 'Đoạn nghỉ, chào khách, chuyển cảnh…')))));
      if (!sections.length) sections.push(h('div', {class: 'empty'}, h('strong', {}, 'Không tìm thấy sản phẩm'), h('p', {}, 'Thử số giỏ hoặc tên khác.')));
      cursor = Math.min(cursor, Math.max(0, flat.length - 1));
      clear(grid, sections);
      markCursor();
    }
    function markCursor() {
      grid.querySelectorAll('.picker-item[aria-selected]').forEach(n => n.removeAttribute('aria-selected'));
      const target = flat[cursor] && grid.querySelector(`.picker-item[data-key="${CSS.escape(flat[cursor].key)}"]`);
      if (target) { target.setAttribute('aria-selected', 'true'); target.scrollIntoView({block: 'nearest'}); }
    }
    search.addEventListener('input', () => { cursor = 0; render(); });
    search.addEventListener('keydown', e => {
      const cols = Math.max(1, Math.floor((grid.clientWidth || 600) / 218));
      if (e.key === 'ArrowDown') { e.preventDefault(); cursor = Math.min(flat.length - 1, cursor + cols); markCursor(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); cursor = Math.max(0, cursor - cols); markCursor(); }
      else if (e.key === 'ArrowRight' && search.selectionStart === search.value.length) { e.preventDefault(); cursor = Math.min(flat.length - 1, cursor + 1); markCursor(); }
      else if (e.key === 'ArrowLeft' && search.selectionStart === 0) { e.preventDefault(); cursor = Math.max(0, cursor - 1); markCursor(); }
      else if (e.key === 'Enter') { e.preventDefault(); if (flat[cursor]) finish(flat[cursor].key); }
    });
    const entry = openModal({
      title, size: 'xwide',
      description: atMs !== null ? `Sản phẩm sẽ được ghim từ ${clock(atMs)} cho tới mốc kế tiếp.` : 'Chọn sản phẩm cho đoạn đang chọn.',
      body: h('div', {class: 'col gap-4'}, h('div', {class: 'search-input'}, icon('search'), search), grid),
      footer: [h('span', {class: 'fs-xs muted grow'}, 'Mẹo: trong Studio, bấm phím số giỏ (1–9, 0 = giỏ 10) để ghim ngay không cần mở bảng này.'), btn('Huỷ', {kind: 'ghost', onClick: () => finish(undefined)})],
      onDismiss: () => { if (!done) { done = true; resolve(undefined); } },
    });
    render();
    requestAnimationFrame(() => search.focus());
  });
}
