import {h, clear, disposer} from '../core/dom.js';
import {icon} from '../core/icons.js';
import {request, friendlyError} from '../core/api.js';
import {clock, relative, bytes, number} from '../core/format.js';
import {btn, chip, errorBox, skeletonRows, pageHead, notice, card, field, input, textarea, select, toggle, empty, stat} from '../ui/kit.js';
import {toast, confirmDialog} from '../ui/overlays.js';

// Dịch vụ chạy nền (đồng bộ video) bật/tắt ở trang tổng; bộ xử lý luôn chạy cùng
// ứng dụng nhưng mỗi bộ có trang riêng: giải thích, cài đặt, thử ngay.
const ABOUT = {
  'chinh-sach-gia': {icon: 'tag', text: 'Hướng dẫn AI báo đúng giá, tôn trọng giá khách thấy và yêu cầu viết lại khi câu trả lời tự tính giá hoặc khẳng định có mã.'},
  'chon-san-pham': {icon: 'target', text: 'Đoán khách đang hỏi sản phẩm nào (tên gọi đã dạy, sản phẩm đang ghim, “chai này”).'},
  'lieu-dung': {icon: 'activity', text: 'Tính liều pha theo đúng nhãn đã duyệt (vd bình 25 lít pha bao nhiêu ml).'},
  'model-deepseek': {icon: 'bot', text: 'Gọi DeepSeek soạn câu trả lời: độ dài, độ sáng tạo, chế độ suy nghĩ, lời nhắc hệ thống.'},
  'thu-du-lieu': {icon: 'database', text: 'Ghi mọi bình luận của phiên LIVE — nguồn của Hộp thư, Khách hàng và việc AI tự học.'},
  'dong-bo-video': {icon: 'film', text: 'Theo dõi riêng Shopee qua nguồn phương tiện OBS và TikTok qua âm thanh TikTok Studio. Mỗi sàn ghim theo vị trí video riêng.'},
};

const VIDEO_STATE = {
  running: ['Đang nhận ra video', 'ok'], watching: ['Đang nghe', 'ok'], detected: ['Đang nhận ra video', 'ok'],
  waiting: ['Đang nghe · chưa khớp video nào', 'warn'], error: ['Lỗi thiết bị âm thanh', 'danger'],
};
const WAIT_REASON = {
  audio_silent: 'thiết bị âm thanh đang im lặng — video có đang phát không?',
  no_fingerprint_match: 'âm thanh không khớp video nào trong thư viện',
  low_margin: 'khớp chưa đủ chắc chắn',
  no_fingerprint: 'thư viện chưa có video nào có vân tay âm thanh',
  audio_device_error: 'mất thiết bị âm thanh',
};

export default {
  mount(ctx) {
    const d = disposer();
    if (ctx.route.sub && ABOUT[ctx.route.sub] && ctx.route.sub !== 'dong-bo-video') {
      processorPage(ctx, d, ctx.route.sub);
    } else {
      overviewPage(ctx, d);
    }
    return {unmount: () => d.dispose()};
  },
};

