import {h, clear, disposer, debounce} from '../core/dom.js';
import {icon} from '../core/icons.js';
import {request, friendlyError} from '../core/api.js';
import {relative, number, fold, dateTime, sizeMismatch} from '../core/format.js';
import {btn, chip, bag, empty, errorBox, skeletonRows, pageHead, tabs, kv, productAvatar, field, input, textarea, notice, segmented} from '../ui/kit.js';
import {toast, openDrawer, openModal, confirmDialog, promptDialog} from '../ui/overlays.js';
import {loadCatalog, invalidateCatalog} from './video/data.js';
import {doseRulesPanel} from './dose-rules-ui.js';
import * as mm from './mapping-model.js';
import * as pd from './profile-delete-model.js';
import * as pl from './product-list-model.js';
import {openProductIngest} from './product-ingest-ui.js';
import {openProfileEnrich, openBulkEnrich} from './product-enrich-ui.js';
import {profilePicker} from '../ui/profile-picker.js';
import {openComboProfiles} from './combo-profile-ui.js';
import {defaultProfilesPanel} from './product-defaults-ui.js';

const TABS = [
  {id: 'products', label: 'Sản phẩm đang bán', icon: 'cart'},
  {id: 'profiles', label: 'Hồ sơ kỹ thuật', icon: 'fileText'},
  {id: 'mapping', label: 'Gắn hồ sơ cho sản phẩm', icon: 'link'},
];

const PROFILE_FIELDS = [
  ['ten_san_pham', 'Tên sản phẩm', 'text'],
  ['phan_khuc', 'Nhóm công dụng', 'text'],
  ['mo_ta_ngan', 'Mô tả ngắn', 'textarea'],
  ['nha_san_xuat', 'Nhà sản xuất', 'text'],
  ['quy_cach_dong_goi', 'Quy cách đóng gói', 'text'],
  ['gia_goc_duyet', 'Giá AI được phép nói', 'text'],
  ['live_promotion', 'Khuyến mãi trong LIVE', 'textarea'],
  ['cong_dung', 'Công dụng (mỗi dòng một ý)', 'list'],
  ['doi_tuong_cay_trong', 'Cây trồng phù hợp (mỗi dòng một loại)', 'list'],
  ['hieu_qua_chinh', 'Hiệu quả chính', 'textarea'],
  ['huong_dan_su_dung', 'Cách dùng & liều lượng (mỗi dòng một hướng dẫn)', 'list'],
  ['luu_y', 'Lưu ý an toàn', 'textarea'],
  ['listing_aliases', 'Tên gọi khác khách hay dùng (mỗi dòng một tên)', 'list'],
  ['nguon_chinh_thuc', 'Nguồn thông tin chính thức', 'text'],
];

function listOf(value) { return Array.isArray(value) ? value : value ? [String(value)] : []; }

/** Ảnh vuông đầu dòng danh sách; thiếu ảnh hoặc ảnh lỗi thì hiện icon thay thế. */
function rowThumb(url, fallbackIcon, size = 52) {
  const box = h('span', {class: 'row-thumb', style: {'--size': size + 'px'}});
  const placeholder = () => { box.classList.add('none'); box.replaceChildren(icon(fallbackIcon, {size: 22})); };
  if (!url) { placeholder(); return box; }
  const img = h('img', {src: url, alt: '', loading: 'lazy', decoding: 'async', draggable: 'false'});
  img.addEventListener('error', placeholder, {once: true});
  box.append(img);
  return box;
}

/** Dòng "Chọn tất cả" theo danh sách đang hiện; onToggle(bật?) do nơi gọi xử lý. */
function selectAllRow(sel, onToggle) {
  const box = h('input', {type: 'checkbox', 'aria-label': 'Chọn tất cả', checked: sel.all, onChange: e => onToggle(e.target.checked)});
  box.indeterminate = sel.some;
  return h('label', {class: 'list-head'}, box,
    h('span', {class: 'fw-7'}, sel.all ? 'Bỏ chọn tất cả' : 'Chọn tất cả'),
    h('span', {class: 'fs-xs muted'}, `${sel.count}/${sel.total} đang hiện`));
}

// Máy mới cài không có hồ sơ nào: chủ shop tự tạo (gắn luôn với SKU nếu tạo từ sản phẩm).
async function createProfile(ctx, product) {
  const name = await promptDialog({title: product ? 'Tạo hồ sơ cho sản phẩm này' : 'Tạo hồ sơ kỹ thuật mới',
    text: 'Đặt tên ngắn gọn (vd “Đạm Cá Amino 5L”). Tạo xong mở ngay trang điền công dụng, cách dùng & liều lượng, lưu ý.',
    label: 'Tên sản phẩm', value: product ? (product.name || '') : '', confirm: 'Tạo hồ sơ'});
  if (!name || !name.trim()) return;
  try {
    const out = await request('/api/system/profiles/create', {method: 'POST', body: {ten_san_pham: name.trim(), product_id: product ? product.product_id : ''}});
    invalidateCatalog();
    toast(product ? 'Đã tạo hồ sơ NHÁP. Điền công dụng hoặc cách dùng rồi bấm Duyệt (tab “Gắn hồ sơ cho sản phẩm”) để AI dùng.' : 'Đã tạo hồ sơ nháp.', 'ok');
    ctx.navigate('catalog', 'profiles', out.id);
  } catch (e) { toast(friendlyError(e), 'danger'); }
}

export default {
  mount(ctx) {
    const d = disposer();
    const tab = TABS.some(t => t.id === ctx.route.sub) ? ctx.route.sub : 'products';
    const content = h('div', {}, skeletonRows(6, 80));
    const tabBar = h('div');
    clear(ctx.shell.main, h('div', {class: 'page'},
      pageHead('Sản phẩm', 'Sản phẩm đang bán trong giỏ hàng LIVE và hồ sơ kỹ thuật AI được phép dùng để tư vấn. AI chỉ tư vấn sản phẩm đã gắn hồ sơ.'),
      tabBar, h('div', {style: {marginTop: '16px'}}, content)));
    let dash = ctx.store.get().dashboard;
    const renderTabs = () => clear(tabBar, tabs(TABS.map(t => ({...t, count: t.id === 'mapping' && dash && dash.catalog && dash.catalog.unmapped ? dash.catalog.unmapped : undefined, alert: t.id === 'mapping'})), tab, id => ctx.navigate('catalog', id)));
    renderTabs();
    d.add(ctx.store.subscribe(state => { dash = state.dashboard; renderTabs(); }));

    const mountTab = {products: mountProducts, profiles: mountProfiles, mapping: mountMapping}[tab];
    const inner = mountTab(ctx, content, d);
    return {unmount: () => d.dispose(), isDirty: inner && inner.isDirty};
  },
};

