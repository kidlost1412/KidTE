// Ngăn xem toàn bộ lời nhắc của một người trực: 4 phần theo đúng thứ tự gửi cho AI, sửa được phần 2-4.
import {h, clear} from '../core/dom.js';
import {request, friendlyError} from '../core/api.js';
import {btn, chip, notice, textarea, input} from '../ui/kit.js';
import {toast, confirmDialog, openDrawer} from '../ui/overlays.js';
import {tryResult} from './learning-ui.js';
import {notesSaveBody} from './persona-model.js';
import {sectionsModel, noticeModel, missingProtected, loadPromptView, saveRules, resetRules, resolveRules,
  savePersonaSection, previewReply, NOTES_URL} from './prompt-view-model.js';

export function openPromptViewer(persona, {onChange} = {}) {
  const root = h('div', {class: 'col gap-3', style: {padding: '16px'}});
  const drawer = openDrawer({title: `Toàn bộ lời nhắc — ${persona.name}`,
    sub: 'Đúng thứ tự AI nhận được. Phần 1 cố định; phần 2, 3, 4 sửa được.', body: root, width: 880,
    onClose: () => onChange && onChange()});
  let view = null;
  let merging = false;

  const reload = async () => {
    try { view = await loadPromptView(request, persona.persona_id); render(); }
    catch (e) { clear(root, notice('danger', null, friendlyError(e))); }
  };
  const run = async (fn, okText) => {
    try { const out = await fn(); if (okText) toast(okText, 'ok'); return out; }
    catch (e) { toast(friendlyError(e), 'danger'); return null; }
  };
  const setView = out => { if (out && out.sections) { view = out; render(); } };

  function counter(area, max) {
    const c = h('div', {class: 'fs-xs muted'});
    const upd = () => { c.textContent = `${area.value.length}/${max} ký tự`; c.style.color = area.value.length > max ? 'var(--danger, #c0392b)' : ''; };
    area.addEventListener('input', upd); upd();
    return c;
  }

  function sectionCard(s) {
    const head = h('header', {class: 'card-head'}, h('div', {class: 'grow'}, h('h3', {}, s.title), h('div', {class: 'sub'}, s.note || '')),
      ...s.badges.map(b => chip(b.label, b.tone)));
    if (!s.editable) {
      return h('section', {class: 'card', 'data-section': s.id}, head, h('div', {class: 'card-body'}, h('pre', {class: 'style-pre'}, s.text)));
    }
    const area = textarea({rows: s.id === 'rules' ? 18 : s.id === 'notes' ? 4 : 9});
    area.value = s.text;
    if (s.id === 'rules' && !s.editable) area.readOnly = true;
    const foot = h('div', {class: 'row', style: {gap: '8px', justifyContent: 'flex-end', flexWrap: 'wrap'}});
    const extras = [];
    if (s.id === 'rules') {
      const warn = h('div', {class: 'fs-xs', style: {color: 'var(--warn, #b7791f)'}});
      const upd = () => {
        const gone = missingProtected(area.value, s.protected);
        warn.textContent = gone.length ? `Các dòng định dạng JSON bắt buộc đã bị xoá — ứng dụng sẽ tự thêm lại ${gone.length} mục khi lưu.` : '';
      };
      area.addEventListener('input', upd); upd();
      extras.push(warn);
      foot.append(
        s.customized ? btn('Đặt lại về bản gốc', {kind: 'ghost', onClick: async () => {
          if (await confirmDialog({title: 'Đặt lại quy tắc bán hàng?', text: 'Bản của bạn bị bỏ, dùng lại bản gốc của ứng dụng.', confirm: 'Đặt lại', tone: 'danger'}))
            setView(await run(() => resetRules(request, persona.persona_id), 'Đã dùng lại bản gốc.'));
        }}) : null,
        btn('Lưu quy tắc của tôi', {kind: 'primary', icon: 'save', onClick: async () => {
          const out = await run(() => saveRules(request, area.value, s, persona.persona_id), 'Đã lưu — áp dụng cho câu tiếp theo.');
          if (out) { if (out.appended) toast('Đã tự thêm lại các dòng định dạng JSON bắt buộc.', 'info'); merging = false; setView(out); }
        }}));
    } else if (s.id === 'notes') {
      extras.push(h('div', {class: 'fs-xs muted'}, `Tiêu đề tự thêm: ${s.header}`));
      foot.append(btn('Lưu ghi chú', {kind: 'primary', icon: 'save', onClick: async () => {
        const {body, error} = notesSaveBody(area.value);
        if (error) return toast(friendlyError(new Error(error)), 'danger');
        if (await run(() => request(NOTES_URL, {method: 'POST', body}), 'Đã lưu ghi chú.')) reload();
      }}));
    } else if (s.id === 'persona') {
      const name = s.is_default ? input({value: 'Phong cách của tôi', maxlength: 80, 'aria-label': 'Tên phong cách riêng'}) : null;
      if (name) extras.push(h('div', {class: 'row', style: {gap: '8px', alignItems: 'center'}}, h('span', {class: 'fs-sm'}, 'Tên phong cách mới:'), name));
      extras.push(h('div', {class: 'fs-xs muted'}, s.delivery));
      foot.append(btn(s.is_default ? 'Lưu thành phong cách riêng' : 'Lưu phong cách', {kind: 'primary', icon: 'save', onClick: async () => {
        const out = await run(() => savePersonaSection(request, s, area.value, name ? name.value : ''), 'Đã lưu phong cách.');
        if (out) { if (s.is_default) toast('Đã tạo phong cách riêng — vào danh sách để chọn người này trực.', 'info'); onChange && onChange(); reload(); }
      }}));
    }
    return h('section', {class: 'card', 'data-section': s.id}, head,
      h('div', {class: 'card-body col gap-2'}, area, counter(area, s.max), ...extras, foot));
  }

  function noticeCard(n) {
    const rows = h('div', {style: {display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '2px 12px', maxHeight: '320px', overflow: 'auto', fontSize: 'var(--fs-xs)'}},
      h('strong', {}, 'Bản của bạn'), h('strong', {}, 'Bản gốc mới'),
      ...n.rows.flatMap(r => {
        const tint = {changed: 'rgba(245,158,11,.18)', removed: 'rgba(239,68,68,.15)', added: 'rgba(34,197,94,.15)'}[r.kind] || '';
        const cell = text => h('div', {style: {whiteSpace: 'pre-wrap', background: tint, padding: '1px 4px'}}, text || ' ');
        return [cell(r.left), cell(r.right)];
      }));
    const act = (label, id, kind) => btn(label, {kind, onClick: async () => {
      if (id === 'merge') { merging = true; render(); return; }
      setView(await run(() => resolveRules(request, id, persona.persona_id),
        id === 'use_new' ? 'Đã dùng bản gốc mới.' : 'Đã giữ bản của bạn.'));
    }});
    return h('section', {class: 'card', 'data-notice': 'rules'},
      h('div', {class: 'card-body col gap-2'},
        notice('warn', n.title, `${n.changed} dòng khác nhau giữa bản của bạn và bản gốc mới. Trong lúc chờ, AI vẫn dùng bản của bạn.`),
        rows,
        h('div', {class: 'row', style: {gap: '8px', justifyContent: 'flex-end', flexWrap: 'wrap'}},
          ...n.buttons.map(b => act(b.label, b.id, b.id === 'use_new' ? 'primary' : ''))),
        merging ? h('div', {class: 'fs-sm'}, 'Gộp tay: sửa ô “2) Quy tắc bán hàng” bên dưới (bản mới ở cột phải để chép), rồi bấm “Lưu quy tắc của tôi” — việc lưu sẽ xác nhận bạn đã xem bản mới.') : null));
  }

  function previewCard() {
    const q = input({placeholder: 'Câu khách hỏi để thử, vd: chai này pha bao nhiêu vậy', 'aria-label': 'Câu khách hỏi'});
    const out = h('div', {});
    const go = async () => {
      clear(out, h('div', {class: 'muted fs-sm'}, 'Đang nhờ AI trả lời thử…'));
      try { clear(out, tryResult(await previewReply(request, q.value, persona.persona_id))); }
      catch (e) { clear(out, notice('danger', null, friendlyError(e))); }
    };
    q.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); go(); } });
    return h('section', {class: 'card', 'data-section': 'preview'},
      h('header', {class: 'card-head'}, h('div', {class: 'grow'}, h('h3', {}, 'Bản xem trước câu trả lời'),
        h('div', {class: 'sub'}, `Chạy đúng đường “Thử AI” với giọng ${persona.name}; không gửi, không ghi Hộp thư.`))),
      h('div', {class: 'card-body col gap-2'}, h('div', {class: 'row', style: {gap: '8px'}}, h('div', {class: 'grow'}, q), btn('Bản xem trước câu trả lời', {kind: 'primary', icon: 'play', onClick: go})), out));
  }

  function render() {
    const n = noticeModel(view);
    clear(root, n.show ? noticeCard(n) : null, ...sectionsModel(view).map(sectionCard), previewCard());
  }

  clear(root, h('div', {class: 'muted fs-sm'}, 'Đang tải…'));
  reload();
  return drawer;
}