// ================================================================ Trang tổng
function overviewPage(ctx, d) {
  let data = null, error = null, busy = '';
  const body = h('div', {}, skeletonRows(3, 110));
  clear(ctx.shell.main, h('div', {class: 'page'},
    pageHead('Trợ lý tự động', 'Các bộ phận giúp AI trả lời đúng và tiện ích ghim đúng sản phẩm. Bấm vào từng bộ xử lý để xem nó làm gì, chỉnh cài đặt và thử ngay.'),
    body));

  function serviceCard(p) {
    const rt = p.runtime || {};
    const running = rt.state === 'running';
    const [label, tone] = running ? ['Đang theo dõi hai nguồn', 'ok'] : rt.state === 'needs_attention' ? ['Cần xử lý', 'danger'] : ['Đang tắt', ''];
    const about = ABOUT[p.id] || {icon: 'puzzle', text: p.description || ''};
    const detail = h('p', {class: 'fs-xs muted'}, running
      ? 'Xem trạng thái và video từng sàn tại Trung tâm LIVE. Mất một nguồn không ảnh hưởng nguồn còn lại.'
      : 'Bật dịch vụ để theo dõi nguồn OBS và TikTok Studio độc lập.');
    return h('section', {class: 'card plugin-card service'},
      h('div', {class: 'card-body col gap-3'},
        h('div', {class: 'plugin-head'}, h('span', {class: 'plugin-icon'}, icon(about.icon)),
          h('div', {class: 'grow'}, h('strong', {}, p.name || p.id), h('div', {class: 'fs-xs muted'}, `Dịch vụ chạy nền · phiên bản ${p.version || '—'}`)),
          chip(label, tone, {dot: true})),
        h('p', {class: 'fs-sm soft'}, about.text),
        detail,
        rt.state === 'needs_attention' && rt.reason ? notice('danger', null, rt.reason === 'restart_limit' ? 'Dịch vụ lỗi liên tục nên tạm khoá 5 phút. Kiểm tra thiết bị âm thanh rồi bật lại.' : 'Dịch vụ cần xử lý: ' + rt.reason) : null),
      h('footer', {class: 'card-foot'},
        h('button', {class: 'btn ghost sm', type: 'button', onClick: () => ctx.navigate('system', 'live')}, icon('volume'), 'Cài nguồn theo sàn'),
        h('button', {class: 'btn ghost sm', type: 'button', onClick: () => ctx.navigate('video')}, icon('film'), 'Lịch ghim'),
        h('span', {class: 'grow'}),
        running ? btn('Khởi động lại', {size: 'sm', icon: 'refresh', disabled: Boolean(busy), onClick: () => act(p.id, 'restart')}) : null,
        running ? btn(busy === p.id ? 'Đang tắt…' : 'Tắt', {size: 'sm', kind: 'danger-ghost', icon: 'pause', disabled: Boolean(busy), onClick: () => act(p.id, 'stop')})
          : btn(busy === p.id ? 'Đang bật…' : 'Bật', {size: 'sm', kind: 'primary', icon: 'play', disabled: Boolean(busy) || data.safe_mode, onClick: () => act(p.id, 'start')})));
  }

  function processorRow(p) {
    const about = ABOUT[p.id] || {icon: 'puzzle', text: p.description || ''};
    return h('button', {class: 'list-row', type: 'button', onClick: () => ctx.navigate('automation', p.id)},
      h('span', {class: 'plugin-icon'}, icon(about.icon)),
      h('div', {class: 'grow'}, h('div', {class: 'title'}, p.name || p.id), h('div', {class: 'sub'}, about.text)),
      chip('Đang chạy', 'ok', {dot: true}), icon('chevronRight', {size: 16}));
  }

  function render() {
    if (error) { clear(body, errorBox(friendlyError(error), load)); return; }
    if (!data) return;
    const plugins = data.plugins || [];
    const services = plugins.filter(p => p.kind === 'dich-vu');
    const processors = plugins.filter(p => p.kind !== 'dich-vu');
    clear(body,
      data.safe_mode ? notice('warn', 'Đang chạy CHẾ ĐỘ AN TOÀN', 'Ứng dụng được mở với --safe-mode nên dịch vụ chạy nền bị khoá.',
        btn('Tắt chế độ an toàn', {kind: 'primary', onClick: () => act('', 'safe_mode', {enabled: false})})) : null,
      Object.keys(data.errors || {}).length ? notice('danger', 'Có bộ phận không nạp được', Object.entries(data.errors).map(([id, err]) => `${id}: ${err}`).join(' · ')) : null,
      h('div', {class: 'settings-grid', style: {marginTop: '12px'}}, services.map(serviceCard)),
      processors.length ? h('section', {class: 'card plugin-card', style: {marginTop: '16px'}},
        h('header', {class: 'card-head'}, h('div', {}, h('h3', {}, 'Bộ xử lý của AI'), h('div', {class: 'sub'}, 'Chạy cho mỗi bình luận. Bấm vào để xem, chỉnh và thử.'))),
        h('div', {class: 'list'}, processors.map(processorRow))) : null);
  }

  async function act(id, action, extra = {}) {
    busy = id || action; render();
    try {
      const out = await request('/api/plugins/action', {method: 'POST', body: {plugin_id: id, action, ...extra}});
      if (out.ok === false) throw new Error(out.error || 'Không thực hiện được.');
      toast({start: 'Đã bật đồng bộ video.', stop: 'Đã tắt đồng bộ video.', restart: 'Đã khởi động lại.', safe_mode: 'Đã tắt chế độ an toàn.'}[action] || 'Xong.', 'ok');
    } catch (e) { toast(e.message || friendlyError(e), 'danger'); }
    busy = '';
    load();
  }

  async function load() {
    error = null;
    try { data = await request('/api/plugins'); } catch (e) { error = e; }
    render();
  }
  load();
  d.interval(load, 10000);
}

