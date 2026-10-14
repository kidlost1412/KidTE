import {h, clear, disposer} from '../core/dom.js';
import {icon} from '../core/icons.js';
import {request, friendlyError} from '../core/api.js';
import {waitJob} from '../core/jobs.js';
import {relative, dateTime, bytes, number} from '../core/format.js';
import {btn, chip, empty, errorBox, skeletonRows, pageHead, tabs, field, input, textarea, notice, kv, card, segmented, select, progress} from '../ui/kit.js';
import {toast, confirmDialog, promptDialog, openDrawer} from '../ui/overlays.js';
import {applyTheme} from '../shell.js';
import {RESOURCE_NAME} from '../labels.js';
import {liveSettings} from './live-settings.js';
import {kindLabel, kindNote, normalizeV2, passwordProblem, exportIndexedDb, startV2, selectionIds, deleteV2, restoreV2Visible, confirmProblem, restoreErrorText, restoreV2, reenableAi} from './backup-v2.js';
import {updates} from './updates.js';
import {defaultProfilesPanel} from './product-defaults-ui.js';
import {CUSTOM_MODEL, geminiModelOptions, chosenGeminiModel} from './gemini-models.js';

const TABS = [
  {id: 'setup', label: 'Cài đặt', icon: 'key'},
  {id: 'live', label: 'Nguồn LIVE theo sàn', icon: 'film'},
  {id: 'audio', label: 'Âm thanh', icon: 'volume'},
  {id: 'whisper', label: 'Chép lời', icon: 'mic'},
  {id: 'updates', label: 'Cập nhật', icon: 'download'},
  {id: 'diagnostics', label: 'Chẩn đoán', icon: 'shield'},
  {id: 'backups', label: 'Sao lưu', icon: 'archive'},
  {id: 'trash', label: 'Thùng rác', icon: 'trash'},
  {id: 'logs', label: 'Nhật ký', icon: 'terminal'},
  {id: 'appearance', label: 'Giao diện', icon: 'sun'},
];

export default {
  mount(ctx) {
    const d = disposer();
    const tab = TABS.some(t => t.id === ctx.route.sub) ? ctx.route.sub : 'setup';
    const host = h('div', {style: {marginTop: '16px'}}, skeletonRows(4, 80));
    clear(ctx.shell.main, h('div', {class: 'page'},
      pageHead('Cài đặt & hệ thống', 'AI dùng chung, nguồn LIVE riêng cho TikTok và Shopee, chép lời, sao lưu và khôi phục.'),
      tabs(TABS, tab, id => ctx.navigate('system', id)), host));
    ({setup, live: liveSettings, audio, whisper, updates, diagnostics, backups, trash, logs, appearance})[tab](ctx, host, d);
    return {unmount: () => d.dispose()};
  },
};

