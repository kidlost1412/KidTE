// Mảnh giao diện dùng chung cho "Trí nhớ AI" — Hộp thư và trang Trí nhớ AI.
import {h, clear} from '../core/dom.js';
import {icon} from '../core/icons.js';
import {request, friendlyError} from '../core/api.js';
import {relative} from '../core/format.js';
import {btn, chip, bag, notice} from '../ui/kit.js';
import {toast} from '../ui/overlays.js';

export const KIND = {
  alias: {label: 'Tên gọi khác', tone: 'info', icon: 'tag', hint: 'Khách nói tên này → AI hiểu ngay là sản phẩm đó (chọn đúng hồ sơ, giá, liều).'},
  fact: {label: 'Dữ kiện', tone: 'ok', icon: 'fileText', hint: 'Điều AI cần nhớ về sản phẩm hoặc về shop.'},
  rule: {label: 'Cách xử lý', tone: 'warn', icon: 'shield', hint: 'Bài học chung: gặp tình huống này thì làm thế nào. Luôn nằm trong đầu AI.'},
};
export const STATUS = {
  suggested: {label: 'Chờ bạn duyệt', tone: 'warn'},
  active: {label: 'Đang dùng', tone: 'ok'},
  retired: {label: 'Đã tắt', tone: ''},
  rejected: {label: 'Đã bỏ', tone: ''},
};

/** Theo dõi một lượt học đến khi xong (≈ 5–15 giây với DeepSeek). */
export function watchRun(runId, onUpdate, {interval = 1500, timeout = 180000} = {}) {
  let stopped = false;
  let timer = 0;
  const started = Date.now();
  async function tick() {
    if (stopped) return;
    try {
      const out = await request(`/api/learning/runs/${encodeURIComponent(runId)}`);
      if (stopped) return;
      onUpdate(out.run);
      if (['queued', 'running'].includes(out.run.status) && Date.now() - started < timeout) timer = setTimeout(tick, interval);
    } catch (e) {
      if (!stopped) timer = setTimeout(tick, interval * 2);
    }
  }
  tick();
  return () => { stopped = true; clearTimeout(timer); };
}

export async function setItemStatus(ids, status) {
  await request('/api/learning/status', {method: 'POST', body: {item_ids: ids, status}});
}

/** Một mục kiến thức: loại · nội dung · từ khoá · sản phẩm · lượt dùng. */
export function itemRow(item, {selectable = false, selected = false, onToggle, actions} = {}) {
  const kind = KIND[item.kind] || KIND.fact;
  const st = STATUS[item.status] || STATUS.active;
  const check = selectable ? h('input', {type: 'checkbox', checked: selected, 'aria-label': 'Chọn mục này', onChange: e => onToggle && onToggle(e.target.checked)}) : null;
  return h('div', {class: 'k-item' + (item.status === 'suggested' ? ' suggested' : '') + (item.status === 'retired' || item.status === 'rejected' ? ' off' : '')},
    check,
    h('span', {class: 'k-kind ' + item.kind, title: kind.hint}, icon(kind.icon, {size: 14})),
    h('div', {class: 'k-main'},
      h('div', {class: 'k-text'}, item.statement),
      h('div', {class: 'k-meta'},
        chip(kind.label, kind.tone),
        item.product_id ? h('span', {class: 'row', style: {gap: '4px'}}, item.bag_index ? bag(item.bag_index) : null, h('span', {class: 'truncate', style: {maxWidth: '220px'}}, item.product_name || 'Sản phẩm không còn trong giỏ')) : h('span', {class: 'muted'}, 'Chung của shop'),
        (item.terms || []).length ? h('span', {class: 'k-terms'}, icon('search', {size: 12}), item.terms.map(t => h('code', {}, t))) : null,
        (item.ambiguous_terms || []).length ? chip(`“${item.ambiguous_terms.join('”, “')}” trùng tên sản phẩm khác — AI không dùng để tự chọn sản phẩm`, 'warn', {iconName: 'alert'}) : null,
        item.status !== 'active' ? chip(st.label, st.tone) : null,
        item.use_count ? h('span', {class: 'muted'}, `đã dùng ${item.use_count} lần`) : null,
        h('span', {class: 'muted'}, item.source === 'manual' ? 'bạn tự dạy' : item.source === 'relearn' ? 'từ lượt học lại' : 'rút từ bản sửa', ' · ', relative(item.updated_at)))),
    actions ? h('div', {class: 'k-actions'}, actions) : null);
}

