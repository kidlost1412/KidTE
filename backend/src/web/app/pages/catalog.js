import {h, clear, disposer, debounce} from '../core/dom.js';
import {icon} from '../core/icons.js';
import {request, friendlyError} from '../core/api.js';
import {relative, number, fold, dateTime, sizeMismatch} from '../core/format.js';
import {btn, chip, bag, empty, errorBox, skeletonRows, pageHead, tabs, kv, productAvatar, field, input, textarea, notice, segmented} from '../ui/kit.js';
import {toast, openDrawer, confirmDialog, promptDialog} from '../ui/overlays.js';
import {loadCatalog, invalidateCatalog} from './video/data.js';
import {doseRulesPanel} from './dose-rules-ui.js';
import * as mm from './mapping-model.js';
import * as pd from './profile-delete-model.js';
import {openProductIngest} from './product-ingest-ui.js';

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
  const search = h('input', {class: 'input', type: 'search', placeholder: 'Tìm theo tên, số giỏ, hồ sơ…'});
  const filterSlot = h('span');
  const grid = h('div', {}, skeletonRows(4, 120));
  const meta = h('span', {class: 'fs-xs muted'});
  clear(host, h('div', {class: 'toolbar'}, h('div', {class: 'search-input'}, icon('search'), search), filterSlot, h('span', {class: 'grow'}), meta,
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
    if (!items.length) { clear(grid, empty('Không có sản phẩm phù hợp', query ? 'Thử từ khoá khác.' : 'Mở phiên LIVE để tiện ích quét giỏ hàng.', {iconName: 'box'})); return; }
    clear(grid, h('div', {class: 'prod-grid'}, items.map(p => h('article', {class: 'card prod-card', tabindex: '0', onClick: () => openProduct(p), onKeydown: e => { if (e.key === 'Enter') openProduct(p); }},
      h('div', {class: 'prod-img'}, p.image_url ? h('img', {src: p.image_url, alt: '', loading: 'lazy'}) : productAvatar(p, 120, {showBag: false}), bag(p.bag_index, {size: 'lg'}),
        h('span', {class: 'prod-flag'}, p.is_pinned ? chip('Đang ghim', 'live', {dot: true}) : null)),
      h('div', {class: 'prod-body'}, h('div', {class: 'prod-name clamp-2'}, p.name), h('div', {class: 'prod-title clamp-2', title: p.title}, p.title),
        h('div', {class: 'row between', style: {marginTop: 'auto', paddingTop: '6px'}}, h('span', {class: 'prod-price'}, p.price || '—'), p.stock !== undefined && p.stock !== null ? h('span', {class: 'fs-xs muted'}, `Kho ${number(p.stock)}`) : null)),
      h('div', {class: 'prod-foot'}, p.profile_id ? (sizeMismatch(p.title, p.profile_name)
        ? [h('span', {style: {color: 'var(--danger-text)', display: 'flex', gap: '5px', alignItems: 'center', minWidth: '0'}, title: `Listing là ${sizeMismatch(p.title, p.profile_name).listing} nhưng hồ sơ ghi ${sizeMismatch(p.title, p.profile_name).profile} — AI có thể nói sai quy cách/liều`}, icon('alert', {size: 14}), h('span', {class: 'truncate'}, `Hồ sơ khác quy cách (${sizeMismatch(p.title, p.profile_name).profile})`))]
        : [icon('checkCircle', {size: 14}), h('span', {class: 'truncate', title: p.profile_name}, p.profile_name)]) : [h('span', {style: {color: 'var(--warn-text)', display: 'flex', gap: '5px', alignItems: 'center'}}, icon('alert', {size: 14}), 'Chưa có hồ sơ — AI sẽ không tư vấn')])))));
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
function mountProfiles(ctx, host, d) {
  let profiles = null, error = null, query = '';
  const picked = new Set();
  const bulkBar = h('span', {});
  const search = h('input', {class: 'input', type: 'search', placeholder: 'Tìm hồ sơ theo tên, công dụng, cây trồng…'});
  const list = h('div', {class: 'card'}, skeletonRows(6, 50));
  clear(host, h('div', {class: 'toolbar'}, h('div', {class: 'search-input'}, icon('search'), search), h('span', {class: 'grow'}),
    h('span', {class: 'fs-xs muted'}, 'Sửa, tạo và xoá ở đây chỉ áp dụng trên máy này.'),
    bulkBar,
    btn('Hồ sơ đã xoá', {kind: 'ghost', icon: 'trash', onClick: () => openDeleted()}),
    btn('Thêm sản phẩm bằng AI', {icon: 'sparkles', onClick: () => openProductIngest(ctx, {onDone: id => { invalidateCatalog(); ctx.navigate('catalog', 'profiles', id); }})}),
    btn('Tạo hồ sơ mới', {kind: 'primary', icon: 'plus', onClick: () => createProfile(ctx, null)})), list);
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
    clear(list, h('div', {class: 'list'}, items.map(p => h('div', {class: 'list-row'},
      h('input', {type: 'checkbox', 'aria-label': 'Chọn ' + p.title, checked: picked.has(p.id), onChange: e => { e.target.checked ? picked.add(p.id) : picked.delete(p.id); renderBulk(); }}),
      h('button', {class: 'list-row', type: 'button', style: {flex: '1', border: '0', background: 'none', padding: '0'}, onClick: () => openProfile(p.id)},
        h('span', {class: 'pavatar', style: {'--size': '46px'}}, p.featured_image_url ? h('img', {src: p.featured_image_url, alt: '', loading: 'lazy', onError: e => e.target.remove()}) : icon('fileText')),
        h('div', {class: 'grow'}, h('div', {class: 'title clamp-2'}, p.title), h('div', {class: 'sub clamp-2'}, listOf(p.cong_dung).slice(0, 2).join(' · ') || p.mo_ta_ngan || '')),
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
    const {title, text} = pd.confirmTextFor(chosen);
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

  async function openDeleted() {
    let items;
    try { items = await pd.loadDeletedProfiles(request); } catch (e) { toast(friendlyError(e), 'danger'); return; }
    const drawer = openDrawer({
      title: 'Hồ sơ đã xoá', sub: 'Giữ 30 ngày trong Thùng rác, khôi phục bất cứ lúc nào trước hạn.', width: 560,
      body: h('div', {class: 'card-body'}, items.length ? h('div', {class: 'list'}, items.map(t => h('div', {class: 'list-row'},
        h('div', {class: 'grow'}, h('div', {class: 'title'}, t.label || t.record_id), h('div', {class: 'sub'}, `xoá ${relative(t.deleted_at)} · giữ tới ${dateTime(t.expires_at)}`)),
        btn('Khôi phục', {size: 'sm', icon: 'undo', onClick: async () => {
          const out = await pd.restoreProfiles(request, [t.record_id]);
          if (out.failed.length) { toast(friendlyError(out.failed[0].error), 'danger'); return; }
          invalidateCatalog(); toast('Đã khôi phục hồ sơ.', 'ok'); drawer.close(); load();
        }})))) : empty('Không có hồ sơ nào đã xoá', '', {iconName: 'trash'})),
      footer: [h('span', {class: 'grow'}), btn('Đóng', {kind: 'ghost', onClick: () => drawer.close()})],
    });
  }

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
        data.featured_image_url ? h('img', {src: data.featured_image_url, alt: '', style: {maxHeight: '180px', objectFit: 'contain', background: '#fff', borderRadius: '12px', border: '1px solid var(--border)'}}) : null,
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
function mountMapping(ctx, host, d) {
  let state = null, catalog = {}, error = null, query = '', filter = 'unmapped';
  let auto = new Map(), autoCounts = null, autoBusy = false;
  const pending = new Map(), skipped = new Set(), picked = new Set(), draftPicked = new Set();
  const search = h('input', {class: 'input', type: 'search', placeholder: 'Tìm sản phẩm…'});
  const filterSlot = h('span');
  const autoBar = h('div');
  const list = h('div', {class: 'card'}, skeletonRows(6, 60));
  const bar = h('div');
  clear(host, h('div', {class: 'toolbar'}, h('div', {class: 'search-input'}, icon('search'), search), filterSlot),
    notice('info', 'Lưu ánh xạ = cho AI được dùng hồ sơ này', 'Sản phẩm nào đã gắn hồ sơ thì AI mới tư vấn thành phần, công dụng, liều dùng. Hồ sơ rỗng không thể gắn.'),
    autoBar, list, bar);
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
      picked.size ? btn(`Tạo hồ sơ nháp (${picked.size})`, {icon: 'plus', onClick: makeDrafts}) : null));
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
    clear(list, h('div', {}, rows.map(r => h('div', {class: 'map-row', style: {gridTemplateColumns: '28px minmax(0, 1fr) auto'}},
      h('input', {type: 'checkbox', 'aria-label': 'Chọn ' + r.name, disabled: !r.ready, checked: draftPicked.has(r.id), onChange: e => { e.target.checked ? draftPicked.add(r.id) : draftPicked.delete(r.id); renderDraftBar(rows); }}),
      h('div', {style: {minWidth: '0'}}, h('strong', {class: 'fs-sm truncate'}, r.name),
        h('div', {class: 'row', style: {gap: '6px', marginTop: '4px', flexWrap: 'wrap'}}, r.chips.map(c => chip((c.done ? '✓ ' : '○ ') + c.label, c.done ? 'ok' : ''))),
        r.ready ? null : h('div', {class: 'fs-xs', style: {color: 'var(--warn-text)', marginTop: '4px'}}, mm.missingForApproval(r)),
        r.draft_for ? h('div', {class: 'fs-xs muted'}, 'Duyệt xong sẽ gắn vào sản phẩm ' + (((state.products || []).find(x => x.product_id === r.draft_for) || {}).title || r.draft_for).slice(0, 90)) : null),
      h('div', {class: 'row'}, btn('Điền nội dung', {size: 'sm', kind: 'ghost', onClick: () => ctx.navigate('catalog', 'profiles', r.id)}),
        btn('Duyệt', {size: 'sm', kind: 'primary', disabled: !r.ready, title: r.ready ? 'AI được dùng hồ sơ này' : mm.missingForApproval(r), onClick: () => approve(rows, new Set([r.id]))}))))));
    renderDraftBar(rows);
  }
  function renderDraftBar(rows) {
    clear(bar, h('div', {class: 'sticky-bar'}, icon('info'), h('span', {class: 'grow'}, 'Duyệt = cho AI được dùng hồ sơ này để tư vấn khách. Hồ sơ chưa đủ nội dung không duyệt được.'),
      btn(`Duyệt đã chọn (${draftPicked.size})`, {kind: 'primary', disabled: !draftPicked.size, onClick: () => approve(rows, draftPicked)})));
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
    try {
      const out = await request('/api/profiles/bulk-draft', {method: 'POST', body: {product_ids: [...picked]}});
      toast(`Đã tạo ${out.created.length} hồ sơ nháp${out.skipped.length ? `, bỏ qua ${out.skipped.length}` : ''}. Điền nội dung rồi Duyệt để AI dùng.`, 'ok');
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

  function render() {
    if (error) { clear(list, h('div', {class: 'card-body'}, errorBox(friendlyError(error), load))); return; }
    if (!state) return;
    const products = state.products || [];
    const current = p => pending.has(p.product_id) ? pending.get(p.product_id) : p.profile_id;
    const unmapped = products.filter(p => !current(p)).length;
    const draftCount = mm.draftRows(state.profiles || []).length;
    clear(filterSlot, segmented([['unmapped', `Chưa có hồ sơ (${unmapped})`], ['all', `Tất cả (${products.length})`], ['changed', `Đang sửa (${pending.size})`], ['drafts', `Hồ sơ nháp (${draftCount})`]], filter, v => { filter = v; render(); }));
    renderAutoBar();
    if (filter === 'drafts') { renderDrafts(); return; }
    const items = products.filter(p => (filter === 'all' || (filter === 'unmapped' && !current(p)) || (filter === 'changed' && pending.has(p.product_id)) || p.product_id === ctx.route.id)
      && (!query || fold(`${p.bag_index} ${p.title} ${profileName(current(p))}`).includes(query)));
    const options = [['', '— Chưa gắn hồ sơ —'], ...(state.profiles || []).filter(p => p.status !== 'draft' || p.approvable).slice().sort((a, b) => a.name.localeCompare(b.name, 'vi')).map(p => [p.id, p.name + (p.package ? ` (${p.package})` : '')])];
    clear(list, items.length ? h('div', {}, items.map(p => {
      const c = catalog[p.product_id] || p;
      const value = current(p);
      const sel = h('select', {class: 'select', 'aria-label': 'Hồ sơ cho ' + c.name, onChange: e => { if (e.target.value === (p.profile_id || '')) pending.delete(p.product_id); else pending.set(p.product_id, e.target.value); render(); }},
        options.map(([id, label]) => h('option', {value: id, selected: id === (value || '')}, label)));
      const pick = !value ? h('input', {type: 'checkbox', 'aria-label': 'Chọn để tạo hồ sơ nháp: ' + (c.name || p.title), checked: picked.has(p.product_id), style: {marginRight: '4px'}, onChange: e => { e.target.checked ? picked.add(p.product_id) : picked.delete(p.product_id); renderAutoBar(); }}) : null;
      return h('div', {class: 'map-row' + (pending.has(p.product_id) ? ' changed' : ''), id: 'map-' + p.product_id},
        productAvatar(c, 48),
        h('div', {style: {minWidth: '0'}}, h('div', {class: 'row'}, pick, bag(p.bag_index), h('strong', {class: 'fs-sm truncate'}, c.name || p.title)),
          h('div', {class: 'fs-xs muted clamp-2', title: p.title}, p.title),
          value && sizeMismatch(p.title, profileName(value)) ? h('div', {class: 'fs-xs', style: {color: 'var(--danger-text)', marginTop: '4px', display: 'flex', gap: '5px', alignItems: 'center'}}, icon('alert', {size: 13}), `Listing ${sizeMismatch(p.title, profileName(value)).listing} ≠ hồ sơ ${sizeMismatch(p.title, profileName(value)).profile}`) : null,
          !value ? suggestionBlock(p) : null,
          !value && !auto.has(p.product_id) && (p.suggestions || []).length ? h('div', {class: 'sugg'}, h('span', {class: 'fs-xs muted'}, 'Gợi ý:'), p.suggestions.map(s => h('button', {type: 'button', title: s.reason, onClick: () => { pending.set(p.product_id, s.id); render(); }}, '+ ' + s.name))) : null),
        sel);
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
      catalog = {};
      (cat.products || []).forEach(p => { catalog[p.product_id] = p; });
    } catch (e) { error = e; }
    render();
    if (state && !auto.size && !autoCounts && !autoBusy && (state.products || []).some(p => !p.profile_id)) runAuto();
  }
  load();
  return {isDirty: () => pending.size > 0};
}