// ---------------------------------------------------------------- Cài đặt
async function setup(ctx, host) {
  let s;
  try { s = await request('/api/settings'); } catch (e) { clear(host, errorBox(friendlyError(e), () => setup(ctx, host))); return; }
  const dash = ctx.store.get().dashboard || {};
  const labels = s.model_labels || {};
  const model = select((s.models || []).map(m => [m, labels[m] || m]), s.selected_model);
  const key = input({type: 'password', placeholder: s.api_key_configured ? '•••••••• (đã cấu hình — để trống nếu không đổi)' : 'Dán khoá API DeepSeek (sk-…)', autocomplete: 'off'});
  // Kiểm tra khoá: gọi một lời gọi rẻ (không đốt token); khoá để trống = kiểm khoá đã lưu. Không bao giờ hiện lại khoá.
  const keyStatus = (provider) => h('div', {class: 'fs-sm', 'data-key-status': provider, role: 'status'});
  const showKeyStatus = (box, out) => {
    const tone = out.status === 'ok' ? 'ok' : (out.status === 'quota' ? 'warn' : 'danger');
    clear(box, notice(tone, out.status === 'ok' ? 'Khoá dùng được' : 'Khoá chưa dùng được', out.message || ''));
  };
  const runKeyCheck = async (provider, box, typed) => {
    clear(box, h('span', {class: 'muted'}, 'Đang kiểm tra khoá…'));
    try {
      const out = await request('/api/settings/check-key', {method: 'POST', body: {provider, api_key: typed || ''}, timeoutMs: 25000});
      showKeyStatus(box, out);
      return out;
    } catch (e) { clear(box, notice('danger', 'Không kiểm tra được', friendlyError(e))); return null; }
  };
  const dsStatus = keyStatus('deepseek');
  const checkDs = btn('Kiểm tra key', {kind: 'ghost', icon: 'shield', onClick: () => runKeyCheck('deepseek', dsStatus, key.value.trim())});
  const saveAi = btn('Lưu cài đặt AI', {kind: 'primary', icon: 'save', onClick: async () => {
    const body = {model: model.value};
    const typed = key.value.trim();
    if (typed) body.api_key = typed;
    try {
      await request('/api/settings', {method: 'POST', body});
      key.value = ''; toast('Đã lưu cài đặt AI.', 'ok');
      if (typed) { await runKeyCheck('deepseek', dsStatus, ''); return; }   // hiện kết quả kiểm khoá ngay dưới nút, không vẽ lại trang
      setup(ctx, host);
    } catch (e) { toast(friendlyError(e), 'danger'); }
  }});
  const gemKey = input({type: 'password', placeholder: s.gemini_key_configured ? '•••••••• (đã cấu hình — để trống nếu không đổi)' : 'Dán khoá Gemini (AIza…)', autocomplete: 'off'});
  let gemCatalog = [];
  const gemModel = select(geminiModelOptions([], s.gemini_model || ''), s.gemini_model || '', {'aria-label': 'Model Gemini'});
  const gemCustom = input({value: s.gemini_model || '', placeholder: 'Tên model do Google cung cấp', hidden: true, 'aria-label': 'Tên model Gemini khác'});
  const gemListStatus = h('div', {class: 'field-hint', role: 'status'}, 'Đang tải danh sách model từ Google…');
  const gemEffective = h('p', {class: 'fs-sm soft', 'data-gemini-model': ''});
  const paintEffective = out => {
    gemEffective.textContent = `Model sẽ thử: ${out.effective_model || s.gemini_model_effective || '—'}` +
      (out.model_source === 'owner' ? ' (bạn chọn)' : ' (tự động / dự phòng)');
  };
  paintEffective({effective_model: s.gemini_model_effective, model_source: s.gemini_model_source});
  gemModel.addEventListener('change', () => { gemCustom.hidden = gemModel.value !== CUSTOM_MODEL; });
  const loadGeminiModels = async refresh => {
    gemRefresh.disabled = true;
    gemListStatus.textContent = 'Đang tải danh sách model từ Google…';
    try {
      const out = await request('/api/settings/gemini-models', {method: 'POST',
        body: {api_key: gemKey.value.trim(), refresh: !!refresh}, timeoutMs: 90000});
      const selected = gemModel.value;
      gemCatalog = out.models || [];
      clear(gemModel, geminiModelOptions(gemCatalog, selected === CUSTOM_MODEL ? '' : selected)
        .map(([value, label]) => h('option', {value, selected: value === selected}, label)));
      gemModel.value = selected;
      gemListStatus.textContent = out.error || `Có ${gemCatalog.length} model Flash để chọn. Chọn bản cố định để tránh tự chuyển lên bản mới hơn.`;
      paintEffective(out);
    } catch (e) { gemListStatus.textContent = friendlyError(e); }
    finally { gemRefresh.disabled = false; }
  };
  const gemRefresh = btn('Tải lại danh sách model', {kind: 'ghost', size: 'sm', icon: 'refresh', onClick: () => loadGeminiModels(true)});
  const gemModelField = field('Model Gemini', gemModel, {hint: 'Bạn có thể chọn phiên bản cũ hơn ngay trong danh sách. Với bản Flash cố định, dự phòng chỉ thử các bản cùng phiên bản hoặc cũ hơn; Google vẫn có thể báo quá tải.'});
  gemModelField.append(gemCustom, gemListStatus, gemRefresh);
  const gemStatus = keyStatus('gemini');
  const checkGem = btn('Kiểm tra key', {kind: 'ghost', icon: 'shield', onClick: () => runKeyCheck('gemini', gemStatus, gemKey.value.trim())});
  const saveGemini = btn('Lưu cài đặt Gemini', {kind: 'primary', icon: 'save', onClick: async () => {
    const body = {};
    const typed = gemKey.value.trim();
    if (typed) body.gemini_api_key = typed;
    const chosen = chosenGeminiModel(gemModel.value, gemCustom.value);
    if (gemModel.value === CUSTOM_MODEL && !chosen) { toast('Nhập tên model hoặc chọn một model trong danh sách.', 'warn'); return; }
    if (chosen !== (s.gemini_model || '')) body.gemini_model = chosen;
    if (!Object.keys(body).length) { toast('Chưa có gì để lưu.', 'info'); return; }
    try {
      await request('/api/settings', {method: 'POST', body});
      gemKey.value = ''; toast('Đã lưu cài đặt Gemini.', 'ok');
      if (typed) { await runKeyCheck('gemini', gemStatus, ''); }
      setup(ctx, host);
    } catch (e) { toast(friendlyError(e), 'danger'); }
  }});
  const userIds = input({value: s.tiktok_user_ids || '', placeholder: 'vd 7496342894195278856, …'});
  const uniqueIds = input({value: s.tiktok_unique_ids || '', placeholder: 'vd tenshopcuaban, …'});
  const saveTiktok = btn('Lưu tài khoản', {kind: 'primary', icon: 'save', onClick: async () => {
    try { await request('/api/settings', {method: 'POST', body: {tiktok_user_ids: userIds.value, tiktok_unique_ids: uniqueIds.value}}); toast('Đã lưu. Bình luận của các tài khoản này được coi là của chủ shop.', 'ok'); } catch (e) { toast(friendlyError(e), 'danger'); }
  }});
  const source = input({value: s.knowledge_source || '', placeholder: 'Đường dẫn file gói kiến thức (.json) hoặc thư mục chia sẻ'});
  const pkgOut = h('div', {class: 'col gap-3'});
  const seedInfo = s.knowledge_seed || {};
  const seedBanner = (seedInfo.state === 'newer' || seedInfo.state === 'missing') ? h('div', {class: 'col gap-3'},
    notice('info', 'Có gói hồ sơ chính thức mới hơn đi kèm bản cài',
      seedInfo.state === 'missing' ? `Máy chưa có hồ sơ sản phẩm. Gói đi kèm phiên bản ${seedInfo.seed_version}.`
        : `Đang dùng ${seedInfo.installed_version || 'bản cũ'}, gói đi kèm là ${seedInfo.seed_version}. Thông tin bạn đã sửa trên máy được giữ nguyên.`,
      h('div', {class: 'row'},
        btn('Xem thay đổi', {kind: 'ghost', icon: 'eye', onClick: async () => {
          try {
            const out = await request('/api/knowledge/update', {method: 'POST', body: {seed: true, che_do: 'xem_truoc'}});
            const t = (out.khac_biet && out.khac_biet.tom_tat) || {};
            clear(seedOut, notice('info', `Gói chính thức ${out.version}`, `Thêm mới ${t.them_moi || 0} · thay đổi ${t.thay_doi || 0} · giữ nguyên ${t.giu_nguyen || 0} · xung đột ${t.xung_dot || 0} (giữ bản trên máy). Có thể hoàn tác sau khi cập nhật.`));
          } catch (e) { clear(seedOut, notice('danger', 'Không đọc được gói đi kèm', friendlyError(e))); }
        }}),
        btn('Cập nhật', {kind: 'primary', onClick: async () => {
          try { await request('/api/knowledge/update', {method: 'POST', body: {seed: true}}); toast('Đã cập nhật hồ sơ chính thức. Có thể “Hoàn tác lần cập nhật”.', 'ok'); setup(ctx, host); }
          catch (e) { toast(friendlyError(e), 'danger'); }
        }})))) : null;
  const seedOut = h('div', {class: 'col gap-3'});
  const ext = dash.extension || {};

  clear(host, h('div', {class: 'settings-grid'},
    card('Về ứng dụng', h('div', {class: 'card-body col gap-3'},
      kv([['Phiên bản', s.version], ['Thư mục dữ liệu', h('span', {style: {overflowWrap: 'anywhere'}}, s.data_root)]]),
      h('p', {class: 'fs-xs muted'}, 'Toàn bộ dữ liệu của máy này (khoá DeepSeek, khách hàng, Trí nhớ AI, tài liệu, video, sao lưu, model chép lời) nằm trong thư mục này. Máy khác cài Kid TE là máy trắng — tự nhập khoá, thông tin shop và dạy AI từ đầu.')),
    {footer: btn('Mở thư mục dữ liệu', {kind: 'ghost', icon: 'archive', onClick: () => openTarget('data')})}),
    card('AI trả lời bình luận', h('div', {class: 'card-body form-grid'},
      dash.ai && dash.ai.deepseek_available ? notice('ok', 'AI đang sẵn sàng', `${labels[s.selected_model] || s.selected_model}. ${s.model_error ? 'Lỗi lấy danh sách model: ' + s.model_error : ''}`) : notice('danger', 'AI chưa sẵn sàng', 'Nhập khoá API DeepSeek để AI có thể trả lời khách.'),
      field('Model trả lời khách', model, {hint: 'Từ 10/09/2026 “deepseek-flash” CHÍNH LÀ DeepSeek V4.1 Flash — nhanh hơn, rẻ hơn và trả lời tốt hơn V4 Pro. Việc HỌC (rút kiến thức, học lại) chọn model riêng ở trang Trí nhớ AI. Độ dài câu, độ sáng tạo, lời nhắc: Trợ lý tự động → Nhà cung cấp DeepSeek.'}),
      field('Khoá API DeepSeek', key, {hint: 'Khoá được lưu trong file .env trên máy này, không hiển thị lại. “Kiểm tra key” thử khoá đang nhập (để trống = khoá đã lưu) và cho biết còn tiền không.'}),
      dsStatus),
    {sub: 'DeepSeek', footer: [checkDs, saveAi]}),
    card('Đọc ảnh & PDF (Gemini)', h('div', {class: 'card-body form-grid'},
      notice(s.gemini_key_configured ? 'ok' : 'info', s.gemini_key_configured ? 'Gemini sẵn sàng' : 'Chưa có khoá Gemini', 'Dùng để “Thêm sản phẩm bằng AI” đọc ảnh nhãn và PDF scan. Không có khoá thì chỉ đọc được file có chữ, địa chỉ web và chữ dán (qua DeepSeek).'),
      field('Key Gemini (đọc ảnh/PDF)', gemKey, {hint: 'Khoá lưu trong file .env trên máy này, không hiển thị lại.'}),
      gemStatus,
      h('div', {}, btn('Lấy key miễn phí', {kind: 'ghost', size: 'sm', icon: 'arrowRight', onClick: () => openTarget('gemini_keys')})),
      gemEffective, gemModelField),
    {sub: 'Google AI Studio', footer: [checkGem, saveGemini]}),
    card('Thông tin shop & sổ tay bán hàng', h('div', {class: 'card-body col gap-3'},
      h('p', {class: 'fs-sm soft'}, 'Thông tin shop (địa chỉ, hotline, giao hàng) và sổ tay tình huống bán hàng nằm ở hai trang riêng trong thanh bên.'),
      h('div', {class: 'row wrap'},
        btn('Mở Thông tin shop', {kind: 'ghost', icon: 'home', onClick: () => ctx.navigate('shop-info')}),
        btn('Mở Sổ tay tình huống', {kind: 'ghost', icon: 'fileText', onClick: () => ctx.navigate('playbook')})))),
    card('Tài khoản TikTok của chủ shop', h('div', {class: 'card-body form-grid'},
      h('p', {class: 'fs-sm soft'}, 'Để hệ thống biết bình luận nào là do chủ shop/host gõ (không phải khách) và không trả lời nhầm chính mình.'),
      field('Mã người dùng (user id)', userIds, {hint: 'Nhiều mã cách nhau bằng dấu phẩy.'}),
      field('Tên tài khoản (@unique id)', uniqueIds)), {footer: saveTiktok}),
    card('Tiện ích Chrome · TikTok', h('div', {class: 'card-body col gap-3'},
      ext.connected ? notice('ok', 'Tiện ích đang kết nối', `Tín hiệu cuối ${Math.round(ext.age_s || 0)} giây trước · AI ${ext.ai_enabled ? 'bật' : 'tắt'} trong tiện ích`) : notice('warn', 'Chưa thấy tín hiệu tiện ích', 'Cài tiện ích Kid TE vào Chrome và mở trang TikTok LIVE Studio / LIVE của Shop.'),
      h('ol', {class: 'list-lines'}, h('li', {}, 'Bấm “Mở hướng dẫn cài tiện ích” bên dưới — Chrome mở trang Tiện ích.'), h('li', {}, 'Bật “Chế độ nhà phát triển” ở góc phải.'), h('li', {}, 'Bấm “Tải tiện ích đã giải nén” và chọn thư mục extension được mở sẵn.'), h('li', {}, 'Mở trang LIVE của TikTok — trạng thái ở trên sẽ chuyển xanh.'))),
    {footer: btn('Mở hướng dẫn cài tiện ích', {icon: 'puzzle', onClick: async () => { try { await request('/api/extension/setup', {method: 'POST', body: {}}); toast('Đã mở Chrome và thư mục tiện ích.', 'ok'); } catch (e) { toast(friendlyError(e), 'danger'); } }})}),
    card('Tiện ích Chrome · Shopee', h('div', {class: 'card-body col gap-3'},
      notice(dash.live_channels?.shopee?.extension?.connected ? 'ok' : 'warn',
        dash.live_channels?.shopee?.extension?.connected ? 'Shopee đã kết nối' : 'Chờ tiện ích Shopee',
        'Cài tiện ích từ thư mục extension-shopee, mở trang quản lý Shopee LIVE. Tiện ích tự đọc giỏ, ghim theo video riêng và trả lời bằng kiến thức dùng chung.'),
      btn('Cài nguồn OBS cho Shopee', {kind:'ghost',onClick:()=>ctx.navigate('system','live')})),
      {footer:btn('Mở hướng dẫn cài tiện ích Shopee',{icon:'puzzle',onClick:async()=>{
        try {await request('/api/extension/setup',{method:'POST',body:{platform:'shopee'}});toast('Đã mở thư mục tiện ích Shopee.','ok');}
        catch(error){toast(friendlyError(error),'danger');}
      }})}),
    card('Gói kiến thức sản phẩm dùng chung', h('div', {class: 'card-body form-grid'},
      h('p', {class: 'fs-sm soft'}, 'Cập nhật hồ sơ sản phẩm từ gói do công ty phát hành. Thông tin bạn đã sửa trên máy được giữ nguyên nếu có xung đột.'),
      defaultProfilesPanel(ctx), seedBanner, seedOut,
      kv([['Đang dùng', s.knowledge_active_file]]),
      field('Nguồn gói', source), pkgOut),
    {footer: [
      btn('Xuất gói từ máy này', {kind: 'ghost', icon: 'upload', onClick: async () => {
        const version = await promptDialog({title: 'Xuất hồ sơ sản phẩm của máy này', text: 'Tạo file gói để cài sang máy khác (máy kia dán đường dẫn file vào ô “Nguồn gói”). Trường riêng của máy không được đưa vào.', label: 'Tên phiên bản', value: new Date().toISOString().slice(0, 10), confirm: 'Xuất'});
        if (!version) return;
        try { const out = await request('/api/knowledge/export', {method: 'POST', body: {version}}); clear(pkgOut, notice('ok', `Đã xuất ${out.products} hồ sơ sản phẩm`, out.path)); } catch (e) { toast(friendlyError(e), 'danger'); }
      }}),
      btn('Hoàn tác lần cập nhật', {kind: 'ghost', icon: 'undo', onClick: async () => { const ok = await confirmDialog({title: 'Hoàn tác lần cập nhật gói kiến thức gần nhất?', confirm: 'Hoàn tác'}); if (!ok) return; try { await request('/api/knowledge/undo', {method: 'POST', body: {}}); toast('Đã hoàn tác.', 'ok'); } catch (e) { toast(friendlyError(e), 'danger'); } }}),
      btn('Xem trước', {icon: 'eye', onClick: async () => {
        try {
          if (source.value.trim() !== (s.knowledge_source || '')) await request('/api/settings', {method: 'POST', body: {knowledge_source: source.value.trim()}});
          const out = await request('/api/knowledge/update', {method: 'POST', body: {source: source.value.trim(), che_do: 'xem_truoc'}});
          const t = (out.khac_biet && out.khac_biet.tom_tat) || {};
          clear(pkgOut, notice('info', `Gói phiên bản ${out.version}`, `Thêm mới ${t.them_moi || 0} · thay đổi ${t.thay_doi || 0} · giữ nguyên ${t.giu_nguyen || 0} · xung đột ${t.xung_dot || 0} (giữ bản trên máy)`),
            btn('Cập nhật theo gói này', {kind: 'primary', onClick: async () => { try { await request('/api/knowledge/update', {method: 'POST', body: {source: source.value.trim()}}); toast('Đã cập nhật gói kiến thức.', 'ok'); clear(pkgOut); } catch (e) { toast(friendlyError(e), 'danger'); } }}));
        } catch (e) { clear(pkgOut, notice('danger', 'Không đọc được gói', friendlyError(e))); }
      }}),
    ]})));
  loadGeminiModels(false);
}

