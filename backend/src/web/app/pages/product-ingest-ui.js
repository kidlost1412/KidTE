import {h, clear} from '../core/dom.js';
import {request, friendlyError} from '../core/api.js';
import {btn, chip, field, input, textarea, notice} from '../ui/kit.js';
import {toast, openDrawer} from '../ui/overlays.js';
import * as pm from './product-ingest-model.js';

const MAX_IMAGES = 8;
const MAX_FILES = 5;
const DOC_ACCEPT = '.pdf,.docx,.txt,.md,.html,.htm,.csv';

function readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

/** Ngăn kéo "Thêm sản phẩm bằng AI". onDone(recordId) chạy sau khi lưu nháp. */
export function openProductIngest(ctx, {onDone} = {}) {
  const files = [], images = [];
  const body = h('div', {class: 'card-body col gap-4'});
  const foot = h('div', {class: 'row', style: {width: '100%'}});
  const drawer = openDrawer({title: 'Thêm sản phẩm bằng AI', sub: 'Đưa tài liệu, ảnh nhãn hoặc địa chỉ web — AI đọc và điền hồ sơ NHÁP để bạn duyệt.', width: 760, body, footer: foot});
  let status = {};
  request('/api/ingest/status').then(s => { status = s; }).catch(() => {});

  // ------------------------------------------------------------ bước 1: đầu vào
  function stepInputs() {
    const docInput = h('input', {type: 'file', accept: DOC_ACCEPT, multiple: true, hidden: true});
    const imgInput = h('input', {type: 'file', accept: 'image/png,image/jpeg,image/webp', multiple: true, hidden: true});
    const url = input({placeholder: 'https://… (trang giới thiệu sản phẩm)'});
    const text = textarea({rows: 5, placeholder: 'Hoặc dán chữ: thành phần, công dụng, cách dùng…'});
    const fileList = h('div', {class: 'col', style: {gap: '4px'}});
    const warn = h('div');
    const isImage = f => f.type.startsWith('image/');
    const renderFiles = () => {
      clear(fileList, [...files, ...images].map(f => h('div', {class: 'row fs-sm'}, chip(isImage(f) ? 'Ảnh' : 'Tệp'), h('span', {class: 'grow truncate'}, f.name),
        btn('', {size: 'sm', kind: 'ghost', icon: 'x', attrs: {'aria-label': 'Bỏ ' + f.name}, onClick: () => { const list = isImage(f) ? images : files; list.splice(list.indexOf(f), 1); renderFiles(); }}))));
      const msg = pm.needsGeminiWarning(images.length > 0, status);
      clear(warn, msg ? notice('warn', 'Lưu ý', msg) : null);
    };
    docInput.addEventListener('change', () => { for (const f of docInput.files) if (files.length < MAX_FILES) files.push(f); docInput.value = ''; renderFiles(); });
    imgInput.addEventListener('change', () => { for (const f of imgInput.files) if (images.length < MAX_IMAGES) images.push(f); imgInput.value = ''; renderFiles(); });
    clear(body,
      h('div', {class: 'row wrap'}, btn('Tải tài liệu (PDF, Word, TXT, HTML)…', {icon: 'fileText', onClick: () => docInput.click()}),
        btn('Tải ảnh nhãn (mặt trước, mặt sau…)', {icon: 'image', onClick: () => imgInput.click()}), docInput, imgInput),
      fileList, field('Địa chỉ web của sản phẩm', url, {hint: 'Chỉ nhận http/https. Ảnh đại diện (og:image) của trang được giữ làm ảnh sản phẩm ứng viên.'}),
      field('Chữ dán', text), warn,
      notice('info', 'Có thể kết hợp nhiều nguồn', 'Mọi thứ ở trên gộp lại thành MỘT sản phẩm. Giá và khuyến mãi không bao giờ được đọc: chúng lấy từ giỏ hàng.'));
    renderFiles();
    clear(foot, h('span', {class: 'grow'}), btn('Đóng', {kind: 'ghost', onClick: () => drawer.close()}),
      btn('AI đọc ngay', {kind: 'primary', icon: 'sparkles', onClick: async e => {
        const button = e.currentTarget;
        button.disabled = true; button.textContent = 'Đang đọc… (có thể mất 10–40 giây)';
        try {
          const payload = {
            files: await Promise.all(files.map(async f => ({name: f.name, data: await readAsDataUrl(f)}))),
            images: await Promise.all(images.map(async f => ({name: f.name, data: await readAsDataUrl(f)}))),
            url: url.value.trim(), text: text.value.trim(),
          };
          const result = await request('/api/ingest/extract', {method: 'POST', body: payload});
          stepReview(result, payload.url);
        } catch (err) { toast(friendlyError(err), 'danger'); button.disabled = false; button.textContent = 'AI đọc ngay'; }
      }}));
  }

  // ------------------------------------------------------------ bước 2: duyệt
  function stepReview(result, sourceUrl) {
    const model = pm.buildReviewModel(result);
    let imageId = pm.defaultImageSource(model);
    const counter = h('span', {class: 'fs-sm'});
    const updateCounter = () => { const n = pm.uncertainCount(model); counter.textContent = n ? `${n} mục cần xem` : 'Không còn mục cần xem'; counter.style.color = n ? 'var(--warn-text)' : 'var(--ok-text)'; };
    const form = h('div', {class: 'form-grid'}, model.rows.map(row => {
      const control = row.type === 'text' ? input() : textarea({rows: row.type === 'textarea' ? 3 : 4});
      control.value = row.value;
      const holder = field(row.label, control, {hint: row.source ? `Nguồn: ${row.source}` : (row.empty ? 'AI không tìm thấy — điền nếu biết' : '')});
      const paint = () => {
        control.style.borderColor = row.uncertain ? 'var(--warn-text)' : '';
        control.style.background = row.uncertain ? 'color-mix(in srgb, var(--warn-text) 8%, transparent)' : '';
      };
      control.addEventListener('input', () => { pm.markEdited(row, control.value); paint(); updateCounter(); });
      paint();
      return holder;
    }));
    updateCounter();
    const thumbs = model.images.length ? h('div', {class: 'col gap-3'}, h('div', {class: 'section-title'}, 'Ảnh sản phẩm (chọn một)'),
      h('div', {class: 'row wrap'}, model.images.map(s => {
        const radio = h('input', {type: 'radio', name: 'ingest-img', checked: s.id === imageId, 'aria-label': 'Chọn ' + s.label, onChange: () => { imageId = s.id; }});
        return h('label', {style: {display: 'grid', gap: '4px', justifyItems: 'center', cursor: 'pointer'}},
          h('img', {src: s.thumb, alt: s.label, style: {width: '110px', height: '110px', objectFit: 'contain', background: '#fff', border: '1px solid var(--border)', borderRadius: '10px'}}), radio,
          h('span', {class: 'fs-xs muted'}, s.kind === 'og_image' ? 'Từ trang web' : s.label.slice(0, 18)));
      })), h('label', {class: 'row fs-sm'}, h('input', {type: 'radio', name: 'ingest-img', onChange: () => { imageId = ''; }}), 'Không dùng ảnh')) : null;
    clear(body,
      h('div', {class: 'row wrap'}, chip(model.providerNote || 'AI', 'info'), counter),
      notice('warn', 'Đây mới là bản NHÁP', 'Ô viền cam là chỗ AI không chắc hoặc không đọc được — hãy đối chiếu với nhãn thật. Chỉ chép đúng liều lượng trên nhãn.'),
      model.filteredPrice ? notice('info', 'Đã bỏ các câu có giá/khuyến mãi', 'Giá và khuyến mãi không đi vào hồ sơ.') : null,
      model.sources.length ? h('div', {class: 'fs-xs muted'}, 'Nguồn đã đọc: ' + model.sources.map(s => s.label).join(' · ')) : null,
      thumbs, form);
    clear(foot, btn('Làm lại', {kind: 'ghost', onClick: stepInputs}), h('span', {class: 'grow'}), btn('Đóng', {kind: 'ghost', onClick: () => drawer.close()}),
      btn('Lưu nháp', {kind: 'primary', icon: 'save', onClick: async e => {
        const button = e.currentTarget;
        if (!pm.canSaveDraft(model)) { toast('Cần có tên sản phẩm.', 'warn'); return; }
        button.disabled = true;
        try {
          const out = await request('/api/ingest/draft', {method: 'POST', body: pm.draftBody(model, {imageSourceId: imageId, sourceUrl})});
          stepSuggest(out);
        } catch (err) { toast(friendlyError(err), 'danger'); button.disabled = false; }
      }}));
  }

  // ------------------------------------------------------------ bước 3: gợi ý SKU
  function stepSuggest(out) {
    const list = out.suggestions || [];
    let useImage = !!out.image_saved;
    const finish = () => { drawer.close(); if (onDone) onDone(out.id); };
    clear(body,
      notice('ok', 'Đã lưu hồ sơ NHÁP', 'AI chưa được dùng hồ sơ này. Điền thêm nếu cần rồi Duyệt ở tab “Gắn hồ sơ cho sản phẩm” → Hồ sơ nháp.'),
      list.length ? h('div', {class: 'col gap-3'}, h('div', {class: 'section-title'}, 'Có phải sản phẩm này trong giỏ hàng LIVE?'),
        out.image_saved ? h('label', {class: 'row fs-sm'}, h('input', {type: 'checkbox', checked: true, onChange: e => { useImage = e.target.checked; }}), 'Dùng ảnh vừa chọn làm ảnh sản phẩm khi xác nhận') : null,
        list.map(s => h('div', {class: 'list-row', style: {border: '1px solid var(--border)', borderRadius: '12px', padding: '10px 12px'}},
          h('div', {class: 'grow'}, h('div', {class: 'fs-sm clamp-2', title: s.title}, s.title), h('div', {class: 'fs-xs muted'}, pm.suggestionLabel(s)),
            (s.reasons || []).length ? h('div', {class: 'fs-xs muted'}, s.reasons.join(' · ')) : null),
          btn('Đúng, gắn vào đây', {size: 'sm', kind: 'primary', onClick: async e => {
            const button = e.currentTarget;
            button.disabled = true;
            try {
              await request('/api/ingest/link', {method: 'POST', body: {id: out.id, product_id: s.product_id, use_image: useImage}});
              toast('Đã ghi nhớ sản phẩm. Duyệt hồ sơ nháp để AI dùng.', 'ok'); finish();
            } catch (err) { toast(friendlyError(err), 'danger'); button.disabled = false; }
          }})))) : notice('info', 'Chưa thấy sản phẩm khớp trong giỏ hàng', 'Bạn vẫn có thể gắn sau ở tab “Gắn hồ sơ cho sản phẩm”.'));
    clear(foot, h('span', {class: 'grow'}), btn(list.length ? 'Để sau' : 'Xong', {kind: 'primary', onClick: finish}));
  }

  stepInputs();
  return drawer;
}