/**
 * Thẻ "AI đã hiểu gì từ bản sửa này": chẩn đoán + các mục đề xuất có ô chọn,
 * nút Ghi nhớ / Bỏ qua. Dùng ngay sau khi bấm "Dạy AI".
 */
export function lessonCard(run, {onChanged, onTry} = {}) {
  const box = h('section', {class: 'lesson'});
  const render = current => {
    if (!current || ['queued', 'running'].includes(current.status)) {
      clear(box, h('div', {class: 'lesson-head'}, h('span', {class: 'spinner'}), h('div', {}, h('strong', {}, 'AI đang hiểu bản sửa của bạn…'), h('div', {class: 'fs-xs muted'}, 'So câu AI đã nói với câu bạn sửa để rút ra kiến thức dùng lại được. Mất vài giây — bạn cứ làm tiếp.'))));
      return;
    }
    if (current.status === 'error') {
      clear(box, notice('warn', 'Chưa rút được kiến thức', 'Câu trả lời mẫu vẫn đã được lưu. ' + (current.error || '')));
      return;
    }
    const out = current.output || {};
    const items = current.items || [];
    const pending = items.filter(i => i.status === 'suggested');
    const chosen = new Set(pending.map(i => i.item_id));
    const decide = async (status, ids) => {
      if (!ids.length) return;
      try {
        await setItemStatus(ids, status);
        toast(status === 'active' ? `AI đã ghi nhớ ${ids.length} điều. Từ giờ áp dụng cho mọi câu hỏi liên quan.` : 'Đã bỏ các đề xuất.', status === 'active' ? 'ok' : 'info');
        items.forEach(i => { if (ids.includes(i.item_id)) i.status = status; });
        render(current);
        if (onChanged) onChanged();
      } catch (e) { toast(friendlyError(e), 'danger'); }
    };
    clear(box,
      h('div', {class: 'lesson-head'}, h('span', {class: 'lesson-icon'}, icon('sparkles')),
        h('div', {class: 'grow'}, h('strong', {}, items.length ? 'AI đã hiểu từ bản sửa này' : 'AI đã lưu câu trả lời mẫu'),
          out.diagnosis ? h('div', {class: 'fs-sm soft', style: {marginTop: '2px'}}, out.diagnosis) : h('div', {class: 'fs-xs muted'}, out.used_model === false ? 'Chưa kết nối DeepSeek nên chỉ rút được từ ghi chú.' : 'Không có kiến thức mới cần nhớ — câu này chỉ dùng làm mẫu giọng.')),
        onTry ? btn('Thử lại câu này', {size: 'sm', icon: 'play', onClick: onTry}) : null),
      items.length ? h('div', {class: 'lesson-items'}, items.map(item => itemRow(item, {
        selectable: item.status === 'suggested', selected: chosen.has(item.item_id),
        onToggle: on => { if (on) chosen.add(item.item_id); else chosen.delete(item.item_id); },
      }))) : null,
      (out.style_notes || []).length ? h('div', {class: 'lesson-style'}, h('span', {class: 'section-title'}, 'Giọng của bạn trong câu này'), h('ul', {}, out.style_notes.map(n => h('li', {}, n))),
        h('div', {class: 'fs-xs muted'}, 'Được gom vào “Phong cách” khi AI học lại toàn bộ.')) : null,
      pending.length ? h('div', {class: 'lesson-foot'},
        btn(`Ghi nhớ ${pending.length > 1 ? 'các mục đã chọn' : 'điều này'}`, {kind: 'primary', icon: 'check', onClick: () => decide('active', [...chosen])}),
        btn('Bỏ qua', {kind: 'ghost', onClick: () => decide('rejected', pending.map(i => i.item_id))}),
        h('span', {class: 'fs-xs muted grow', style: {textAlign: 'right'}}, 'Chỉ mục bạn duyệt mới được AI dùng.')) : null);
  };
  render(run);
  return {root: box, update: render};
}