// ---------------------------------------------------------------- Âm thanh
async function audio(ctx, host) {
  let s;
  try { s = await request('/api/settings'); } catch (e) { clear(host, errorBox(friendlyError(e), () => audio(ctx, host))); return; }
  const channel = (await request('/api/live/sources')).channels.tiktok.settings;
  const devices = s.audio_devices || [];
  const loopback = devices.filter(x => x.loopback);
  const inputs = devices.filter(x => !x.loopback);
  const row = dev => {
    const selected = channel.mode === 'device' && channel.device_id === dev.id;
    return h('div', {class: 'device-row' + (dev.disabled ? ' disabled' : '')},
      h('span', {}, icon(dev.loopback ? 'volume' : 'mic', {size: 18})),
      h('div', {style: {minWidth: '0'}}, h('strong', {class: 'fs-sm'}, dev.name), h('div', {class: 'fs-xs muted'}, dev.disabled ? dev.reason : dev.loopback ? 'Âm thanh máy đang phát (dùng cho đồng bộ video)' : 'Micro / nguồn thu vào')),
      dev.loopback && !dev.disabled ? h('div', {class: 'row'},
        btn('Nghe thử', {size: 'sm', icon: 'play', onClick: async e => {
          const b = e.currentTarget; b.disabled = true;
          try { const out = await request('/api/audio/test', {method: 'POST', body: {audio_device_id: dev.id}}); toast(out.message, out.heard ? 'ok' : 'warn'); } catch (err) { toast(friendlyError(err), 'danger'); } finally { b.disabled = false; }
        }}),
        selected ? chip('Đang dùng', 'ok') : btn('Dùng thiết bị này', {size: 'sm', kind: 'primary', onClick: async () => {
          try { await request('/api/live/sources', {method: 'POST', body: {platform:'tiktok',settings:{mode:'device',device_id:dev.id}}}); toast('Đã chọn nguồn riêng cho TikTok.', 'ok'); audio(ctx, host); } catch (err) { toast(friendlyError(err), 'danger'); }
        }})) : null);
  };
  clear(host, h('div', {class: 'col gap-4'},
    notice('info', 'Thiết bị âm thanh riêng cho TikTok', 'Mặc định Kid TE nghe riêng tiến trình TikTok Studio. Chỉ chọn thiết bị ở đây khi đường âm của nó dành riêng cho TikTok. Shopee đọc Media Source OBS ở trang Nguồn LIVE theo sàn.'),
    card(`Nguồn âm máy đang phát (${loopback.length})`, h('div', {}, loopback.length ? loopback.map(row) : empty('Không tìm thấy thiết bị', 'Kiểm tra driver âm thanh.')), {sub: s.selected_audio_device === 'auto' ? 'Đang để tự động chọn' : ''}),
    inputs.length ? card(`Nguồn thu vào (${inputs.length})`, h('div', {}, inputs.map(row)), {sub: 'Không dùng cho đồng bộ video'}) : null));
}

