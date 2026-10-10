// Panel "Quy tắc liều" cho một sản phẩm (D01/D04): duyệt/loại rule, nhập từ nhãn cũ.
import {h, clear} from '../core/dom.js';
import {request, friendlyError} from '../core/api.js';
import {btn, chip, notice, field, input, textarea} from '../ui/kit.js';
import {toast} from '../ui/overlays.js';
import {loadDoseRules, reviewDoseRule, importLabel} from './dose-rules-model.js';

export function doseRulesPanel(sku) {
  const list = h('div', {class: 'col gap-2'});
  const summary = h('div');
  async function refresh() {
    try {
      const model = await loadDoseRules(request, sku);
      clear(summary, notice(model.noApproved ? 'warn' : 'info', 'Chỉ quy tắc đã duyệt được dùng', model.warning));
      clear(list, model.rows.length ? model.rows.map(row => h('div', {class: 'row', style: {padding: '8px 10px', borderRadius: '10px', background: 'var(--surface-2)', gap: '8px', alignItems: 'center'}},
        chip(row.chip.label, row.chip.tone),
        row.needsReview ? chip('cần duyệt', 'warn') : null,
        h('div', {class: 'grow'}, h('strong', {}, row.text), h('div', {class: 'fs-xs muted'}, [row.meta, 'Nguồn: ' + row.source].filter(Boolean).join(' · '))),
        row.canReview ? btn('Duyệt', {size: 'sm', onClick: () => decide(row.id, 'approve')}) : null,
        row.canReview ? btn('Loại', {size: 'sm', kind: 'ghost', onClick: () => decide(row.id, 'reject')}) : null))
        : h('div', {class: 'fs-sm muted'}, 'Chưa có quy tắc liều nào cho sản phẩm này.'));
    } catch (e) { clear(list, notice('danger', 'Không tải được quy tắc liều', friendlyError(e))); }
  }
  async function decide(id, decision) {
    try { await reviewDoseRule(request, id, decision); toast(decision === 'approve' ? 'Đã duyệt quy tắc.' : 'Đã loại quy tắc.', 'ok'); }
    catch (e) { toast(friendlyError(e), 'danger'); }
    refresh();
  }
  const text = textarea({rows: 3, placeholder: 'Dán chữ trên nhãn cũ, ví dụ: Pha 10ml cho bình 16 lít; hoặc 20ml cho bình 16 lít'});
  const doc = input({placeholder: 'Tên tài liệu/nhãn (nguồn)'});
  const importBtn = btn('Nhập từ nhãn cũ', {size: 'sm', onClick: async () => {
    try {
      const r = await importLabel(request, {sku, text: text.value, sourceDoc: doc.value});
      toast(r.message, r.imported ? 'ok' : 'warn');
      text.value = '';
    } catch (e) { toast(friendlyError(e), 'danger'); }
    refresh();
  }});
  const root = h('section', {class: 'card flat'},
    h('header', {class: 'card-head'}, h('div', {class: 'grow'}, h('h3', {}, 'Quy tắc liều'), h('div', {class: 'sub'}, 'Duyệt từng tỷ lệ pha trước khi AI được dùng.'))),
    h('div', {class: 'card-body col gap-3'}, summary, list,
      h('div', {class: 'section-title'}, 'Nhập từ nhãn cũ'), field('Nội dung nhãn', text), field('Nguồn', doc), h('div', {class: 'row'}, importBtn)));
  refresh();
  return root;
}
