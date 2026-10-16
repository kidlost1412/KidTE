// "Bổ sung bằng Google": Gemini tra web, chủ shop xem cũ → đề xuất + nguồn rồi bấm Duyệt.
import {h, clear} from '../core/dom.js';
import {request, friendlyError} from '../core/api.js';
import {btn, notice} from '../ui/kit.js';
import {toast, openModal} from '../ui/overlays.js';
import * as em from './product-enrich-model.js';

const POLL_MS = 2000;
const MAX_WAIT_MS = 5 * 60 * 1000;

function profileUrl(id, tail = '') { return `/api/profiles/${encodeURIComponent(id)}/enrich${tail}`; }

/** Hộp duyệt đề xuất cho một hồ sơ. ``start``: true thì gửi yêu cầu tra mới trước. */
export async function openProfileEnrich(id, {name = '', start = true, onApplied} = {}) {
  const body = h('div', {class: 'col gap-3'});
  const foot = h('div', {class: 'row', style: {width: '100%'}});
  let stopped = false;
  const modal = openModal({title: 'Bổ sung bằng Google', description: name ? `Hồ sơ: ${name}` : '', body, footer: foot, size: 'lg', onDismiss: () => { stopped = true; }});
  const waiting = text => { clear(body, notice('info', 'Đang tìm trên Google…', text)); clear(foot, h('span', {class: 'grow'}), btn('Đóng', {kind: 'ghost', onClick: () => { stopped = true; modal.close(); }})); };
  waiting('Máy đang nhờ Gemini tra nhãn sản phẩm, trang nhà sản xuất. Thường mất 10–60 giây. Anh/chị có thể đóng hộp này, kết quả vẫn được giữ lại.');
  try {
    if (start) await request(profileUrl(id), {method: 'POST', body: {}});
    const began = Date.now();
    let out;
    for (;;) {
      if (stopped) return;
      out = await request(profileUrl(id));
      const state = out.job ? out.job.state : (out.proposal ? 'done' : 'none');
      if (state === 'error') { showError(out.job.error); return; }
      if (state !== 'running') break;
      if (Date.now() - began > MAX_WAIT_MS) { showError('Tra cứu quá lâu. Đóng hộp này rồi thử lại sau.'); return; }
      await new Promise(r => setTimeout(r, POLL_MS));
    }
    showProposal(out.proposal);
  } catch (e) { showError(friendlyError(e)); }

  function showError(text) {
    clear(body, notice('danger', 'Chưa bổ sung được', text || 'Lỗi không rõ.'));
    clear(foot, h('span', {class: 'grow'}), btn('Đóng', {kind: 'ghost', onClick: () => modal.close()}));
  }

  function showProposal(proposal) {
    const rows = em.proposalRows(proposal);
    const dropped = em.droppedText(proposal);
    if (!rows.length) {
      clear(body, notice('warn', 'Không có gì để bổ sung', (proposal && proposal.message) || 'Google không tìm thấy thông tin có nguồn cho các mục còn thiếu.'));
      clear(foot, h('span', {class: 'grow'}), btn('Đóng', {kind: 'ghost', onClick: () => modal.close()}));
      return;
    }
    const approve = btn('', {kind: 'primary', icon: 'check', onClick: () => apply()});
    const refresh = () => { const n = em.chosenFields(rows).length; approve.textContent = n === rows.length ? 'Duyệt cả hồ sơ' : `Duyệt ${n} mục đã chọn`; approve.disabled = !n; };
    clear(body,
      h('p', {class: 'fs-sm muted'}, 'Chỉ điền các mục đang TRỐNG, mục đã có giữ nguyên. Đọc kỹ rồi tích những mục đúng; bấm vào nguồn để xem trang gốc. Không có giá hay khuyến mãi.'),
      rows.map(row => h('section', {class: 'card flat', style: {padding: '10px 12px'}},
        h('label', {class: 'row', style: {gap: '8px', alignItems: 'center'}},
          h('input', {type: 'checkbox', checked: row.checked, onChange: e => { row.checked = e.target.checked; refresh(); }}),
          h('strong', {}, row.label)),
        h('div', {class: 'fs-xs muted', style: {marginTop: '6px'}}, 'Hiện tại: ' + row.old),
        h('div', {style: {whiteSpace: 'pre-wrap', marginTop: '4px', padding: '8px', borderRadius: '8px', background: 'var(--surface-2)'}}, row.value),
        h('div', {class: 'fs-xs', style: {marginTop: '6px', display: 'flex', flexWrap: 'wrap', gap: '8px'}}, 'Nguồn:',
          row.sources.map(s => h('a', {href: s.url, target: '_blank', rel: 'noopener noreferrer'}, s.title))))),
      dropped ? h('div', {class: 'fs-xs muted'}, dropped) : null);
    clear(foot, h('span', {class: 'grow'}), btn('Để sau', {kind: 'ghost', onClick: () => modal.close()}), approve);
    refresh();

    async function apply() {
      approve.disabled = true;
      try {
        const out = await request(profileUrl(id, '/apply'), {method: 'POST', body: {fields: em.chosenFields(rows)}});
        toast(`Đã bổ sung ${out.applied.length} mục vào hồ sơ.${out.skipped.length ? ` ${out.skipped.length} mục đã có sẵn nên giữ nguyên.` : ''}`, 'ok',
          {action: 'Hoàn tác', onAction: async () => { await request(`/api/workspace/profiles/${encodeURIComponent(id)}/rollback`, {method: 'POST', body: {}}); if (onApplied) onApplied(); }});
        modal.close();
        if (onApplied) onApplied();
      } catch (e) { toast(friendlyError(e), 'danger'); approve.disabled = false; }
    }
  }
}