// ================================================================ Trang một bộ xử lý
function processorPage(ctx, d, id) {
  const about = ABOUT[id];
  const head = h('div');
  const body = h('div', {class: 'col gap-4'}, skeletonRows(4, 90));
  clear(ctx.shell.main, h('div', {class: 'page'}, head, body));
  let data = null;

  const setHead = name => clear(head, pageHead(name, about.text,
    btn('Tất cả bộ xử lý', {kind: 'ghost', icon: 'arrowLeft', onClick: () => ctx.navigate('automation')}),
    [h('a', {href: '#/automation'}, 'Trợ lý tự động'), ' / ', name]));
  setHead('Bộ xử lý');

  async function load() {
    try { data = await request('/api/system/processors/' + encodeURIComponent(id)); }
    catch (e) { clear(body, errorBox(friendlyError(e), load)); return; }
    setHead(data.name);
    clear(body,
      aboutCard(data),
      settingsCard(data, fresh => { data = fresh; load(); }),
      extraCards(ctx, data, load),
      data.try ? tryCard(ctx, data) : null,
      data.links.length ? card('Liên quan', h('div', {class: 'card-body row wrap', style: {gap: '8px'}},
        data.links.map(l => btn(l.label, {kind: 'ghost', size: 'sm', icon: 'arrowRight', onClick: () => ctx.navigate(l.page, l.sub || undefined)})))) : null);
  }
  load();
}

function aboutCard(data) {
  return card('Bộ này làm gì', h('div', {class: 'card-body col gap-3'},
    h('p', {class: 'fs-sm'}, data.about.what),
    h('ol', {class: 'list-lines'}, data.about.how.map(line => h('li', {class: 'fs-sm soft'}, line)))),
  {sub: `Phiên bản ${data.version || '—'} · luôn chạy cùng ứng dụng`});
}