// ---------------------------------------------------------------- Chép lời
async function whisper(ctx, host, d) {
  let s = null, timer = null;
  const QUALITY = ['', 'Kém', 'Tạm', 'Khá', 'Tốt', 'Rất tốt'];
  const SPEED = ['', 'Rất chậm', 'Chậm', 'Vừa', 'Nhanh', 'Rất nhanh'];
  const custom = input({placeholder: 'D:\\models\\faster-whisper-large-v3-turbo'});

  async function call(path, body, okText) {
    try { s = await request('/api/system/whisper' + path, {method: 'POST', body: body || {}}); if (okText) toast(okText, 'ok'); render(); }
    catch (e) { toast(friendlyError(e), 'danger'); }
  }
  async function load() {
    try { s = await request('/api/system/whisper'); } catch (e) { clear(host, errorBox(friendlyError(e), load)); return; }
    render();
  }
  function schedule() {
    clearTimeout(timer);
    if (s && ((s.download && s.download.state === 'downloading') || (s.hardware && s.hardware.probing))) timer = setTimeout(load, s.hardware && s.hardware.probing ? 1000 : 1500);
  }
  d.add(() => clearTimeout(timer));

  function modelRow(m) {
    const job = s.download || {};
    const mine = job.model === m.id;
    const downloading = mine && job.state === 'downloading';
    const pct = mine && job.total ? Math.round(job.done / job.total * 100) : 0;
    const busyOther = job.state === 'downloading' && !mine;
    const recommended = s.recommended === m.id;
    return h('div', {class: 'model-row' + (m.active ? ' active' : '')},
      h('div', {class: 'grow col gap-1'},
        h('div', {class: 'row wrap', style: {gap: '8px'}}, h('strong', {}, m.name),
          recommended ? chip('Hợp với máy này', 'ok', {iconName: 'star'}) : null,
          m.active ? chip('Đang dùng', 'info', {dot: true}) : null,
          !m.fits ? chip('Máy này hơi yếu cho model này', 'warn') : null),
        h('div', {class: 'fs-xs soft'}, m.note),
        h('div', {class: 'fs-xs muted'}, `Tải ${bytes(m.bytes)} · cần RAM ~${m.ram_gb} GB · chính xác: ${QUALITY[m.quality]} · tốc độ: ${SPEED[m.speed]} · video 60 phút ≈ ${m.minutes_per_hour} phút chép lời trên máy này`),
        downloading ? h('div', {class: 'col gap-1'}, progress(pct), h('div', {class: 'fs-xs muted'}, `${pct}% · ${bytes(job.done)} / ${bytes(job.total)}${job.file ? ' · ' + job.file : ''}`)) : null,
        mine && job.state === 'error' ? notice('danger', null, job.error) : null,
        mine && job.state === 'cancelled' ? h('div', {class: 'fs-xs muted'}, 'Đã dừng — bấm Tải tiếp, phần đã tải được giữ.') : null,
        m.installed ? h('div', {class: 'fs-xs muted', style: {overflowWrap: 'anywhere'}}, `Trên máy: ${bytes(m.disk_bytes)} · ${m.path}`) : null),
      h('div', {class: 'col gap-2', style: {alignItems: 'flex-end'}},
        downloading ? btn('Dừng', {size: 'sm', kind: 'ghost', icon: 'pause', onClick: () => call('/cancel')})
          : !m.installed ? btn(m.partial ? 'Tải tiếp' : 'Tải về', {size: 'sm', kind: recommended ? 'primary' : '', icon: 'download', disabled: busyOther, onClick: () => call('/download', {model: m.id}, 'Đang tải — có thể tiếp tục dùng ứng dụng.')})
            : !m.active ? btn('Dùng model này', {size: 'sm', kind: 'primary', onClick: () => call('/select', {model: m.id}, `Đã chuyển sang ${m.name}.`)}) : null,
        m.installed || m.partial ? btn('Xoá', {size: 'sm', kind: 'ghost', icon: 'trash', disabled: downloading, onClick: async () => {
          const ok = await confirmDialog({title: `Xoá model ${m.name} khỏi máy?`, text: `Giải phóng ${bytes(m.disk_bytes || 0)}. Muốn dùng lại thì tải về lại.`, confirm: 'Xoá', tone: 'danger'});
          if (ok) call('/delete', {model: m.id}, 'Đã xoá model.');
        }}) : null,
        h('a', {href: m.page_url, target: '_blank', rel: 'noopener', class: 'fs-xs'}, 'Trang tải thủ công')));
  }

  function render() {
    const hw = s.hardware || {};
    const gpu = hw.gpu ? `${hw.gpu.name} (${hw.gpu.vram_gb} GB)` : 'Không có card NVIDIA';
    const deviceNote = hw.probing ? notice('info', 'Đang kiểm tra card đồ hoạ…', 'Lần đầu mở mất vài giây. Danh sách model bên dưới dùng được ngay, gợi ý tốc độ sẽ cập nhật khi kiểm tra xong.')
      : s.device === 'gpu' ? notice('ok', 'Chép lời bằng card đồ hoạ (GPU)', 'Nhanh gấp khoảng 10 lần CPU. Nếu GPU gặp lỗi giữa chừng, ứng dụng tự chuyển sang CPU để không dừng việc.')
      : hw.gpu ? notice('warn', 'Có card NVIDIA nhưng chưa dùng được', 'Cập nhật driver NVIDIA mới nhất (GeForce Experience hoặc nvidia.com/drivers), khởi động lại máy rồi bấm “Đo lại”. Trong lúc đó chép lời chạy bằng CPU (chậm hơn).')
        : notice('info', 'Máy không có card NVIDIA — chép lời bằng CPU', 'Vẫn chạy được, chỉ chậm hơn. Model gợi ý bên dưới đã tính theo CPU của máy này.');
    const active = s.models.find(m => m.active);
    clear(host, h('div', {class: 'col gap-4'},
      s.active_path ? notice('ok', `Đang dùng: ${active ? active.name : 'model tự chọn'}`, s.env_override ? 'Đặt bằng biến KID_TE_WHISPER_MODEL trong .env.' : s.active_path)
        : notice('warn', 'Máy này chưa có model chép lời', 'Không có model thì khi tải video lên, ứng dụng không tự chép lời và gợi ý lịch ghim được. Tải model được đánh dấu “Hợp với máy này” (một lần, cần mạng).'),
      deviceNote,
      card('Cấu hình máy này', h('div', {class: 'card-body'}, kv([
        ['RAM', `${hw.ram_gb || '?'} GB`], ['CPU', `${hw.cpu_cores || '?'} luồng`], ['Card đồ hoạ', gpu],
        ['Chỗ trống ổ dữ liệu', `${hw.disk_free_gb ?? '?'} GB`], ['Thư mục model', s.folder]])),
      {actions: [btn('Đo lại', {size: 'sm', kind: 'ghost', icon: 'refresh', onClick: () => call('/refresh')}),
        btn('Mở thư mục', {size: 'sm', kind: 'ghost', icon: 'archive', onClick: () => openTarget('models')})]}),
      card('Chọn model', h('div', {class: 'list'}, s.models.map(modelRow)), {sub: 'Model lớn chép đúng hơn nhưng cần máy mạnh. Tải trực tiếp từ Hugging Face; ngắt mạng thì bấm Tải tiếp.'}),
      card('Đã có model ở chỗ khác?', h('div', {class: 'card-body form-grid'},
        h('p', {class: 'fs-sm soft'}, 'Chép thư mục model (faster-whisper, có config.json + model.bin) từ máy khác hoặc USB, rồi dán đường dẫn vào đây — không cần tải lại.'),
        field('Thư mục model', custom, {hint: s.custom_path ? 'Đang dùng: ' + s.custom_path : ''})),
      {footer: btn('Dùng thư mục này', {kind: 'primary', onClick: () => call('/select', {custom_path: custom.value}, 'Đã dùng model ở thư mục này.')})})));
    schedule();
  }
  load();
}