/** Chạy hàng loạt (tuần tự, có nút dừng); xong thì liệt kê hồ sơ có đề xuất để duyệt từng cái. */
export async function openBulkEnrich(ids, {names = {}, onApplied} = {}) {
  const body = h('div', {class: 'col gap-3'});
  const foot = h('div', {class: 'row', style: {width: '100%'}});
  let stopped = false;
  const modal = openModal({title: 'Bổ sung hàng loạt bằng Google', description: 'Tra lần lượt từng hồ sơ, hồ sơ thiếu nhiều mục làm trước. Mỗi hồ sơ tốn một lượt Gemini.', body, footer: foot, size: 'lg', onDismiss: () => { stopped = true; }});
  const line = h('div', {});
  const cancel = btn('Dừng', {kind: 'danger', onClick: async () => { try { await request('/api/profiles/enrich-batch/cancel', {method: 'POST', body: {}}); } catch (_) { /* */ } }});
  clear(body, line);
  clear(foot, h('span', {class: 'grow'}), btn('Đóng', {kind: 'ghost', onClick: () => { stopped = true; modal.close(); }}), cancel);
  let status;
  try {
    status = await request('/api/profiles/enrich-batch', {method: 'POST', body: ids && ids.length ? {ids} : {}});
    while (status.state === 'running' && !stopped) {
      clear(line, notice('info', 'Đang tra Google', em.batchText(status)));
      await new Promise(r => setTimeout(r, POLL_MS));
      status = await request('/api/profiles/enrich-batch');
    }
  } catch (e) { clear(body, notice('danger', 'Chưa chạy được', friendlyError(e))); cancel.remove(); return; }
  if (stopped) return;
  cancel.remove();
  const ready = (status.results || []).filter(r => r.ok && (r.fields || []).length);
  const failed = (status.results || []).filter(r => !r.ok);
  clear(body, notice(failed.length ? 'warn' : 'ok', 'Đã tra xong', em.batchText(status)),
    ready.map(r => h('div', {class: 'row between', style: {padding: '6px 0', borderBottom: '1px solid var(--border)'}},
      h('span', {}, names[r.id] || r.name || r.id, h('span', {class: 'fs-xs muted'}, ` · ${r.fields.length} mục đề xuất`)),
      btn('Xem & duyệt', {size: 'sm', onClick: () => openProfileEnrich(r.id, {name: names[r.id] || r.name, start: false, onApplied})}))),
    failed.slice(0, 5).map(r => h('div', {class: 'fs-xs', style: {color: 'var(--danger-text)'}}, `${r.name || r.id}: ${r.error}`)));
}