// ======================================================================
// SẢN PHẨM ĐANG BÁN
// ======================================================================
function mountProducts(ctx, host, d) {
  let products = null, workspace = {}, error = null, query = '', filter = 'all';
  const picked = new Set();
  const search = h('input', {class: 'input', type: 'search', placeholder: 'Tìm theo tên, số giỏ, hồ sơ…'});
  const filterSlot = h('span');
  const bulkBar = h('span', {class: 'row'});
  const grid = h('div', {class: 'card'}, skeletonRows(4, 60));
  const meta = h('span', {class: 'fs-xs muted'});
  clear(host, h('div', {class: 'toolbar'}, h('div', {class: 'search-input'}, icon('search'), search), filterSlot, h('span', {class: 'grow'}), meta, bulkBar,
    btn('Làm mới', {kind: 'ghost', icon: 'refresh', onClick: load})), grid);
  search.addEventListener('input', debounce(() => { query = fold(search.value); render(); }, 150, d));

  function render() {
    if (error) { clear(grid, errorBox(friendlyError(error), load)); return; }
    if (!products) return;
    const mismatched = products.filter(p => p.profile_id && sizeMismatch(p.title, p.profile_name));
    clear(filterSlot, segmented([['all', `Tất cả (${products.length})`], ['unmapped', `Chưa có hồ sơ (${products.filter(p => !p.profile_id).length})`], ['mismatch', `Lệch quy cách (${mismatched.length})`], ['pinned', 'Đang được ghim']], filter, v => { filter = v; render(); }));
    let items = products.filter(p => (filter === 'all' || (filter === 'unmapped' && !p.profile_id) || (filter === 'mismatch' && mismatched.includes(p)) || (filter === 'pinned' && p.is_pinned))
      && (!query || fold(`${p.bag_index} #${p.bag_index} ${p.name} ${p.title} ${p.profile_name}`).includes(query)));
    meta.textContent = `Quét lúc ${relative(products.syncedAt)}`;
    const ids = items.map(p => p.product_id);
    renderBulk();
    if (!items.length) { clear(grid, empty('Không có sản phẩm phù hợp', query ? 'Thử từ khoá khác.' : 'Mở phiên LIVE để tiện ích quét giỏ hàng.', {iconName: 'box'})); return; }
    clear(grid, h('div', {class: 'list'}, selectAllRow(pl.selectionState(ids, picked), on => { pl.setAllVisible(picked, ids, on); render(); }),
      items.map(p => productRow(p))));
  }

  function productRow(p) {
    const sizeBad = p.profile_id && sizeMismatch(p.title, p.profile_name);
    const full = p.title || p.name || '';
    const name = p.name || pl.shortProductName(full, 60);
    return h('div', {class: 'list-row prod-row'},
      h('input', {type: 'checkbox', 'aria-label': 'Chọn ' + name, checked: picked.has(p.product_id), onChange: e => { e.target.checked ? picked.add(p.product_id) : picked.delete(p.product_id); render(); }}),
      h('button', {class: 'prod-open', type: 'button', title: full, onClick: () => openProduct(p)},
        rowThumb(p.image_url, 'box', 52),
        h('span', {class: 'prod-info'},
          h('span', {class: 'row', style: {gap: '6px', minWidth: '0'}}, bag(p.bag_index), p.is_pinned ? chip('Đang ghim', 'live', {dot: true}) : null, h('strong', {class: 'fs-sm truncate'}, name)),
          h('span', {class: 'fs-xs muted truncate'}, full))),
      h('span', {class: 'prod-side'},
        h('span', {class: 'prod-price'}, p.price || '—'),
        p.stock !== undefined && p.stock !== null ? h('span', {class: 'fs-xs muted'}, `Kho ${number(p.stock)}`) : null,
        p.profile_id ? (sizeBad
          ? h('span', {class: 'fs-xs', style: {color: 'var(--danger-text)', display: 'flex', gap: '5px', alignItems: 'center'}, title: `Listing là ${sizeBad.listing} nhưng hồ sơ ghi ${sizeBad.profile} — AI có thể nói sai quy cách/liều`}, icon('alert', {size: 13}), `Hồ sơ khác quy cách (${sizeBad.profile})`)
          : h('span', {class: 'fs-xs muted', style: {display: 'flex', gap: '5px', alignItems: 'center'}, title: p.profile_name}, icon('checkCircle', {size: 13}), h('span', {class: 'truncate', style: {maxWidth: '180px'}}, p.profile_name)))
          : h('span', {class: 'fs-xs', style: {color: 'var(--warn-text)', display: 'flex', gap: '5px', alignItems: 'center'}}, icon('alert', {size: 13}), 'Chưa có hồ sơ')));
  }

  // Hàng loạt: chỉ sản phẩm CHƯA có hồ sơ mới tạo được hồ sơ nháp.
  function renderBulk() {
    const chosen = [...picked].filter(id => products.some(p => p.product_id === id));
    const noProfile = chosen.filter(id => !products.find(p => p.product_id === id).profile_id);
    clear(bulkBar, picked.size ? [
      h('span', {class: 'fs-xs muted'}, `Đã chọn ${picked.size}`),
      btn(`Tạo hồ sơ nháp (${noProfile.length})`, {kind: 'primary', size: 'sm', icon: 'plus', disabled: !noProfile.length,
        title: noProfile.length ? 'Tạo một hồ sơ nháp cho mỗi sản phẩm chưa có hồ sơ' : 'Các sản phẩm đã chọn đều có hồ sơ', onClick: () => bulkDraft(noProfile)}),
    ] : null);
  }

  async function bulkDraft(ids) {
    const {title, text} = pl.bulkDraftText(ids.length);
    if (!await confirmDialog({title, text, confirm: 'Tạo hồ sơ nháp'})) return;
    try {
      const out = await request('/api/profiles/bulk-draft', {method: 'POST', body: {product_ids: ids}});
      toast(`${pl.bulkResultText('Đã tạo', out.created.length, 0)}${out.skipped.length ? ` Bỏ qua ${out.skipped.length}.` : ''} Điền nội dung rồi Duyệt để AI dùng.`, 'ok');
      picked.clear(); invalidateCatalog(); load();
    } catch (e) { toast(friendlyError(e), 'danger'); }
  }

  function openProduct(p) {
    const ws = workspace[p.product_id] || {};
    const fileInput = h('input', {type: 'file', accept: 'image/png,image/jpeg,image/webp', hidden: true});
    const imgBox = h('div', {style: {background: '#fff', borderRadius: '14px', border: '1px solid var(--border)', aspectRatio: '1/1', display: 'grid', placeItems: 'center', overflow: 'hidden'}},
      p.image_url ? h('img', {src: p.image_url, alt: p.name, style: {width: '100%', height: '100%', objectFit: 'contain'}}) : productAvatar(p, 160));
    fileInput.addEventListener('change', () => {
      const file = fileInput.files[0];
      if (!file) return;
      if (file.size > 10 * 1024 * 1024) { toast('Ảnh phải nhỏ hơn 10 MB.', 'warn'); return; }
      const reader = new FileReader();
      reader.onload = async () => {
        try { await request(`/api/workspace/products/${encodeURIComponent(p.product_id)}/image`, {method: 'POST', body: {data_url: reader.result}}); toast('Đã thay ảnh sản phẩm.', 'ok'); drawer.close(); load(); }
        catch (e) { toast(friendlyError(e), 'danger'); }
      };
      reader.readAsDataURL(file);
    });
    const drawer = openDrawer({
      title: p.name, sub: `Giỏ #${p.bag_index} · mã ${p.product_id}`, width: 600,
      body: h('div', {class: 'card-body col gap-4'},
        h('div', {class: 'two-col'}, imgBox, h('div', {class: 'col gap-3'},
          h('div', {class: 'row'}, bag(p.bag_index, {size: 'xl'}), p.is_pinned ? chip('Đang được ghim trên LIVE', 'live', {dot: true}) : null),
          h('div', {class: 'fs-xl fw-7', style: {color: 'var(--warn-text)'}}, p.price || 'Chưa có giá'),
          p.original_price ? h('div', {class: 'fs-sm muted', style: {textDecoration: 'line-through'}}, p.original_price) : null,
          p.promotion ? notice('info', 'Khuyến mãi', p.promotion) : null,
          kv([['Tồn kho', p.stock !== undefined && p.stock !== null ? number(p.stock) : '—'], ['Quy cách', p.package || '—']]),
          h('div', {class: 'row wrap'}, btn('Thay ảnh…', {size: 'sm', icon: 'image', onClick: () => fileInput.click()}),
            ws.image_url && ws.image_url.startsWith('/api/product/image/') ? btn('Về ảnh gốc TikTok', {size: 'sm', kind: 'ghost', onClick: async () => {
              const ok = await confirmDialog({title: 'Xoá ảnh đã thay?', text: 'Sản phẩm quay về ảnh gốc của gian hàng.', confirm: 'Xoá ảnh', tone: 'danger'});
              if (!ok) return;
              try { await request(`/api/workspace/products/${encodeURIComponent(p.product_id)}/image`, {method: 'DELETE'}); toast('Đã quay về ảnh gốc.', 'ok'); drawer.close(); load(); } catch (e) { toast(friendlyError(e), 'danger'); }
            }}) : null, fileInput))),
        h('div', {class: 'col'}, h('div', {class: 'section-title'}, 'Tên trên TikTok Shop'), h('p', {class: 'fs-sm'}, p.title)),
        h('div', {class: 'col'}, h('div', {class: 'section-title'}, 'Hồ sơ kỹ thuật AI dùng để tư vấn'),
          p.profile_id && sizeMismatch(p.title, p.profile_name) ? notice('danger', 'Hồ sơ khác quy cách với sản phẩm', `Sản phẩm là ${sizeMismatch(p.title, p.profile_name).listing} nhưng hồ sơ ghi ${sizeMismatch(p.title, p.profile_name).profile}. AI dùng hồ sơ này có thể nói sai dung tích hoặc liều. Nên gắn đúng hồ sơ quy cách này.`) : null,
          p.profile_id ? h('div', {class: 'row between', style: {padding: '10px 12px', border: '1px solid var(--border)', borderRadius: '12px'}},
            h('div', {}, h('strong', {}, p.profile_name), h('div', {class: 'fs-xs muted'}, p.profile_id)),
            h('div', {class: 'row'}, btn('Xem hồ sơ', {size: 'sm', onClick: () => { drawer.close(); ctx.navigate('catalog', 'profiles', p.profile_id); }}), btn('Đổi', {size: 'sm', kind: 'ghost', onClick: () => { drawer.close(); ctx.navigate('catalog', 'mapping', p.product_id); }})))
            : notice('warn', 'Chưa gắn hồ sơ', 'AI sẽ không tư vấn công dụng/liều lượng cho sản phẩm này cho tới khi được gắn hồ sơ.',
              h('div', {class: 'row', style: {gap: '6px'}},
                btn('Tạo hồ sơ cho sản phẩm này', {size: 'sm', kind: 'primary', icon: 'plus', onClick: () => { drawer.close(); createProfile(ctx, p); }}),
                btn('Gắn hồ sơ có sẵn', {size: 'sm', kind: 'ghost', onClick: () => { drawer.close(); ctx.navigate('catalog', 'mapping', p.product_id); }}))))),
    });
  }

  async function load() {
    error = null;
    try {
      const [cat, ws] = await Promise.all([loadCatalog({force: true}), request('/api/workspace/products?limit=100')]);
      products = cat.products || [];
      products.syncedAt = cat.synced_at;
      workspace = {};
      (ws.items || []).forEach(row => { workspace[row.product_id] = row; });
      products.forEach(p => { if (workspace[p.product_id]) p.in_pin_schedule = workspace[p.product_id].in_pin_schedule; });
    } catch (e) { error = e; }
    render();
  }
  load();
}

