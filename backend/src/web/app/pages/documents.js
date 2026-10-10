// Trí nhớ AI → Tài liệu: tải tài liệu lên để DeepSeek đọc (công ty, sản phẩm, kiến thức nông nghiệp).
import {h, clear} from '../core/dom.js';
import {icon} from '../core/icons.js';
import {request, friendlyError} from '../core/api.js';
import {relative, bytes, number} from '../core/format.js';
import {btn, chip, empty, errorBox, skeletonRows, notice, card, field, input, select, toggle} from '../ui/kit.js';
import {toast, confirmDialog, openDrawer} from '../ui/overlays.js';

const ACCEPT = '.txt,.md,.csv,.docx,.pdf,.html,.htm';

export function mountDocuments(ctx, host, d) {
  let state = null, timer = null;
  d.add(() => clearTimeout(timer));
  const category = select([['san_pham', 'Sản phẩm'], ['cong_ty', 'Về công ty / shop'], ['nong_nghiep', 'Kiến thức nông nghiệp'], ['khac', 'Khác']], 'san_pham');
  const fileInput = h('input', {type: 'file', accept: ACCEPT, multiple: true, hidden: true, onChange: () => { upload([...fileInput.files]); fileInput.value = ''; }});
  const uploadSlot = h('div', {class: 'col gap-2'});
  const listSlot = h('div', {}, skeletonRows(3, 60));
  const probe = input({placeholder: 'vd: sầu riêng bị xì mủ thân dùng gì', value: ''});
  const probeOut = h('div', {class: 'col gap-2'});

  const drop = h('div', {class: 'dropzone'},
    h('span', {class: 'dz-icon'}, icon('upload')),
    h('div', {class: 'grow'}, h('strong', {}, 'Kéo thả tài liệu vào đây'),
      h('p', {class: 'fs-sm muted'}, 'Word (.docx), PDF có chữ, .txt, .md, .csv, trang web (.html). Tối đa 30 MB mỗi file. Tài liệu chỉ nằm trên máy này.')),
    h('div', {class: 'col gap-2', style: {alignItems: 'flex-end'}}, field('Loại tài liệu', category),
      btn('Chọn file…', {kind: 'primary', icon: 'upload', onClick: () => fileInput.click()})), fileInput);
  ['dragenter', 'dragover'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.add('drag'); }));
  ['dragleave', 'drop'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.remove('drag'); }));
  drop.addEventListener('drop', e => upload([...(e.dataTransfer.files || [])]));

  clear(host, h('div', {class: 'col gap-4'},
    notice('info', 'AI dùng tài liệu theo hai cách', h('span', {},
      '① Mỗi câu khách hỏi, AI tự tìm vài đoạn liên quan trong tài liệu để trả lời cho đúng — không cần làm gì thêm. ',
      '② Bấm “AI đọc & rút kiến thức”: DeepSeek đọc hết tài liệu, đề xuất các dữ kiện ngắn vào Trí nhớ AI để bạn duyệt. ',
      h('strong', {}, 'Giá, khuyến mãi, liều pha vẫn luôn theo hồ sơ sản phẩm'), ' (tài liệu có thể đã cũ).')),
    drop, uploadSlot,
    h('div', {class: 'col', id: 'doc-list'}, listSlot),
    card('Thử: khách hỏi câu này thì AI đọc được đoạn nào', h('div', {class: 'card-body col gap-3'},
      h('div', {class: 'row', style: {gap: '8px'}}, h('div', {class: 'grow'}, probe), btn('Tìm', {icon: 'search', onClick: runProbe})), probeOut))));
  probe.addEventListener('keydown', e => { if (e.key === 'Enter') runProbe(); });

  async function upload(files) {
    for (const file of files) {
      const row = h('div', {class: 'fs-sm'}, `Đang đọc “${file.name}”…`);
      uploadSlot.append(row);
      try {
        const url = `/api/system/documents/upload?filename=${encodeURIComponent(file.name)}&category=${encodeURIComponent(category.value)}`;
        const out = await request(url, {method: 'POST', body: file, headers: {'Content-Type': 'application/octet-stream'}});
        clear(row, notice('ok', `Đã thêm “${out.document.title}”`, `${number(out.document.chars)} ký tự · ${out.document.chunks} đoạn — AI dùng được ngay.`));
      } catch (e) { clear(row, notice('danger', `Không thêm được “${file.name}”`, friendlyError(e))); }
    }
    d.timeout(() => clear(uploadSlot), 8000);
    load();
  }

  async function runProbe() {
    const q = probe.value.trim();
    if (q.length < 2) return;
    clear(probeOut, h('div', {class: 'fs-sm muted'}, 'Đang tìm…'));
    try {
      const out = await request('/api/system/documents/search', {method: 'POST', body: {comment: q}});
      clear(probeOut, out.results.length ? out.results.map(r => h('div', {class: 'try-why'},
        h('div', {class: 'row', style: {gap: '8px'}}, h('strong', {class: 'fs-sm'}, r.tai_lieu), chip(r.loai, 'info'), h('span', {class: 'fs-xs muted'}, `điểm ${r.diem}`)),
        h('div', {class: 'fs-sm soft', style: {whiteSpace: 'pre-wrap'}}, r.doan)))
        : h('div', {class: 'fs-sm muted'}, 'Không đoạn nào đủ liên quan — AI sẽ không dùng tài liệu cho câu này.'));
    } catch (e) { clear(probeOut, notice('danger', null, friendlyError(e))); }
  }

  async function act(doc, path, body, okText) {
    try { await request(`/api/system/documents/${doc.id}/${path}`, {method: 'POST', body: body || {}}); if (okText) toast(okText, 'ok'); load(); }
    catch (e) { toast(friendlyError(e), 'danger'); }
  }

  async function view(doc) {
    const bodyNode = h('div', {class: 'card-body'}, skeletonRows(4, 40));
    openDrawer({title: doc.title, sub: `${doc.filename} · ${doc.category_label}`, body: bodyNode, width: 720});
    try {
      const out = await request('/api/system/documents/' + doc.id);
      clear(bodyNode, h('pre', {class: 'log-pre', style: {maxHeight: 'none'}}, out.document.text + (out.document.truncated ? '\n\n… (chỉ hiện 30.000 ký tự đầu)' : '')));
    } catch (e) { clear(bodyNode, errorBox(friendlyError(e))); }
  }

  function docRow(doc) {
    const job = doc.distill || {};
    const running = job.state === 'running';
    return h('div', {class: 'list-row', style: {alignItems: 'flex-start'}},
      h('span', {class: 'plugin-icon'}, icon('fileText')),
      h('div', {class: 'grow col gap-1'},
        h('div', {class: 'row wrap', style: {gap: '8px'}}, h('strong', {class: 'fs-sm'}, doc.title), chip(doc.category_label, 'info'),
          doc.enabled ? null : chip('AI không dùng', '')),
        h('div', {class: 'fs-xs muted'}, `${doc.filename} · ${bytes(doc.bytes)} · ${number(doc.chars)} ký tự · ${doc.chunks} đoạn · thêm ${relative(doc.created_at)}`),
        running ? h('div', {class: 'fs-xs'}, `AI đang đọc… ${job.done}/${job.total} phần · đã rút ${job.items} dữ kiện`) : null,
        job.state === 'error' ? notice('danger', null, job.error) : null,
        job.state === 'done' ? h('div', {class: 'fs-xs', style: {color: 'var(--ok-text)'}}, `Xong: đề xuất ${job.items} dữ kiện — vào tab Kiến thức để duyệt.`) : null,
        doc.learned_items && job.state !== 'done' ? h('div', {class: 'fs-xs muted'}, `Đã rút ${doc.learned_items} dữ kiện ${relative(doc.learned_at)}`) : null),
      h('div', {class: 'col gap-2', style: {alignItems: 'flex-end'}},
        toggle('AI dùng', doc.enabled, on => act(doc, 'update', {enabled: on}, on ? 'AI dùng lại tài liệu này.' : 'AI không dùng tài liệu này nữa.')),
        h('div', {class: 'row', style: {gap: '4px'}},
          btn('Xem', {size: 'sm', kind: 'ghost', icon: 'eye', onClick: () => view(doc)}),
          btn(running ? 'Đang đọc…' : 'AI đọc & rút kiến thức', {size: 'sm', icon: 'sparkles', disabled: running || !state.ai_ready,
            title: state.ai_ready ? '' : 'Cần khoá DeepSeek', onClick: () => act(doc, 'distill', {}, 'AI bắt đầu đọc tài liệu — có thể làm việc khác.')}),
          btn('Xoá', {size: 'sm', kind: 'ghost', icon: 'trash', onClick: async () => {
            const ok = await confirmDialog({title: `Xoá tài liệu “${doc.title}”?`, text: 'AI không dùng tài liệu này nữa. Dữ kiện đã rút vào Trí nhớ AI vẫn được giữ (tắt riêng ở tab Kiến thức nếu muốn).', confirm: 'Xoá', tone: 'danger'});
            if (ok) act(doc, 'delete', {}, 'Đã xoá tài liệu.');
          }}))));
  }

  function render() {
    const docs = state.documents || [];
    const st = state.stats || {};
    clear(listSlot, card(`Tài liệu (${docs.length})`, docs.length ? h('div', {class: 'list'}, docs.map(docRow))
      : h('div', {class: 'card-body'}, empty('Chưa có tài liệu nào', 'Tải lên giới thiệu công ty, catalogue sản phẩm, bài kỹ thuật canh tác… để AI hiểu shop và tư vấn sâu hơn.', {iconName: 'fileText'})),
    {sub: docs.length ? `AI đang dùng ${st.enabled} tài liệu · ${number(st.chunks)} đoạn` : ''}));
    clearTimeout(timer);
    if (docs.some(x => x.distill && x.distill.state === 'running')) timer = setTimeout(load, 2500);
  }

  async function load() {
    try { state = await request('/api/system/documents'); render(); }
    catch (e) { clear(listSlot, errorBox(friendlyError(e), load)); }
  }
  load();
}