// ---------------------------------------------------------------- Chẩn đoán
async function diagnostics(ctx, host) {
  let data;
  try { data = await request('/api/workspace/diagnostics?limit=100'); } catch (e) { clear(host, errorBox(friendlyError(e), () => diagnostics(ctx, host))); return; }
  const items = data.items || [];
  const bad = items.filter(x => x.status !== 'ok');
  const again = () => diagnostics(ctx, host);
  const fixButton = f => {
    if (!f || !f.label) return null;
    if (f.page) return btn(f.label, {size: 'sm', kind: 'primary', onClick: () => ctx.navigate(f.page, f.sub || undefined)});
    if (f.open) return btn(f.label, {size: 'sm', kind: 'primary', icon: 'arrowRight', onClick: () => openTarget(f.open)});
    if (f.api) {
      return btn(f.label, {size: 'sm', kind: 'primary', icon: 'wand', onClick: async () => {
        try { await request(f.api, {method: 'POST', body: {}}); toast('Đã sửa.', 'ok'); again(); } catch (e) { toast(friendlyError(e), 'danger'); }
      }});
    }
    return null;
  };
  const row = x => h('div', {class: 'diag-row ' + x.status},
    h('span', {class: 'diag-icon'}, icon(x.status === 'ok' ? 'checkCircle' : 'alert', {size: 18})),
    h('div', {class: 'grow col gap-1', style: {minWidth: '0'}},
      h('strong', {class: 'fs-sm'}, x.label),
      h('div', {class: 'fs-xs muted', style: {overflowWrap: 'anywhere'}}, x.detail),
      x.status !== 'ok' && x.impact ? h('div', {class: 'fs-xs'}, 'Ảnh hưởng: ', x.impact) : null,
      x.status !== 'ok' && x.fix ? h('div', {class: 'diag-fix'}, icon('wand', {size: 14}), h('span', {}, x.fix.text)) : null),
    x.status !== 'ok' ? fixButton(x.fix) : null);
  const order = {error: 0, warn: 1, ok: 2};
  const sorted = [...items].sort((a, b) => order[a.status] - order[b.status]);
  clear(host, h('div', {class: 'col gap-4'},
    bad.length ? notice(bad.some(x => x.status === 'error') ? 'danger' : 'warn', `${bad.length} mục cần xử lý`, 'Mỗi mục có hướng dẫn sửa ngay bên dưới; bấm nút bên phải để làm luôn.')
      : notice('ok', 'Mọi thứ hoạt động bình thường', `${items.length} mục kiểm tra đều đạt.`),
    card('Kết quả kiểm tra', h('div', {}, sorted.map(row)),
      {actions: [btn('Kiểm tra lại', {icon: 'refresh', onClick: async () => { try { await request('/api/diagnostics/fix', {method: 'POST', body: {}}); again(); } catch (e) { toast(friendlyError(e), 'danger'); } }}),
        btn('Tạo báo cáo hỗ trợ', {kind: 'ghost', icon: 'fileText', onClick: async () => {
          try {
            const preview = await request('/api/support/preview');
            const ok = await confirmDialog({title: 'Tạo báo cáo hỗ trợ?', text: 'Báo cáo chỉ lưu trên máy (không tự gửi đi đâu). Không chứa: ' + (preview.excluded || []).join(', ') + '.', confirm: 'Tạo báo cáo'});
            if (!ok) return;
            const out = await request('/api/support/create', {method: 'POST', body: {}});
            toast('Đã tạo báo cáo: ' + (out.path || out.report_path || 'thư mục reports'), 'ok', {duration: 9000});
          } catch (e) { toast(friendlyError(e), 'danger'); }
        }})]})));
}

// ---------------------------------------------------------------- Sao lưu
const PART_ICON = {khach_hang: 'users', hop_thu: 'message', tri_nho_ai: 'sparkles', lich_ghim: 'film', san_pham: 'box', tai_lieu: 'fileText', cai_dat: 'settings', luong_binh_luan: 'database'};
const REASON_TEXT = {startup: 'Tự động khi mở ứng dụng', before_restore: 'Tự động trước khi khôi phục', manual: 'Tạo tay'};

let restoredNotice = null;   // survives the re-render that follows a restore

