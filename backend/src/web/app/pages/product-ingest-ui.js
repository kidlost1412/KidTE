import {h, clear} from '../core/dom.js';
import {request, friendlyError} from '../core/api.js';
import {btn, chip, field, input, select, textarea, notice} from '../ui/kit.js';
import {toast, openDrawer} from '../ui/overlays.js';
import * as pm from './product-ingest-model.js';

const MAX_IMAGES = 8;
const MAX_FILES = 5;
const DOC_ACCEPT = '.pdf,.docx,.txt,.md,.html,.htm,.csv';
const UPLOAD_TIMEOUT = 10 * 60 * 1000;
const READ_TIMEOUT = 15 * 60 * 1000;

/** Đồng hồ trên nút: "nhãn… (m:ss)". Trả hàm dừng. */
function startElapsed(button, label) {
  const t0 = Date.now();
  const tick = () => {
    const s = Math.floor((Date.now() - t0) / 1000);
    button.textContent = `${label} (${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')})`;
  };
  tick();
  const timer = setInterval(tick, 1000);
  return () => clearInterval(timer);
}

/** Kết quả quét thêm trang sản phẩm (nếu có). */
function crawlNotice(crawl) {
  if (!crawl || !crawl.enabled) return null;
  const failed = crawl.failed ? `, ${crawl.failed} trang lỗi được bỏ qua` : '';
  return notice('info', 'Đã quét website', `Tìm thấy ${crawl.found || 0} liên kết sản phẩm, đọc được ${crawl.read || 0} trang${failed}.`);
}

/** Tải tệp thô lên máy chủ (không base64: danh mục PDF có thể nặng hàng chục MB). */
async function uploadFile(f) {
  const out = await request('/api/ingest/upload?name=' + encodeURIComponent(f.name), {method: 'POST', body: f, timeoutMs: UPLOAD_TIMEOUT});
  return {name: f.name, upload_id: out.upload_id};
}