// ---------------------------------------------------------------- Cài đặt
function settingsCard(data, onSaved) {
  const values = JSON.parse(JSON.stringify(data.values));
  const changed = new Set(data.changed || []);
  const controls = {};
  const saveBtn = btn('Lưu cài đặt', {kind: 'primary', icon: 'save', disabled: true, onClick: save});
  const dirty = () => { saveBtn.disabled = false; syncDepends(); };

  function control(f) {
    const v = values[f.key];
    if (f.type === 'bool') return toggle(f.label, Boolean(v), on => { values[f.key] = on; dirty(); });
    if (f.type === 'number') {
      return input({type: 'number', value: v, min: f.min, max: f.max, step: f.step || (f.integer ? 1 : 0.1), style: {maxWidth: '160px'},
        onInput: e => { values[f.key] = e.target.value === '' ? '' : Number(e.target.value); dirty(); }});
    }
    if (f.type === 'select') return select(f.options, v, {onChange: e => { values[f.key] = e.target.value; dirty(); }});
    if (f.type === 'list') {
      const count = h('span', {class: 'fs-xs muted'}, `${v.length} mục`);
      const area = textarea({rows: Math.min(10, Math.max(3, v.length)), value: v.join('\n'),
        onInput: e => { values[f.key] = e.target.value.split('\n').map(x => x.trim()).filter(Boolean); count.textContent = `${values[f.key].length} mục`; dirty(); }});
      return h('div', {class: 'col gap-1'}, area, count);
    }
    return textarea({rows: 2, value: v, maxlength: f.max_length || 2000, onInput: e => { values[f.key] = e.target.value; dirty(); }});
  }

  function row(f) {
    const node = control(f);
    controls[f.key] = node;
    const mark = changed.has(f.key) ? chip('Đã đổi', 'info', {size: 'sm'}) : null;
    if (f.type === 'bool') {
      return h('div', {class: 'proc-field', dataset: {key: f.key}}, h('div', {class: 'row', style: {gap: '8px'}}, node, mark),
        f.help ? h('div', {class: 'field-hint'}, f.help) : null);
    }
    const hint = [f.help, f.type === 'number' ? `Trong khoảng ${f.min}–${f.max}.` : ''].filter(Boolean).join(' ');
    return h('div', {class: 'proc-field', dataset: {key: f.key}},
      field(h('span', {}, f.label, mark ? ' ' : '', mark), node, {hint}));
  }

  // Ô phụ thuộc một công tắc: công tắc tắt thì làm mờ (vẫn sửa được, chỉ là chưa có tác dụng).
  function syncDepends() {
    data.schema.filter(f => f.depends).forEach(f => {
      const wrap = controls[f.key]?.closest?.('.proc-field');
      if (wrap) wrap.classList.toggle('inactive', !values[f.depends]);
    });
  }

  async function save() {
    saveBtn.disabled = true;
    try {
      const fresh = await request(`/api/system/processors/${encodeURIComponent(data.id)}/settings`, {method: 'POST', body: {values}});
      toast('Đã lưu — áp dụng ngay cho câu trả lời tiếp theo.', 'ok');
      onSaved(fresh);
    } catch (e) { saveBtn.disabled = false; toast(friendlyError(e), 'danger'); }
  }

  async function reset() {
    const ok = await confirmDialog({title: 'Đưa bộ xử lý này về cài đặt gốc?', text: 'Mọi thay đổi ở trang này bị bỏ, dùng lại giá trị mặc định của ứng dụng.', confirm: 'Về mặc định'});
    if (!ok) return;
    try { onSaved(await request(`/api/system/processors/${encodeURIComponent(data.id)}/reset`, {method: 'POST', body: {}})); toast('Đã về cài đặt gốc.', 'ok'); }
    catch (e) { toast(friendlyError(e), 'danger'); }
  }

  const groups = [];
  data.schema.forEach(f => {
    let g = groups.find(x => x.name === (f.group || ''));
    if (!g) groups.push(g = {name: f.group || '', fields: []});
    g.fields.push(f);
  });
  const node = card('Cài đặt', h('div', {class: 'card-body col gap-4'},
    groups.map(g => h('div', {class: 'col gap-3'}, g.name ? h('div', {class: 'proc-group'}, g.name) : null, g.fields.map(row)))),
  {sub: changed.size ? `Đã chỉnh ${changed.size} mục so với mặc định` : 'Đang dùng cài đặt gốc',
    footer: [changed.size ? btn('Về mặc định', {kind: 'ghost', icon: 'undo', onClick: reset}) : null, h('span', {class: 'grow'}), saveBtn]});
  syncDepends();
  return node;
}