async function backups(ctx, host, d) {
  const controller = new AbortController();
  d?.add(() => controller.abort());
  const signal = controller.signal;
  let data, catalog;
  try {
    [data, catalog] = await Promise.all([request('/api/workspace/backups?limit=1000', {signal}), request('/api/system/backup-parts', {signal})]);
  } catch (e) { if (!signal.aborted) clear(host, errorBox(friendlyError(e), () => backups(ctx, host, d))); return; }
  if (signal.aborted) return;
  let v2Items = [];
  try { v2Items = normalizeV2((await request('/api/backup/v2/list', {signal})).items); } catch (e) { if (signal.aborted) return; }
  const items = data.items || [];
  const parts = catalog.parts || [];
  const names = Object.fromEntries(parts.map(p => [p.id, p.name]));
  const dash = ctx.store.get().dashboard || {};
  const showRestoreV2 = restoreV2Visible(dash);
  const refresh = () => { if (!signal.aborted) { controller.abort(); backups(ctx, host, d); } };
  const selectedBackups = new Set();
  const jobStatus = h('div', {class: 'fs-sm', role: 'status', 'aria-live': 'polite'});
  let busy = false;
  async function runJob(path, body) {
    const started = await request(path, {method: 'POST', body, signal});
    jobStatus.textContent = 'Đang sao lưu dữ liệu…';
    return waitJob(started.job_id, {signal, onProgress: job => {
      jobStatus.textContent = `Đang thực hiện · ${Math.round(job.progress * 100)}%`;
    }});
  }
  async function deleteBackups(ids, all = false) {
    if (busy || (!all && !ids.length)) return;
    busy = true;
    try {
      const checked = await request('/api/system/backups/delete/preview', {method: 'POST', body: all ? {all: true} : {ids}, signal});
      if (signal.aborted) return;
      if (!await confirmDialog({title: `Xóa ${checked.ids.length} bản sao lưu?`,
        text: `Thu hồi ${bytes(checked.reclaimable_bytes)} dữ liệu file. Thao tác xóa không tạo bản sao lưu mới.`, confirm: 'Xóa', tone: 'danger'})) return;
      if (signal.aborted) return;
      const result = await runJob('/api/system/backups/delete', {ids: checked.ids, preview_token: checked.preview_token});
      toast(`Đã xóa ${result.deleted_ids.length} bản sao lưu.`, 'ok');
      refresh();
    } catch (e) { if (!signal.aborted) { jobStatus.textContent = 'Không hoàn tất. ' + friendlyError(e); toast(friendlyError(e), 'danger'); } }
    finally {busy = false;}
  }

  const selectedV2 = new Set();
  async function deleteBackupsV2(all = false) {
    const ids = selectionIds(v2Items, selectedV2, all);
    if (busy || !ids.length) { if (!ids.length) toast('Chọn ít nhất một bản sao lưu.', 'warn'); return; }
    busy = true;
    try {
      const out = await deleteV2({request, signal, ids, confirm: checked => confirmDialog({
        title: `Xóa ${checked.ids.length} bản sao lưu v2?`,
        text: `Thu hồi ${bytes(checked.reclaim_bytes)} dữ liệu file. Không thể hoàn tác.`, confirm: 'Xóa', tone: 'danger'})});
      if (out) { toast(`Đã xóa ${out.deleted_ids.length} bản sao lưu v2.`, 'ok'); refresh(); }
    } catch (e) { if (!signal.aborted) { jobStatus.textContent = 'Không hoàn tất. ' + friendlyError(e); toast(friendlyError(e), 'danger'); } }
    finally {busy = false;}
  }

  async function backupNow(selected) {
    if (busy) return;
    busy = true;
    try {
      const out = await runJob('/api/system/backup/start', selected ? {parts: selected} : {});
      toast(`Đã sao lưu: ${out.parts.map(p => names[p] || p).join(', ')}.`, 'ok');
      refresh();
    } catch (e) { if (!signal.aborted) { jobStatus.textContent = 'Không hoàn tất. ' + friendlyError(e); toast(friendlyError(e), 'danger'); } }
    finally {busy = false;}
  }

  async function backupV2(full) {
    if (busy) return;
    const password = await promptDialog({title: full ? 'Sao lưu v2 đầy đủ' : 'Sao lưu v2 một phần',
      text: 'Mật khẩu bảo vệ phần bí mật trong bản sao lưu. Hãy ghi nhớ: không có mật khẩu thì không khôi phục được.',
      label: 'Mật khẩu', confirm: 'Sao lưu', validate: passwordProblem});
    if (!password || signal.aborted) return;
    busy = true;
    try {
      const out = await startV2({request, waitJob, password, full, signal,
        onStart: id => { jobStatus.textContent = 'Đang thực hiện · ' + id; }});
      toast(out.complete ? 'Đã tạo bản sao lưu v2 đầy đủ.' : 'Đã tạo bản sao lưu v2 một phần (còn thiếu nguồn).', out.complete ? 'ok' : 'warn');
      refresh();
    } catch (e) { if (!signal.aborted) { jobStatus.textContent = 'Không hoàn tất. ' + friendlyError(e); toast(friendlyError(e), 'danger'); } }
    finally {busy = false;}
  }
  async function restoreBackupV2(b) {
    if (busy) return;
    const password = await promptDialog({title: 'Khôi phục từ bản sao lưu v2',
      text: 'Nhập mật khẩu của bản sao lưu này. Không có mật khẩu thì không khôi phục được.',
      label: 'Mật khẩu', confirm: 'Tiếp tục', validate: passwordProblem});
    if (!password || signal.aborted) return;
    const typed = await promptDialog({title: `Gõ mã bản sao lưu để xác nhận`,
      text: `Dữ liệu hiện tại sẽ được thay bằng bản lúc ${dateTime(b.created_at)}. Thư mục cũ được giữ làm bản hoàn tác, AI sẽ tắt cho đến khi bạn bật lại. Gõ chính xác: ${b.id}`,
      label: 'Mã bản sao lưu', confirm: 'Khôi phục', validate: v => confirmProblem(v, b.id)});
    if (!typed || signal.aborted) return;
    busy = true;
    try {
      const out = await restoreV2({request, backupId: b.id, password, typed, onPhase: t => { jobStatus.textContent = t; }});
      restoredNotice = out;
      toast('Đã khôi phục. AI đang tắt cho đến khi bạn bật lại.', 'ok', {duration: 8000});
      refresh();
    } catch (e) {
      jobStatus.textContent = '';
      toast(restoreErrorText(e), 'danger', {duration: 10000});
    } finally {busy = false;}
  }

  function aiOffNotice() {
    if (!restoredNotice) return null;
    const button = btn('Bật lại AI', {kind: 'primary', onClick: async () => {
      try { await reenableAi({request}); restoredNotice = null; toast('Đã bật lại AI.', 'ok'); refresh(); }
      catch (e) { toast(friendlyError(e), 'danger'); }
    }});
    return notice('warn', 'AI đang tắt sau khi khôi phục', 'Kiểm tra dữ liệu rồi bấm Bật lại AI để trả lời bình luận và tự ghim. Thư mục cũ vẫn được giữ' + (restoredNotice.rollbackDir ? ': ' + restoredNotice.rollbackDir : '') + '.', button);
  }
  async function exportIdb() {
    try {
      const out = await exportIndexedDb({idb: indexedDB, request, signal});
      toast('Đã xuất điểm khôi phục Studio (' + bytes(out.bytes || 0) + ').', 'ok');
    } catch (e) { if (!signal.aborted) toast(friendlyError(e), 'danger'); }
  }

  function pickParts() {
    const chosen = new Set(parts.filter(p => p.available).map(p => p.id));
    const list = h('div', {class: 'col gap-2'}, parts.map(p => h('label', {class: 'part-pick' + (p.available ? '' : ' disabled')},
      h('input', {type: 'checkbox', checked: chosen.has(p.id), disabled: !p.available, onChange: e => { if (e.target.checked) chosen.add(p.id); else chosen.delete(p.id); }}),
      icon(PART_ICON[p.id] || 'archive', {size: 16}),
      h('div', {class: 'grow'}, h('div', {class: 'fs-sm'}, h('strong', {}, p.name), ' · ', bytes(p.bytes)), h('div', {class: 'fs-xs muted'}, p.available ? p.description : 'Chưa có dữ liệu trên máy')))));
    const drawer = openDrawer({title: 'Sao lưu từng mục', sub: 'Chỉ chép những mục được chọn. Giữ một bản từng mục mới nhất, riêng với bản đầy đủ. Chỉ xóa bản cũ sau khi bản mới được kiểm tra thành công.', body: h('div', {class: 'card-body'}, list),
      footer: [h('span', {class: 'grow'}), btn('Sao lưu mục đã chọn', {kind: 'primary', icon: 'archive', onClick: () => { if (!chosen.size) { toast('Chọn ít nhất một mục.', 'warn'); return; } drawer.close(); backupNow([...chosen]); }})]});
  }

  async function restore(b, selected) {
    const what = selected ? selected.map(p => names[p] || p).join(', ') : 'tất cả mục trong bản này';
    const ok = await confirmDialog({title: `Khôi phục ${what} về lúc ${dateTime(b.created_at)}?`, text: 'Dữ liệu hiện tại của các mục này được tự sao lưu trước, nên vẫn quay lại được.', confirm: 'Khôi phục', tone: 'danger'});
    if (!ok) return false;
    try {
      const out = await request(`/api/system/backups/${encodeURIComponent(b.id)}/restore`, {method: 'POST', body: selected ? {parts: selected} : {}});
      toast(`Đã khôi phục: ${out.parts.map(p => names[p] || p).join(', ')}. Tải lại trang để thấy dữ liệu mới.`, 'ok', {duration: 8000});
      refresh();
      return true;
    } catch (e) { toast(friendlyError(e), 'danger'); return false; }
  }

  async function showDetail(b) {
    const bodyNode = h('div', {class: 'card-body'}, skeletonRows(4, 60));
    const drawer = openDrawer({title: `Bản sao lưu ${dateTime(b.created_at)}`, sub: `${REASON_TEXT[b.reason] || 'Sao lưu'} · ${b.scope === 'partial' ? 'từng mục' : 'đầy đủ'} · ${bytes(b.bytes)}`, body: bodyNode, width: 680,
      footer: [h('span', {class: 'grow'}), btn('Khôi phục tất cả', {kind: 'danger', icon: 'undo', onClick: async () => { if (await restore(b)) drawer.close(); }})]});
    let detail;
    try { detail = await request('/api/system/backups/' + encodeURIComponent(b.id)); } catch (e) { clear(bodyNode, errorBox(friendlyError(e))); return; }
    const delta = c => {
      if (c.current === null || c.current === undefined) return h('span', {class: 'muted'}, 'hiện tại —');
      const diff = c.current - c.backup;
      if (!diff) return h('span', {class: 'muted'}, 'bằng hiện tại');
      return h('span', {class: diff > 0 ? 'delta up' : 'delta down'}, diff > 0 ? `hiện tại nhiều hơn ${number(diff)}` : `hiện tại ÍT hơn ${number(-diff)} (đã xoá / mất)`);
    };
    clear(bodyNode, h('div', {class: 'col gap-3'},
      detail.integrity_error ? notice('danger', 'Bản sao lưu bị hỏng', detail.integrity_error) : notice('ok', 'Bản sao lưu nguyên vẹn', 'Đã kiểm tra từng file (mã SHA-256).'),
      detail.parts.map(p => h('section', {class: 'backup-part'},
        h('header', {class: 'row', style: {gap: '10px'}}, icon(PART_ICON[p.id] || 'archive', {size: 18}),
          h('div', {class: 'grow'}, h('strong', {}, p.name), h('div', {class: 'fs-xs muted'}, `${p.description} · ${bytes(p.bytes)} · ${p.files.length} file`)),
          p.restorable ? btn('Khôi phục mục này', {size: 'sm', kind: 'ghost', icon: 'undo', onClick: async () => { if (await restore(b, [p.id])) drawer.close(); }}) : null),
        p.counts.length ? h('table', {class: 'table compact'}, h('tbody', {}, p.counts.map(c => h('tr', {},
          h('td', {}, c.label), h('td', {class: 'num'}, h('strong', {}, number(c.backup))), h('td', {class: 'fs-xs'}, delta(c)))))) : null,
        p.files.some(f => f.missing_now) ? h('div', {class: 'fs-xs', style: {color: 'var(--warn-text)'}}, 'Có file trong bản này hiện KHÔNG còn trên máy: ' + p.files.filter(f => f.missing_now).map(f => f.path).join(', ')) : null))));
  }

  clear(host, h('div', {class: 'col gap-4'},
    jobStatus,
    aiOffNotice(),
    dash.backup && dash.backup.overdue ? notice('warn', 'Đã lâu chưa sao lưu', 'Nên tạo bản sao lưu trước mỗi phiên LIVE lớn.') : notice('info', 'Sao lưu tự động và thủ công', 'Ứng dụng tự sao lưu sau khi mở (và mỗi 12 giờ nếu đã quá cũ), tạm hoãn khi đang LIVE. Bấm Sao lưu dữ liệu để lưu ngay sản phẩm, khách hàng, trí nhớ AI và lịch làm việc. Không chép video gốc hoặc model lớn. Bản mới thành công mới dọn bản cũ; giữ 20 bản tự động gần nhất.'),
    card(`Sao lưu dữ liệu (${items.length})`, items.length ? h('div', {class: 'table-wrap'}, h('table', {class: 'table'},
      h('thead', {}, h('tr', {}, ['Chọn', 'Thời điểm', 'Gồm', 'Dung lượng', ''].map(t => h('th', {}, t)))),
      h('tbody', {}, items.map(b => h('tr', {class: 'clickable', onClick: e => { if (!e.target.closest('button,input')) showDetail(b); }},
        h('td', {}, h('input', {type: 'checkbox', 'aria-label': 'Chọn bản sao lưu ' + b.id, onChange: e => {
          if (e.target.checked) selectedBackups.add(b.id); else selectedBackups.delete(b.id);
        }})),
        h('td', {}, h('strong', {class: 'fs-sm'}, dateTime(b.created_at)), h('div', {class: 'fs-xs muted'}, `${relative(b.created_at)} · ${REASON_TEXT[b.reason] || (b.scope === 'partial' ? 'Từng mục' : 'Đầy đủ')}`)),
        h('td', {}, h('div', {class: 'row wrap', style: {gap: '4px'}}, (b.parts || []).map(p => chip(names[p] || p, '', {iconName: PART_ICON[p]})))),
        h('td', {class: 'num'}, bytes(b.bytes || 0)),
        h('td', {class: 'nowrap'}, btn('Xem', {size: 'sm', kind: 'ghost', icon: 'eye', onClick: () => showDetail(b)}),
          btn('Khôi phục', {size: 'sm', kind: 'ghost', icon: 'undo', onClick: () => restore(b)}),
          btn('Xóa', {size: 'sm', kind: 'ghost', icon: 'trash', onClick: () => deleteBackups([b.id])}))))))) : empty('Chưa có bản sao lưu', ''),
    {actions: [btn('Mở thư mục', {kind: 'ghost', size: 'sm', icon: 'archive', onClick: () => openTarget('backups')}),
      btn('Xóa đã chọn', {kind: 'ghost', icon: 'trash', onClick: () => selectedBackups.size ? deleteBackups([...selectedBackups]) : toast('Chọn ít nhất một bản sao lưu.', 'warn')}),
      btn('Xóa tất cả bản sao lưu', {kind: 'ghost', icon: 'trash', onClick: () => deleteBackups([], true)}),
      btn('Sao lưu từng mục…', {kind: 'ghost', icon: 'list', onClick: pickParts}),
      btn('Sao lưu dữ liệu', {kind: 'primary', icon: 'archive', onClick: () => backupNow(null)})]}),
    h('details', {class: 'card'}, h('summary', {class: 'card-body clickable'}, 'Bản sao lưu mở rộng và bản cũ (' + v2Items.length + ')'),
    card(`Bản sao lưu v2 (${v2Items.length})`, v2Items.length ? h('div', {class: 'table-wrap'}, h('table', {class: 'table', 'data-backup-v2': ''},
      h('thead', {}, h('tr', {}, (showRestoreV2 ? ['Chọn', 'Thời điểm', 'Loại', 'Dung lượng', 'Kiểm tra', ''] : ['Chọn', 'Thời điểm', 'Loại', 'Dung lượng', 'Kiểm tra']).map(t => h('th', {}, t)))),
      h('tbody', {}, v2Items.map(b => h('tr', {'data-kind': b.kind},
        h('td', {}, h('input', {type: 'checkbox', 'aria-label': 'Chọn bản sao lưu v2 ' + b.id, onChange: e => {
          if (e.target.checked) selectedV2.add(b.id); else selectedV2.delete(b.id);
        }})),
        h('td', {}, h('strong', {class: 'fs-sm'}, dateTime(b.created_at))),
        h('td', {}, chip(kindLabel(b.kind), b.kind === 'full' ? 'ok' : 'warn'), h('div', {class: 'fs-xs muted'}, kindNote(b.kind))),
        h('td', {class: 'num'}, bytes(b.size)),
        h('td', {}, b.verified ? 'Đã xác minh' : 'Chưa xác minh'),
        showRestoreV2 ? h('td', {}, btn('Khôi phục v2…', {kind: 'danger', size: 'sm', icon: 'undo', attrs: {'data-restore-v2': b.id}, onClick: () => restoreBackupV2(b)})) : null))))) : empty('Chưa có bản sao lưu v2', ''),
    {actions: [v2Items.length ? btn('Xóa đã chọn', {kind: 'danger', size: 'sm', onClick: () => deleteBackupsV2(false)}) : null,
      v2Items.length ? btn('Xóa tất cả bản cũ', {kind: 'danger', size: 'sm', onClick: () => deleteBackupsV2(true)}) : null,
      btn('Xuất điểm khôi phục Studio', {kind: 'ghost', size: 'sm', icon: 'archive', onClick: exportIdb}),
      btn('Sao lưu v2 một phần…', {kind: 'ghost', icon: 'archive', onClick: () => backupV2(false)}),
      btn('Sao lưu v2 đầy đủ…', {kind: 'primary', icon: 'archive', onClick: () => backupV2(true)})].filter(Boolean)}))));
}

