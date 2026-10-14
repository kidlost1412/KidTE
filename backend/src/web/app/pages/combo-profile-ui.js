import {h, clear} from '../core/dom.js';
import {request, friendlyError} from '../core/api.js';
import {btn, field, input, textarea, select, notice, chip} from '../ui/kit.js';
import {openDrawer, toast} from '../ui/overlays.js';
import {profilePicker} from '../ui/profile-picker.js';
import {CUSTOM_MODEL, geminiModelOptions} from './gemini-models.js';

/** Review AI identity suggestions; source doses stay separate and draft-only. */
export function openComboProfile(ctx, product, {onDone, proposal} = {}) {
  const body = h('div', {class: 'card-body col gap-4'}), footer = h('div', {class: 'row'});
  let componentPicker = null;
  const drawer = openDrawer({title: 'AI đề xuất hồ sơ sản phẩm / combo', sub: product.title,
    width: 780, body, footer, onClose: () => componentPicker?.dispose()});
  const closeDrawer = () => { componentPicker?.dispose(); drawer.close(); };
  const provider = select([['auto', 'Ưu tiên Gemini, dự phòng DeepSeek'], ['gemini', 'Gemini'], ['deepseek', 'DeepSeek (chỉ đọc chữ)']], 'auto');
  const model = select([['__settings__', 'Theo Cài đặt Gemini']], '__settings__');
  const extra = textarea({rows: 3, placeholder: 'Bổ sung thành phần combo, số gói/chai, thông tin trên nhãn nếu biết…'});
  const modelField = field('Model Gemini', model);
  provider.addEventListener('change', () => { modelField.hidden = provider.value === 'deepseek'; });
  request('/api/settings/gemini-models').then(out => {
    const selected = model.value;
    clear(model, [['__settings__', 'Theo Cài đặt Gemini'], ...geminiModelOptions(out.models || []).filter(([id]) => id !== CUSTOM_MODEL)]
      .map(([value, label]) => h('option', {value, selected: value === selected}, label)));
  }).catch(() => {});
  clear(body,
    product.image_url ? h('img', {src: product.image_url, alt: product.title, style: {width: '140px', height: '140px', objectFit: 'contain', background: '#fff', borderRadius: '10px'}}) : null,
    field('AI đọc sản phẩm', provider), modelField, field('Thông tin bổ sung', extra),
    notice('info', 'Đề xuất để anh kiểm tra', 'AI đọc listing và ảnh (Gemini) rồi chọn các hồ sơ sản phẩm lẻ. Thành phần và liều dùng được giữ riêng theo từng nguồn; hồ sơ tạo ra vẫn là nháp để anh duyệt.'));
  const read = btn('AI phân tích và đề xuất', {kind: 'primary', icon: 'sparkles', onClick: async () => {
    read.disabled = true; read.textContent = 'AI đang đọc sản phẩm…';
    try {
      const out = await request('/api/ingest/combo/propose', {method: 'POST', timeoutMs: 15 * 60 * 1000,
        body: {product_id: product.product_id, provider: provider.value, text: extra.value,
          ...(model.value !== '__settings__' ? {gemini_model: model.value} : {})}});
      if (body.isConnected) review(out);
    } catch (e) { toast(friendlyError(e), 'danger'); read.disabled = false; read.textContent = 'AI phân tích và đề xuất'; }
  }});
  clear(footer, btn('Đóng', {kind: 'ghost', onClick: closeDrawer}), read);

  function review(out) {
    const chosen = new Set(out.components.map(c => c.id));
    const byId = new Map(out.available_profiles.map(p => [p.id, p]));
    const details = Object.fromEntries(out.components.map(p => [p.id, {quantity: p.quantity ?? null, package: p.proposed_package || p.package || ''}]));
    const packageInput = input({value: out.package || '', placeholder: 'Quy cách bán riêng, ví dụ 1 lít hoặc 500ml'});
    const name = input({value: out.name});
    const notes = textarea({rows: 3, placeholder: 'Ghi chú của anh: xác nhận số lượng, quy cách, nội dung cần bổ sung…'});
    const components = h('div', {class: 'col gap-3'});
    const useImage = h('input', {type: 'checkbox', checked: !!out.image_thumb});
    const save = btn('Lưu hồ sơ nháp cho sản phẩm này', {kind: 'primary', icon: 'save', onClick: async () => {
      if (!chosen.size) { toast('Chọn ít nhất một sản phẩm lẻ để tạo hồ sơ.', 'warn'); return; }
      save.disabled = true;
      try {
        const result = await request('/api/ingest/combo/draft', {method: 'POST', body: {
          proposal_id: out.proposal_id, component_ids: [...chosen], name: name.value,
          notes: notes.value, use_image: useImage.checked, component_details: details, package: packageInput.value,
        }});
        toast('Đã lưu hồ sơ nháp kèm sản phẩm nguồn. Duyệt hồ sơ để gắn vào listing và cho AI sử dụng.', 'ok');
        closeDrawer(); onDone?.(result.id);
      } catch (e) { toast(friendlyError(e), 'danger'); save.disabled = false; }
    }});
    const describe = p => {
      const f = p.fields;
      return ['Thành phần: ' + (f.thanh_phan_dang_ky ? JSON.stringify(f.thanh_phan_dang_ky) : 'chưa có trong nguồn'),
        'Công dụng: ' + (Array.isArray(f.cong_dung) ? f.cong_dung.join(' · ') : f.cong_dung || 'chưa có'),
        'Liều/cách dùng riêng: ' + (Array.isArray(f.huong_dan_su_dung) ? f.huong_dan_su_dung.join('\n') : f.huong_dan_su_dung || 'chưa có trong nguồn'),
        f.luu_y ? 'Lưu ý: ' + f.luu_y : ''].filter(Boolean).join('\n');
    };
    function paintComponents() {
      clear(components, [...chosen].map(id => {
        const p = byId.get(id);
        const detail = details[id] ||= {quantity: null, package: p.proposed_package || p.package || ''};
        const quantity = input({type: 'number', min: 1, max: 999, value: detail.quantity ?? '', placeholder: 'Chưa rõ'});
        quantity.addEventListener('input', () => { detail.quantity = quantity.value ? Number(quantity.value) : null; });
        const pack = input({value: detail.package, placeholder: 'Dung tích/khối lượng mỗi chai/gói'});
        pack.addEventListener('input', () => { detail.package = pack.value; });
        return h('div', {class: 'card card-body col gap-3'},
          h('div', {class: 'row'}, p.image_url ? h('img', {src: p.image_url, alt: '', style: {width: '60px', height: '60px', objectFit: 'contain'}}) : null,
            h('strong', {class: 'grow'}, p.name + (p.package ? ` (${p.package})` : '')),
            btn('Bỏ', {size: 'sm', kind: 'ghost', onClick: () => { chosen.delete(id); paintComponents(); }})),
          out.is_combo !== false ? h('div', {class: 'form-grid'}, field('Số chai/gói trong combo', quantity), field('Quy cách mỗi chai/gói', pack)) : null,
          p.reason ? h('p', {class: 'fs-sm'}, 'AI nhận diện: ' + p.reason) : null,
          h('pre', {class: 'fs-xs', style: {whiteSpace: 'pre-wrap', margin: '0', overflowWrap: 'anywhere'}}, describe(p)));
      }));
      save.disabled = !chosen.size;
    }
    componentPicker?.dispose();
    componentPicker = profilePicker(out.available_profiles, '', {label: 'Thêm sản phẩm lẻ vào combo', onChange: id => {
      if (id) { chosen.add(id); paintComponents(); }
    }});
    clear(body, chip(`Đã đọc bằng ${out.model || out.provider}`, 'info'),
      out.fallback_note ? notice('warn', 'Đã chuyển nhà cung cấp', out.fallback_note) : null,
      out.summary ? notice('info', 'AI đề xuất cách tạo hồ sơ', out.summary) : null,
      out.questions.length ? notice('warn', 'Cần anh xác nhận/bổ sung', out.questions.join(' · ')) : null,
      !chosen.size ? notice('warn', 'Chưa xác định được sản phẩm lẻ', 'AI chưa có đủ bằng chứng. Chọn hồ sơ nguồn nếu anh biết, hoặc bổ sung nhãn/tài liệu rồi đọc lại.') : null,
      out.variant ? notice('info', 'Tạo hồ sơ quy cách riêng', `Dùng công thức nguồn và tạo quy cách ${out.package}. Liều vẫn tính theo lượng dùng; không nhân theo kích thước chai.`) : null,
      field('Tên hồ sơ đề xuất', name), out.is_combo === false ? field('Quy cách bán', packageInput) : null, field('Thêm sản phẩm lẻ (có ảnh và tìm tên)', componentPicker.node), components,
      out.image_thumb ? h('label', {class: 'row'}, useImage,
        h('img', {src: out.image_thumb, alt: 'Ảnh listing dùng cho hồ sơ', style: {width: '90px', height: '90px', objectFit: 'contain'}}), 'Lưu ảnh listing vào hồ sơ nháp') : null,
      field('Ghi chú của chủ shop', notes),
      notice('warn', 'Liều dùng giữ riêng cho từng sản phẩm', 'Chưa có xác nhận dùng/pha chung. Không tự cộng liều hoặc hàm lượng. Thiếu liều nào cần bổ sung nguồn cho sản phẩm lẻ đó trước khi duyệt.'));
    paintComponents();
    clear(footer, btn('Đóng', {kind: 'ghost', onClick: closeDrawer}), save);
  }
  if (proposal) review(proposal);
  return drawer;
}