/** Kết quả "Thử AI": câu trả lời + AI đã dựa vào đâu. */
export function tryResult(out, productsById = {}) {
  const e = out.explain || {};
  const product = e.product && productsById[e.product.product_id];
  const m = out.metrics || {};
  const reasons = {
    missing_data: 'AI sẽ im lặng: thiếu dữ kiện bắt buộc (' + (e.missing || []).join(', ') + ').',
    model_skipped: 'AI chọn không trả lời: ' + (out.missing_fields || []).join(', '),
    model_not_ready: 'Chưa kết nối DeepSeek — vào Cài đặt để nhập khoá.',
    no_live_catalog: 'Chưa có danh mục LIVE — mở tiện ích trên phiên LIVE một lần để đồng bộ giỏ hàng.',
  };
  return h('div', {class: 'try-result'},
    out.reply ? h('div', {class: 'bubble reply ai'}, out.reply, h('div', {class: 'b-meta'}, 'AI trả lời thử · không gửi lên LIVE', m.total_ms ? h('span', {}, `${(m.total_ms / 1000).toFixed(1)} giây`) : null))
      : notice('warn', 'AI không trả lời câu này', reasons[out.reason] || out.reason || ''),
    h('div', {class: 'try-why'},
      h('div', {class: 'section-title'}, 'AI đã dựa vào'),
      h('div', {class: 'try-row'}, icon('box', {size: 15}), h('span', {class: 'grow'}, product ? h('span', {class: 'row', style: {gap: '6px'}}, bag(product.bag_index), product.name) : (e.product && e.product.title) || 'Không xác định được sản phẩm'),
        e.learned_product ? chip('nhờ tên gọi đã học', 'info', {iconName: 'sparkles'}) : null,
        e.product && e.product.product_id && !e.has_profile ? chip('chưa có hồ sơ kỹ thuật', 'warn') : null),
      (e.learned || []).map(k => h('div', {class: 'try-row'}, icon((KIND[k.loai] || KIND.fact).icon, {size: 15}), h('span', {class: 'grow'}, k.noi_dung), h('span', {class: 'fs-xs muted nowrap'}, k.ly_do_dua_vao))),
      (e.examples || []).map(x => h('div', {class: 'try-row'}, icon('message', {size: 15}), h('span', {class: 'grow'}, h('span', {class: 'muted'}, 'Câu mẫu: '), x.answer))),
      e.persona ? h('div', {class: 'try-row'}, icon('user', {size: 15}), h('span', {class: 'grow'}, 'Giọng: ', e.persona)) : null,
      !(e.learned || []).length && !(e.examples || []).length ? h('div', {class: 'fs-xs muted'}, 'Không có kiến thức đã học nào khớp câu này.') : null,
      m.prompt_tokens ? h('div', {class: 'fs-xs muted', style: {marginTop: '6px'}}, `${m.model || ''} · ${Math.round((m.prompt_cache_hit_tokens || 0) / m.prompt_tokens * 100)}% ngữ cảnh lấy từ bộ nhớ đệm (rẻ & nhanh)`) : null));
}

export async function tryAI(comment, extra = {}) {
  return request('/api/learning/try', {method: 'POST', body: {comment, ...extra}});
}