/** Ngăn kéo "Thêm sản phẩm bằng AI". onDone(recordId) chạy sau khi lưu nháp. */
export function openProductIngest(ctx, {onDone} = {}) {
  const files = [], images = [];
  const body = h('div', {class: 'card-body col gap-4'});
  const foot = h('div', {class: 'row', style: {width: '100%'}});
  const drawer = openDrawer({title: 'Thêm sản phẩm bằng AI', sub: 'Đưa tài liệu, ảnh nhãn hoặc địa chỉ web — AI đọc và điền hồ sơ NHÁP để bạn duyệt.', width: 760, body, footer: foot});
  let status = {};
  let provider = 'gemini';
  let providerTouched = false;
  let refreshInputs = () => {};
  request('/api/ingest/status').then(s => {
    status = s;
    if (!providerTouched) provider = pm.defaultProvider(s);
    refreshInputs();
  }).catch(() => {});

  // ------------------------------------------------------------ bước 1: đầu vào
  function stepInputs() {
    const docInput = h('input', {type: 'file', accept: DOC_ACCEPT, multiple: true, hidden: true});
    const imgInput = h('input', {type: 'file', accept: 'image/png,image/jpeg,image/webp', multiple: true, hidden: true});
    const url = input({placeholder: 'https://… (trang giới thiệu sản phẩm)'});
    const crawlBox = h('input', {type: 'checkbox', checked: true, 'aria-label': 'Quét thêm các trang sản phẩm cùng website'});
    const crawlRow = h('label', {class: 'row fs-sm', hidden: true}, crawlBox,
      'Quét thêm các trang sản phẩm cùng website (tối đa 20 trang)');
    url.addEventListener('input', () => {
      crawlBox.checked = pm.isRootUrl(url.value);                  // trang chủ: mặc định bật; trang con: tắt
      crawlRow.hidden = !url.value.trim();
    });
    const text = textarea({rows: 5, placeholder: 'Hoặc dán chữ: thành phần, công dụng, cách dùng…'});
    const fileList = h('div', {class: 'col', style: {gap: '4px'}});
    const warn = h('div');
    const providerSelect = select(pm.PROVIDER_OPTIONS, provider, {'aria-label': 'AI đọc tài liệu'});
    const imgButton = btn('Tải ảnh nhãn (mặt trước, mặt sau…)', {icon: 'image', onClick: () => imgInput.click()});
    const providerHint = h('div', {class: 'field-hint'});
    const providerField = field('AI đọc tài liệu', providerSelect);
    providerField.append(providerHint);
    const isImage = f => f.type.startsWith('image/');
    const renderFiles = () => {
      clear(fileList, [...files, ...images].map(f => h('div', {class: 'row fs-sm'}, chip(isImage(f) ? 'Ảnh' : 'Tệp'), h('span', {class: 'grow truncate'}, f.name),
        h('span', {class: 'fs-xs muted'}, (f.size / 1048576).toFixed(1) + ' MB'),
        btn('', {size: 'sm', kind: 'ghost', icon: 'x', attrs: {'aria-label': 'Bỏ ' + f.name}, onClick: () => { const list = isImage(f) ? images : files; list.splice(list.indexOf(f), 1); renderFiles(); }}))));
      const msgs = [];
      if (!pm.imagesAllowed(provider) && images.length) msgs.push('DeepSeek không đọc được ảnh. Bỏ ảnh hoặc chọn Gemini.');
      if (provider === 'gemini') { const w = pm.needsGeminiWarning(images.length > 0, status); if (w) msgs.push(w); }
      clear(warn, msgs.length ? notice('warn', 'Lưu ý', msgs.join(' ')) : null);
      providerHint.textContent = pm.providerHint(provider, status);
      imgButton.disabled = !pm.imagesAllowed(provider);
      imgButton.title = imgButton.disabled ? 'DeepSeek chỉ đọc chữ — chọn Gemini để đọc ảnh nhãn' : '';
    };
    refreshInputs = () => { providerSelect.value = provider; renderFiles(); };
    providerSelect.addEventListener('change', () => { provider = providerSelect.value; providerTouched = true; renderFiles(); });
    docInput.addEventListener('change', () => { for (const f of docInput.files) if (files.length < MAX_FILES) files.push(f); docInput.value = ''; renderFiles(); });
    imgInput.addEventListener('change', () => { for (const f of imgInput.files) if (images.length < MAX_IMAGES) images.push(f); imgInput.value = ''; renderFiles(); });
    clear(body,
      providerField,
      h('div', {class: 'row wrap'}, btn('Tải tài liệu (PDF, Word, TXT, HTML)…', {icon: 'fileText', onClick: () => docInput.click()}),
        imgButton, docInput, imgInput),
      fileList, field('Địa chỉ web của sản phẩm', url, {hint: 'Chỉ nhận http/https. Ảnh đại diện (og:image) của trang được giữ làm ảnh sản phẩm ứng viên.'}),
      crawlRow,
      field('Chữ dán', text), warn,
      notice('info', 'Một sản phẩm hoặc cả danh mục', 'Tài liệu có nhiều sản phẩm (danh mục, bảng giá…): AI liệt kê từng sản phẩm để bạn chọn cái nào tạo hồ sơ nháp. Giá và khuyến mãi không bao giờ được đọc: chúng lấy từ giỏ hàng.'));
    renderFiles();
    clear(foot, h('span', {class: 'grow'}), btn('Đóng', {kind: 'ghost', onClick: () => drawer.close()}),
      btn('AI đọc ngay', {kind: 'primary', icon: 'sparkles', onClick: async e => {
        const button = e.currentTarget;
        if (!pm.imagesAllowed(provider) && images.length) { toast('DeepSeek không đọc được ảnh. Bỏ ảnh hoặc chọn Gemini.', 'warn'); return; }
        const big = pm.oversizeMessage([...files, ...images]);
        if (big) { toast(big, 'danger'); return; }
        button.disabled = true; button.textContent = 'Đang tải tệp lên…';
        try {
          const payload = {
            files: await Promise.all(files.map(uploadFile)),
            images: await Promise.all(images.map(uploadFile)),
            url: url.value.trim(), text: text.value.trim(), provider,
            crawl: !!url.value.trim() && crawlBox.checked,
          };
          // Tiến trình: một yêu cầu duy nhất, nên nhãn nút đổi theo giai đoạn đang chạy.
          const label = payload.crawl
            ? 'Đang đọc trang chủ và quét các trang sản phẩm… (có thể mất vài phút)'
            : 'Đang đọc… (có thể mất 10–90 giây)';
          const stop = startElapsed(button, label);
          let result;
          try { result = await request('/api/ingest/extract', {method: 'POST', body: payload, timeoutMs: READ_TIMEOUT}); } finally { stop(); }
          stepReview(result, payload.url);
        } catch (err) { toast(friendlyError(err), 'danger'); button.disabled = false; button.textContent = 'AI đọc ngay'; }
      }}));
  }

  // ------------------------------------------------------------ bước 2: duyệt
  function stepReview(result, sourceUrl) {
    const entries = pm.buildProductsModels(result);
    const multi = entries.length > 1;
    let active = entries[0];
    let imageId = multi ? '' : pm.defaultImageSource(active.model);
    const counter = h('span', {class: 'fs-sm'});
    const formHost = h('div', {});
    const listHost = h('div', {class: 'col', style: {gap: '2px', maxHeight: '260px', overflowY: 'auto'}});
    const saveBtn = btn('Lưu nháp', {kind: 'primary', icon: 'save', onClick: save});
    const updateCounter = () => {
      const n = pm.uncertainCount(active.model);
      counter.textContent = n ? `${n} mục cần xem` : 'Không còn mục cần xem';
      counter.style.color = n ? 'var(--warn-text)' : 'var(--ok-text)';
    };
    const updateSave = () => {
      saveBtn.textContent = pm.saveLabel(entries);
      saveBtn.disabled = pm.pickedForSave(entries).length === 0;
    };
    function renderList() {
      clear(listHost, entries.map(entry => h('div', {class: 'row fs-sm', style: {padding: '4px 6px', borderRadius: '8px', background: entry === active ? 'var(--surface-2)' : ''}},
        h('input', {type: 'checkbox', checked: entry.picked, 'aria-label': 'Tạo nháp: ' + pm.productName(entry), onChange: e => { entry.picked = e.target.checked; updateSave(); }}),
        h('button', {type: 'button', class: 'grow truncate', style: {border: '0', background: 'none', textAlign: 'left', cursor: 'pointer', font: 'inherit', color: 'inherit'},
          onClick: () => { active = entry; renderList(); renderForm(); }}, pm.productName(entry)))));
    }
    function renderForm() {
      const model = active.model;
      const form = h('div', {class: 'form-grid'}, model.rows.map(row => {
        const control = row.type === 'text' ? input() : textarea({rows: row.type === 'textarea' ? 3 : 4});
        control.value = row.value;
        const holder = field(row.label, control, {hint: row.source ? `Nguồn: ${row.source}` : (row.empty ? 'AI không tìm thấy — điền nếu biết' : '')});
        const paint = () => {
          control.style.borderColor = row.uncertain ? 'var(--warn-text)' : '';
          control.style.background = row.uncertain ? 'color-mix(in srgb, var(--warn-text) 8%, transparent)' : '';
        };
        control.addEventListener('input', () => { pm.markEdited(row, control.value); paint(); updateCounter(); if (row.key === 'ten_san_pham') { renderList(); updateSave(); } });
        paint();
        return holder;
      }));
      updateCounter();
      const thumbs = !multi && model.images.length ? h('div', {class: 'col gap-3'}, h('div', {class: 'section-title'}, 'Ảnh sản phẩm (chọn một)'),
        h('div', {class: 'row wrap'}, model.images.map(s => {
          const radio = h('input', {type: 'radio', name: 'ingest-img', checked: s.id === imageId, 'aria-label': 'Chọn ' + s.label, onChange: () => { imageId = s.id; }});
          return h('label', {style: {display: 'grid', gap: '4px', justifyItems: 'center', cursor: 'pointer'}},
            h('img', {src: s.thumb, alt: s.label, style: {width: '110px', height: '110px', objectFit: 'contain', background: '#fff', border: '1px solid var(--border)', borderRadius: '10px'}}), radio,
            h('span', {class: 'fs-xs muted'}, s.kind === 'og_image' ? 'Từ trang web' : s.label.slice(0, 18)));
        })), h('label', {class: 'row fs-sm'}, h('input', {type: 'radio', name: 'ingest-img', onChange: () => { imageId = ''; }}), 'Không dùng ảnh')) : null;
      clear(formHost, thumbs, form);
    }
    const first = active.model;
    const setAll = value => { entries.forEach(e => { e.picked = value; }); renderList(); updateSave(); };
    clear(body,
      h('div', {class: 'row wrap'}, chip(first.providerNote || 'AI', 'info'), multi ? chip(`Tìm thấy ${entries.length} sản phẩm`, 'ok') : null, counter),
      result.truncated ? notice('warn', 'Danh sách có thể chưa đủ', 'Tài liệu dài nên AI chỉ liệt kê được một phần. Lưu các sản phẩm này rồi đọc lại phần còn lại.') : null,
      notice('warn', 'Đây mới là bản NHÁP', 'Ô viền cam là chỗ AI không chắc hoặc không đọc được — hãy đối chiếu với nhãn thật. Chỉ chép đúng liều lượng trên nhãn.'),
      entries.some(e => e.model.filteredPrice) ? notice('info', 'Đã bỏ các câu có giá/khuyến mãi', 'Giá và khuyến mãi không đi vào hồ sơ.') : null,
      crawlNotice(result.crawl),
      first.sources.length ? h('div', {class: 'fs-xs muted'}, 'Nguồn đã đọc: ' + first.sources.map(s => s.label).join(' · ')) : null,
      multi ? h('div', {class: 'col gap-3'}, h('div', {class: 'row'}, h('div', {class: 'section-title grow'}, 'Chọn sản phẩm cần tạo hồ sơ nháp'),
        btn('Chọn hết', {size: 'sm', kind: 'ghost', onClick: () => setAll(true)}), btn('Bỏ chọn', {size: 'sm', kind: 'ghost', onClick: () => setAll(false)})), listHost) : null,
      formHost);
    renderList(); renderForm(); updateSave();
    clear(foot, btn('Làm lại', {kind: 'ghost', onClick: stepInputs}), h('span', {class: 'grow'}), btn('Đóng', {kind: 'ghost', onClick: () => drawer.close()}), saveBtn);

    async function save() {
      const chosen = pm.pickedForSave(entries);
      if (!chosen.length) { toast('Chọn ít nhất một sản phẩm có tên.', 'warn'); return; }
      saveBtn.disabled = true;
      const outs = [], failed = [];
      for (const entry of chosen) {
        try {
          const out = await request('/api/ingest/draft', {method: 'POST', body: pm.draftBody(entry.model, {imageSourceId: multi ? '' : imageId, sourceUrl})});
          outs.push({...out, name: pm.productName(entry)});
        } catch (err) { failed.push({name: pm.productName(entry), err}); }
      }
      if (failed.length) toast(`${failed.length} sản phẩm chưa lưu được: ${failed[0].name} — ${friendlyError(failed[0].err)}`, 'danger');
      if (outs.length) stepSuggest(outs);
      else saveBtn.disabled = false;
    }
  }

  // ------------------------------------------------------------ bước 3: gợi ý SKU
  function stepSuggest(outs) {
    const finish = () => { drawer.close(); if (onDone) onDone(outs[0].id); };
    const imageUse = new Map(outs.map(o => [o.id, !!o.image_saved]));
    const withSuggestions = outs.filter(o => (o.suggestions || []).length);
    const section = out => h('div', {class: 'col gap-3'},
      h('div', {class: 'section-title'}, outs.length > 1 ? `${out.name}: có phải sản phẩm này trong giỏ hàng LIVE?` : 'Có phải sản phẩm này trong giỏ hàng LIVE?'),
      out.image_saved ? h('label', {class: 'row fs-sm'}, h('input', {type: 'checkbox', checked: true, onChange: e => { imageUse.set(out.id, e.target.checked); }}), 'Dùng ảnh vừa chọn làm ảnh sản phẩm khi xác nhận') : null,
      out.suggestions.map(s => h('div', {class: 'list-row', style: {border: '1px solid var(--border)', borderRadius: '12px', padding: '10px 12px'}},
        h('div', {class: 'grow'}, h('div', {class: 'fs-sm clamp-2', title: s.title}, s.title), h('div', {class: 'fs-xs muted'}, pm.suggestionLabel(s)),
          (s.reasons || []).length ? h('div', {class: 'fs-xs muted'}, s.reasons.join(' · ')) : null),
        btn('Đúng, gắn vào đây', {size: 'sm', kind: 'primary', onClick: async e => {
          const button = e.currentTarget;
          button.disabled = true;
          try {
            await request('/api/ingest/link', {method: 'POST', body: {id: out.id, product_id: s.product_id, use_image: imageUse.get(out.id)}});
            toast('Đã ghi nhớ sản phẩm. Duyệt hồ sơ nháp để AI dùng.', 'ok');
            button.textContent = 'Đã gắn';
          } catch (err) { toast(friendlyError(err), 'danger'); button.disabled = false; }
        }}))));
    clear(body,
      notice('ok', outs.length > 1 ? `Đã lưu ${outs.length} hồ sơ NHÁP` : 'Đã lưu hồ sơ NHÁP', 'AI chưa được dùng các hồ sơ này. Điền thêm nếu cần rồi Duyệt ở tab “Gắn hồ sơ cho sản phẩm” → Hồ sơ nháp.'),
      ...(withSuggestions.length ? withSuggestions.map(section)
        : [notice('info', 'Chưa thấy sản phẩm khớp trong giỏ hàng', 'Bạn vẫn có thể gắn sau ở tab “Gắn hồ sơ cho sản phẩm”.')]));
    clear(foot, h('span', {class: 'grow'}), btn(withSuggestions.length ? 'Để sau' : 'Xong', {kind: 'primary', onClick: finish}));
  }

  stepInputs();
  return drawer;
}