// ======================================================================
// HỒ SƠ KỸ THUẬT
// ======================================================================
/** Ngăn kéo "Hồ sơ đã xoá": khôi phục từng hồ sơ hoặc tất cả cùng lúc. Dùng cho cả tab Hồ sơ và tab Gắn hồ sơ. */
export async function openDeletedProfiles(onChanged) {
  let items;
  try { items = await pd.loadDeletedProfiles(request); } catch (e) { toast(friendlyError(e), 'danger'); return null; }
  const chosen = new Set();
  const body = h('div', {class: 'card-body'});
  const foot = h('div', {class: 'row', style: {width: '100%'}});
  const drawer = openDrawer({title: 'Hồ sơ đã xoá', sub: 'Giữ 30 ngày trong Thùng rác, khôi phục bất cứ lúc nào trước hạn.', width: 560, body, footer: foot});
  // Khôi phục một hồ sơ, cả nhóm đã chọn hoặc tất cả; báo số lượng thành công/lỗi rồi làm mới danh sách.
  const restore = async ids => {
    const out = await pd.restoreProfiles(request, ids);
    if (out.failed.length) toast(`${out.failed.length} hồ sơ chưa khôi phục được: ${friendlyError(out.failed[0].error)}`, 'danger');
    if (out.done.length) {
      items = items.filter(t => !out.done.includes(t.record_id));
      out.done.forEach(id => chosen.delete(id));
      toast(out.done.length === 1 ? 'Đã khôi phục hồ sơ.' : `Đã khôi phục ${out.done.length} hồ sơ.`, 'ok');
      if (onChanged) onChanged();
    }
    render();
  };
  async function restoreChosen() {
    const ids = [...chosen];
    const {title, text} = pl.bulkRestoreText(ids.length);
    if (!await confirmDialog({title, text, confirm: 'Khôi phục'})) return;
    await restore(ids);
  }
  function render() {
    const ids = items.map(t => t.record_id);
    clear(body, items.length ? h('div', {class: 'list'}, selectAllRow(pl.selectionState(ids, chosen), on => { pl.setAllVisible(chosen, ids, on); render(); }),
      items.map(t => h('div', {class: 'list-row'},
        h('input', {type: 'checkbox', 'aria-label': 'Chọn ' + (t.label || t.record_id), checked: chosen.has(t.record_id), onChange: e => { e.target.checked ? chosen.add(t.record_id) : chosen.delete(t.record_id); render(); }}),
        h('div', {class: 'grow', style: {minWidth: '0'}}, h('div', {class: 'title truncate', title: t.label || t.record_id}, pl.shortProductName(t.label || t.record_id, 60)),
          h('div', {class: 'sub'}, `xoá ${relative(t.deleted_at)} · giữ tới ${dateTime(t.expires_at)}`)),
        btn('Khôi phục', {size: 'sm', icon: 'undo', onClick: e => { e.currentTarget.disabled = true; restore([t.record_id]); }}))))
      : empty('Không có hồ sơ nào đã xoá', '', {iconName: 'trash'}));
    clear(foot, chosen.size ? btn(`Khôi phục ${chosen.size} đã chọn`, {size: 'sm', icon: 'undo', onClick: restoreChosen}) : null, h('span', {class: 'grow'}),
      items.length > 1 ? btn(`Khôi phục tất cả (${items.length})`, {kind: 'primary', icon: 'undo', onClick: e => { e.currentTarget.disabled = true; restore(ids); }}) : null,
      btn('Đóng', {kind: 'ghost', onClick: () => drawer.close()}));
  }
  render();
  return drawer;
}