/** One model request per bounded group, with independent review and retry of missing items. */
export function openComboProfiles(ctx, products, {onDone} = {}) {
  const body = h('div', {class: 'card-body col gap-4'}), footer = h('div', {class: 'row wrap'});
  const drawer = openDrawer({title: 'AI đề xuất hàng loạt', sub: `${products.length} sản phẩm đã chọn`, width: 900, body, footer});
  const provider = select([['auto', 'Ưu tiên Gemini, dự phòng DeepSeek'], ['gemini', 'Gemini'], ['deepseek', 'DeepSeek (chỉ đọc chữ)']], 'auto');
  const model = select([['__settings__', 'Theo Cài đặt Gemini']], '__settings__');
  const extra = textarea({rows: 3, placeholder: 'Thông tin bổ sung chung cho các sản phẩm đã chọn…'});
  const modelField = field('Model Gemini', model);
  provider.addEventListener('change', () => { modelField.hidden = provider.value === 'deepseek'; });
  request('/api/settings/gemini-models').then(out => clear(model,
    [['__settings__', 'Theo Cài đặt Gemini'], ...geminiModelOptions(out.models || []).filter(([id]) => id !== CUSTOM_MODEL)]
      .map(([value, label]) => h('option', {value}, label)))).catch(() => {});
  const results = new Map(), errors = new Map(), chosen = new Set(), saved = new Set();
  let busy = false, calls = 0;
  const read = btn('AI đề xuất cho tất cả đã chọn', {kind: 'primary', icon: 'sparkles', onClick: () => analyze(products.map(p => p.product_id))});
  clear(body, field('AI đọc sản phẩm', provider), modelField, field('Thông tin bổ sung', extra),
    notice('info', 'Đọc chung để giảm lượt gọi', 'Gửi danh mục hồ sơ nguồn chung và ảnh của từng sản phẩm trong cùng yêu cầu. Tối đa 20 sản phẩm mỗi nhóm; chỉ chia nhỏ thêm khi ảnh quá lớn.'),
    h('div', {class: 'col gap-3'}, products.map(p => h('div', {class: 'row'},
      p.image_url ? h('img', {src: p.image_url, alt: '', style: {width: '48px', height: '48px', objectFit: 'contain'}}) : null, p.title))));
  clear(footer, btn('Đóng', {kind: 'ghost', onClick: () => drawer.close()}), read);
  async function analyze(ids) {
    if (busy) return;
    busy = true; read.disabled = true; read.textContent = 'AI đang đọc chung…';
    try {
      const out = await request('/api/ingest/combo/propose-batch', {method: 'POST', timeoutMs: 15 * 60 * 1000,
        body: {product_ids: ids, provider: provider.value, text: extra.value,
          ...(model.value !== '__settings__' ? {gemini_model: model.value} : {})}});
      calls += out.ai_calls || 0;
      for (const p of out.products || []) { results.set(p.product_id, p); errors.delete(p.product_id); }
      for (const e of out.errors || []) errors.set(e.product_id, e.error);
      if (body.isConnected) paint();
    } catch (e) { toast(friendlyError(e), 'danger'); }
    finally { busy = false; read.disabled = false; read.textContent = 'AI đề xuất cho tất cả đã chọn'; if (body.isConnected && (results.size || errors.size)) paint(); }
  }
  function paint() {
    clear(body, chip(`Đã có ${results.size}/${products.length} đề xuất · ${calls} lượt yêu cầu AI`, 'info'),
      notice('info', 'Duyệt rồi chọn hồ sơ cần lưu', 'Xem thành phần, liều riêng và ảnh từng sản phẩm. Lưu nháp không gọi AI thêm. Hồ sơ vẫn cần được duyệt trước khi gắn.'),
      products.map(product => {
        const out = results.get(product.product_id), id = product.product_id;
        const pick = h('input', {type: 'checkbox', checked: chosen.has(id), disabled: busy || !out?.components.length || saved.has(id),
          'aria-label': 'Chọn lưu nháp: ' + product.title, onChange: e => { e.target.checked ? chosen.add(id) : chosen.delete(id); paint(); }});
        return h('div', {class: 'card card-body col gap-3'}, h('div', {class: 'row'}, pick,
          (out?.image_thumb || product.image_url) ? h('img', {src: out?.image_thumb || product.image_url, alt: '', style: {width: '64px', height: '64px', objectFit: 'contain'}}) : null,
          h('strong', {class: 'grow'}, out?.name || product.title),
          saved.has(id) ? chip('Đã lưu nháp', 'ok') : out ? btn('Xem / chỉnh đề xuất', {size: 'sm', disabled: busy, onClick: () => openComboProfile(ctx, product, {
            proposal: out, onDone: () => { saved.add(id); chosen.delete(id); paint(); onDone?.(); }
          })}) : null),
          out ? h('p', {class: 'fs-sm'}, out.components.map(c => `${c.quantity ? c.quantity + ' × ' : ''}${c.name}${c.proposed_package ? ' · ' + c.proposed_package : ''}`).join(' + ') || 'Chưa xác định được hồ sơ nguồn') : null,
          out?.summary ? h('p', {class: 'fs-sm muted'}, out.summary) : null,
          out?.questions.length ? notice('warn', 'Cần xác nhận', out.questions.join(' · ')) : null,
          out?.fallback_note ? notice('warn', 'Đã dùng AI dự phòng', out.fallback_note) : null,
          errors.has(id) ? notice('warn', 'Chưa đọc được', errors.get(id)) : null);
      }));
    const save = btn(`Lưu nháp đã chọn (${chosen.size})`, {kind: 'primary', disabled: busy || !chosen.size, onClick: async () => {
      if (busy) return;
      busy = true; paint();
      let count = 0;
      for (const id of [...chosen]) {
        const out = results.get(id);
        try {
          await request('/api/ingest/combo/draft', {method: 'POST', body: {proposal_id: out.proposal_id,
            component_ids: out.components.map(c => c.id), name: out.name, use_image: !!out.image_thumb, package: out.package,
            component_details: Object.fromEntries(out.components.map(c => [c.id, {quantity: c.quantity ?? null, package: c.proposed_package || c.package || ''}]))}});
          saved.add(id); chosen.delete(id); count++;
        } catch (e) { toast(`${out.name}: ${friendlyError(e)}`, 'danger'); }
      }
      if (count) { toast(`Đã lưu ${count} hồ sơ nháp kèm ảnh.`, 'ok'); onDone?.(); }
      busy = false;
      if (body.isConnected) paint();
    }});
    const retry = btn(`Đọc lại mục lỗi (${errors.size})`, {disabled: busy, onClick: async () => {
      retry.disabled = true; retry.textContent = 'AI đang đọc lại mục lỗi…';
      await analyze([...errors.keys()]);
      retry.disabled = false;
    }});
    clear(footer, btn('Đóng', {kind: 'ghost', onClick: () => drawer.close()}), errors.size ? retry : null, save);
  }
  return drawer;
}
