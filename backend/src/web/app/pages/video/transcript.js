import {h, clear, debounce} from '../../core/dom.js';
import {icon} from '../../core/icons.js';
import {clock, fold} from '../../core/format.js';
import {bag, iconBtn, empty} from '../../ui/kit.js';

// Từ chung của tên sản phẩm nông nghiệp — không bao giờ tự nó là dấu hiệu.
const GENERIC = new Set(('san pham chai can cho cay giup nong duoc toan cau phan bon thuoc huu co cong nghe sieu dang moi cac loai trong chuyen dung '
  + 'manh xanh la phat trien cuc chat dinh duong mai vang hap thu nhanh cai tien thoi bao day cung hanh vua gia goi combo tang kem mua ban nha nhe voi cua mot hai '
  + 'lit ra hoa dau qua trai lon to re kich bung choi dot vot tri sach benh nam sau hang che pham tac mat an toan').split(' '));

function tokensOf(text) { return fold(text).split(/[^a-z0-9]+/).filter(Boolean); }

export function normalizeLines(transcript) {
  const segments = (transcript && transcript.segments) || [];
  return segments.map((seg, i) => {
    const startMs = Math.round(Number(seg.start_ms ?? (Number(seg.start) || 0) * 1000));
    const endMs = Math.round(Number(seg.end_ms ?? (Number(seg.end) || 0) * 1000)) || startMs + 2000;
    const text = String(seg.text || '').trim();
    const folded = fold(text);
    return {i, startMs, endMs, text, folded, padded: ' ' + folded.split(/[^a-z0-9]+/).filter(Boolean).join(' ') + ' '};
  }).filter(line => line.text);
}

/**
 * Dấu hiệu nhận diện sản phẩm trong lời thoại.
 *
 * Bản cũ lấy TỪ ĐƠN hiếm trong tên sản phẩm => các từ rất thông dụng ("con",
 * "sâu", "bấm", "hàng", "đẹp", "gốc") thành dấu hiệu và câu "bấm vào giỏ hàng"
 * bị gắn nhầm "Siêu bám dính". Nay:
 *  - ứng viên là cụm 1–3 từ liên tiếp trong TÊN GỌN (bỏ từ chung ngành);
 *  - cụm chỉ được dùng nếu thuộc riêng ≤ 1 sản phẩm;
 *  - lọc theo chính lời thoại: từ đơn xuất hiện ở > 1,5% số câu là từ nói
 *    thường ngày (trừ mã có chữ + số như tc01, ga3); cụm nhiều từ ngưỡng 8%.
 */
export function productAnchors(products, lines = []) {
  const docFreq = new Map();
  lines.forEach(line => { new Set(line.padded.trim().split(' ')).forEach(t => docFreq.set(t, (docFreq.get(t) || 0) + 1)); });
  const phraseFreq = phrase => { let n = 0; for (const l of lines) if (l.padded.includes(' ' + phrase + ' ')) n += 1; return n; };
  const total = Math.max(1, lines.length);
  const cands = products.map(p => {
    const words = tokensOf(p.name).filter(w => !/^\d+(ml|l|g|gr|kg|lit)?$/.test(w));
    const set = new Map();
    for (let n = 1; n <= 3; n += 1) {
      for (let i = 0; i + n <= words.length; i += 1) {
        const win = words.slice(i, i + n);
        const code = win.some(w => /[a-z]/.test(w) && /\d/.test(w));
        if (!code && win.every(w => GENERIC.has(w))) continue;
        if (n === 1 && !code && (GENERIC.has(win[0]) || win[0].length < 4)) continue;
        set.set(win.join(' '), {phrase: win.join(' '), n, code});
      }
    }
    return {product: p, set};
  });
  const spread = new Map();
  cands.forEach(c => c.set.forEach((_, k) => spread.set(k, (spread.get(k) || 0) + 1)));
  return cands.map(({product, set}) => ({product, anchors: [...set.values()].filter(a => {
    if (spread.get(a.phrase) > 1) return false;
    if (!lines.length) return true;
    if (a.code) return true;
    const rate = (a.n === 1 ? (docFreq.get(a.phrase) || 0) : phraseFreq(a.phrase)) / total;
    return rate <= (a.n === 1 ? 0.015 : 0.08);
  }).map(a => ({...a, weight: a.code ? 3 : (a.n >= 2 && a.phrase.replace(/ /g, '').length >= 5) ? 2 : 1}))}));
}

