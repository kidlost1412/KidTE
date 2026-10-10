// Mô hình dữ liệu của Studio video — THUẦN, không DOM (kiểm thử: node --test).
//
// Một kịch bản ghim là dãy "điểm đổi sản phẩm": mỗi đoạn bắt đầu ở `start`
// và kéo dài tới điểm đổi kế tiếp (hoặc hết video). Người dựng nhìn thấy
// ĐOẠN (khối có độ dài), còn file lưu vẫn là dãy mốc {at_ms, product}.
//
// Hai nguồn dùng chung mô hình này:
//  - library: timeline đã phát hành (data/videos/<id>/timeline.json)
//      events[{at_ms, product: <khoá>, alternate_with, alternate_every_ms}]
//      script_contexts[{at_ms, topic, facts}]
//  - review : bản gợi ý của AI chờ chủ shop duyệt (video_uploads/<job>/draft.json)
//      events[{at_ms, product_id, review_status, confidence, evidence, …}]

export const MIN_SEGMENT_MS = 1000;
export const SHORT_SEGMENT_MS = 5000;
export const MIN_ALTERNATE_MS = 10000;
const SEGMENT_KEYS = new Set(['uid', 'start', 'product', 'alt', 'altEvery', 'topic', 'facts', 'extra', 'review', 'ai']);

let counter = 0;
export function newUid() { counter += 1; return 's' + counter.toString(36) + Math.random().toString(36).slice(2, 7); }

export function keyForProductId(productId) { return productId ? 'sku_' + String(productId) : null; }
export function productIdForKey(doc, key) {
  if (!key) return '';
  const entry = doc.products && doc.products[key];
  if (entry && entry.product_id) return String(entry.product_id);
  return key.startsWith('sku_') ? key.slice(4) : '';
}

function clone(value) { return JSON.parse(JSON.stringify(value)); }

// ------------------------------------------------------------------ nạp / ghi

export function fromLibrary(timeline, {draft = null} = {}) {
  const source = draft && Array.isArray(draft.events) ? {...timeline, ...draft, products: {...(timeline.products || {}), ...(draft.products || {})}} : timeline;
  const contexts = Array.isArray(source.script_contexts) ? source.script_contexts : [];
  const ctxByAt = new Map(contexts.map(ctx => [Number(ctx.at_ms), ctx]));
  const segments = (source.events || []).map(event => {
    const at = Math.max(0, Math.round(Number(event.at_ms) || 0));
    const ctx = ctxByAt.get(at) || {};
    const extra = {};
    Object.keys(event).forEach(k => { if (!['at_ms', 'product', 'alternate_with', 'alternate_every_ms', 'topic'].includes(k)) extra[k] = event[k]; });
    return {
      uid: newUid(), start: at, product: event.product || null,
      alt: event.alternate_with || null, altEvery: Number(event.alternate_every_ms) || 0,
      topic: String(ctx.topic ?? event.topic ?? ''), facts: Array.isArray(ctx.facts) ? ctx.facts.slice() : [],
      extra, review: null, ai: null,
    };
  }).sort((a, b) => a.start - b.start);
  return {
    kind: 'library', id: timeline.video_id, title: String(source.title || timeline.title || timeline.video_id || ''),
    durationMs: Math.round(Number(timeline.cycle_ms) || 0), repinEveryMs: Number(timeline.repin_every_ms) || 20000,
    products: clone(timeline.products || {}), segments,
  };
}

/** Thay đổi gửi lên PATCH /api/workspace/videos/<id>. */
export function toLibraryChanges(doc) {
  const segments = normalized(doc).segments;
  return {
    title: doc.title,
    repin_every_ms: doc.repinEveryMs || 20000,
    events: segments.map(seg => {
      const event = {...(seg.extra || {}), at_ms: seg.start, product: seg.product || null};
      if (seg.product && seg.alt && seg.altEvery) { event.alternate_with = seg.alt; event.alternate_every_ms = seg.altEvery; }
      else { delete event.alternate_with; delete event.alternate_every_ms; }
      return event;
    }),
    script_contexts: segments.map(seg => ({at_ms: seg.start, topic: seg.topic || '', facts: seg.facts || []})),
  };
}

/** Bản nháp lưu vào video_drafts (cùng hình dạng timeline để mở lại được). */
export function toLibraryDraft(doc) {
  const changes = toLibraryChanges(doc);
  return {...changes, products: doc.products, saved_by: 'studio'};
}