// ---------------------------------------------------------------- Thẻ riêng từng bộ
function extraCards(ctx, data, reload) {
  const s = data.stats || {};
  if (data.id === 'thu-du-lieu') {
    const last = (s.days || []).slice(-1)[0];
    return card('Dữ liệu đã ghi', h('div', {class: 'card-body col gap-3'},
      h('div', {class: 'stat-grid'},
        stat('Tổng bình luận', number(s.rows || 0), `${(s.days || []).length} ngày có LIVE`),
        stat('Dung lượng', bytes(s.bytes || 0), 'Trong thư mục dữ liệu'),
        stat('Hôm nay', number(s.today_rows || 0), s.today_duplicates ? `${s.today_duplicates} bình luận trùng đã loại` : 'Không có bình luận trùng'),
        stat('Ngày gần nhất', last ? last.day.split('-').reverse().join('/') : '—', last ? `${number(last.rows)} bình luận` : '')),
      h('div', {class: 'fs-xs muted', style: {overflowWrap: 'anywhere'}}, s.folder || '')),
    {actions: btn('Mở thư mục', {kind: 'ghost', size: 'sm', icon: 'archive', onClick: () => openTarget('raw_stream')})});
  }
  if (data.id === 'lieu-dung') {
    const rows = s.coverage || [];
    const STATUS = {ok: ['Tính được', 'ok'], no_ratio: ['Nhãn không ghi theo lít nước', 'warn'], no_directions: ['Hồ sơ chưa có hướng dẫn', 'warn'], no_profile: ['Chưa có hồ sơ', '']};
    return card(`Sản phẩm trong giỏ: AI tính được liều ${s.ok || 0}/${s.total || 0}`, rows.length ? h('div', {class: 'list'}, rows.map(r => h('div', {class: 'list-row'},
      h('div', {class: 'grow'}, h('div', {class: 'title'}, r.title), r.status !== 'ok' && r.directions.length ? h('div', {class: 'sub'}, 'Nhãn ghi: ' + r.directions.join(' · ').slice(0, 220)) : null),
      chip(...STATUS[r.status]),
      r.status !== 'ok' ? btn('Sửa hồ sơ', {size: 'sm', kind: 'ghost', onClick: () => ctx.navigate('catalog', 'profiles')}) : null)))
      : h('div', {class: 'card-body'}, empty('Chưa có giỏ hàng', 'Mở TikTok LIVE Manager để tiện ích quét giỏ.')),
    {sub: 'Nhãn ghi kiểu “100ml cho 10 bình phun” hay “3kg/ha” thì AI chỉ đọc nguyên văn, không tự quy đổi cho bình khách hỏi. Muốn AI tính được: thêm một dòng theo mẫu “20ml cho bình 16–25 lít” trong hồ sơ.'});
  }
  if (data.id === 'model-deepseek') {
    const area = textarea({rows: 16, value: s.prompt || '', style: {fontFamily: 'var(--font-mono)', fontSize: '12px'}});
    const saveP = btn('Lưu lời nhắc', {kind: 'primary', icon: 'save', onClick: async () => {
      try { await request('/api/system/processors/model-deepseek/prompt', {method: 'POST', body: {text: area.value}}); toast('Đã lưu lời nhắc — áp dụng cho câu tiếp theo.', 'ok'); reload(); }
      catch (e) { toast(friendlyError(e), 'danger'); }
    }});
    const resetP = s.prompt_custom ? btn('Về bản gốc', {kind: 'ghost', icon: 'undo', onClick: async () => {
      const ok = await confirmDialog({title: 'Bỏ lời nhắc đã sửa, dùng lại bản gốc của ứng dụng?', confirm: 'Về bản gốc'});
      if (!ok) return;
      try { await request('/api/system/processors/model-deepseek/prompt', {method: 'POST', body: {text: ''}}); toast('Đã dùng lại bản gốc.', 'ok'); reload(); }
      catch (e) { toast(friendlyError(e), 'danger'); }
    }}) : null;
    return h('div', {class: 'col gap-4'},
      card('Kết nối DeepSeek', h('div', {class: 'card-body col gap-3'},
        !s.key_configured ? notice('danger', 'Chưa có khoá API', 'AI không trả lời được cho tới khi nhập khoá.')
          : s.balance_ok ? notice('ok', 'Đang kết nối', `Model: ${s.model_label || s.model}`)
            : notice('warn', 'Chưa dùng được', s.balance_error || 'Không kiểm tra được số dư.')),
      {actions: [btn('Đổi khoá / model', {kind: 'ghost', size: 'sm', icon: 'key', onClick: () => ctx.navigate('system', 'setup')}),
        btn('Nạp tiền', {kind: 'ghost', size: 'sm', icon: 'arrowRight', onClick: () => openTarget('deepseek_topup')})]}),
      card('Lời nhắc hệ thống (cách AI được dặn trả lời)', h('div', {class: 'card-body col gap-3'},
        notice('info', null, 'Đây là “bản hướng dẫn” gửi kèm mọi câu hỏi: giọng nói, luật giá, luật liều, khi nào im lặng. Sửa sai có thể làm AI trả lời lệch — thử lại ở ô “Thử ngay” bên dưới sau khi lưu.'),
        area),
      {sub: s.prompt_custom ? 'Đang dùng bản ĐÃ SỬA trên máy này' : 'Đang dùng bản gốc của ứng dụng',
        footer: [resetP, h('span', {class: 'grow'}), saveP]}));
  }
  return null;
}

