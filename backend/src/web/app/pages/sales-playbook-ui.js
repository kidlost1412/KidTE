// Panel "Sổ tay tình huống bán hàng": chủ shop sửa cách AI xử lý câu khó (bán chịu, hàng thật/giả, chê đắt...).
import {h, clear} from '../core/dom.js';
import {request, friendlyError} from '../core/api.js';
import {btn, chip, notice, field, input, textarea, card, skeletonRows, errorBox} from '../ui/kit.js';
import {toast, confirmDialog} from '../ui/overlays.js';
import {loadPlaybook, savePlaybook, blankRow, ensureIds, errorText, validateRows, LIMITS} from './sales-playbook-model.js';
import {createDirtyTracker} from '../core/dirty-guard.js';

export function salesPlaybookCard() {
  const body = h('div', {class: 'card-body col gap-3'}, skeletonRows(3, 40));
  let rows = [];
  // So sánh với bản đã tải/đã lưu; xoá hoặc sửa tình huống mà chưa "Lưu sổ tay" là chưa lưu.
  const tracker = createDirtyTracker(() => rows);
  let marks = new Map();
  // Mục đang mở. Mặc định mọi mục thu gọn; chỉ mục vừa thêm được mở sẵn.
  const opened = new Set();

  function render() {
    marks = new Map();
    clear(body,
      notice('info', 'AI dùng sổ tay này cho những câu khó',
        'Khi khách hỏi bán chịu, hàng thật/giả, chê đắt, ship lâu, hỏi có phải AI… AI xoay khéo và vui về hướng mua, chỉ bằng dữ kiện thật. Lưu xong dùng ngay cho câu tiếp theo.'),
      rows.length ? rows.map((row, index) => entryEditor(row, index)) : h('div', {class: 'fs-sm muted'}, 'Chưa có tình huống nào.'),
      h('div', {class: 'row wrap'},
        btn('Thêm tình huống', {size: 'sm', kind: 'ghost', disabled: rows.length >= LIMITS.entries, onClick: () => { const row = blankRow(); opened.add(row); rows.push(row); render(); }}),
        chip(`${rows.length}/${LIMITS.entries}`, '')));
  }

  function entryEditor(row, index) {
    const bind = (field_, el) => { marks.set(`${index}:${field_}`, el); el.addEventListener('input', () => { row[field_] = el.value; }); return el; };
    const title = bind('tinhHuong', input({value: row.tinhHuong, placeholder: 'vd Khách hỏi bán chịu / cho nợ'}));
    const detail = h('div', {class: 'card-body form-grid', hidden: !opened.has(row)},
        field('Tình huống', title),
        field('Dấu hiệu (mỗi dòng một cụm)', bind('dauHieu', textarea({rows: 3, value: row.dauHieu, placeholder: 'bán chịu\ncho nợ'})),
          {hint: 'Khách gõ cụm nào thì AI được nhắc tình huống này. Không phân biệt hoa/thường.'}),
        field('Hướng xử lý (khéo, vui, trung thực)', bind('huongXuLy', textarea({rows: 3, value: row.huongXuLy}))),
        field('Dữ kiện được dùng (mỗi dòng một ý)', bind('duKien', textarea({rows: 2, value: row.duKien, placeholder: 'thong_tin_shop.giao_hang'}))),
        field('Điều cấm (mỗi dòng một ý)', bind('cam', textarea({rows: 2, value: row.cam, placeholder: 'không hứa nợ thật'}))),
        h('div', {class: 'row'}, btn('Xoá tình huống này', {size: 'sm', kind: 'ghost', onClick: async () => {
          const ok = await confirmDialog({title: 'Xoá tình huống này khỏi sổ tay?', text: 'Chỉ có hiệu lực sau khi bấm Lưu sổ tay.', confirm: 'Xoá'});
          if (!ok) return;
          opened.delete(row);
          rows.splice(index, 1);
          render();
        }})));
    const placeholderTitle = 'Tình huống chưa đặt tên';
    const headTitle = h('strong', {}, row.tinhHuong || placeholderTitle);
    title.addEventListener('input', () => { headTitle.textContent = row.tinhHuong || placeholderTitle; });
    const toggle = h('button', {type: 'button', class: 'playbook-entry-head', 'aria-expanded': String(opened.has(row)),
      onClick: () => {
        const wasOpen = opened.has(row);
        if (wasOpen) opened.delete(row); else opened.add(row);
        detail.hidden = wasOpen;
        toggle.setAttribute('aria-expanded', String(!wasOpen));
      }},
      headTitle);
    return h('section', {class: 'card flat'}, toggle, detail);
  }

  const save = btn('Lưu sổ tay', {kind: 'primary', icon: 'save', onClick: async () => {
    ensureIds(rows);
    const problems = validateRows(rows);
    if (problems.length) {
      const first = problems[0];
      toast(first.index >= 0 ? `Tình huống ${first.index + 1}: ${first.message}` : first.message, 'danger');
      const target = marks.get(`${first.index}:${first.field}`);
      if (target) target.focus();
      return;
    }
    try { const model = await savePlaybook(request, rows); rows = model.rows; tracker.reset(); render(); toast('Đã lưu — AI dùng ngay cho câu tiếp theo.', 'ok'); }
    catch (e) { toast(e.validation ? e.message : (errorText(e) || friendlyError(e)), 'danger'); }
  }});

  loadPlaybook(request).then(model => { rows = model.rows; tracker.reset(); render(); })
    .catch(e => clear(body, errorBox(friendlyError(e))));
  return {node: card('Sổ tay tình huống bán hàng', body, {sub: 'Cách AI xử lý câu khó: bán chịu, hàng giả, chê đắt, ship lâu…', footer: save}), isDirty: tracker.isDirty};
}