export function fromReview(job) {
  const draft = job.draft || {};
  const durationMs = Math.round(Number(draft.cycle_ms) || Number(job.duration_s || 0) * 1000);
  const products = {};
  (draft.catalog || job.catalog_snapshot || []).forEach(row => {
    if (!row || !row.product_id) return;
    products[keyForProductId(row.product_id)] = {product_id: String(row.product_id), bag_index: Number(row.bag_index) || 0, short_name: row.short_name || row.title, title: row.title};
  });
  const segments = (draft.events || []).map(event => {
    const extra = {};
    Object.keys(event).forEach(k => { if (!['at_ms', 'product_id', 'alternate_product_id', 'alternate_every_ms', 'review_status', 'topic'].includes(k)) extra[k] = event[k]; });
    return {
      uid: newUid(), start: Math.max(0, Math.round(Number(event.at_ms) || 0)),
      product: keyForProductId(event.product_id), alt: keyForProductId(event.alternate_product_id), altEvery: Number(event.alternate_every_ms) || 0,
      topic: String(event.topic || ''), facts: [], extra,
      review: event.review_status || 'pending',
      ai: {
        confidence: event.confidence || '', evidence: event.evidence || '', end: Number(event.end_ms) || 0,
        contextBefore: event.context_before || '', contextAfter: event.context_after || '',
        matched: event.matched_terms || [], candidates: event.candidates || [],
        suggestedProduct: keyForProductId(event.product_id), suggestedStart: Math.round(Number(event.at_ms) || 0),
      },
    };
  }).sort((a, b) => a.start - b.start);
  return {
    kind: 'review', id: job.job_id, title: String(draft.title || job.title || job.filename || ''),
    durationMs, repinEveryMs: Number(draft.repin_every_ms) || 20000, products, segments,
  };
}

/** Bản duyệt gửi lên POST /api/workspace/video-jobs/<id>/draft. */
export function toReviewDraft(doc, original) {
  const base = clone(original || {});
  base.title = doc.title;
  base.events = doc.segments.slice().sort((a, b) => a.start - b.start).map(seg => ({
    ...(seg.extra || {}),
    at_ms: seg.start,
    product_id: productIdForKey(doc, seg.product),
    alternate_product_id: seg.product && seg.alt ? productIdForKey(doc, seg.alt) : '',
    alternate_every_ms: seg.product && seg.alt ? (seg.altEvery || 60000) : 0,
    review_status: seg.review || 'pending',
    ...(seg.topic ? {topic: seg.topic} : {}),
  }));
  delete base.saved_at;
  return base;
}

// ------------------------------------------------------------------ truy vấn

// withEnds được gọi ở mỗi khung hình khi phát video: nhớ theo mảng segments
// (bất biến — mọi chỉnh sửa tạo mảng mới) để không cấp phát lại liên tục.
const endsMemo = new WeakMap();
export function withEnds(doc) {
  const list = doc.segments;
  const hit = endsMemo.get(list);
  if (hit && hit.duration === doc.durationMs) return hit.value;
  const value = list.map((seg, i) => ({...seg, index: i, end: i + 1 < list.length ? list[i + 1].start : doc.durationMs}));
  endsMemo.set(list, {duration: doc.durationMs, value});
  return value;
}

