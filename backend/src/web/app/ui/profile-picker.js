import {h, uid} from '../core/dom.js';
import {icon} from '../core/icons.js';

/** Profile selector with thumbnails inside the popup options. */
export function profilePicker(profiles, value, {label, onChange} = {}) {
  const options = [{id: '', name: '— Chưa gắn hồ sơ —'}, ...profiles];
  const selected = options.find(p => p.id === value) || options[0];
  const nameOf = p => p.name + (p.package ? ` (${p.package})` : '');
  const listId = uid('profile-options');
  const button = h('button', {type: 'button', class: 'select profile-picker',
    'aria-label': label || 'Chọn hồ sơ', 'aria-haspopup': 'listbox', 'aria-expanded': 'false'},
    h('span', {class: 'profile-picker-label', title: nameOf(selected)}, nameOf(selected)), icon('chevronDown', {size: 16}));
  let popup = null, active = 0, rows = [], visible = options, search = null, optionList = null;
  const fold = value => String(value || '').toLowerCase().replace(/đ/g, 'd').normalize('NFD').replace(/[\u0300-\u036f]/g, '');

  function close(focus = false) {
    if (!popup) return;
    popup.remove(); popup = null; rows = [];
    button.setAttribute('aria-expanded', 'false');
    button.removeAttribute('aria-controls');
    document.removeEventListener('pointerdown', outside, true);
    window.removeEventListener('resize', reposition);
    window.removeEventListener('scroll', scrolling, true);
    if (focus && button.isConnected) button.focus();
  }
  function outside(event) {
    if (!button.contains(event.target) && !popup?.contains(event.target)) close();
  }
  function scrolling(event) {
    if (popup && !popup.contains(event.target)) close();
  }
  function reposition() {
    if (!popup) return;
    const rect = button.getBoundingClientRect();
    const width = Math.min(680, window.innerWidth - 16, Math.max(rect.width, 420));
    popup.style.width = `${width}px`;
    popup.style.left = `${Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8))}px`;
    const below = window.innerHeight - rect.bottom - 12;
    const above = rect.top - 12;
    const up = below < Math.min(300, above);
    popup.style.maxHeight = `${Math.max(80, Math.min(440, up ? above : below))}px`;
    popup.style.top = up ? '' : `${rect.bottom + 4}px`;
    popup.style.bottom = up ? `${window.innerHeight - rect.top + 4}px` : '';
  }
  function focusRow(index) {
    active = Math.max(0, Math.min(visible.length - 1, index));
    rows.forEach((row, i) => { row.tabIndex = i === active ? 0 : -1; });
    rows[active]?.focus();
    rows[active]?.scrollIntoView?.({block: 'nearest'});
  }
  function choose(id) {
    close(true);
    if (id !== (value || '')) onChange?.(id);
  }
  function keyboard(event) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(true); }
    else if (event.key === 'ArrowDown') { event.preventDefault(); focusRow(event.target === search ? 0 : active + 1); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); focusRow(event.target === search ? visible.length - 1 : active - 1); }
    else if (event.target === search && (event.key === 'Home' || event.key === 'End')) return;
    else if (event.key === 'Home') { event.preventDefault(); focusRow(0); }
    else if (event.key === 'End') { event.preventDefault(); focusRow(visible.length - 1); }
    else if (event.key === 'Tab') { event.preventDefault(); close(true); }
  }
  function renderOptions() {
    const query = fold(search.value.trim());
    visible = options.filter(p => !query || (p.id && fold(nameOf(p)).includes(query)));
    rows = visible.map(p => {
      const imageBox = h('span', {class: 'profile-picker-image', 'aria-hidden': 'true'}, icon('fileText', {size: 22}));
      if (p.image_url) {
        const img = h('img', {src: p.image_url, alt: '', loading: 'lazy', decoding: 'async'});
        img.addEventListener('error', () => img.remove(), {once: true});
        imageBox.append(img);
      }
      return h('button', {type: 'button', class: 'profile-picker-option', role: 'option',
        'aria-selected': String(p.id === (value || '')), tabIndex: -1,
        onClick: () => choose(p.id), onFocus: () => { active = visible.indexOf(p); }},
        imageBox, h('span', {class: 'profile-picker-name'}, nameOf(p)),
        p.id === (value || '') ? icon('check', {size: 16}) : null);
    });
    optionList.replaceChildren(...rows);
    if (!rows.length) optionList.append(h('div', {class: 'profile-picker-empty', role: 'status'}, 'Không có hồ sơ phù hợp'));
    active = Math.max(0, visible.findIndex(p => p.id === (value || '')));
  }
  function open(last = false) {
    if (popup) return;
    search = h('input', {type: 'search', class: 'input', placeholder: 'Tìm hồ sơ theo tên…', 'aria-label': 'Tìm hồ sơ theo tên',
      onInput: renderOptions});
    optionList = h('div', {id: listId, role: 'listbox', 'aria-label': label || 'Chọn hồ sơ'});
    popup = h('div', {class: 'profile-picker-popup', onKeydown: keyboard},
      h('div', {class: 'profile-picker-search'}, search), optionList);
    renderOptions();
    document.body.append(popup);
    button.setAttribute('aria-expanded', 'true');
    button.setAttribute('aria-controls', listId);
    reposition();
    if (last) focusRow(options.length - 1); else search.focus();
    document.addEventListener('pointerdown', outside, true);
    window.addEventListener('resize', reposition);
    window.addEventListener('scroll', scrolling, true);
  }
  button.addEventListener('click', () => popup ? close(true) : open());
  button.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault(); open(event.key === 'ArrowUp');
    }
  });
  return {node: button, dispose: () => close()};
}