async function openTarget(target) {
  try { await request('/api/system/open', {method: 'POST', body: {target}}); } catch (e) { toast(friendlyError(e), 'danger'); }
}

// ---------------------------------------------------------------- Thử ngay
const TRY_TEXT = {
  price: {title: 'Thử ngay: AI được nói giá nào, câu nào bị chặn', sample: 'đạm cá này nhiêu vậy shop', reply: true, product: true},
  select: {title: 'Thử ngay: khách nói câu này thì AI hiểu là sản phẩm nào', sample: 'chai này pha bao nhiêu', pin: true},
  dose: {title: 'Thử ngay: tính liều cho một sản phẩm', sample: 'bình 25 lít pha bao nhiêu', product: true},
  reply: {title: 'Thử ngay: gửi câu khách hỏi cho DeepSeek (không gửi lên TikTok)', sample: 'phân này bón cho sầu riêng được không', pin: true},
};

function productOptions(products, empty_ = 'Chọn sản phẩm…') {
  return [['', empty_], ...products.map(p => [p.product_id, (p.pinned ? '📌 ' : '') + p.title])];
}

function tryCard(ctx, data) {
  const t = TRY_TEXT[data.try];
  const products = data.products || [];
  const comment = input({value: t.sample, placeholder: 'Câu khách gõ trên LIVE'});
  const pinned = products.find(p => p.pinned);
  const product = t.product || t.pin ? select(productOptions(products, t.pin ? 'Sản phẩm đang ghim trên LIVE' : 'Chọn sản phẩm…'), pinned ? pinned.product_id : '') : null;
  const reply = t.reply ? input({placeholder: 'vd: 2 can là 1.110k nha anh (để trống nếu chỉ xem giá)'}) : null;
  const out = h('div', {class: 'try-result'});
  const run = btn('Thử', {kind: 'primary', icon: 'play', onClick: go});
  comment.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });

  async function go() {
    run.disabled = true;
    clear(out, h('div', {class: 'muted fs-sm'}, data.try === 'reply' ? 'Đang hỏi DeepSeek…' : 'Đang tính…'));
    const body = {comment: comment.value};
    if (t.product) body.product_id = product.value;
    if (t.pin) body.pin_id = product.value;
    if (reply) body.reply = reply.value;
    try {
      const r = await request(`/api/system/processors/${encodeURIComponent(data.id)}/try`, {method: 'POST', body});
      clear(out, ({price: priceResult, select: selectResult, dose: doseResult, reply: replyResult})[data.try](r, ctx));
    } catch (e) { clear(out, notice('danger', null, friendlyError(e))); }
    run.disabled = false;
  }

  return card(t.title, h('div', {class: 'card-body col gap-3'},
    !products.length ? notice('warn', 'Chưa có giỏ hàng để thử', 'Mở TikTok LIVE Manager trong Chrome → bấm tiện ích Kid TE → QUÉT GIỎ TRƯỚC LIVE.') : null,
    h('div', {class: 'form-grid'},
      field('Khách nói', comment),
      product ? field(t.pin ? 'Sản phẩm đang ghim' : 'Sản phẩm', product) : null,
      reply ? field('Câu AI định trả lời (để kiểm tra có bị chặn không)', reply) : null),
    h('div', {class: 'row'}, run), out),
  {sub: 'Chạy đúng phần xử lý dùng khi LIVE, với cài đặt đang lưu. Không ghi gì, không gửi gì.'});
}

