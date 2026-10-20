import {request} from '../../core/api.js';
import {productIdForKey} from './model.js';

let catalogCache = null;
let catalogAt = 0;

/**
 * Danh mục đang bán (ảnh, giá, giỏ, tên gọn). Cache 30s cho cả khu Video.
 * cart = khoá giỏ (tài khoản LIVE) cho Studio; rỗng = giỏ đang hoạt động (có cache).
 */
export async function loadCatalog({force = false, cart = ''} = {}) {
  if (cart) {
    const data = await request('/api/video/catalog?cart=' + encodeURIComponent(cart));
    return withIndex(data);
  }
  if (!force && catalogCache && Date.now() - catalogAt < 30000) return catalogCache;
  catalogCache = withIndex(await request('/api/video/catalog'));
  catalogAt = Date.now();
  return catalogCache;
}

function withIndex(data) {
  const byId = {};
  (data.products || []).forEach(p => { byId[String(p.product_id)] = p; });
  return {...data, byId};
}

export function invalidateCatalog() { catalogCache = null; }

/**
 * Thông tin hiển thị của một khoá sản phẩm trong timeline.
 * Ưu tiên danh mục HIỆN TẠI (ảnh, giá, giỏ đúng hôm nay); nếu sản phẩm đã
 * rời giỏ thì dùng thông tin lưu trong timeline và đánh dấu missing.
 */
export function resolveProduct(doc, key, catalog) {
  if (!key) return null;
  const entry = (doc.products && doc.products[key]) || {};
  const productId = productIdForKey(doc, key);
  const current = productId && catalog ? catalog.byId[productId] : null;
  if (current) {
    return {
      key, product_id: productId, bag_index: current.bag_index, name: current.name, title: current.title,
      price: current.price, image_url: current.image_url, profile_name: current.profile_name, stock: current.stock,
      timelineBag: Number(entry.bag_index) || 0, missing: false,
      bagChanged: doc.kind === 'library' && Number(entry.bag_index) > 0 && Number(entry.bag_index) !== Number(current.bag_index),
    };
  }
  const title = entry.title || entry.short_name || key;
  return {
    key, product_id: productId, bag_index: Number(entry.bag_index) || 0, name: shortName(entry.short_name || title), title,
    price: '', image_url: productId ? `/api/product/thumb/${encodeURIComponent(productId)}` : '', profile_name: '',
    timelineBag: Number(entry.bag_index) || 0, missing: true, bagChanged: false,
  };
}

export function shortName(text) {
  const head = String(text || '').split(/\s[-–|]\s|\|/)[0].trim();
  return head.length > 46 ? head.slice(0, 44).replace(/\s+\S*$/, '') + '…' : head;
}

/** Theo dõi poster/filmstrip/waveform; gọi onUpdate mỗi lần trạng thái đổi. */
export function watchAssets(params, onUpdate) {
  let stopped = false;
  let timer = 0;
  let waveformLoaded = false;
  const query = params.jobId ? `job_id=${encodeURIComponent(params.jobId)}` : `video_id=${encodeURIComponent(params.videoId)}`;
  async function tick() {
    if (stopped) return;
    try {
      const state = await request('/api/video/assets?' + query);
      if (stopped) return;
      if (state.waveform_url && !waveformLoaded) {
        try {
          state.waveform = await request(state.waveform_url);
          waveformLoaded = true;
        } catch (_) { /* thử lại lượt sau */ }
      }
      onUpdate(state);
      if (state.state !== 'ready' && state.state !== 'error' && state.state !== 'missing_media') timer = setTimeout(tick, 2500);
    } catch (error) {
      if (!stopped) timer = setTimeout(tick, 5000);
    }
  }
  tick();
  return {
    stop() { stopped = true; clearTimeout(timer); },
    async retry() {
      await request('/api/video/assets/retry', {method: 'POST', body: params.jobId ? {job_id: params.jobId} : {video_id: params.videoId}});
      clearTimeout(timer); waveformLoaded = false; tick();
    },
  };
}