function mountProfiles(ctx, host, d) {
  let profiles = null, error = null, query = '';
  const picked = new Set();
  const bulkBar = h('span', {});
  const search = h('input', {class: 'input', type: 'search', placeholder: 'Tìm hồ sơ theo tên, công dụng, cây trồng…'});
  const list = h('div', {class: 'card'}, skeletonRows(6, 50));
  const defaults = defaultProfilesPanel(ctx, {onDone: () => load()});
  clear(host, h('div', {class: 'toolbar'}, h('div', {class: 'search-input'}, icon('search'), search), h('span', {class: 'grow'}),
    h('span', {class: 'fs-xs muted'}, 'Sửa, tạo và xoá ở đây chỉ áp dụng trên máy này.'),
    bulkBar,
    btn('Hồ sơ đã xoá', {kind: 'ghost', icon: 'trash', onClick: () => openDeleted()}),
    btn('Bổ sung hàng loạt', {kind: 'ghost', icon: 'search', attrs: {title: 'Nhờ Google tra thông tin còn thiếu cho các hồ sơ đã chọn (không chọn = tất cả hồ sơ còn thiếu). Anh/chị duyệt trước khi lưu.'},
      onClick: () => openBulkEnrich([...picked], {names: Object.fromEntries((profiles || []).map(p => [p.id, p.title])), onApplied: () => { invalidateCatalog(); load(); }})}),
    btn('Thêm sản phẩm bằng AI', {icon: 'sparkles', onClick: () => openProductIngest(ctx, {onDone: id => { invalidateCatalog(); ctx.navigate('catalog', 'profiles', id); }})}),
    btn('Tạo hồ sơ mới', {kind: 'primary', icon: 'plus', onClick: () => createProfile(ctx, null)})), defaults, list);
  search.addEventListener('input', debounce(() => { query = fold(search.value); render(); }, 150, d));

  function render() {
    if (error) { clear(list, h('div', {class: 'card-body'}, errorBox(friendlyError(error), load))); return; }
    if (!profiles) return;
    const items = profiles.filter(p => !query || fold(`${p.title} ${listOf(p.cong_dung).join(' ')} ${listOf(p.doi_tuong_cay_trong).join(' ')} ${p.phan_khuc || ''}`).includes(query));
    if (!items.length) {
      clear(list, profiles.length ? empty('Không có hồ sơ phù hợp', 'Thử từ khoá khác.', {iconName: 'fileText'})
        : empty('Chưa có hồ sơ kỹ thuật nào', 'Tạo hồ sơ cho từng sản phẩm để AI tư vấn công dụng, cách dùng, liều lượng. Nhanh nhất: vào tab “Sản phẩm đang bán”, bấm một sản phẩm → “Tạo hồ sơ cho sản phẩm này”.',
          {iconName: 'fileText', action: btn('Tạo hồ sơ mới', {kind: 'primary', icon: 'plus', onClick: () => createProfile(ctx, null)})}));
      return;
    }
    const ids = items.map(p => p.id);
    clear(list, h('div', {class: 'list'}, selectAllRow(pl.selectionState(ids, picked), on => { pl.setAllVisible(picked, ids, on); renderBulk(); render(); }),
      items.map(p => h('div', {class: 'list-row'},
        h('input', {type: 'checkbox', 'aria-label': 'Chọn ' + p.title, checked: picked.has(p.id), onChange: e => { e.target.checked ? picked.add(p.id) : picked.delete(p.id); renderBulk(); render(); }}),
        h('button', {class: 'list-row prod-open', type: 'button', title: p.title, onClick: () => openProfile(p.id)},
          rowThumb(p.image_url || (p.featured_image_url ? profileThumbUrl(p.id) : ''), 'fileText', 52),
          h('div', {class: 'grow', style: {minWidth: '0'}}, h('div', {class: 'title truncate'}, pl.shortProductName(p.title, 60)),
            h('div', {class: 'sub clamp-2'}, listOf(p.cong_dung).slice(0, 2).join(' · ') || p.mo_ta_ngan || '')),
          h('div', {class: 'col', style: {alignItems: 'flex-end', gap: '4px'}},
            p.mapped_skus ? chip(`${p.mapped_skus} SP đang dùng`, 'ok') : chip('Chưa gắn SP nào', ''),
            p.local_override ? chip('Đã sửa trên máy', 'info') : null)),
        btn('', {size: 'sm', kind: 'ghost', icon: 'trash', attrs: {title: 'Xoá hồ sơ này (vào Thùng rác 30 ngày)', 'aria-label': 'Xoá ' + p.title}, onClick: () => removeProfiles([p.id])})))));
  }

  function renderBulk() {
    clear(bulkBar, picked.size ? btn(`Xoá ${picked.size} hồ sơ đã chọn`, {kind: 'danger', size: 'sm', icon: 'trash', onClick: () => removeProfiles([...picked])}) : null);
  }

  async function removeProfiles(ids, drawer) {
    const chosen = (profiles || []).filter(p => ids.includes(p.id));
    if (!chosen.length) return;
    const {title, text} = pd.confirmTextFor(chosen, {total: (profiles || []).length});
    const ok = await confirmDialog({title, text, confirm: 'Xoá', tone: 'danger'});
    if (!ok) return;
    const {done, failed} = await pd.deleteProfiles(request, ids);
    done.forEach(id => picked.delete(id));
    if (drawer) drawer.close();
    if (done.length) {
      invalidateCatalog();
      toast(done.length === 1 ? 'Đã chuyển hồ sơ vào Thùng rác.' : `Đã chuyển ${done.length} hồ sơ vào Thùng rác.`, 'ok', {action: 'Hoàn tác', onAction: async () => { await pd.restoreProfiles(request, done); invalidateCatalog(); load(); }});
    }
    if (failed.length) toast(`${failed.length} hồ sơ chưa xoá được: ${friendlyError(failed[0].error)}`, 'danger');
    load();
  }

  function openDeleted() { return openDeletedProfiles(() => { invalidateCatalog(); load(); }); }

  async function openProfile(id) {
    let detail;
    try { detail = await request(`/api/workspace/profiles/${encodeURIComponent(id)}`); } catch (e) { toast(friendlyError(e), 'danger'); return; }
    const data = detail.data || {};
    const controls = {};
    const form = h('div', {class: 'form-grid'}, PROFILE_FIELDS.map(([key, label, type]) => {
      const value = data[key];
      const control = type === 'list' ? textarea({rows: Math.min(8, Math.max(3, listOf(value).length + 1))}) : type === 'textarea' ? textarea({rows: 3}) : input();
      control.value = type === 'list' ? listOf(value).join('\n') : String(value || '');
      controls[key] = [control, type, control.value];
      return field(label, control);
    }));
    const liters = input({type: 'number', min: '0.1', step: '0.5', value: '16', style: {width: '110px'}});
    const doseOut = h('div', {class: 'col gap-3'});
    const doseBtn = btn('Tính thử', {size: 'sm', onClick: async () => {
      try {
        const out = await request(`/api/workspace/profiles/${encodeURIComponent(id)}/dose-test`, {method: 'POST', body: {liters: Number(liters.value)}});
        clear(doseOut, (out.results || []).length ? out.results.map(r => h('div', {style: {padding: '8px 10px', borderRadius: '10px', background: 'var(--surface-2)'}}, h('strong', {}, r.amount), h('div', {class: 'fs-xs muted'}, r.source))) : notice('warn', 'Chưa đọc được tỷ lệ pha từ hướng dẫn', 'AI sẽ không tự tính liều cho sản phẩm này. Viết hướng dẫn dạng “pha 20ml cho bình 16 lít”.'));
      } catch (e) { toast(friendlyError(e), 'danger'); }
    }});
    const composition = listOf(data.thanh_phan_dang_ky);
    const history = detail.history || [];
    const drawer = openDrawer({
      title: data.ten_san_pham || id, sub: `${id} · ${data.mapped_skus || 0} sản phẩm đang dùng hồ sơ này`, width: 720,
      body: h('div', {class: 'card-body col gap-4'},
        data.image_url || data.featured_image_url ? h('img', {src: data.image_url || profileThumbUrl(id), alt: '', style: {maxHeight: '180px', objectFit: 'contain', background: '#fff', borderRadius: '12px', border: '1px solid var(--border)'}}) : null,
        composition.length ? h('div', {class: 'col'}, h('div', {class: 'section-title'}, 'Thành phần đăng ký (chỉ đọc)'), h('ul', {class: 'list-lines'}, composition.map(line => h('li', {}, line)))) : null,
        h('section', {class: 'card flat'}, h('header', {class: 'card-head'}, h('div', {class: 'grow'}, h('h3', {}, 'Thử AI tính liều'), h('div', {class: 'sub'}, 'Nhập dung tích bình để xem AI sẽ trả lời liều bao nhiêu theo nhãn.'))),
          h('div', {class: 'card-body col gap-3'}, h('div', {class: 'row'}, liters, h('span', {class: 'fs-sm'}, 'lít nước'), doseBtn), doseOut)),
        doseRulesPanel(id),
        form,
        history.length ? h('div', {class: 'col'}, h('div', {class: 'section-title'}, 'Lịch sử thay đổi'), h('div', {class: 'col', style: {gap: '4px'}}, history.slice(0, 8).map(row => h('div', {class: 'fs-xs'}, `${dateTime(row.changed_at)} · ${row.reason || row.action}`)))) : null),
      footer: [
        history.length ? btn('Khôi phục bản trước', {kind: 'ghost', icon: 'undo', onClick: async () => {
          const ok = await confirmDialog({title: 'Khôi phục bản trước của hồ sơ?', text: 'Lần sửa gần nhất sẽ được hoàn tác.', confirm: 'Khôi phục'});
          if (!ok) return;
          try { await request(`/api/workspace/profiles/${encodeURIComponent(id)}/rollback`, {method: 'POST', body: {}}); toast('Đã khôi phục bản trước.', 'ok'); drawer.close(); load(); } catch (e) { toast(friendlyError(e), 'danger'); }
        }}) : null,
        btn('Xoá hồ sơ', {kind: 'ghost', icon: 'trash', onClick: () => removeProfiles([id], drawer)}),
        h('span', {class: 'grow'}),
        btn('Bổ sung bằng Google', {icon: 'search', attrs: {title: 'Tra Google để điền các mục còn trống (thành phần, cách dùng, cây trồng, lưu ý). Anh/chị xem nguồn và duyệt trước khi lưu.'},
          onClick: () => openProfileEnrich(id, {name: data.ten_san_pham || id, onApplied: () => { drawer.close(); invalidateCatalog(); load(); }})}),
        btn('Đóng', {kind: 'ghost', onClick: () => drawer.close()}),
        btn('Lưu hồ sơ', {kind: 'primary', icon: 'save', onClick: async () => {
          const changes = {};
          Object.entries(controls).forEach(([key, [control, type, initial]]) => {
            if (control.value === initial) return;
            changes[key] = type === 'list' ? control.value.split('\n').map(l => l.trim()).filter(Boolean) : control.value.trim();
          });
          if (!Object.keys(changes).length) { toast('Chưa có gì thay đổi.', 'info'); return; }
          try {
            await request(`/api/workspace/profiles/${encodeURIComponent(id)}`, {method: 'PATCH', body: {expected_revision: detail.revision, changes, reason: 'Chủ shop sửa hồ sơ'}});
            toast('Đã lưu hồ sơ. AI dùng thông tin mới ngay.', 'ok', {action: 'Hoàn tác', onAction: async () => { await request(`/api/workspace/profiles/${encodeURIComponent(id)}/rollback`, {method: 'POST', body: {}}); load(); }});
            drawer.close(); load();
          } catch (e) { toast(friendlyError(e), 'danger'); }
        }}),
      ],
    });
  }

  async function load() {
    error = null;
    try {
      const first = await request('/api/workspace/profiles?limit=100');
      profiles = first.items || [];
    } catch (e) { error = e; }
    renderBulk();
    render();
    if (ctx.route.id && profiles) openProfile(ctx.route.id);
  }
  load();
}