const tryRow = (iconName, ...content) => h('div', {class: 'try-row'}, icon(iconName, {size: 16}), h('div', {}, ...content));

function priceResult(r) {
  const p = r.price || {}, sig = r.signal || {}, prod = r.product || {};
  const SOURCE = {gia_goc_da_duyet: 'giá gốc đã duyệt trong hồ sơ', original_price: 'giá gốc TikTok trả về', shop_listed_price: 'giá niêm yết của shop'};
  const RELATION = {lower_than_shop_visible_price: 'THẤP hơn giá shop → coi là giá sau mã của tài khoản khách, không bác bỏ', higher_than_shop_visible_price: 'CAO hơn giá shop', matches_or_unknown: 'bằng giá shop / không so được'};
  return h('div', {class: 'try-why'},
    tryRow('tag', p.quotable_price ? h('span', {}, 'AI được nói: ', h('strong', {}, p.quotable_price), ` (${SOURCE[p.quotable_price_source] || ''})`) : 'Sản phẩm này chưa có giá nào AI được nói → câu hỏi giá vào Hộp thư.'),
    prod.sale_price && prod.sale_price !== p.quotable_price ? tryRow('eyeOff', `Giá đang giảm trên LIVE (${prod.sale_price}) — AI KHÔNG được nói.`) : null,
    tryRow('message', p.voucher_pitch || ''),
    sig.kind ? tryRow('info', `Khách tự báo giá ${sig.reported_price_text}: ${RELATION[sig.relation] || sig.relation}.`, sig.da_mua_roi ? h('div', {class: 'fs-xs muted'}, 'Khách đã mua rồi → câu chăm sóc, không được thúc mua thêm.') : null) : null,
    r.reply_check ? (r.reply_check.blocked
      ? notice('danger', 'Câu này sẽ bị CHẶN và AI phải viết lại', r.reply_check.reason)
      : notice('ok', 'Câu này qua được kiểm tra giá', '')) : null);
}

function selectResult(r) {
  const top = Math.max(r.threshold * 1.5, ...r.ranked.map(x => x.score), 0.1);
  return h('div', {class: 'col gap-3'},
    notice(r.chosen.product_id ? 'ok' : 'warn', r.chosen.title || 'Không chọn được sản phẩm', r.reason),
    r.missing.length ? notice('warn', 'Sản phẩm này thiếu dữ kiện cho câu hỏi', 'AI sẽ không trả lời, câu vào Hộp thư: ' + r.missing.join(', ')) : null,
    h('div', {class: 'fs-xs muted'}, 'Chữ dùng để đoán: ', r.words.length ? r.words.map(w => h('code', {class: 'k-terms'}, w + ' ')) : '(không có — toàn từ để hỏi)'),
    !r.ranked.some(x => x.score > 0) ? h('div', {class: 'fs-sm muted'}, 'Không chữ nào trong câu khớp với tên sản phẩm trong giỏ.') : null,
    h('div', {class: 'list'}, r.ranked.filter(x => x.score > 0).map(x => h('div', {class: 'list-row'},
      h('div', {class: 'grow'}, h('div', {class: 'title'}, x.title, x.pinned ? ' 📌' : ''),
        h('div', {class: 'score-bar'}, h('i', {style: {width: Math.min(100, x.score / top * 100) + '%'}}), h('b', {style: {left: Math.min(100, r.threshold / top * 100) + '%'}, title: 'Ngưỡng'}))),
      h('span', {class: 'num fs-sm'}, x.score.toFixed(2))))),
    h('div', {class: 'fs-xs muted'}, `Vạch dọc = ngưỡng ${r.threshold}. Điểm dưới ngưỡng → giữ sản phẩm đang ghim.`));
}