// ---------------------------------------------------------------- Thùng rác
async function trash(ctx, host) {
  let data;
  try { data = await request('/api/trash?limit=100'); } catch (e) { clear(host, errorBox(friendlyError(e), () => trash(ctx, host))); return; }
  const items = data.items || [];
  const purge = async (body, title, text) => {
    const ok = await confirmDialog({title, text, confirm: 'Xoá vĩnh viễn', tone: 'danger'});
    if (!ok) return;
    try { const out = await request('/api/trash/purge', {method: 'POST', body}); toast(`Đã xoá vĩnh viễn ${out.purged ?? 0} mục.`, 'ok'); trash(ctx, host); } catch (e) { toast(friendlyError(e), 'danger'); }
  };
  const counts = {};
  items.forEach(t => { counts[t.resource] = (counts[t.resource] || 0) + 1; });
  clear(host, h('div', {class: 'col gap-4'},
    notice('info', 'Nội dung đã xoá được giữ 30 ngày', 'Khôi phục bất cứ lúc nào trước hạn. Không cần giữ thì “Xoá vĩnh viễn” để dọn ngay.'),
    card(`Thùng rác (${items.length})`, items.length ? h('div', {class: 'list'},
      h('div', {class: 'row wrap', style: {gap: '6px', padding: '10px 16px'}}, Object.entries(counts).map(([r, n]) => chip(`${RESOURCE_NAME[r] || r}: ${n}`))),
      items.map(t => h('div', {class: 'list-row'},
        h('span', {class: 'pavatar', style: {'--size': '38px'}}, icon(t.resource === 'videos' || t.resource === 'video-jobs' ? 'film' : t.resource === 'profiles' ? 'fileText' : t.resource === 'conversations' ? 'message' : 'archive')),
        h('div', {class: 'grow'}, h('div', {class: 'title'}, t.label || t.record_id), h('div', {class: 'sub'}, `${RESOURCE_NAME[t.resource] || t.resource} · xoá ${relative(t.deleted_at)} · giữ tới ${dateTime(t.expires_at)}`)),
        btn('Khôi phục', {size: 'sm', icon: 'undo', onClick: async () => {
          try { await request(`/api/workspace/${encodeURIComponent(t.resource)}/${encodeURIComponent(t.record_id)}/restore`, {method: 'POST', body: {}}); toast('Đã khôi phục.', 'ok'); trash(ctx, host); } catch (e) { toast(friendlyError(e), 'danger'); }
        }}),
        btn('Xoá vĩnh viễn', {size: 'sm', kind: 'ghost', icon: 'trash', onClick: () => purge({trash_ids: [t.id || t.trash_id]}, 'Xoá vĩnh viễn mục này?', 'Không khôi phục lại được.')})))) : empty('Thùng rác trống', '', {iconName: 'trash'}),
    {actions: items.length ? h('div', {class: 'row', style: {gap: '8px'}},
      btn('Dọn mục quá hạn', {kind: 'ghost', size: 'sm', onClick: async () => {
        try { const out = await request('/api/trash/empty-expired', {method: 'POST', body: {}}); toast(`Đã dọn ${out.purged ?? 0} mục quá hạn.`, 'ok'); trash(ctx, host); } catch (e) { toast(friendlyError(e), 'danger'); }
      }}),
      btn('Dọn sạch thùng rác', {kind: 'danger', size: 'sm', icon: 'trash', onClick: () => purge({all: true}, `Xoá vĩnh viễn toàn bộ ${items.length} mục?`, 'Tất cả nội dung trong Thùng rác sẽ bị xoá hẳn, không khôi phục được.')})) : null})));
}