export function indexAt(doc, ms) {
  const list = doc.segments;
  if (!list.length) return -1;
  let lo = 0, hi = list.length - 1, found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (list[mid].start <= ms) { found = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return found;
}

/** Sản phẩm THỰC SỰ được ghim ở giây `ms` (tính cả luân phiên, bỏ đoạn bị loại). */
export function pinnedAt(doc, ms) {
  let i = indexAt(doc, ms);
  while (i >= 0 && doc.segments[i].review === 'rejected') i -= 1;
  if (i < 0) {
    // Trước mốc đầu tiên: tiện ích ghim sản phẩm của mốc đầu (schedule.js eventAt)
    const first = doc.segments.find(seg => seg.review !== 'rejected');
    return first ? {index: doc.segments.indexOf(first), product: first.product, alternating: false, before: true} : {index: -1, product: null};
  }
  const seg = doc.segments[i];
  if (seg.product && seg.alt && seg.altEvery >= MIN_ALTERNATE_MS) {
    const phase = Math.floor((ms - seg.start) / seg.altEvery) % 2;
    return {index: i, product: phase ? seg.alt : seg.product, alternating: true, phase};
  }
  return {index: i, product: seg.product, alternating: false};
}

export function usedProductKeys(doc) {
  const keys = new Set();
  doc.segments.forEach(seg => { if (seg.product) keys.add(seg.product); if (seg.alt) keys.add(seg.alt); });
  return [...keys];
}

/** Tổng thời lượng lên sóng của từng sản phẩm (để thấy SP nào bị bỏ quên). */
export function airtime(doc) {
  const totals = {};
  withEnds(doc).forEach(seg => {
    if (seg.review === 'rejected' || !seg.product) return;
    const dur = Math.max(0, seg.end - seg.start);
    if (seg.alt && seg.altEvery >= MIN_ALTERNATE_MS) {
      totals[seg.product] = (totals[seg.product] || 0) + dur / 2;
      totals[seg.alt] = (totals[seg.alt] || 0) + dur / 2;
    } else totals[seg.product] = (totals[seg.product] || 0) + dur;
  });
  return totals;
}

// ------------------------------------------------------------------ chỉnh sửa
// Mọi hàm trả doc MỚI (không sửa tại chỗ) để hoàn tác/làm lại đơn giản.

function withSegments(doc, segments) { return {...doc, segments: segments.slice().sort((a, b) => a.start - b.start)}; }
function touched(seg, doc) { return doc.kind === 'review' && seg.review !== 'rejected' ? {...seg, review: 'edited'} : seg; }
function clampStart(doc, ms) { return Math.max(0, Math.min(Math.round(ms), Math.max(0, doc.durationMs - MIN_SEGMENT_MS))); }

/** Chèn điểm đổi sản phẩm tại `ms`. Nếu sát một mốc có sẵn (<0.5s) thì dùng mốc đó. */
export function splitAt(doc, ms, productKey) {
  const at = clampStart(doc, ms);
  const near = doc.segments.findIndex(seg => Math.abs(seg.start - at) < 500);
  if (near >= 0) {
    if (productKey === undefined) return {doc, index: near};
    const segments = doc.segments.slice();
    segments[near] = touched({...segments[near], product: productKey, alt: productKey ? segments[near].alt : null}, doc);
    if (segments[near].alt === productKey) segments[near].alt = null;
    return {doc: withSegments(doc, segments), index: near};
  }
  const i = indexAt(doc, at);
  const base = i >= 0 ? doc.segments[i] : null;
  const next = doc.segments[i + 1];
  if (base && at - base.start < MIN_SEGMENT_MS) return {doc, index: i};
  if (next && next.start - at < MIN_SEGMENT_MS) return {doc, index: i + 1};
  const seg = {
    uid: newUid(), start: at,
    product: productKey === undefined ? (base ? base.product : null) : productKey,
    alt: productKey === undefined && base ? base.alt : null,
    altEvery: productKey === undefined && base ? base.altEvery : 0,
    topic: '', facts: [], extra: {},
    review: doc.kind === 'review' ? 'edited' : null, ai: null,
  };
  const segments = doc.segments.concat(seg);
  const out = withSegments(doc, segments);
  return {doc: out, index: out.segments.findIndex(s => s.uid === seg.uid)};
}

/** Ghim nhanh: từ giây `ms` chuyển sang sản phẩm `key` (bỏ qua nếu đang đúng SP đó). */
export function pinFrom(doc, ms, key) {
  const i = indexAt(doc, ms);
  if (i >= 0 && doc.segments[i].product === key && !doc.segments[i].alt) return {doc, index: i, unchanged: true};
  return splitAt(doc, ms, key);
}

export function update(doc, index, patch) {
  if (index < 0 || index >= doc.segments.length) return doc;
  const segments = doc.segments.slice();
  let seg = {...segments[index]};
  Object.entries(patch).forEach(([k, v]) => { if (SEGMENT_KEYS.has(k)) seg[k] = v; });
  if (!seg.product) { seg.alt = null; seg.altEvery = 0; }
  if (seg.alt && seg.alt === seg.product) seg.alt = null;
  if (seg.alt && !seg.altEvery) seg.altEvery = 60000;
  if (!('review' in patch)) seg = touched(seg, doc);
  segments[index] = seg;
  return withSegments(doc, segments);
}

/** Kéo mốc bắt đầu của đoạn `index` (không vượt đoạn kề, không về trước 0). */
export function moveStart(doc, index, ms) {
  if (index <= 0 && doc.kind === 'library') return doc;
  const segments = doc.segments;
  if (index < 0 || index >= segments.length) return doc;
  const prev = segments[index - 1];
  const next = segments[index + 1];
  const lo = prev ? prev.start + MIN_SEGMENT_MS : 0;
  const hi = next ? next.start - MIN_SEGMENT_MS : doc.durationMs - MIN_SEGMENT_MS;
  const at = Math.round(Math.max(lo, Math.min(hi, ms)));
  if (at === segments[index].start) return doc;
  const copy = segments.slice();
  copy[index] = touched({...copy[index], start: at}, doc);
  return {...doc, segments: copy};
}

export function moveBounds(index, doc) {
  const prev = doc.segments[index - 1];
  const next = doc.segments[index + 1];
  return {lo: prev ? prev.start + MIN_SEGMENT_MS : 0, hi: next ? next.start - MIN_SEGMENT_MS : doc.durationMs - MIN_SEGMENT_MS};
}

/** Xoá điểm bắt đầu của đoạn: đoạn trước kéo dài phủ lên. Đoạn đầu: đoạn sau lùi về 0. */
export function removeAt(doc, index) {
  const segments = doc.segments.slice();
  if (index < 0 || index >= segments.length) return {doc, index: -1};
  if (doc.kind === 'review') {
    segments.splice(index, 1);
    return {doc: withSegments(doc, segments), index: Math.min(index, segments.length - 1)};
  }
  if (segments.length === 1) {
    segments[0] = {...segments[0], product: null, alt: null, altEvery: 0};
    return {doc: withSegments(doc, segments), index: 0};
  }
  if (index === 0) {
    segments.splice(0, 1);
    segments[0] = {...segments[0], start: 0};
    return {doc: withSegments(doc, segments), index: 0};
  }
  segments.splice(index, 1);
  return {doc: withSegments(doc, segments), index: index - 1};
}

export function mergeSameNeighbors(doc) {
  const out = [];
  doc.segments.forEach(seg => {
    const last = out[out.length - 1];
    if (last && last.product === seg.product && last.alt === seg.alt && seg.review !== 'rejected' && last.review !== 'rejected') {
      if (!last.topic && seg.topic) last.topic = seg.topic;
      return;
    }
    out.push({...seg});
  });
  return {...doc, segments: out};
}

/** Mốc đầu về 0 (bắt buộc với timeline phát hành; hành vi ghim không đổi). */
export function normalized(doc) {
  if (!doc.segments.length) return doc;
  const segments = doc.segments.slice().sort((a, b) => a.start - b.start);
  if (segments[0].start !== 0) segments[0] = {...segments[0], start: 0};
  return {...doc, segments};
}

export function setReview(doc, indices, status) {
  const set = new Set(indices);
  return {...doc, segments: doc.segments.map((seg, i) => set.has(i) ? {...seg, review: status} : seg)};
}

export function reviewCounts(doc) {
  const counts = {pending: 0, approved: 0, edited: 0, rejected: 0};
  doc.segments.forEach(seg => { counts[seg.review || 'pending'] = (counts[seg.review || 'pending'] || 0) + 1; });
  return counts;
}

// ------------------------------------------------------------------ kiểm tra

/**
 * Trả danh sách vấn đề cho bảng "Kiểm tra trước khi áp dụng".
 * catalogById: {product_id: {bag_index, name, ...}} theo danh mục đang bán.
 */
export function checks(doc, catalogById = {}) {
  const issues = [];
  const list = withEnds(doc);
  const add = (level, code, message, index = -1, fix = null) => issues.push({level, code, message, index, fix});
  if (!list.length) add('error', 'empty', 'Chưa có đoạn nào. Bấm “Ghim sản phẩm” hoặc phím số giỏ khi đang xem video.');
  if (doc.kind === 'library' && list.length && list[0].start !== 0) add('warn', 'first_not_zero', 'Đoạn đầu chưa bắt đầu từ 00:00 — khi áp dụng sẽ tự đưa về 00:00.', 0, 'normalize');
  const seenStarts = new Set();
  let hasPinned = false;
  list.forEach((seg, i) => {
    if (seg.review === 'rejected') return;
    if (seenStarts.has(seg.start)) add('error', 'duplicate_start', 'Hai đoạn bắt đầu cùng một thời điểm.', i);
    seenStarts.add(seg.start);
    if (doc.durationMs && seg.start >= doc.durationMs) add('error', 'out_of_range', 'Đoạn nằm ngoài thời lượng video.', i);
    if (seg.product) hasPinned = true;
    const dur = seg.end - seg.start;
    if (dur > 0 && dur < SHORT_SEGMENT_MS) add('warn', 'short', `Đoạn chỉ dài ${Math.round(dur / 100) / 10} giây — có thể là bấm nhầm.`, i);
    if (seg.alt) {
      if (!seg.product) add('error', 'alt_without_primary', 'Luân phiên cần có sản phẩm chính.', i);
      if (seg.altEvery < MIN_ALTERNATE_MS) add('error', 'alt_short', 'Chu kỳ luân phiên phải từ 10 giây.', i);
    }
    [seg.product, seg.alt].filter(Boolean).forEach(key => {
      const productId = productIdForKey(doc, key);
      const current = productId ? catalogById[productId] : null;
      const entry = doc.products[key] || {};
      if (!current) add('warn', 'not_in_catalog', `“${entry.short_name || entry.title || key}” không có trong giỏ hàng hiện tại — tiện ích sẽ không ghim được.`, i);
      else if (doc.kind === 'library' && entry.bag_index && Number(entry.bag_index) !== Number(current.bag_index)) {
        add('warn', 'bag_changed', `Kịch bản ghi giỏ #${entry.bag_index} nhưng hôm nay “${current.name}” nằm ở giỏ #${current.bag_index}.`, i, 'sync_bags');
      }
    });
    const next = list[i + 1];
    if (next && next.review !== 'rejected' && next.product === seg.product && next.alt === seg.alt) add('info', 'same_neighbor', 'Hai đoạn liền nhau cùng sản phẩm — có thể gộp.', i + 1, 'merge');
  });
  if (list.length && !hasPinned) add('error', 'no_product', 'Chưa có đoạn nào ghim sản phẩm.');
  if (doc.kind === 'review') {
    const pending = list.filter(seg => (seg.review || 'pending') === 'pending').length;
    if (pending) add('error', 'pending', `Còn ${pending} mốc AI gợi ý chưa duyệt.`, list.findIndex(seg => (seg.review || 'pending') === 'pending'));
  }
  // Gom cảnh báo trùng mã + trùng thông điệp (vd bag_changed lặp ở nhiều đoạn)
  const unique = [];
  const seen = new Map();
  issues.forEach(issue => {
    const k = issue.code + '|' + issue.message;
    if (seen.has(k)) { seen.get(k).count += 1; return; }
    const copy = {...issue, count: 1};
    seen.set(k, copy);
    unique.push(copy);
  });
  const order = {error: 0, warn: 1, info: 2};
  return unique.sort((a, b) => order[a.level] - order[b.level]);
}

export function blocking(issues) { return issues.some(issue => issue.level === 'error' && issue.code !== 'pending'); }

// ------------------------------------------------------------------ lịch sử

export function createHistory(initial, limit = 200) {
  let past = [], future = [], present = initial;
  return {
    get: () => present,
    push(next, label = '') {
      if (next === present) return present;
      past.push({doc: present, label});
      if (past.length > limit) past.shift();
      present = next; future = [];
      return present;
    },
    /** Ghi đè bước hiện tại (dùng khi kéo liên tục, chỉ lưu 1 bước). */
    replace(next) { present = next; return present; },
    undo() { if (!past.length) return null; const prev = past.pop(); future.push({doc: present, label: prev.label}); present = prev.doc; return {doc: present, label: prev.label}; },
    redo() { if (!future.length) return null; const next = future.pop(); past.push({doc: present, label: next.label}); present = next.doc; return {doc: present, label: next.label}; },
    canUndo: () => past.length > 0,
    canRedo: () => future.length > 0,
    reset(doc) { past = []; future = []; present = doc; },
  };
}

export function sameContent(a, b) {
  if (!a || !b) return false;
  const pick = doc => JSON.stringify({t: doc.title, s: doc.segments.map(seg => [seg.start, seg.product, seg.alt, seg.altEvery, seg.topic, seg.review])});
  return pick(a) === pick(b);
}

// ------------------------------------------------------------------ đối chiếu lời thoại

/**
 * Tìm chỗ host NÓI VỀ một sản phẩm nhưng lịch đang ghim sản phẩm khác.
 *
 * mentions: [{ms, endMs, key}] — mỗi câu thoại có nhắc tới sản phẩm `key`.
 * Gom các câu liên tiếp cùng sản phẩm (cách nhau ≤ gapMs) thành một cụm.
 * Cụm đủ mạnh (≥ minMentions câu) mà phần lớn thời gian của cụm lại đang
 * ghim sản phẩm khác => đề xuất ghim `key` từ đầu cụm. Bỏ qua nếu đã có mốc
 * ghim đúng sản phẩm đó gần đầu cụm (±toleranceMs).
 */
export function findPinMismatches(doc, mentions, {minMentions = 2, gapMs = 40000, toleranceMs = 25000, limit = 40} = {}) {
  const sorted = mentions.filter(m => m && m.key).slice().sort((a, b) => a.ms - b.ms);
  const clusters = [];
  sorted.forEach(m => {
    const last = clusters[clusters.length - 1];
    if (last && last.key === m.key && m.ms - last.endMs <= gapMs) {
      last.count += 1; last.endMs = Math.max(last.endMs, m.endMs || m.ms); last.lines.push(m);
    } else clusters.push({key: m.key, startMs: m.ms, endMs: m.endMs || m.ms, count: 1, lines: [m]});
  });
  const out = [];
  clusters.forEach(c => {
    if (c.count < minMentions) return;
    const span = Math.max(1000, c.endMs - c.startMs);
    // Bao nhiêu phần của cụm đang ghim đúng sản phẩm được nhắc?
    let right = 0;
    const step = Math.max(500, span / 20);
    let samples = 0;
    for (let t = c.startMs; t <= c.endMs; t += step) {
      samples += 1;
      const pin = pinnedAt(doc, t);
      if (pin.product === c.key || (pin.index >= 0 && doc.segments[pin.index].alt === c.key)) right += 1;
    }
    if (samples && right / samples >= 0.5) return;
    const covered = doc.segments.some(seg => seg.review !== 'rejected' && (seg.product === c.key || seg.alt === c.key) && Math.abs(seg.start - c.startMs) <= toleranceMs);
    if (covered) return;
    const pin = pinnedAt(doc, c.startMs);
    out.push({key: c.key, startMs: c.startMs, endMs: c.endMs, count: c.count, currentKey: pin.product || null, text: c.lines[0].text || ''});
  });
  return out.sort((a, b) => b.count - a.count || a.startMs - b.startMs).slice(0, limit).sort((a, b) => a.startMs - b.startMs);
}

/** Mốc chưa duyệt kế tiếp (vòng lại từ đầu). -1 nếu hết. */
export function nextPending(doc, fromIndex = -1) {
  const list = doc.segments;
  for (let step = 1; step <= list.length; step += 1) {
    const i = (fromIndex + step + list.length) % list.length;
    if ((list[i].review || 'pending') === 'pending') return i;
  }
  return -1;
}

/**
 * Ghim `key` trong khoảng [startMs, endMs) rồi TRẢ LẠI sản phẩm đang ghim trước
 * đó — dùng cho đề xuất từ lời thoại: host chen vào nói về sản phẩm khác một
 * lúc rồi quay về, không được đè mất phần còn lại của đoạn.
 * Nếu mốc kế tiếp đã nằm gần endMs (≤ tailMs) thì không cần mốc trả lại.
 */
export function pinRange(doc, startMs, endMs, key, {tailMs = 30000} = {}) {
  const before = pinnedAt(doc, startMs);
  const restoreKey = before.index >= 0 ? doc.segments[before.index].product : null;
  const first = pinFrom(doc, startMs, key);
  if (first.unchanged) return first;
  let out = first.doc;
  const next = out.segments[first.index + 1];
  const restoreAt = Math.round(endMs);
  if (restoreKey !== key && restoreAt > startMs + MIN_SEGMENT_MS && (!next || next.start - restoreAt > tailMs) && restoreAt < out.durationMs - MIN_SEGMENT_MS) {
    out = splitAt(out, restoreAt, restoreKey).doc;
  }
  return {doc: out, index: out.segments.findIndex(s => s.start === out.segments[first.index].start)};
}