function doseResult(r) {
  return h('div', {class: 'col gap-3'},
    r.target_liters ? tryRow('activity', `Cỡ bình khách hỏi: ${r.target_liters} lít`) : notice('warn', 'Không thấy cỡ bình trong câu', 'Gõ kiểu “bình 16 lít”, “phuy 200 lít”.'),
    r.directions.length ? h('div', {class: 'try-why'}, h('strong', {class: 'fs-xs'}, 'Hướng dẫn trên nhãn đã duyệt'), r.directions.map(line => h('div', {class: 'fs-sm'}, '• ' + line)))
      : notice('warn', 'Hồ sơ chưa có hướng dẫn sử dụng', 'AI sẽ không trả lời câu hỏi liều cho sản phẩm này.'),
    r.rows.length ? h('div', {class: 'try-why'}, h('strong', {class: 'fs-xs'}, 'AI sẽ được đưa phép tính này'),
      r.rows.map(x => h('div', {class: 'fs-sm'}, (x.is_extrapolated ? '≈ ' : '✓ ') + x.calculation + (x.is_extrapolated ? ' (quy đổi ngoài khoảng nhãn)' : ''))))
      : r.target_liters && r.directions.length ? notice('info', 'Không có phép tính nào', 'Dòng hướng dẫn không có dạng “số ml/g cho số lít”, hoặc đã tắt quy đổi ngoài khoảng. AI chỉ đọc nguyên văn nhãn.') : null);
}

function replyResult(r) {
  const x = r.explain || {};
  const REASON = {missing_data: 'Thiếu dữ kiện nên AI im lặng (câu vào Hộp thư)', model_not_ready: 'Chưa có khoá DeepSeek', policy_blocked: 'Câu bị chặn vì vi phạm chính sách giá', model_skipped: 'DeepSeek không trả lời', no_live_catalog: 'Chưa có giỏ hàng'};
  const m = r.metrics || {};
  return h('div', {class: 'col gap-3'},
    r.reply ? h('div', {class: 'bubble reply ai'}, r.reply) : notice('warn', REASON[r.reason] || 'Không có câu trả lời', (x.missing || r.missing_fields || []).join(', ')),
    h('div', {class: 'try-why'},
      x.product ? tryRow('box', 'Sản phẩm: ', h('strong', {}, x.product.title || '—'), x.learned_product ? ' (theo tên gọi đã dạy)' : '', x.has_profile ? '' : ' · chưa có hồ sơ') : null,
      (x.learned || []).length ? tryRow('sparkles', `Dùng ${x.learned.length} điều đã dạy`) : null,
      (x.examples || []).length ? tryRow('message', `Tham khảo ${x.examples.length} câu mẫu của chủ shop`) : null,
      x.persona ? tryRow('user', 'Phong cách: ' + x.persona) : null,
      m.total_ms ? tryRow('clock', `${(m.total_ms / 1000).toFixed(1)} giây · ${number(m.prompt_tokens || 0)} token vào (${number(m.prompt_cache_hit_tokens || 0)} từ bộ nhớ đệm) · ${number(m.completion_tokens || 0)} token ra${m.shortened_from ? ` · đã tự viết gọn từ ${m.shortened_from} ký tự` : ''}`) : null));
}