// Từ báo hiệu QUÀ TẶNG: sản phẩm đứng sau các từ này là quà đi kèm, không phải
// hàng host đang bán ("combo đạm cá em tặng thêm 1 kích rễ" -> kích rễ là quà).
const GIFT_WORDS = [' tang ', ' qua tang ', ' duoc nhan ', ' nhan them ', ' nhan duoc ', ' kem theo ', ' free ', ' mien phi '];

/**
 * Sản phẩm được nhắc trong câu: {product, score, gift} hoặc null.
 * Cần điểm ≥ 2 và không hoà với sản phẩm khác.
 */
export function detectMention(line, anchors) {
  let best = null, second = 0;
  anchors.forEach(({product, anchors: list}) => {
    let score = 0, first = Infinity;
    list.forEach(a => {
      const at = line.padded.indexOf(' ' + a.phrase + ' ');
      if (at >= 0) { score += a.weight; first = Math.min(first, at); }
    });
    if (!score) return;
    if (!best || score > best.score) { second = best ? best.score : 0; best = {product, score, at: first}; }
    else if (score > second) second = score;
  });
  if (!best || best.score < 2 || best.score <= second) return null;
  const gift = GIFT_WORDS.some(word => { const g = line.padded.indexOf(word); return g >= 0 && g < best.at; });
  return {product: best.product, score: best.score, gift};
}

function highlight(text, query) {
  if (!query) return text;
  const folded = fold(text);
  const idx = folded.indexOf(query);
  if (idx < 0) return text;
  // fold() giữ nguyên độ dài với tiếng Việt thông thường (bỏ dấu kết hợp)
  return [text.slice(0, idx), h('mark', {}, text.slice(idx, idx + query.length)), text.slice(idx + query.length)];
}