// ======================================================================
// ÁNH XẠ SẢN PHẨM -> HỒ SƠ
// ======================================================================
// Ảnh hồ sơ luôn qua cache máy (/api/profile/thumb), không nhúng thẳng link web của nhà sản xuất.
function profileThumbUrl(id) { return '/api/profile/thumb/' + encodeURIComponent(id); }

/** Ô ảnh vuông 56px: có ảnh thì bấm để phóng to; thiếu ảnh thì hiện icon thay thế. */
function thumb(url, label, {fallbackIcon, dashed = false} = {}) {
  const box = h('span', {class: 'map-thumb' + (dashed ? ' sugg' : ''), title: label || ''});
  const placeholder = () => { box.classList.add('none'); box.removeAttribute('role'); box.removeAttribute('tabindex'); return icon(fallbackIcon, {size: 22}); };
  if (!url) { box.append(placeholder()); return box; }
  const img = h('img', {src: url, alt: label || '', loading: 'lazy', decoding: 'async', draggable: 'false'});
  img.addEventListener('error', () => { img.replaceWith(placeholder()); }, {once: true});
  box.append(img);
  const open = () => {
    if (!box.querySelector('img')) return;
    openModal({title: label || '', body: h('img', {src: url, alt: label || '', style: {display: 'block', maxWidth: '100%', maxHeight: '70vh', margin: '0 auto', borderRadius: '12px', objectFit: 'contain', background: '#fff'}})});
  };
  box.setAttribute('role', 'button');
  box.setAttribute('tabindex', '0');
  box.setAttribute('aria-label', 'Phóng to ảnh ' + (label || ''));
  box.addEventListener('click', open);
  box.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  return box;
}