// ---------------------------------------------------------------- Nhật ký
async function logs(ctx, host) {
  let severity = 'problems', rows = [];
  const list = h('div', {}, skeletonRows(6, 24));
  const filterSlot = h('span');
  const SEV = {error: ['Lỗi', 'danger'], critical: ['Lỗi', 'danger'], warning: ['Cảnh báo', 'warn'], info: ['Thông tin', '']};
  async function load() {
    clear(filterSlot, segmented([['problems', 'Sự cố'], ['', 'Mọi hoạt động']], severity, v => { severity = v; load(); }));
    try {
      rows = (await request('/api/logs?limit=300&severity=' + encodeURIComponent(severity))).rows || [];
      // Cùng một sự cố lặp lại liền nhau → một dòng "×N" (vd tắt đột ngột nhiều lần).
      const groups = [];
      rows.forEach(r => {
        const last = groups[groups.length - 1];
        if (last && last.row.event === r.event && last.row.title === r.title && last.row.hint === r.hint) last.count += 1;
        else groups.push({row: r, count: 1, oldest: r});
        groups[groups.length - 1].oldest = r;
      });
      clear(list, groups.length ? groups.map(({row: r, count, oldest}) => {
        const [label, tone] = SEV[r.severity] || [r.severity, ''];
        return h('div', {class: 'log-item'},
          h('div', {class: 'row wrap', style: {gap: '8px'}}, chip(label, tone), h('strong', {class: 'fs-sm'}, r.title),
            count > 1 ? chip(`×${count}`, '', {title: `Từ ${dateTime(oldest.timestamp)}`}) : null,
            h('span', {class: 'grow'}), h('span', {class: 'fs-xs muted', title: dateTime(r.timestamp)}, relative(r.timestamp))),
          r.hint ? h('div', {class: 'fs-sm soft'}, r.hint) : null,
          r.message || r.exception ? h('details', {class: 'teach-more'}, h('summary', {}, 'Chi tiết kỹ thuật'),
            h('pre', {class: 'log-pre'}, [dateTime(r.timestamp) + ' · ' + (r.event || ''), r.message, r.exception].filter(Boolean).join('\n'))) : null);
      }) : empty(severity ? 'Không có sự cố nào' : 'Chưa có nhật ký', severity ? 'Ứng dụng chạy bình thường.' : '', {iconName: 'checkCircle'}));
    } catch (e) { clear(list, errorBox(friendlyError(e), load)); }
  }
  const copy = btn('Sao chép cho kỹ thuật', {kind: 'ghost', size: 'sm', icon: 'fileText', onClick: async () => {
    const text = rows.map(r => JSON.stringify(r)).join('\n');
    try { await navigator.clipboard.writeText(text); toast(`Đã sao chép ${rows.length} dòng — dán vào tin nhắn gửi kỹ thuật.`, 'ok'); }
    catch (_) { toast('Không sao chép được. Bấm “Mở thư mục” rồi gửi file core.jsonl.', 'warn'); }
  }});
  clear(host, card('Nhật ký', list, {sub: 'Mặc định chỉ hiện sự cố (lỗi và cảnh báo) kèm cách xử lý. “Chi tiết kỹ thuật” dành cho người sửa lỗi.',
    actions: [filterSlot, copy, btn('Mở thư mục', {kind: 'ghost', size: 'sm', icon: 'archive', onClick: () => openTarget('logs')}), btn('Làm mới', {kind: 'ghost', size: 'sm', icon: 'refresh', onClick: load})]}));
  load();
}

async function openTarget(target) {
  try { await request('/api/system/open', {method: 'POST', body: {target}}); } catch (e) { toast(friendlyError(e), 'danger'); }
}

// ---------------------------------------------------------------- Giao diện
function appearance(ctx, host) {
  let theme = 'system', density = 'comfortable';
  try { theme = localStorage.getItem('lr-theme') || 'system'; density = localStorage.getItem('lr-density') || 'comfortable'; } catch (_) { /* */ }
  const render = () => clear(host, card('Giao diện', h('div', {class: 'card-body form-grid'},
    h('div', {class: 'field'}, h('span', {class: 'field-label'}, 'Chủ đề màu'), segmented([['system', 'Theo Windows'], ['light', 'Sáng'], ['dark', 'Tối']], theme, v => { theme = v; applyTheme(v); render(); })),
    h('div', {class: 'field'}, h('span', {class: 'field-label'}, 'Mật độ hiển thị'), segmented([['comfortable', 'Thoải mái'], ['compact', 'Gọn']], density, v => {
      density = v; try { localStorage.setItem('lr-density', v); } catch (_) { /* */ }
      if (v === 'compact') document.documentElement.dataset.density = 'compact'; else delete document.documentElement.dataset.density; render();
    })),
    h('p', {class: 'fs-xs muted'}, 'Studio video luôn dùng nền tối để hình ảnh rõ nhất. Phím ? xem phím tắt; Ctrl+K tìm nhanh.'))));
  render();
}