export function createTranscript({onSeek, onPinFrom}) {
  let lines = [];
  let anchors = [];
  let chips = new Map();
  let gifts = new Set();
  let activeIndex = -1;
  let query = '';
  let matches = [];
  let matchPos = -1;
  let follow = true;
  let userScrollAt = 0;
  let range = {lo: -1, hi: -2, color: ''};   // các câu thuộc đoạn đang chọn
  let pinResolver = null;                     // ms → sản phẩm đang ghim (từ Studio)
  const rows = [];

  const counter = h('span', {class: 'fs-xs muted nowrap'});
  const searchInput = h('input', {class: 'input', type: 'search', placeholder: 'Tìm trong lời thoại · Ctrl+F (không cần dấu)', 'aria-label': 'Tìm trong lời thoại'});
  const followBtn = iconBtn('target', 'Tự cuộn theo video', () => { follow = !follow; syncFollow(); if (follow) scrollToActive(true); }, {size: 'sm', attrs: {class: 'btn icon ghost sm follow-btn'}});
  const prevBtn = iconBtn('chevronUp', 'Kết quả trước (Shift+Enter)', () => jump(-1), {size: 'sm'});
  const nextBtn = iconBtn('chevronDown', 'Kết quả sau (Enter)', () => jump(1), {size: 'sm'});
  const list = h('div', {class: 'tlist', role: 'list'});
  const body = h('div', {class: 'side-body'}, list);
  const tools = h('div', {class: 'side-tools'}, h('div', {class: 'search-input'}, icon('search'), searchInput), counter, prevBtn, nextBtn, followBtn);
  const root = h('div', {style: {display: 'flex', flexDirection: 'column', minHeight: '0', flex: '1'}}, tools, body);
  function syncFollow() { followBtn.setAttribute('aria-pressed', String(follow)); followBtn.style.color = follow ? 'var(--studio-accent)' : ''; }
  syncFollow();

  body.addEventListener('wheel', () => { userScrollAt = Date.now(); }, {passive: true});

  function renderRows() {
    rows.length = 0;
    const nodes = lines.map(line => {
      const product = chips.get(line.i);
      const node = h('div', {class: 'tline', role: 'listitem', dataset: {i: line.i}, title: 'Bấm để tua tới câu này'},
        h('time', {}, clock(line.startMs)),
        h('div', {}, product ? h('span', {class: 'tchip' + (gifts.has(line.i) ? ' gift' : ''), title: gifts.has(line.i) ? `Quà tặng kèm: ${product.name}` : `Đang nói về: ${product.name}`}, gifts.has(line.i) ? icon('star', {size: 11}) : null, bag(product.bag_index, {ghost: gifts.has(line.i)})) : null,
          h('span', {class: 'ttext'}, highlight(line.text, query))));
      rows.push(node);
      return node;
    });
    for (let i = Math.max(0, range.lo); i <= range.hi && i < rows.length; i += 1) rows[i].classList.add('in-seg');
    if (pinResolver) applyPins();
    if (!nodes.length) clear(list, empty('Chưa có lời thoại', 'Video này chưa được chuyển lời nói thành chữ. Video tải lên qua “Phân tích bằng AI” sẽ có lời thoại.', {iconName: 'message'}));
    else clear(list, nodes);
  }

  list.addEventListener('click', e => {
    const pinBtn = e.target.closest('[data-pin]');
    const row = e.target.closest('.tline');
    if (!row) return;
    const line = lines.find(l => l.i === Number(row.dataset.i));
    if (!line) return;
    if (pinBtn) onPinFrom(line.startMs, pinBtn.dataset.pin === 'direct' ? pinBtn.dataset.key : undefined); else onSeek(line.startMs);
  });
  // Nút ghim chỉ gắn khi rê chuột (không dựng 1000+ nút cùng lúc). Câu có
  // nhắc sản phẩm → nút ghim THẲNG sản phẩm đó (1 cú bấm), kèm nút chọn khác.
  let hoverRow = null;
  const directPin = h('button', {class: 'btn sm primary', type: 'button', dataset: {pin: 'direct'}, style: {minHeight: '24px', padding: '0 8px'}});
  const pickPin = h('button', {class: 'btn sm soft', type: 'button', dataset: {pin: 'pick'}, title: 'Chọn sản phẩm để ghim từ câu này', style: {minHeight: '24px', padding: '0 8px'}}, icon('pin'), 'Ghim từ đây');
  const pinAction = h('span', {class: 'row', style: {position: 'absolute', right: '58px', top: '4px', gap: '4px'}}, directPin, pickPin);
  list.addEventListener('mouseover', e => {
    const row = e.target.closest('.tline');
    if (!row || row === hoverRow) return;
    hoverRow = row;
    const said = chips.get(Number(row.dataset.i));
    const gift = gifts.has(Number(row.dataset.i));
    directPin.hidden = !said || gift;
    if (said && !gift) {
      directPin.dataset.key = 'sku_' + said.product_id;
      directPin.textContent = `Ghim #${said.bag_index} từ đây`;
      directPin.title = `Ghim ${said.name} bắt đầu từ câu này`;
      clear(pickPin, icon('more'));
      pickPin.title = 'Ghim sản phẩm khác từ câu này';
    } else {
      clear(pickPin, icon('pin'), 'Ghim từ đây');
      pickPin.title = 'Chọn sản phẩm để ghim từ câu này';
    }
    row.style.position = 'relative';
    row.append(pinAction);
  });
  list.addEventListener('mouseleave', () => { pinAction.remove(); hoverRow = null; });

  const runSearch = debounce(() => {
    query = fold(searchInput.value);
    matches = query ? lines.filter(l => l.folded.includes(query)).map(l => l.i) : [];
    matchPos = matches.length ? 0 : -1;
    renderRows();
    rows.forEach(r => r.classList.toggle('hit', Boolean(query) && matches.includes(Number(r.dataset.i))));
    counter.textContent = query ? (matches.length ? `${matchPos + 1}/${matches.length}` : '0 kết quả') : '';
    if (matches.length) focusMatch();
  }, 160);
  searchInput.addEventListener('input', runSearch);
  searchInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); jump(e.shiftKey ? -1 : 1, true); }
    if (e.key === 'Escape') { searchInput.value = ''; runSearch(); searchInput.blur(); }
  });

  function focusMatch(seek = false) {
    const i = matches[matchPos];
    const row = rows.find(r => Number(r.dataset.i) === i);
    if (row) { follow = false; syncFollow(); row.scrollIntoView({block: 'center'}); }
    counter.textContent = `${matchPos + 1}/${matches.length}`;
    if (seek) { const line = lines.find(l => l.i === i); if (line) onSeek(line.startMs); }
  }
  function jump(dir, seek = true) {
    if (!matches.length) return;
    matchPos = (matchPos + dir + matches.length) % matches.length;
    focusMatch(seek);
  }

  /**
   * Cột phải của mỗi câu: đang ghim giỏ nào lúc câu đó được nói. Chỉ ghi số
   * giỏ ở câu ĐẦU của mỗi đoạn (đỡ rối); các câu sau có vạch màu giỏ. Câu
   * host nhắc sản phẩm KHÁC sản phẩm đang ghim được tô cảnh báo.
   */
  function applyPins() {
    let prevSig = null;
    lines.forEach((line, k) => {
      const row = rows[k];
      if (!row) return;
      const pin = pinResolver(line.startMs);
      const sig = pin ? pin.sig : '';
      const start = Boolean(pin) && sig !== prevSig;
      prevSig = sig;
      const said = chips.get(line.i);
      const mismatch = Boolean(said && !gifts.has(line.i) && !(pin && pin.accept.includes('sku_' + said.product_id)));
      const state = `${sig}|${start}|${mismatch}|${pin ? pin.label : ''}`;
      if (row._pin === state) return;
      row._pin = state;
      row.style.setProperty('--pin-color', pin ? pin.color : 'transparent');
      row.classList.toggle('pin-start', start);
      row.classList.toggle('mismatch', mismatch);
      let tag = row.querySelector('.tpin');
      if (start) {
        if (!tag) { tag = h('span', {class: 'tpin'}); row.append(tag); }
        tag.textContent = pin.label;
        tag.title = pin.title;
      } else if (tag) tag.remove();
      if (mismatch) row.title = `Host nói về #${said.bag_index} ${said.name} nhưng lúc này ${pin ? pin.title.toLowerCase() : 'không ghim sản phẩm nào'}`;
      else row.title = 'Bấm để tua tới câu này';
    });
  }

  function lineAt(ms) {
    let lo = 0, hi = lines.length - 1, found = -1;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (lines[mid].startMs <= ms) { found = mid; lo = mid + 1; } else hi = mid - 1; }
    return found;
  }
  function scrollToActive(force = false) {
    if (activeIndex < 0 || !rows[activeIndex]) return;
    if (!force && (!follow || Date.now() - userScrollAt < 3000)) return;
    const row = rows[activeIndex];
    const top = row.offsetTop - body.clientHeight * 0.35;
    const delta = Math.abs(body.scrollTop - top);
    // Nhảy xa (tua, chuyển mốc) thì tới ngay; chỉ cuộn mượt khi nhích từng câu.
    if (delta > 4) body.scrollTo({top, behavior: force || delta > body.clientHeight ? 'auto' : 'smooth'});
  }

  return {
    root,
    focusSearch() { searchInput.focus(); searchInput.select(); },
    setData(transcript, products) {
      lines = normalizeLines(transcript);
      anchors = productAnchors(products || [], lines);
      chips = new Map();
      gifts = new Set();
      lines.forEach(line => { const m = detectMention(line, anchors); if (m) { chips.set(line.i, m.product); if (m.gift) gifts.add(line.i); } });
      activeIndex = -1;
      renderRows();
    },
    lines: () => lines,
    /** Các câu có nhắc sản phẩm: [{ms, endMs, key, text, product}] cho bộ đối chiếu. */
    mentions: () => lines.filter(l => chips.has(l.i) && !gifts.has(l.i)).map(l => ({ms: l.startMs, endMs: l.endMs, key: 'sku_' + chips.get(l.i).product_id, text: l.text, product: chips.get(l.i)})),
    count: () => lines.length,
    setTime(ms) {
      const i = lineAt(ms);
      const within = i >= 0 && ms <= lines[i].endMs + 1500 ? i : -1;
      if (within === activeIndex) return;
      if (activeIndex >= 0 && rows[activeIndex]) rows[activeIndex].classList.remove('active');
      activeIndex = within;
      if (activeIndex >= 0 && rows[activeIndex]) { rows[activeIndex].classList.add('active'); scrollToActive(); }
    },
    /** resolve(ms) → {sig, accept:[khoá], color, label, title} | null */
    setPins(resolve) { pinResolver = resolve; if (resolve) applyPins(); },
    /** Đánh dấu các câu nằm trong [startMs, endMs) — viền màu giỏ của đoạn đang chọn. */
    setRange(startMs, endMs, color = '') {
      if (color !== range.color) { list.style.setProperty('--seg-color', color || 'transparent'); range.color = color; }
      let lo = -1, hi = -2;
      if (lines.length && endMs > startMs) {
        lo = Math.max(0, lineAt(startMs));
        if (lo < lines.length && lines[lo].endMs < startMs) lo += 1;
        hi = lineAt(Math.max(startMs, endMs - 1));
      }
      if (lo === range.lo && hi === range.hi) return;
      for (let i = Math.max(0, range.lo); i <= range.hi && i < rows.length; i += 1) if (i < lo || i > hi) rows[i].classList.remove('in-seg');
      for (let i = Math.max(0, lo); i <= hi && i < rows.length; i += 1) if (i < range.lo || i > range.hi) rows[i].classList.add('in-seg');
      range = {...range, lo, hi};
    },
    destroy() { runSearch.cancel(); },
    textAround(ms, spanMs = 8000) { return lines.filter(l => l.endMs >= ms - spanMs && l.startMs <= ms + spanMs).map(l => l.text).join(' '); },
  };
}