function mountMapping(ctx, host, d) {
  let profilePickers = [];
  const disposePickers = () => { profilePickers.forEach(p => p.dispose()); profilePickers = []; };
  d.add(disposePickers);
  let state = null, catalog = {}, error = null, query = '', filter = 'unmapped';
  let auto = new Map(), autoCounts = null, autoBusy = false;
  const pending = new Map(), skipped = new Set(), picked = new Set(), draftPicked = new Set();
  const search = h('input', {class: 'input', type: 'search', placeholder: 'Tìm sản phẩm…'});
  const filterSlot = h('span');
  const autoBar = h('div');
  const trashSlot = h('div');
  let trashTotal = 0;
  const list = h('div', {class: 'card'}, skeletonRows(6, 60));
  const pickHead = h('div');
  const bar = h('div');
  clear(host, h('div', {class: 'toolbar'}, h('div', {class: 'search-input'}, icon('search'), search), filterSlot),
    notice('info', 'Lưu ánh xạ = cho AI được dùng hồ sơ này', 'Sản phẩm nào đã gắn hồ sơ thì AI mới tư vấn thành phần, công dụng, liều dùng. Hồ sơ rỗng không thể gắn.'),
    trashSlot, autoBar, pickHead, list, bar);
  search.addEventListener('input', debounce(() => { query = fold(search.value); render(); }, 150, d));

  const profileName = id => ((state && state.profiles) || []).find(p => p.id === id)?.name || id;
  const mappedIds = () => new Set(((state && state.products) || []).filter(p => p.profile_id).map(p => p.product_id));

  function renderAutoBar() {
    const counts = autoCounts || mm.countTiers([...auto.values()]);
    const sure = Object.keys(mm.sureAssignments([...auto.values()], {skipped, pending, mapped: mappedIds()}));
    clear(autoBar, h('div', {class: 'toolbar', style: {marginTop: '12px'}},
      btn(autoBusy ? 'Đang gợi ý…' : 'Gợi ý hồ sơ tự động', {kind: 'ghost', icon: 'refresh', disabled: autoBusy, onClick: runAuto}),
      autoCounts ? [chip(`Chắc chắn ${counts.chac_chan}`, 'ok'), chip(`Cần xem ${counts.can_xem}`, 'warn'), chip(`Không có ${counts.khong_co}`)] : null,
      h('span', {class: 'grow'}),
      sure.length ? btn(`Chấp nhận tất cả cặp chắc chắn (${sure.length})`, {kind: 'primary', icon: 'checkCircle', onClick: acceptAllSure}) : null,
      picked.size ? btn(`AI đề xuất hàng loạt (${picked.size})`, {kind: 'primary', icon: 'sparkles', onClick: () => {
        const products = state.products.filter(p => picked.has(p.product_id)).map(p => ({...p, image_url: p.image_url || catalog[p.product_id]?.image_url || ''}));
        if (products.length > 60) { toast('Chọn tối đa 60 sản phẩm mỗi đợt.', 'warn'); return; }
        openComboProfiles(ctx, products, {onDone: () => { invalidateCatalog(); load(); }});
      }}) : null,
      picked.size ? btn(`Tạo hồ sơ nháp (${picked.size})`, {icon: 'plus', onClick: makeDrafts}) : null,
      btn('Sản phẩm đã xóa', {kind: 'ghost', icon: 'trash', onClick: showRemoved})));
  }

  async function removeProduct(p) {
    if (!await confirmDialog({title: 'Xóa sản phẩm khỏi KidTE?',
      text: `${p.title}\nSản phẩm sẽ bị bỏ khỏi danh sách và AI, kể cả khi giỏ hàng đồng bộ lại. Hồ sơ vẫn được giữ; có thể khôi phục ở “Sản phẩm đã xóa”. Thao tác này không xóa trên sàn.`, confirm: 'Xóa khỏi KidTE'})) return;
    try {
      await request('/api/ingest/product/remove', {method: 'POST', body: {product_id: p.product_id}});
      pending.delete(p.product_id); picked.delete(p.product_id); auto.delete(p.product_id);
      autoCounts = null;
      invalidateCatalog(); await load(); toast('Đã xóa khỏi danh sách KidTE. Có thể khôi phục.', 'ok');
    } catch (e) { toast(friendlyError(e), 'danger'); }
  }
  async function showRemoved() {
    const body = h('div', {class: 'card-body col gap-3'});
    const drawer = openDrawer({title: 'Sản phẩm đã xóa khỏi KidTE', body, width: 650});
    async function refresh() {
      try {
        const out = await request('/api/ingest/removed-products');
        clear(body, out.products.length ? out.products.map(p => h('div', {class: 'row'}, h('span', {class: 'grow'}, p.title),
          btn('Khôi phục', {size: 'sm', onClick: async () => {
            try { await request('/api/ingest/product/restore', {method: 'POST', body: {product_id: p.product_id}});
              invalidateCatalog(); await load(); await refresh(); toast('Đã bỏ trạng thái xóa. Sản phẩm xuất hiện nếu còn trong giỏ được đồng bộ.', 'ok');
            } catch (e) { toast(friendlyError(e), 'danger'); }
          }}))) : empty('Chưa xóa sản phẩm nào', '', {iconName: 'trash'}));
      } catch (e) { clear(body, errorBox(friendlyError(e), refresh)); }
    }
    refresh(); return drawer;
  }

  function suggestionBlock(p) {
    const s = auto.get(p.product_id);
    if (!s || skipped.has(p.product_id) || pending.has(p.product_id)) return null;
    const info = mm.tierInfo(s.tier);
    return h('div', {class: 'sugg-auto', style: {marginTop: '6px', display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap'}},
      chip(info.label, info.tone),
      s.size_match === false ? chip('Lệch quy cách', 'danger') : null,
      s.is_combo ? chip('Combo', 'warn') : null,
      h('span', {class: 'fs-xs', title: (s.reasons || []).join('\n')}, `${s.profile_name} · ${Math.round((s.score || 0) * 100)}%`),
      btn('Nhận', {size: 'sm', kind: 'ghost', onClick: () => { pending.set(p.product_id, s.profile_id); render(); }}),
      btn('Bỏ qua', {size: 'sm', kind: 'ghost', onClick: () => { skipped.add(p.product_id); render(); }}),
      h('div', {class: 'fs-xs muted', style: {flexBasis: '100%'}}, (s.reasons || []).join(' · ')));
  }

  function renderDrafts() {
    const rows = mm.draftRows((state && state.profiles) || []);
    if (!rows.length) { clear(list, empty('Chưa có hồ sơ nháp', 'Chọn các sản phẩm chưa có hồ sơ rồi bấm “Tạo hồ sơ nháp”.', {iconName: 'fileText'})); clear(bar); return; }
    const readyIds = rows.filter(r => r.ready).map(r => r.id);
    clear(list, h('div', {}, readyIds.length ? selectAllRow(pl.selectionState(readyIds, draftPicked), on => { pl.setAllVisible(draftPicked, readyIds, on); renderDrafts(); }) : null,
      rows.map(r => h('div', {class: 'map-row', style: {gridTemplateColumns: '28px minmax(0, 1fr) auto'}},
      h('input', {type: 'checkbox', 'aria-label': 'Chọn ' + r.name, disabled: !r.ready, checked: draftPicked.has(r.id), onChange: e => { e.target.checked ? draftPicked.add(r.id) : draftPicked.delete(r.id); renderDrafts(); }}),
      h('div', {style: {minWidth: '0'}}, h('strong', {class: 'fs-sm truncate', title: r.name}, pl.shortProductName(r.name, 60)),
        h('div', {class: 'row', style: {gap: '6px', marginTop: '4px', flexWrap: 'wrap'}}, r.chips.map(c => chip((c.done ? '✓ ' : '○ ') + c.label, c.done ? 'ok' : ''))),
        r.ready ? null : h('div', {class: 'fs-xs', style: {color: 'var(--warn-text)', marginTop: '4px'}}, mm.missingForApproval(r)),
        r.draft_for ? h('div', {class: 'fs-xs muted'}, 'Duyệt xong sẽ gắn vào sản phẩm ' + (((state.products || []).find(x => x.product_id === r.draft_for) || {}).title || r.draft_for).slice(0, 90)) : null),
      h('div', {class: 'row'}, btn('Điền nội dung', {size: 'sm', kind: 'ghost', onClick: () => ctx.navigate('catalog', 'profiles', r.id)}),
        btn('Duyệt', {size: 'sm', kind: 'primary', disabled: !r.ready, title: r.ready ? 'AI được dùng hồ sơ này' : mm.missingForApproval(r), onClick: () => approve(rows, new Set([r.id]))}))))));
    renderDraftBar(rows);
  }
  function renderDraftBar(rows) {
    const readyCount = mm.approvalItems(rows, draftPicked).length;
    clear(bar, h('div', {class: 'sticky-bar'}, icon('info'), h('span', {class: 'grow'}, 'Duyệt = cho AI được dùng hồ sơ này để tư vấn khách. Hồ sơ chưa đủ nội dung không duyệt được.'),
      btn(`Duyệt đã chọn (${readyCount})`, {kind: 'primary', disabled: !readyCount, onClick: () => approveBulk(rows)})));
  }
  async function approveBulk(rows) {
    const n = mm.approvalItems(rows, draftPicked).length;
    if (!n) return;
    const {title, text} = pl.bulkApproveText(n);
    if (!await confirmDialog({title, text, confirm: 'Duyệt'})) return;
    approve(rows, draftPicked);
  }
  async function approve(rows, ids) {
    const items = mm.approvalItems(rows, ids);
    if (!items.length) return;
    try {
      const out = await request('/api/profiles/approve', {method: 'POST', body: {items}});
      toast(mm.approvalSummary(out), 'ok');
      draftPicked.clear(); invalidateCatalog(); load();
    } catch (e) { toast(friendlyError(e), 'danger'); }
  }
  async function makeDrafts() {
    const ids = [...picked];
    const {title, text} = pl.bulkDraftText(ids.length);
    if (!await confirmDialog({title, text, confirm: 'Tạo hồ sơ nháp'})) return;
    try {
      const out = await request('/api/profiles/bulk-draft', {method: 'POST', body: {product_ids: ids}});
      toast(`${pl.bulkResultText('Đã tạo', out.created.length, 0)}${out.skipped.length ? ` Bỏ qua ${out.skipped.length}.` : ''} Điền nội dung rồi Duyệt để AI dùng.`, 'ok');
      picked.clear(); filter = 'drafts'; invalidateCatalog(); load();
    } catch (e) { toast(friendlyError(e), 'danger'); }
  }
  async function runAuto() {
    autoBusy = true; renderAutoBar();
    try {
      const out = await request('/api/mapping/auto-match', {method: 'POST', body: {}});
      auto = mm.suggestionIndex(out.results); autoCounts = out.counts; skipped.clear();
    } catch (e) { toast(friendlyError(e), 'danger'); }
    autoBusy = false; render();
  }
  function acceptAllSure() {
    const picks = mm.sureAssignments([...auto.values()], {skipped, pending, mapped: mappedIds()});
    Object.entries(picks).forEach(([pid, profile]) => pending.set(pid, profile));
    toast(`Đã chọn ${Object.keys(picks).length} cặp chắc chắn. Kiểm tra rồi bấm “Lưu ánh xạ”.`, 'info');
    render();
  }

  function noProfilesNotice() {
    const restore = btn(trashTotal ? `Xem và khôi phục (${trashTotal})` : 'Mở Hồ sơ đã xoá', {size: 'sm', icon: 'undo', onClick: () => openDeletedProfiles(() => { invalidateCatalog(); load(); })});
    return trashTotal
      ? notice('warn', 'Chưa có hồ sơ nào để gắn', `Có ${trashTotal} hồ sơ trong Thùng rác (đã xoá trên máy này). Khôi phục để gắn lại cho sản phẩm.`, restore)
      : notice('info', 'Chưa có hồ sơ nào để gắn', 'Tạo hồ sơ ở tab “Hồ sơ kỹ thuật” hoặc dùng “Thêm sản phẩm bằng AI”.');
  }

  function render() {
    disposePickers();
    if (error) { clear(list, h('div', {class: 'card-body'}, errorBox(friendlyError(error), load))); return; }
    if (!state) return;
    clear(trashSlot, !(state.profiles || []).length ? noProfilesNotice() : null);
    const products = state.products || [];
    const current = p => pending.has(p.product_id) ? pending.get(p.product_id) : p.profile_id;
    const unmapped = products.filter(p => !current(p)).length;
    const draftCount = mm.draftRows(state.profiles || []).length;
    clear(filterSlot, segmented([['unmapped', `Chưa có hồ sơ (${unmapped})`], ['all', `Tất cả (${products.length})`], ['changed', `Đang sửa (${pending.size})`], ['drafts', `Hồ sơ nháp (${draftCount})`]], filter, v => { filter = v; render(); }));
    renderAutoBar();
    if (filter === 'drafts') { clear(pickHead); renderDrafts(); return; }
    const items = products.filter(p => (filter === 'all' || (filter === 'unmapped' && !current(p)) || (filter === 'changed' && pending.has(p.product_id)) || p.product_id === ctx.route.id)
      && (!query || fold(`${p.bag_index} ${p.title} ${profileName(current(p))}`).includes(query)));
    // Chọn tất cả chỉ áp cho sản phẩm CHƯA có hồ sơ đang hiện (để tạo hồ sơ nháp hàng loạt).
    const pickable = items.filter(p => !current(p)).map(p => p.product_id);
    clear(pickHead, pickable.length ? selectAllRow(pl.selectionState(pickable, picked), on => { pl.setAllVisible(picked, pickable, on); renderAutoBar(); render(); }) : null);
    const options = (state.profiles || []).filter(p => p.status !== 'draft' || p.approvable).slice().sort((a, b) => a.name.localeCompare(b.name, 'vi'));
    clear(list, items.length ? h('div', {}, items.map(p => {
      const c = catalog[p.product_id] || p;
      const value = current(p);
      const picker = profilePicker(options, value || '', {label: 'Hồ sơ cho ' + (c.name || p.title), onChange: id => {
        if (id === (p.profile_id || '')) pending.delete(p.product_id); else pending.set(p.product_id, id); render();
      }});
      profilePickers.push(picker);
      const sel = picker.node;
      const pick = !value ? h('input', {type: 'checkbox', 'aria-label': 'Chọn để tạo hồ sơ nháp: ' + (c.name || p.title), checked: picked.has(p.product_id), style: {marginRight: '4px'}, onChange: e => { e.target.checked ? picked.add(p.product_id) : picked.delete(p.product_id); renderAutoBar(); render(); }}) : null;
      // Listing stays at the left; profile images are shown inside the picker options.
      const shownProfile = value || (!value && auto.has(p.product_id) && !skipped.has(p.product_id) ? auto.get(p.product_id).profile_id : '');
      const thumbs = mm.rowThumbs({...p, image_url: p.image_url || c.image_url || '', name: c.name || p.title}, shownProfile, state.profiles);
      return h('div', {class: 'map-row thumbs' + (pending.has(p.product_id) ? ' changed' : ''), id: 'map-' + p.product_id},
        thumb(thumbs.listing.url, thumbs.listing.label, {fallbackIcon: 'box'}),
        h('div', {style: {minWidth: '0'}}, h('div', {class: 'row'}, pick, bag(p.bag_index), h('strong', {class: 'fs-sm truncate'}, c.name || p.title)),
          h('div', {class: 'fs-xs muted clamp-2', title: p.title}, p.title),
          value && sizeMismatch(p.title, profileName(value)) ? h('div', {class: 'fs-xs', style: {color: 'var(--danger-text)', marginTop: '4px', display: 'flex', gap: '5px', alignItems: 'center'}}, icon('alert', {size: 13}), `Listing ${sizeMismatch(p.title, profileName(value)).listing} ≠ hồ sơ ${sizeMismatch(p.title, profileName(value)).profile}`) : null,
          !value ? suggestionBlock(p) : null,
          !value && !auto.has(p.product_id) && (p.suggestions || []).length ? h('div', {class: 'sugg'}, h('span', {class: 'fs-xs muted'}, 'Gợi ý:'), p.suggestions.map(s => h('button', {type: 'button', title: s.reason, onClick: () => { pending.set(p.product_id, s.id); render(); }}, '+ ' + s.name))) : null),
        h('div', {class: 'map-actions col gap-3'}, sel,
          h('div', {class: 'row wrap'}, btn('Xóa sản phẩm', {size: 'sm', kind: 'ghost', icon: 'trash', onClick: () => removeProduct(p)}))));
    })) : empty(filter === 'unmapped' ? 'Mọi sản phẩm đều đã có hồ sơ' : 'Không có sản phẩm phù hợp', '', {iconName: 'checkCircle'}));
    clear(bar, pending.size ? h('div', {class: 'sticky-bar'}, icon('info'), h('span', {class: 'grow'}, `${pending.size} thay đổi chưa lưu · ${mm.SAVE_NOTE}`),
      btn('Bỏ thay đổi', {kind: 'ghost', onClick: () => { pending.clear(); render(); }}),
      btn('Lưu ánh xạ', {kind: 'primary', icon: 'save', onClick: save})) : h('div', {class: 'sticky-bar'}, h('span', {class: 'grow fs-xs muted'}, state.history_count ? `Đã lưu ${state.history_count} lần` : ''),
      state.history_count ? btn('Hoàn tác lần lưu gần nhất', {kind: 'ghost', icon: 'undo', onClick: undo}) : null));
    if (ctx.route.id) { document.getElementById('map-' + ctx.route.id)?.scrollIntoView({block: 'center'}); }
  }

  async function save() {
    const assignments = Object.fromEntries(pending);
    try {
      const out = await request('/api/mapping', {method: 'POST', body: {assignments}});
      toast(`Đã lưu ${out.changed ?? pending.size} ánh xạ. AI được dùng các hồ sơ này ngay.`, 'ok', {action: 'Hoàn tác', onAction: undo});
      pending.clear();
      invalidateCatalog();
      load();
    } catch (e) { toast(friendlyError(e), 'danger'); }
  }
  async function undo() {
    const ok = await confirmDialog({title: 'Hoàn tác lần lưu ánh xạ gần nhất?', confirm: 'Hoàn tác'});
    if (!ok) return;
    try { await request('/api/mapping/undo', {method: 'POST', body: {}}); toast('Đã hoàn tác.', 'ok'); load(); } catch (e) { toast(friendlyError(e), 'danger'); }
  }
  async function load() {
    error = null;
    try {
      const [m, cat] = await Promise.all([request('/api/mapping'), loadCatalog().catch(() => ({products: []}))]);
      state = m;
      trashTotal = 0;
      if (!(m.profiles || []).length) { try { trashTotal = Number((await request('/api/trash?resource=profiles&limit=1')).total) || 0; } catch (_) { trashTotal = 0; } }
      catalog = {};
      (cat.products || []).forEach(p => { catalog[p.product_id] = p; });
    } catch (e) { error = e; }
    render();
    if (state && !auto.size && !autoCounts && !autoBusy && (state.products || []).some(p => !p.profile_id)) runAuto();
  }
  load();
  return {isDirty: () => pending.size > 0};
}
