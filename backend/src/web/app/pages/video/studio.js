import {h, clear, disposer, debounce, isTypingTarget, uid} from '../../core/dom.js';
import {icon} from '../../core/icons.js';
import {request, friendlyError} from '../../core/api.js';
import {clock, parseClock, shortDuration, durationText, timeOnly, timeOrDate, relative, number} from '../../core/format.js';
import {btn, iconBtn, chip, bag, bagColor, productAvatar, empty, errorBox, notice, toggle, select, field, progress} from '../../ui/kit.js';
import {toast, confirmDialog, isOverlayOpen} from '../../ui/overlays.js';
import {REVIEW_STATUS, CONFIDENCE, lookup} from '../../labels.js';
import * as M from './model.js';
import {loadCatalog, invalidateCatalog, resolveProduct, watchAssets} from './data.js';
import {createPlayer} from './player.js';
import {createTimeline} from './timeline.js';
import {createTranscript} from './transcript.js';
import {pickProduct} from './picker.js';
import {saveRecovery, listRecovery} from './recovery.js';
import {cartSelect} from '../cart-picker.js';

const ALT_CHOICES = [[30000, '30 giây'], [45000, '45 giây'], [60000, '1 phút'], [90000, '1 phút 30'], [120000, '2 phút'], [180000, '3 phút']];
// Phát lại từ trước mốc để nghe được cả câu dẫn — host thường giới thiệu vài giây trước khi gọi tên sản phẩm.
const PREROLL_MS = 3000;

const pref = {
  get(key, fallback) { try { const v = localStorage.getItem('lr-studio-' + key); return v === null ? fallback : JSON.parse(v); } catch (_) { return fallback; } },
  set(key, value) { try { localStorage.setItem('lr-studio-' + key, JSON.stringify(value)); } catch (_) { /* */ } },
};

export function mountStudio(ctx, host, {kind, id}) {
  const d = disposer();
  let closed = false;
  let loadGeneration = 0;
  const loadController = new AbortController();
  d.add(() => {closed = true; loadGeneration++; loadController.abort();});
  const isReview = kind === 'review';
  let catalog = {byId: {}, products: []};
  let catalogCart = '';           // giỏ (tài khoản LIVE) của danh mục đang nạp; '' = giỏ đang hoạt động
  let history = null;
  let published = null;
  let original = null;
  let revision = '';
  let draftRevision = 0;
  let lastAutosaved = null;
  let recoveryTimer = 0;
  let recoveryError = null;
  let recoveryWrite = Promise.resolve();
  let selected = 0;
  let playheadMs = 0;
  let saveState = {state: 'clean', at: null, error: null};
  let pendingSave = null;
  let transcriptData = {segments: []};
  let sideTab = 'segment';        // 'segment' (chi tiết đoạn) | 'segments' | 'checks'
  let appliedAt = null;           // lúc lịch ghim được ghi lần cuối (thư viện)
  let loopOn = false;
  let autoPlay = pref.get('autoplay', true);
  let followUntil = 0;           // chặn "đi theo đầu phát" ngay sau khi người dùng tự chọn đoạn
  let lastNowKey = '';
  let jobState = null;
  let suggestions = [];
  const dismissed = new Set();
  let player = null, timeline = null, transcript = null, assetsWatch = null;
  const els = {};

  clear(host, h('div', {class: 'studio', style: {display: 'grid', placeItems: 'center'}}, h('div', {class: 'loading', style: {color: '#a9b4c2'}}, h('span', {class: 'spinner'}), 'Đang mở Studio…')));
  const doc = () => history.get();

  // ============================================================ tải dữ liệu
  async function load() {
    const generation = ++loadGeneration;
    try {
      const [detail, cat] = await Promise.all([
        request(isReview ? `/api/workspace/video-jobs/${encodeURIComponent(id)}` : `/api/workspace/videos/${encodeURIComponent(id)}`, {signal: loadController.signal}),
        loadCatalog({force: true}).catch(() => ({byId: {}, products: []})),
      ]);
      if (closed || generation !== loadGeneration) return;
      catalog = cat;
      const data = detail.data || {};
      revision = detail.revision || data.revision || '';
      draftRevision = data.draft_revision || 0;
      transcriptData = data.transcript || {segments: []};
      let current;
      if (isReview) {
        if (data.state === 'published' && data.video_id) { ctx.navigate('video', 'studio', data.video_id, {replace: true}); return; }
        jobState = data;
        original = data.draft || {};
        current = M.fromReview(data);
        published = current;
      } else {
        const timelineData = data.timeline || {};
        appliedAt = data.applied_at || null;
        published = M.fromLibrary({...timelineData, video_id: id});
        current = data.saved_draft ? M.fromLibrary({...timelineData, video_id: id}, {draft: data.saved_draft}) : published;
        if (data.saved_draft && M.sameContent(current, published)) current = published;
        if (current !== published) saveState = {state: 'draft', at: data.draft_saved_at, error: null};
      }
      if (current.cartKey) {
        // Timeline nhớ giỏ đã chọn: nạp đúng danh mục giỏ đó.
        const own = await loadCatalog({force: true, cart: current.cartKey}).catch(() => null);
        if (closed || generation !== loadGeneration) return;
        if (own) { catalog = own; catalogCart = current.cartKey; }
      }
      history = M.createHistory(current);
      lastAutosaved = current;
      build();
    } catch (error) {
      if (closed || generation !== loadGeneration) return;
      clear(host, h('div', {class: 'page'}, errorBox(friendlyError(error), load),
        h('div', {style: {marginTop: '16px'}}, btn('Về thư viện video', {icon: 'arrowLeft', onClick: () => ctx.navigate('video')}))));
    }
  }

  // ============================================================ bố cục
  function build() {
    const mediaUrl = isReview ? `/api/video/job-media/${encodeURIComponent(id)}` : `/api/video/media/${encodeURIComponent(id)}`;
    player = createPlayer({src: mediaUrl, durationMs: doc().durationMs, onTime: ms => onTime(ms)});
    player.onPrevSegment = () => jumpSegment(-1);
    player.onNextSegment = () => jumpSegment(1);

    timeline = createTimeline({
      getDoc: doc, getCatalog: () => catalog, getSelection: () => selected,
      onSeek: ms => seek(ms),
      onSelect: (index, {seekTo} = {}) => { showSegmentTab(); select_(index); if (seekTo !== undefined) seek(seekTo); },
      onCommitMove: (index, ms) => commit(M.moveStart(doc(), index, ms), `Đổi giờ bắt đầu đoạn ${index + 1} → ${clock(ms)}`, {select: index}),
      onEditProduct: index => editProduct(index),
      onScrubPreview: ms => player.seek(ms),
      onMarker: marker => { if (marker && marker.suggestion) focusSuggestion(marker.suggestion); },
    });
    transcript = createTranscript({onSeek: ms => seek(ms), onPinFrom: (ms, key) => (key ? pinKeyFrom(ms, key) : pinWithPicker(ms))});
    transcript.setData(transcriptData, catalog.products);
    timeline.setTranscript(transcript.lines());

    els.title = h('input', {value: doc().title, 'aria-label': 'Tên video', maxlength: '200', onChange: e => {
      const value = e.target.value.trim();
      if (!value || value === doc().title) { e.target.value = doc().title; return; }
      commit({...doc(), title: value}, 'Đổi tên video');
    }, onKeydown: e => { if (e.key === 'Enter') e.target.blur(); if (e.key === 'Escape') { e.target.value = doc().title; e.target.blur(); } }});
    els.sub = h('div', {class: 'studio-sub'});
    els.saveState = h('span', {class: 'save-state'});
    els.undo = iconBtn('undo', 'Hoàn tác (Ctrl+Z)', () => undo());
    els.redo = iconBtn('redo', 'Làm lại (Ctrl+Y)', () => redo());
    els.primary = h('span', {class: 'row'});
    els.live = h('span', {class: 'row'});
    // Mỗi tài khoản LIVE một giỏ: chọn giỏ để lấy đúng sản phẩm; lưu vào timeline (cart_key).
    els.cart = cartSelect(doc().cartKey || '', key => commit({...doc(), cartKey: key}, 'Đổi giỏ sản phẩm'));
    const top = h('header', {class: 'studio-top'},
      iconBtn('arrowLeft', 'Về thư viện video', () => ctx.navigate('video')),
      h('div', {class: 'studio-title'}, els.title, els.sub),
      els.cart, els.live, els.saveState, els.undo, els.redo,
      iconBtn('keyboard', 'Phím tắt (?)', () => ctx.shell.showShortcuts()),
      btn('Phục hồi', {size: 'sm', kind: 'ghost', onClick: restoreRecovery}),
      els.primary);

    // Bố cục: [video] [lời thoại] [bảng chỉnh].
    // Lời thoại nằm SÁT video để mắt đọc chữ chạy mà vẫn thấy hình; bảng
    // chỉnh gọn bên phải (đang ghim → duyệt → chi tiết đoạn/kiểm tra).
    els.review = h('section', {class: 'review-strip'});
    els.drift = h('div', {class: 'col', style: {gap: '8px'}});
    els.now = h('section', {class: 'now-box'});
    els.inspector = h('div', {class: 'insp'});
    els.sideTabs = h('div', {class: 'side-tabs', role: 'tablist'});
    els.sideBody = h('div', {class: 'side-body'});
    const panel = h('aside', {class: 'panel-col'},
      // Duyệt: chỉ tiến độ duyệt ở trên để bảng quyết định lên cao; chỉnh lịch: đang ghim + cảnh báo.
      isReview ? h('div', {class: 'panel-top'}, els.review) : h('div', {class: 'panel-top'}, els.now, els.drift),
      els.sideTabs, els.sideBody);
    const transcriptCol = h('section', {class: 'tx-col', 'aria-label': 'Lời thoại'}, transcript.root);
    const studio = h('div', {class: 'studio' + (isReview ? ' is-review' : '')}, top, h('div', {class: 'studio-main'}, player.root, transcriptCol, panel), timeline.root);
    try { const saved = localStorage.getItem('lr-tl-h'); if (saved) studio.style.setProperty('--tl-h', saved); } catch (_) { /* */ }
    // Nút bấm bằng chuột không giữ focus: nếu không, Enter/Space kế tiếp vừa
    // kích hoạt lại nút vừa chạy phím tắt (duyệt xong lại bị bỏ duyệt).
    studio.addEventListener('click', e => {
      const b = e.target.closest('button');
      if (b && e.detail > 0 && !b.closest('.modal')) b.blur();
    });
    clear(host, studio);
    els.studio = studio;
    els.sideBody.addEventListener('scroll', () => { sideScroll[sideTab] = els.sideBody.scrollTop; }, {passive: true});

    assetsWatch = watchAssets(isReview ? {jobId: id} : {videoId: id}, state => {
      timeline.setAssets(state);
      if (state.media && state.media.fps) player.setFps(state.media.fps);
    });
    d.add(() => assetsWatch.stop());

    const renderLive = dash => {
      const v = dash && dash.video;
      const playingHere = !isReview && v && v.video_id === id && ['watching', 'detected', 'running', 'tracking'].includes(v.state) && Number.isFinite(Number(v.position_s));
      if (!playingHere) { clear(els.live); return; }
      const ms = Math.round(Number(v.position_s) * 1000);
      clear(els.live, h('span', {class: 'live-follow', title: 'Tiện ích đang nghe âm thanh LIVE và nhận ra video này'}, h('i', {class: 'status-dot live'}), `LIVE đang phát tới ${clock(ms)}`,
        btn('Tới đó', {size: 'sm', onClick: () => { seek(ms); timeline.reveal(ms); }})));
    };
    d.add(ctx.store.subscribe(state => renderLive(state.dashboard)));
    renderLive(ctx.store.get().dashboard);

    recomputeSuggestions();
    refreshAll();
    requestAnimationFrame(() => timeline.relayout());
    if (isReview) {
      const first = M.nextPending(doc(), -1);
      if (first >= 0) goTo(first, {play: false});
    }
  }

  // ============================================================ vẽ lại
  function refreshAll() {
    if (closed) return;
    if ((doc().cartKey || '') !== catalogCart) reloadCartCatalog();
    renderHeader();
    syncTranscriptPins();
    timeline.render();
    syncMarkers();
    renderNow(true);
    renderInspector();
    renderSide();
    if (isReview) renderReviewStrip();
  }

  let cartLoading = null;
  function reloadCartCatalog() {
    const want = doc().cartKey || '';
    if (cartLoading === want) return;
    cartLoading = want;
    loadCatalog({force: true, cart: want}).then(cat => {
      if (closed || (doc().cartKey || '') !== want) return;
      catalog = cat; catalogCart = want; cartLoading = null;
      transcript.setData(transcriptData, catalog.products);
      refreshAll();
    }).catch(error => { cartLoading = null; catalogCart = want; toast(friendlyError(error), 'danger'); });
  }

  function renderHeader() {
    if (closed) return;
    const current = doc();
    const issues = M.checks(current, catalog.byId);
    const counts = M.reviewCounts(current);
    const keys = M.usedProductKeys(current);
    clear(els.sub,
      h('span', {}, clock(current.durationMs)),
      h('span', {}, `${current.segments.length} đoạn`),
      h('span', {}, `${keys.length} sản phẩm`),
      transcript.count() ? h('span', {}, `${number(transcript.count())} câu thoại`) : null,
      isReview ? null : h('span', {class: 'mono'}, id));
    els.undo.disabled = !history.canUndo();
    els.redo.disabled = !history.canRedo();

    const state = saveState.state;
    const differs = !isReview && !M.sameContent(current, published);
    const drift = !isReview && issues.some(i => i.code === 'bag_changed');
    // Trạng thái nói KẾT QUẢ, không nói hành động: "Đã áp dụng lúc 15:10"
    // thay cho nút mờ "Đang áp dụng" (đọc như đang chạy dở).
    const dot = state === 'saving' || state === 'dirty' || state === 'draft' ? 'warn' : state === 'error' ? 'danger' : 'ok';
    const text = state === 'saving' ? 'Đang lưu…'
      : state === 'error' ? 'Lưu lỗi — bấm để thử lại'
      : state === 'dirty' ? 'Chưa lưu'
      : isReview ? (saveState.at ? `Đã lưu ${timeOnly(saveState.at)}` : 'Chưa có thay đổi')
      : state === 'draft' || differs ? `Bản nháp chưa áp dụng${saveState.at ? ' · lưu ' + relative(saveState.at) : ''}`
      : drift ? 'Đang dùng cho LIVE · số giỏ cũ'
      : `Đã áp dụng cho LIVE${appliedAt ? ' · ' + timeOrDate(appliedAt) : ''}`;
    clear(els.saveState, h('i', {class: 'status-dot ' + (drift && state === 'clean' ? 'warn' : dot)}), text);
    els.saveState.classList.toggle('applied', !isReview && state === 'clean' && !differs && !drift);
    els.saveState.style.cursor = state === 'error' ? 'pointer' : '';
    els.saveState.onclick = state === 'error' ? () => flushSave() : null;

    const errors = issues.filter(i => i.level === 'error' && i.code !== 'pending');
    if (isReview) {
      clear(els.primary, btn(counts.pending ? `Còn ${counts.pending} mốc chưa duyệt` : 'Phát hành video', {kind: counts.pending ? '' : 'primary', icon: counts.pending ? 'clock' : 'send',
        disabled: Boolean(counts.pending) || errors.length > 0, onClick: publish, attrs: {title: counts.pending ? 'Duyệt hết các mốc rồi mới phát hành được' : 'Đưa lịch ghim này vào thư viện để dùng khi LIVE'}}));
    } else {
      const title = errors.length ? errors[0].message : 'Ghi lịch ghim này để tiện ích dùng ở phiên LIVE (Ctrl+S)';
      clear(els.primary,
        differs ? btn('Bỏ thay đổi', {kind: 'ghost', onClick: discardDraft}) : null,
        differs ? btn('Áp dụng cho LIVE', {kind: 'primary', icon: 'send', disabled: errors.length > 0, onClick: () => apply(), attrs: {title}})
          : drift ? btn('Cập nhật số giỏ', {kind: 'primary', icon: 'refresh', disabled: errors.length > 0, onClick: () => apply({syncOnly: true}), attrs: {title: 'Giờ ghim và sản phẩm giữ nguyên, chỉ đổi số giỏ theo giỏ hàng hôm nay'}})
          : null);
      renderDriftBanner(issues);
    }
  }

  /** Lời thoại ↔ lịch ghim: mỗi câu biết lúc đó đang ghim giỏ nào. */
  function syncTranscriptPins() {
    const current = doc();
    const cache = new Map();
    transcript.setPins(ms => {
      const pin = M.pinnedAt(current, ms);
      if (pin.index < 0 || !pin.product) return null;
      const seg = current.segments[pin.index];
      const sig = `${pin.index}:${seg.product}:${seg.alt || ''}`;
      if (cache.has(sig)) return cache.get(sig);
      const p = resolveProduct(current, seg.product, catalog);
      const a = seg.alt ? resolveProduct(current, seg.alt, catalog) : null;
      const value = p ? {sig, accept: [seg.product, seg.alt].filter(Boolean), color: bagColor(p.bag_index),
        label: a ? `#${p.bag_index}·${a.bag_index}` : `#${p.bag_index}`,
        title: `Đang ghim #${p.bag_index} ${p.name}${a ? ` ↔ #${a.bag_index} ${a.name}` : ''}`} : null;
      cache.set(sig, value);
      return value;
    });
  }

  function renderReviewStrip() {
    const current = doc();
    const counts = M.reviewCounts(current);
    const total = current.segments.length || 1;
    const done = total - counts.pending;
    const high = current.segments.filter(s => (s.review || 'pending') === 'pending' && s.ai && s.ai.confidence === 'high').length;
    const autoBtn = h('button', {class: 'btn sm ghost auto-btn', type: 'button', 'aria-pressed': String(autoPlay), title: 'Tự phát từ 3 giây trước mốc khi chuyển mốc', onClick: () => { autoPlay = !autoPlay; pref.set('autoplay', autoPlay); renderReviewStrip(); }},
      icon(autoPlay ? 'play' : 'pause', {size: 14}), autoPlay ? 'Tự phát' : 'Không tự phát');
    clear(els.review,
      h('div', {class: 'row between', style: {gap: '8px'}},
        h('div', {style: {minWidth: '0'}}, h('strong', {class: 'fs-sm'}, counts.pending ? `Đã duyệt ${done}/${current.segments.length} mốc` : 'Đã duyệt xong'),
          h('span', {class: 'fs-xs muted', style: {marginLeft: '8px'}}, `${counts.approved} đúng · ${counts.edited} sửa · ${counts.rejected} loại`)),
        h('div', {class: 'row', style: {gap: '4px', flex: 'none'}}, autoBtn,
          counts.pending ? btn('Chưa duyệt', {size: 'sm', icon: 'arrowRight', kbd: 'N', onClick: () => { const n = M.nextPending(doc(), selected); if (n >= 0) goTo(n); }}) : null)),
      progress(done / total * 100, {tone: 'ok'}),
      high ? btn(`Duyệt nhanh ${high} mốc AI chắc chắn cao`, {size: 'sm', kind: 'soft', icon: 'sparkles', onClick: approveHighConfidence, attrs: {title: 'Tên sản phẩm được nhắc rõ ràng — đánh dấu đúng hết, các mốc “cần xem lại” vẫn chờ bạn'}}) : null,
      !counts.pending ? notice('ok', null, 'Xem tab “Kiểm tra” lần cuối rồi bấm “Phát hành video”.') : null);
  }

  function renderDriftBanner(issues) {
    const drift = issues.filter(i => i.code === 'bag_changed');
    const missing = issues.filter(i => i.code === 'not_in_catalog');
    clear(els.drift,
      drift.length ? notice('danger', `${drift.length} sản phẩm đã đổi số giỏ so với lúc làm kịch bản`,
        'Nếu phát video này khi LIVE, tiện ích sẽ ghim theo số giỏ cũ. Cập nhật để số giỏ khớp giỏ hàng hôm nay — giờ ghim và sản phẩm không đổi.',
        btn('Cập nhật số giỏ', {kind: 'primary', icon: 'refresh', onClick: () => apply({syncOnly: M.sameContent(doc(), published)})})) : null,
      missing.length ? notice('warn', `${missing.length} sản phẩm trong kịch bản không còn trong giỏ hàng hôm nay`,
        'Các đoạn đó sẽ không ghim được. Bấm vào đoạn → Đổi sang sản phẩm đang bán.') : null);
  }

  function renderNow(force = false) {
    const current = doc();
    const pin = M.pinnedAt(current, playheadMs);
    const list = M.withEnds(current);
    const seg = pin.index >= 0 ? list[pin.index] : null;
    const next = list.find(s => s.start > playheadMs && s.review !== 'rejected') || null;
    const key = `${pin.index}|${pin.product}|${next ? next.index + ':' + next.product : ''}|${catalog.products.length}`;
    if (!force && key === lastNowKey) { updateNowProgress(seg, next); return; }
    lastNowKey = key;
    const product = resolveProduct(current, pin.product, catalog);
    const nextProduct = next ? resolveProduct(current, next.product, catalog) : null;
    const body = h('div', {class: 'now-card compact', style: {'--bag': product ? bagColor(product.bag_index) : 'var(--bag-none)'}, title: product ? product.title : ''},
      productAvatar(product, 52),
      h('div', {class: 'grow', style: {minWidth: '0'}},
        h('div', {class: 'now-label'}, h('i', {class: 'status-dot live'}), 'Đang ghim tại đầu phát'),
        product ? [
          h('div', {class: 'now-name'}, product.name),
          null,
          h('div', {class: 'now-meta'}, bag(product.bag_index, {size: 'lg'}), product.price ? h('span', {class: 'now-price'}, product.price) : null,
            pin.alternating ? chip('Đang luân phiên', 'info', {iconName: 'shuffle'}) : null,
            product.missing ? chip('Không còn trong giỏ hôm nay', 'danger', {iconName: 'alert'}) : null,
            product.bagChanged ? chip(`Kịch bản ghi giỏ #${product.timelineBag}`, 'warn', {iconName: 'alert'}) : null,
            pin.before ? chip('Trước mốc đầu', 'warn') : null),
        ] : [h('div', {class: 'now-name', style: {color: 'var(--studio-text-2)'}}, 'Không ghim sản phẩm'), null],
        h('div', {class: 'now-progress'}, h('i', {class: 'now-progress-bar'}))));
    const nextRow = next ? h('div', {class: 'now-next'}, icon('arrowRight', {size: 15}), 'Tiếp theo',
      nextProduct ? productAvatar(nextProduct, 24, {showBag: false}) : null,
      h('strong', {class: 'truncate'}, nextProduct ? `#${nextProduct.bag_index} ${nextProduct.name}` : 'Không ghim'),
      h('span', {class: 'grow'}), h('span', {class: 'mono now-countdown'}, '')) : null;
    clear(els.now, body, nextRow);
    updateNowProgress(seg, next);
  }
  function updateNowProgress(seg, next) {
    const bar = els.now.querySelector('.now-progress-bar');
    if (bar && seg) bar.style.width = Math.max(0, Math.min(100, (playheadMs - seg.start) / Math.max(1, seg.end - seg.start) * 100)) + '%';
    const countdown = els.now.querySelector('.now-countdown');
    if (countdown && next) countdown.textContent = 'sau ' + clock(next.start - playheadMs);
  }

  function renderInspector() {
    const current = doc();
    const list = M.withEnds(current);
    const seg = list[selected];
    if (!seg) {
      transcript.setRange(0, 0);
      clear(els.inspector, h('div', {class: 'panel-d-body'}, empty('Chưa có đoạn nào', 'Phát video và bấm phím số giỏ (1–9) khi host bắt đầu giới thiệu sản phẩm, hoặc bấm “Ghim sản phẩm tại đây”.', {iconName: 'pin',
        action: btn('Ghim sản phẩm tại đây', {kind: 'primary', icon: 'pin', onClick: () => pinWithPicker(playheadMs)})})));
      return;
    }
    const product = resolveProduct(current, seg.product, catalog);
    const alt = seg.alt ? resolveProduct(current, seg.alt, catalog) : null;
    transcript.setRange(seg.start, seg.end, product ? bagColor(product.bag_index) : 'var(--bag-none)');
    const status = lookup(REVIEW_STATUS, seg.review || 'pending');
    const canMove = isReview || selected > 0;
    const uidAtRender = seg.uid;
    const liveSeg = () => { const i = doc().segments.findIndex(s => s.uid === uidAtRender); return i >= 0 ? {i, seg: doc().segments[i]} : null; };

    const startInput = h('input', {class: 'input', value: clock(seg.start, {tenths: true}), disabled: !canMove, 'aria-label': 'Giờ bắt đầu', title: canMove ? 'Gõ phút:giây (vd 12:34 hoặc 12:34.5) rồi Enter' : 'Đoạn đầu luôn bắt đầu từ 00:00'});
    const initialText = startInput.value;
    const applyStart = () => {
      if (startInput.value === initialText) return;
      const ms = parseClock(startInput.value);
      const target = liveSeg();
      if (!target) return;
      if (ms === null) { toast('Giờ chưa đúng dạng phút:giây — ví dụ 12:34', 'warn'); startInput.value = initialText; return; }
      const next = M.moveStart(doc(), target.i, ms);
      if (next !== doc()) commit(next, `Đổi giờ bắt đầu → ${clock(ms)}`, {select: target.i, seek: true});
      else { startInput.value = initialText; toast('Giờ bắt đầu phải nằm giữa đoạn trước và đoạn sau.', 'info'); }
    };
    startInput.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); startInput.blur(); } if (e.key === 'Escape') { startInput.value = initialText; startInput.blur(); } });
    startInput.addEventListener('blur', applyStart);
    const nudge = delta => { const t = liveSeg(); if (!t) return; const next = M.moveStart(doc(), t.i, t.seg.start + delta); if (next !== doc()) commit(next, `Chỉnh giờ ${delta > 0 ? '+' : ''}${delta / 1000}s`, {select: t.i, seek: true, coalesce: 'nudge-' + uidAtRender}); };

    const head = h('header', {class: 'panel-d-head'},
      h('span', {class: 'chip lg', style: {background: bagColor(product && product.bag_index), color: '#fff'}}, `${isReview ? 'Mốc' : 'Đoạn'} ${selected + 1}/${list.length}`),
      h('h3', {class: 'grow mono'}, `${clock(seg.start)} → ${clock(seg.end)}`, h('span', {class: 'muted', style: {fontFamily: 'var(--font-sans)', fontWeight: 500, marginLeft: '8px'}}, shortDuration(seg.end - seg.start))),
      isReview ? chip(status.label, status.tone, {dot: true}) : null,
      iconBtn('zoomIn', 'Phóng timeline vào đoạn này (Z)', () => timeline.zoomToRange(seg.start, seg.end), {size: 'sm'}),
      iconBtn('chevronLeft', 'Trước ([)', () => jumpSegment(-1), {size: 'sm', disabled: selected <= 0}),
      iconBtn('chevronRight', 'Sau (])', () => jumpSegment(1), {size: 'sm', disabled: selected >= list.length - 1}));

    const productBtn = h('button', {class: 'insp-product', type: 'button', onClick: () => editProduct(selected), title: 'Đổi sản phẩm (nháy đúp khối trên timeline cũng được)'},
      productAvatar(product, 56),
      h('span', {class: 'grow', style: {minWidth: '0'}},
        h('strong', {class: 'clamp-2'}, product ? product.name : 'Không ghim sản phẩm'),
        h('small', {class: 'truncate'}, product ? `Giỏ #${product.bag_index}${product.price ? ' · ' + product.price : ''}${product.missing ? ' · không còn bán' : ''}` : 'Bấm để chọn sản phẩm')),
      h('span', {class: 'btn sm'}, icon('edit'), 'Đổi'));

    const altRow = seg.product ? h('div', {class: 'col', style: {gap: '8px'}},
      toggle('Luân phiên với sản phẩm khác (vd combo ↔ can lẻ)', Boolean(seg.alt), async on => {
        const t = liveSeg(); if (!t) return;
        if (!on) { commit(M.update(doc(), t.i, {alt: null, altEvery: 0}), 'Tắt luân phiên', {select: t.i}); return; }
        const key = await pickProduct({doc: doc(), catalog, title: 'Chọn sản phẩm luân phiên', allowNone: false});
        const t2 = liveSeg();
        if (key && t2 && key !== t2.seg.product) commit(M.update(doc(), t2.i, {alt: key, altEvery: t2.seg.altEvery || 60000}), 'Bật luân phiên', {select: t2.i});
        else renderInspector();
      }),
      seg.alt ? h('div', {class: 'row wrap', style: {gap: '8px', paddingLeft: '42px'}},
        h('button', {class: 'insp-product', type: 'button', style: {width: 'auto', flex: '1', padding: '6px'}, onClick: async () => {
          const key = await pickProduct({doc: doc(), catalog, current: seg.alt, title: 'Đổi sản phẩm luân phiên', allowNone: false});
          const t = liveSeg();
          if (key && t && key !== t.seg.product) commit(M.update(doc(), t.i, {alt: key}), 'Đổi sản phẩm luân phiên', {select: t.i});
        }}, productAvatar(alt, 34), h('span', {class: 'grow'}, h('strong', {class: 'truncate'}, alt ? alt.name : '—'), h('small', {}, alt ? `Giỏ #${alt.bag_index}` : ''))),
        h('label', {class: 'row', style: {gap: '6px'}}, h('span', {class: 'fs-xs muted'}, 'đổi mỗi'),
          select(ALT_CHOICES.some(([v]) => v === seg.altEvery) ? ALT_CHOICES : [...ALT_CHOICES, [seg.altEvery, shortDuration(seg.altEvery)]], seg.altEvery, {style: {width: '110px', minHeight: '32px'},
            onChange: e => { const t = liveSeg(); if (t) commit(M.update(doc(), t.i, {altEvery: Number(e.target.value)}), 'Đổi chu kỳ luân phiên', {select: t.i}); }}))) : null) : null;

    const topic = h('textarea', {class: 'textarea', rows: 2, placeholder: 'Host đang nói gì ở đoạn này? AI dùng ghi chú này làm ngữ cảnh khi trả lời khách.', style: {minHeight: '56px'}}, seg.topic || '');
    topic.addEventListener('change', () => { const t = liveSeg(); if (t && topic.value.trim() !== (t.seg.topic || '')) commit(M.update(doc(), t.i, {topic: topic.value.trim()}), 'Sửa ghi chú đoạn', {select: t.i}); });

    const decision = isReview ? h('div', {class: 'review-actions'},
      h('button', {class: 'btn approve', type: 'button', 'aria-pressed': String(seg.review === 'approved' || seg.review === 'edited'), onClick: () => decide('approved')}, icon('check'), seg.review === 'edited' ? 'Đã sửa · Đúng' : 'Đúng', h('span', {class: 'kbd'}, 'Enter')),
      h('button', {class: 'btn reject', type: 'button', 'aria-pressed': String(seg.review === 'rejected'), onClick: () => decide('rejected')}, icon('x'), 'Sai · loại bỏ', h('span', {class: 'kbd'}, 'X'))) : null;

    const body = h('div', {class: 'panel-d-body col', style: {gap: '14px'}},
      isReview && seg.ai ? aiEvidence(seg) : null,
      isReview && !seg.ai ? notice('info', null, 'Mốc do bạn thêm (AI không đề xuất).') : null,
      decision,
      isReview ? h('div', {class: 'row wrap', style: {gap: '6px'}},
        btn('Nghe lại', {size: 'sm', icon: 'repeat', kbd: 'R', onClick: () => replay()}),
        seg.ai && seg.ai.end > seg.start ? btn('Nghe đúng câu AI nghe thấy', {size: 'sm', kind: 'ghost', onClick: () => { player.setLoop(null); seek(Math.max(0, (seg.ai.suggestedStart || seg.start) - 1000)); player.play(); }}) : null) : null,
      h('div', {class: 'field'}, h('span', {class: 'field-label'}, 'Sản phẩm được ghim'), productBtn),
      h('div', {class: 'field'}, h('span', {class: 'field-label'}, 'Bắt đầu ghim lúc'),
        h('div', {class: 'time-edit'}, startInput,
          h('div', {class: 'nudge'}, [-5000, -1000, -100, 100, 1000, 5000].map(v => btn((v > 0 ? '+' : '−') + (Math.abs(v) >= 1000 ? Math.abs(v) / 1000 + 's' : '0.1s'), {size: 'sm', disabled: !canMove, onClick: () => nudge(v)}))),
          btn('= giây đang xem', {size: 'sm', kind: 'soft', icon: 'target', disabled: !canMove, attrs: {title: 'Đặt giờ bắt đầu bằng vị trí video đang dừng'}, onClick: () => { const t = liveSeg(); if (!t) return; const next = M.moveStart(doc(), t.i, playheadMs); if (next !== doc()) commit(next, 'Đặt giờ bắt đầu = giây đang xem', {select: t.i}); }})),
        !canMove ? h('div', {class: 'field-hint'}, 'Đoạn đầu tiên luôn bắt đầu từ 00:00.') : null),
      h('div', {class: 'field'}, h('span', {class: 'field-label'}, 'Kết thúc'),
        h('div', {class: 'row wrap'}, h('span', {class: 'mono fw-7'}, clock(seg.end)), h('span', {class: 'fs-xs muted'}, selected < list.length - 1 ? '= lúc mốc kế tiếp bắt đầu' : '= hết video'),
          selected < list.length - 1 ? btn('Kéo tới giây đang xem', {size: 'sm', disabled: playheadMs <= seg.start + M.MIN_SEGMENT_MS, onClick: () => { const t = liveSeg(); if (!t) return; const next = M.moveStart(doc(), t.i + 1, playheadMs); if (next !== doc()) commit(next, 'Đổi giờ kết thúc', {select: t.i}); }}) : null)),
      altRow,
      isReview ? null : h('div', {class: 'field'}, h('span', {class: 'field-label'}, 'Ghi chú cho AI (host đang nói gì)'), topic),
      h('div', {class: 'row wrap', style: {gap: '8px', paddingTop: '10px', borderTop: '1px solid var(--studio-line)'}},
        btn(loopOn ? 'Đang lặp đoạn' : 'Lặp đoạn này', {size: 'sm', icon: 'repeat', kind: loopOn ? 'primary' : '', onClick: () => { loopOn = !loopOn; applyLoop(); renderInspector(); if (loopOn) { seek(seg.start); player.play(); } }}),
        btn('Cắt tại giây đang xem', {size: 'sm', icon: 'scissors', kbd: 'S', onClick: () => splitHere()}),
        h('span', {class: 'grow'}),
        (!isReview || !seg.ai) ? btn('Xoá mốc', {size: 'sm', kind: 'danger-ghost', icon: 'trash', onClick: () => removeSegment(selected), attrs: {title: selected === 0 && !isReview ? 'Xoá: đoạn sau sẽ bắt đầu từ 00:00' : 'Xoá: đoạn trước kéo dài phủ lên'}}) : null));
    clear(els.inspector, head, body);
  }

  function aiEvidence(seg) {
    const ai = seg.ai;
    const conf = lookup(CONFIDENCE, ai.confidence, 'Không rõ');
    const markTerms = text => {
      const terms = (ai.matched || []).filter(Boolean);
      if (!terms.length) return text;
      const re = new RegExp(`(${terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'gi');
      const folded = text.normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/đ/g, 'd').replace(/Đ/g, 'D');
      if (folded.length !== text.length) return text;
      const out = []; let last = 0; let m;
      while ((m = re.exec(folded))) { out.push(text.slice(last, m.index), h('mark', {}, text.slice(m.index, m.index + m[0].length))); last = m.index + m[0].length; }
      out.push(text.slice(last));
      return out;
    };
    const suggested = ai.suggestedProduct ? resolveProduct(doc(), ai.suggestedProduct, catalog) : null;
    const changed = ai.suggestedProduct !== seg.product || ai.suggestedStart !== seg.start;
    const candidates = (ai.candidates || []).filter(c => M.keyForProductId(c.product_id) !== seg.product);
    return h('div', {class: 'col', style: {gap: '10px'}},
      h('div', {class: 'row wrap'}, chip('AI nghe thấy', 'ai', {iconName: 'sparkles'}), chip(conf.label, conf.tone),
        changed && suggested ? h('span', {class: 'fs-xs muted'}, `Ban đầu AI đề xuất #${suggested.bag_index} ${suggested.name} lúc ${clock(ai.suggestedStart)}`) : null),
      h('div', {class: 'ai-evidence'},
        ai.contextBefore ? h('span', {class: 'ctx'}, '… ' + ai.contextBefore.slice(-160) + ' ') : null,
        h('strong', {}, '“', markTerms(ai.evidence || ''), '”'),
        ai.contextAfter ? h('span', {class: 'ctx'}, ' ' + ai.contextAfter.slice(0, 160) + ' …') : null),
      candidates.length ? h('div', {class: 'row wrap', style: {gap: '6px'}}, h('span', {class: 'fs-xs muted'}, 'Hoặc có thể là:'),
        h('div', {class: 'candidates'}, candidates.slice(0, 3).map(c => {
          const p = resolveProduct(doc(), M.keyForProductId(c.product_id), catalog);
          return h('button', {type: 'button', onClick: () => commit(M.update(doc(), selected, {product: p.key}), 'Chọn sản phẩm AI đề xuất khác', {select: selected})}, productAvatar(p, 24, {showBag: false}), `#${p.bag_index} ${p.name}`);
        }))) : null);
  }

  // ------------------------------------------------ tab bên phải
  function renderSide() {
    const current = doc();
    const issues = M.checks(current, catalog.byId);
    const live = suggestions.filter(s => !dismissed.has(suggestionKey(s)));
    const problemCount = issues.filter(i => i.level !== 'info' && i.code !== 'pending').length + live.length;
    const tabs = [
      ['segment', isReview ? 'Mốc này' : 'Đoạn này', null],
      ['segments', isReview ? 'Các mốc' : 'Các đoạn', current.segments.length],
      ['checks', 'Kiểm tra', problemCount || null],
    ];
    clear(els.sideTabs, tabs.map(([key, label, count]) => h('button', {type: 'button', role: 'tab', 'aria-selected': String(sideTab === key), onClick: () => { sideTab = key; renderSide(); }},
      label, count !== null ? h('span', {class: 'count' + (key === 'checks' && problemCount ? ' alert' : '')}, String(count)) : null)));
    if (sideTab === 'segment') {
      if (els.inspector.parentNode !== els.sideBody) clear(els.sideBody, els.inspector);
      return;
    }
    const bodyNode = h('div', {class: 'side-list'});
    if (sideTab === 'segments') {
      const list = M.withEnds(current);
      const playing = M.indexAt(current, playheadMs);
      bodyNode.append(...list.map(seg => {
        const p = resolveProduct(current, seg.product, catalog);
        const st = lookup(REVIEW_STATUS, seg.review || 'pending');
        return h('div', {class: 'seg-row' + (seg.review === 'rejected' ? ' rejected' : '') + (seg.index === playing ? ' playing' : ''), 'aria-current': String(seg.index === selected), style: {'--bag': p ? bagColor(p.bag_index) : 'var(--bag-none)'}, onClick: () => goTo(seg.index, {play: false})},
          h('span', {class: 'seg-color'}), productAvatar(p, 40),
          h('div', {style: {minWidth: '0'}}, h('strong', {class: 'truncate', style: {display: 'block'}}, p ? `#${p.bag_index} ${p.name}` : 'Không ghim'),
            h('div', {class: 'seg-time'}, `${clock(seg.start)} → ${clock(seg.end)} · ${shortDuration(seg.end - seg.start)}`)),
          isReview ? chip(st.label, st.tone) : (p && (p.bagChanged || p.missing) ? h('span', {style: {color: '#f5c26b'}, title: p.missing ? 'Không còn bán' : 'Giỏ đã đổi'}, icon('alert', {size: 16})) : null));
      }));
    } else {
      const fixes = {
        merge: () => btn('Gộp các đoạn trùng', {size: 'sm', onClick: () => commit(M.mergeSameNeighbors(doc()), 'Gộp đoạn liền nhau cùng sản phẩm')}),
        normalize: () => btn('Đưa về 00:00', {size: 'sm', onClick: () => commit(M.normalized(doc()), 'Đưa đoạn đầu về 00:00')}),
        sync_bags: () => btn('Cập nhật số giỏ', {size: 'sm', kind: 'primary', onClick: () => apply({syncOnly: M.sameContent(doc(), published)})}),
      };
      // 1) Đề xuất từ lời thoại
      bodyNode.append(h('div', {class: 'side-section'}, h('div', {class: 'section-title'}, `Đối chiếu với lời thoại${live.length ? ` · ${live.length} chỗ nên xem` : ''}`),
        h('p', {class: 'fs-xs muted', style: {marginTop: '4px'}}, 'Những lúc host nói về một sản phẩm nhiều lần nhưng lịch đang ghim sản phẩm khác. Quà tặng kèm ("em tặng thêm…") không tính.')));
      if (!transcript.count()) bodyNode.append(h('div', {class: 'check-row info'}, icon('info'), h('div', {class: 'grow'}, 'Video này chưa có lời thoại nên không đối chiếu được.')));
      else if (!live.length) bodyNode.append(h('div', {class: 'check-row info'}, icon('checkCircle'), h('div', {class: 'grow'}, 'Lịch ghim khớp với những gì host nói.')));
      live.forEach(s => {
        const said = resolveProduct(current, s.key, catalog);
        const now = s.currentKey ? resolveProduct(current, s.currentKey, catalog) : null;
        bodyNode.append(h('div', {class: 'check-row warn suggestion'},
          icon('message'),
          h('div', {class: 'grow'},
            h('div', {}, h('span', {class: 'mono'}, clock(s.startMs)), ` — host nhắc `, h('strong', {}, `#${said.bag_index} ${said.name}`), ` ${s.count} lần nhưng đang ghim `, h('strong', {}, now ? `#${now.bag_index} ${now.name}` : 'không sản phẩm nào'), '.'),
            h('div', {class: 'fs-xs muted', style: {marginTop: '3px'}}, `“${s.text.slice(0, 110)}”`),
            h('div', {class: 'row', style: {marginTop: '6px', gap: '6px'}},
              btn('Nghe', {size: 'sm', kind: 'ghost', icon: 'play', onClick: () => focusSuggestion(s)}),
              btn(`Ghim #${said.bag_index} ${clock(s.startMs)}–${clock(s.endMs + 8000)}`, {size: 'sm', kind: 'primary', attrs: {title: 'Ghim đúng khoảng host nói, sau đó tự trả lại sản phẩm đang ghim'}, onClick: () => applySuggestion(s)}),
              btn('Bỏ qua', {size: 'sm', kind: 'ghost', onClick: () => { dismissed.add(suggestionKey(s)); syncMarkers(); renderSide(); }})))));
      });
      // 2) Kiểm tra kỹ thuật
      const tech = issues.filter(i => i.code !== 'pending');
      bodyNode.append(h('div', {class: 'side-section'}, h('div', {class: 'section-title'}, 'Kiểm tra lịch ghim')));
      if (!tech.length) bodyNode.append(h('div', {class: 'check-row info'}, icon('checkCircle'), h('div', {class: 'grow'}, isReview ? 'Không có lỗi kỹ thuật.' : 'Lịch ghim hợp lệ.')));
      tech.forEach(issue => bodyNode.append(h('div', {class: 'check-row ' + issue.level},
        icon(issue.level === 'error' ? 'xCircle' : issue.level === 'warn' ? 'alert' : 'info'),
        h('div', {class: 'grow'}, issue.message, issue.count > 1 ? h('span', {class: 'muted'}, ` (×${issue.count})`) : null,
          h('div', {class: 'row', style: {marginTop: '6px', gap: '6px'}},
            issue.index >= 0 ? btn('Xem', {size: 'sm', kind: 'ghost', icon: 'eye', onClick: () => { showSegmentTab(); goTo(issue.index, {play: false}); }}) : null,
            issue.fix && fixes[issue.fix] ? fixes[issue.fix]() : null)))));
      // 3) Thời lượng lên sóng
      const air = M.airtime(current);
      const rows = Object.entries(air).sort((a, b) => b[1] - a[1]);
      const max = rows.length ? rows[0][1] : 1;
      bodyNode.append(h('div', {class: 'side-section'}, h('div', {class: 'section-title'}, 'Thời lượng lên sóng của từng sản phẩm')),
        ...rows.map(([key, ms]) => { const p = resolveProduct(current, key, catalog); return h('div', {class: 'airtime-row', style: {'--bag': bagColor(p.bag_index)}}, bag(p.bag_index), h('div', {style: {minWidth: '0'}}, h('div', {class: 'truncate'}, p.name), h('div', {class: 'bar'}, h('i', {style: {width: (ms / max * 100) + '%'}}))), h('span', {class: 'mono fs-xs', style: {textAlign: 'right'}}, shortDuration(ms))); }));
    }
    clear(els.sideBody, bodyNode);
    const scrollTop = sideScroll[sideTab] || 0;
    if (scrollTop) els.sideBody.scrollTop = scrollTop;
  }
  // Giữ vị trí cuộn của từng tab: đổi qua lại không bị nhảy về đầu danh sách.
  const sideScroll = {};

  // ------------------------------------------------ đề xuất từ lời thoại
  const suggestionKey = s => `${s.key}@${Math.round(s.startMs / 5000)}`;
  function recomputeSuggestions() {
    suggestions = transcript && transcript.count() ? M.findPinMismatches(doc(), transcript.mentions()) : [];
  }
  const recomputeLater = debounce(() => { recomputeSuggestions(); syncMarkers(); if (sideTab === 'checks') renderSide(); else renderSide(); }, 250, d);
  function syncMarkers() {
    const list = [];
    if (isReview) {
      doc().segments.forEach((s, i) => {
        if (s.ai && s.ai.suggestedStart !== undefined) list.push({kind: 'evidence', ms: s.ai.suggestedStart, endMs: Math.max(s.ai.end || 0, s.ai.suggestedStart + 2000), active: i === selected, label: `AI nghe thấy: “${(s.ai.evidence || '').slice(0, 80)}”`});
      });
    }
    suggestions.filter(s => !dismissed.has(suggestionKey(s))).forEach(s => {
      const p = resolveProduct(doc(), s.key, catalog);
      list.push({kind: 'suggest', ms: s.startMs, label: `Host nhắc #${p.bag_index} ${p.name} ${s.count} lần — bấm để xem`, suggestion: s});
    });
    timeline.setMarkers(list);
  }
  function focusSuggestion(s) {
    sideTab = 'checks'; renderSide();
    followUntil = Date.now() + 4000;
    player.setLoop(null);
    seek(Math.max(0, s.startMs - 1500));
    timeline.reveal(s.startMs);
    player.play();
  }
  function applySuggestion(s) {
    // Kết thúc cụm + 8 s đệm (host thường nói nốt câu) rồi trả lại sản phẩm cũ.
    const result = M.pinRange(doc(), s.startMs, s.endMs + 8000, s.key);
    if (result.unchanged) { toast('Đoạn này đã ghim đúng sản phẩm đó.', 'info'); return; }
    dismissed.add(suggestionKey(s));
    const p = resolveProduct(doc(), s.key, catalog);
    commit(result.doc, `Ghim #${p.bag_index} theo lời thoại lúc ${clock(s.startMs)}`, {select: result.index});
    toast(`Đã ghim #${p.bag_index} ${p.name} từ ${clock(s.startMs)}`, 'ok', {action: 'Hoàn tác', onAction: undo});
  }

  // ============================================================ thời gian
  function onTime(ms) {
    playheadMs = ms;
    timeline.setPlayhead(ms);
    transcript.setTime(ms);
    renderNow();
    // "Đi theo đầu phát" chỉ ở chế độ chỉnh lịch; khi duyệt, lựa chọn do người duyệt quyết.
    if (!isReview && !player.paused() && !timeline.isDragging() && Date.now() > followUntil) {
      const i = M.indexAt(doc(), ms);
      if (i >= 0 && i !== selected && !els.inspector.contains(document.activeElement)) {
        selected = i; timeline.render(); renderInspector(); if (sideTab === 'segments') renderSide();
      }
    }
  }
  function seek(ms) { player.seek(ms); if (!player.hasMedia()) onTime(ms); }
  function applyLoop() {
    const seg = M.withEnds(doc())[selected];
    player.setLoop(loopOn && seg ? {start: seg.start, end: seg.end} : null);
  }

  // ============================================================ điều hướng
  function select_(index) {
    const list = doc().segments;
    if (!list.length) { selected = 0; renderInspector(); return; }
    selected = Math.max(0, Math.min(list.length - 1, index));
    followUntil = Date.now() + 1500;
    timeline.render();
    syncMarkers();
    renderInspector();
    if (sideTab === 'segments') renderSide();
    if (loopOn) applyLoop();
  }
  /** Chọn đoạn + tua (duyệt: lùi trước 3 s và tự phát). */
  function goTo(index, {play = autoPlay && isReview} = {}) {
    select_(index);
    const seg = doc().segments[selected];
    if (!seg) return;
    const at = isReview ? Math.max(0, seg.start - PREROLL_MS) : seg.start;
    followUntil = Date.now() + PREROLL_MS + 2500;
    seek(at);
    timeline.reveal(seg.start);
    if (play) player.play();
  }
  function showSegmentTab() { if (sideTab !== 'segment') { sideTab = 'segment'; renderSide(); } }
  function jumpSegment(dir) { goTo(selected + dir, {play: autoPlay && isReview}); }
  function replay() { const seg = doc().segments[selected]; if (!seg) return; seek(Math.max(0, seg.start - PREROLL_MS)); player.play(); }

  // ============================================================ chỉnh sửa
  let lastCoalesce = '';
  function commit(next, label, {select: sel, seek: doSeek = false, coalesce = ''} = {}) {
    if (!next || next === doc()) return;
    if (coalesce && coalesce === lastCoalesce) history.replace(next); else history.push(next, label);
    lastCoalesce = coalesce;
    if (sel !== undefined) selected = Math.max(0, Math.min(next.segments.length - 1, sel));
    markDirty();
    refreshAll();
    recomputeLater();
    if (loopOn) applyLoop();
    if (doSeek) { const seg = doc().segments[selected]; if (seg) seek(seg.start); }
  }
  function undo() { const r = history.undo(); if (!r) return; lastCoalesce = ''; selected = Math.min(selected, Math.max(0, doc().segments.length - 1)); markDirty(); refreshAll(); recomputeLater(); toast('Đã hoàn tác: ' + (r.label || 'thay đổi'), 'info', {duration: 2200}); }
  function redo() { const r = history.redo(); if (!r) return; lastCoalesce = ''; markDirty(); refreshAll(); recomputeLater(); toast('Làm lại: ' + (r.label || 'thay đổi'), 'info', {duration: 2200}); }

  async function editProduct(index) {
    const seg = doc().segments[index];
    if (!seg) return;
    const key = await pickProduct({doc: doc(), catalog, current: seg.product, title: `Sản phẩm cho ${isReview ? 'mốc' : 'đoạn'} ${index + 1} (${clock(seg.start)})`});
    if (key === undefined || key === seg.product) return;
    const i = doc().segments.findIndex(s => s.uid === seg.uid);
    if (i >= 0) commit(M.update(doc(), i, {product: key}), 'Đổi sản phẩm', {select: i});
  }
  async function pinWithPicker(ms) {
    const at = Math.round(ms);
    const key = await pickProduct({doc: doc(), catalog, atMs: at, title: `Ghim sản phẩm từ ${clock(at)}`});
    if (key === undefined) return;
    const result = M.pinFrom(doc(), at, key);
    if (result.unchanged) { toast('Đoạn này đang ghim đúng sản phẩm đó rồi.', 'info'); return; }
    commit(result.doc, `Ghim từ ${clock(at)}`, {select: result.index});
  }
  function pinKeyFrom(ms, key) {
    const at = Math.round(ms);
    const result = M.pinFrom(doc(), at, key);
    if (result.unchanged) { toast('Đoạn này đang ghim đúng sản phẩm đó rồi.', 'info'); return; }
    const p = resolveProduct(doc(), key, catalog);
    commit(result.doc, `Ghim #${p ? p.bag_index : ''} từ ${clock(at)}`, {select: result.index});
    toast(`Ghim #${p ? p.bag_index : ''} ${p ? p.name : ''} từ ${clock(at)}`, 'ok', {duration: 2200, action: 'Hoàn tác', onAction: undo});
  }
  function pinBag(bagIndex) {
    const product = catalog.products.find(p => Number(p.bag_index) === bagIndex);
    if (!product) { toast(`Giỏ hàng hôm nay không có giỏ số ${bagIndex}.`, 'warn'); return; }
    const key = M.keyForProductId(product.product_id);
    const at = playheadMs;
    const result = M.pinFrom(doc(), at, key);
    if (result.unchanged) { toast(`Đang ghim giỏ #${bagIndex} rồi.`, 'info', {duration: 1800}); return; }
    commit(result.doc, `Ghim giỏ #${bagIndex} từ ${clock(at)}`, {select: result.index});
    toast(`Ghim giỏ #${bagIndex} · ${product.name} từ ${clock(at)}`, 'ok', {duration: 2200, action: 'Hoàn tác', onAction: undo});
  }
  function splitHere() {
    const result = M.splitAt(doc(), playheadMs);
    if (result.doc === doc()) { toast('Quá sát mốc có sẵn — dịch đầu phát ra ít nhất 1 giây.', 'info'); return; }
    commit(result.doc, `Cắt đoạn tại ${clock(playheadMs)}`, {select: result.index});
  }
  function removeSegment(index) {
    const seg = doc().segments[index];
    if (!seg) return;
    const result = M.removeAt(doc(), index);
    commit(result.doc, `Xoá mốc ${clock(seg.start)}`, {select: Math.max(0, result.index)});
    toast(`Đã xoá mốc ${clock(seg.start)}`, 'info', {action: 'Hoàn tác', onAction: undo, duration: 5000});
  }
  /** Quyết định duyệt. Phím tắt không bao giờ "bỏ duyệt"; bấm lại nút cùng trạng thái mới bỏ. */
  function decide(status, {viaKey = false} = {}) {
    const seg = doc().segments[selected];
    if (!seg) return;
    let next = status === 'approved' && seg.review === 'edited' ? 'edited' : status;
    if (!viaKey && seg.review === next && next !== 'edited') next = 'pending';
    commit(M.setReview(doc(), [selected], next), next === 'rejected' ? 'Loại mốc AI' : next === 'pending' ? 'Bỏ duyệt' : 'Duyệt mốc', {select: selected});
    if (next === 'pending') return;
    const n = M.nextPending(doc(), selected);
    if (n >= 0) goTo(n);
    else { player.pause(); toast('Đã duyệt hết. Xem tab “Kiểm tra” rồi bấm “Phát hành video”.', 'ok'); sideTab = 'checks'; renderSide(); }
  }
  function approveHighConfidence() {
    const indices = doc().segments.map((s, i) => ((s.review || 'pending') === 'pending' && s.ai && s.ai.confidence === 'high') ? i : -1).filter(i => i >= 0);
    if (!indices.length) return;
    commit(M.setReview(doc(), indices, 'approved'), `Duyệt nhanh ${indices.length} mốc chắc chắn`);
    toast(`Đã đánh dấu đúng ${indices.length} mốc AI chắc chắn cao. Các mốc “cần xem lại” vẫn chờ bạn.`, 'ok', {action: 'Hoàn tác', onAction: undo});
    const n = M.nextPending(doc(), -1);
    if (n >= 0) goTo(n);
  }

  // ============================================================ lưu
  function markDirty() {
    saveState = {...saveState, state: 'dirty', error: null}; renderHeader(); scheduleSave();
    if (!recoveryTimer) recoveryTimer = setTimeout(() => {
      recoveryTimer = 0; preserveRecovery().catch(() => {});
    }, 300);
  }
  function preserveRecovery() {
    if (!history) return Promise.resolve();
    const snapshot = {document: structuredClone(doc()), revision,
      draft_revision: draftRevision, selected, kind, video_or_job_id: id};
    recoveryWrite = recoveryWrite.catch(() => {}).then(() => saveRecovery(`${kind}:${id}`, snapshot));
    return recoveryWrite.then(value => {recoveryError = null; return value;}, error => {
      recoveryError = error;
      if (!closed) toast('Không lưu được bản phục hồi cục bộ. Có thể bộ nhớ đã đầy; giữ Studio mở và lưu lại trước khi rời trang.', 'danger');
      throw error;
    });
  }
  async function restoreRecovery() {
    try {
      const rows = await listRecovery(`${kind}:${id}`);
      if (closed) return;
      const choice = select([['autosave', 'Quay về bản tự lưu gần nhất'], ...rows.map(row => [row.snapshot_id,
        'Mốc phục hồi ' + new Date(row.saved_at_ms).toLocaleString('vi-VN')])], 'autosave');
      if (!await confirmDialog({title: 'Khôi phục công việc trong Studio',
        text: 'Nội dung đang sửa sẽ được giữ thành một mốc phục hồi trước. Lịch LIVE chỉ đổi khi bạn bấm áp dụng.',
        details: field('Chọn bản cần mở', choice), confirm: 'Mở bản đã chọn'})) return;
      scheduleSave.cancel();
      if (pendingSave) await pendingSave.catch(() => {});
      const target = choice.value === 'autosave' ? lastAutosaved : rows.find(row => row.snapshot_id === choice.value)?.document;
      if (!target || closed) return;
      await preserveRecovery();
      if (closed) return;
      history.reset(structuredClone(target));
      selected = Math.min(selected, Math.max(0, target.segments.length - 1));
      markDirty(); refreshAll(); recomputeLater();
    } catch (error) {if (!closed) toast(friendlyError(error), 'danger');}
  }
  const scheduleSave = debounce(() => flushSave(), 1200, d);
  async function flushSave() {
    scheduleSave.cancel();
    if (pendingSave) { await pendingSave.catch(() => {}); }
    if (saveState.state !== 'dirty' && saveState.state !== 'error') return;
    const snapshot = doc();
    saveState = {...saveState, state: 'saving'};
    renderHeader();
    pendingSave = (async () => {
      if (isReview) {
        const out = await request(`/api/workspace/video-jobs/${encodeURIComponent(id)}/draft`, {method: 'POST', body: {expected_revision: revision, draft: M.toReviewDraft(snapshot, original), reason: 'Chủ shop duyệt trong Studio'}});
        revision = out.revision; original = out.draft;
        saveState = {state: doc() === snapshot ? 'clean' : 'dirty', at: new Date(), error: null};
      } else if (M.sameContent(snapshot, published)) {
        const out = await request(`/api/workspace/videos/${encodeURIComponent(id)}/draft`, {method: 'POST', body: {expected_revision: revision, expected_draft_revision: draftRevision, discard: true}});
        draftRevision = out.draft_revision;
        saveState = {state: doc() === snapshot ? 'clean' : 'dirty', at: null, error: null};
      } else {
        const out = await request(`/api/workspace/videos/${encodeURIComponent(id)}/draft`, {method: 'POST', body: {expected_revision: revision, expected_draft_revision: draftRevision, draft: M.toLibraryDraft(snapshot)}});
        draftRevision = out.draft_revision;
        saveState = {state: doc() === snapshot ? 'draft' : 'dirty', at: out.saved_at, error: null};
      }
      lastAutosaved = snapshot;
    })();
    try { await pendingSave; }
    catch (error) { saveState = {...saveState, state: 'error', error}; toast('Chưa lưu được: ' + friendlyError(error), 'danger'); }
    finally { pendingSave = null; renderHeader(); if (!closed && saveState.state === 'dirty') scheduleSave(); }
  }

  async function apply({syncOnly = false} = {}) {
    await flushSave();
    if (saveState.state === 'error') return;
    const current = M.normalized(doc());
    const issues = M.checks(current, catalog.byId);
    if (M.blocking(issues)) { sideTab = 'checks'; renderSide(); toast('Còn lỗi cần sửa trước khi áp dụng — xem tab Kiểm tra.', 'danger'); return; }
    const bagIssues = issues.filter(i => i.code === 'bag_changed');
    const warnList = issues.filter(i => i.level === 'warn' && i.code !== 'bag_changed');
    const openSuggestions = suggestions.filter(s => !dismissed.has(suggestionKey(s)));
    let syncBags = bagIssues.length > 0;
    const ok = await confirmDialog({
      title: syncOnly ? 'Cập nhật số giỏ cho lịch ghim này?' : 'Áp dụng lịch ghim này cho LIVE?',
      text: 'Từ phiên LIVE tiếp theo (hoặc ngay nếu video đang phát), tiện ích sẽ ghim theo lịch mới. Bản cũ được lưu lại và có thể khôi phục.',
      confirm: syncOnly ? 'Cập nhật số giỏ' : 'Áp dụng cho LIVE', tone: 'primary',
      details: h('div', {class: 'col gap-3'},
        h('div', {class: 'row wrap'}, chip(`${current.segments.length} đoạn`, 'info'), chip(`${M.usedProductKeys(current).length} sản phẩm`, 'info'), chip(clock(current.durationMs), '')),
        bagIssues.length ? toggle(`Cập nhật số giỏ theo giỏ hàng hôm nay (${bagIssues.length} sản phẩm đang lệch)`, true, v => { syncBags = v; }) : null,
        openSuggestions.length ? notice('warn', `${openSuggestions.length} chỗ host nói khác sản phẩm đang ghim`, 'Xem tab Kiểm tra nếu muốn rà lại trước khi áp dụng.') : null,
        warnList.length ? notice('warn', `${warnList.length} lưu ý`, h('ul', {style: {margin: '4px 0 0', paddingLeft: '18px'}}, warnList.slice(0, 4).map(i => h('li', {}, i.message)))) : null),
    });
    if (!ok) return;
    try {
      const changes = M.toLibraryChanges(current);
      if (syncBags) changes.sync_products = true;
      const out = await request(`/api/workspace/videos/${encodeURIComponent(id)}`, {method: 'PATCH', body: {expected_revision: revision, changes, reason: `Áp dụng từ Studio lúc ${new Date().toLocaleTimeString('vi-VN')}`}});
      if (closed) return;
      revision = out.revision;
      const timelineData = (out.data && out.data.timeline) || null;
      invalidateCatalog();
      published = timelineData ? M.fromLibrary({...timelineData, video_id: id}) : current;
      history.reset(published);
      lastAutosaved = published;
      draftRevision = out.data?.draft_revision ?? draftRevision;
      appliedAt = (out.data && out.data.applied_at) || new Date().toISOString();
      saveState = {state: 'clean', at: null, error: null};
      // Danh mục có thể vừa đổi số giỏ — nạp lại để cảnh báo "lệch giỏ" tính đúng.
      loadCatalog({force: true, cart: catalogCart}).then(cat => { if (!closed) {catalog = cat; refreshAll();} }).catch(() => {});
      refreshAll(); recomputeLater();
      toast(syncOnly ? 'Đã cập nhật số giỏ — lịch ghim đang dùng cho LIVE.' : 'Đã áp dụng lịch ghim cho LIVE.', 'ok', {action: 'Khôi phục bản trước', onAction: async () => {
        try { await request(`/api/workspace/videos/${encodeURIComponent(id)}/rollback`, {method: 'POST', body: {}}); toast('Đã khôi phục bản trước.', 'ok'); ctx.navigate('video', 'studio', id, {force: true}); } catch (e) { toast(friendlyError(e), 'danger'); }
      }});
    } catch (error) { toast(friendlyError(error), 'danger'); }
  }

  async function discardDraft() {
    const ok = await confirmDialog({title: 'Bỏ toàn bộ bản nháp, quay về lịch LIVE?', text: 'Nội dung hiện tại sẽ được giữ thành một mốc phục hồi trước khi bỏ bản nháp.', confirm: 'Bỏ bản nháp', tone: 'danger'});
    if (!ok) return;
    scheduleSave.cancel();
    if (pendingSave) await pendingSave.catch(() => {});
    try {
      await preserveRecovery();
      const out = await request(`/api/workspace/videos/${encodeURIComponent(id)}/draft`, {method: 'POST', body: {expected_revision: revision, expected_draft_revision: draftRevision, discard: true}});
      draftRevision = out.draft_revision;
    } catch (error) {
      saveState = {...saveState, state: 'error', error};
      renderHeader();
      toast('Chưa bỏ được bản nháp: ' + friendlyError(error), 'danger');
      return;
    }
    history.reset(published);
    lastAutosaved = published;
    saveState = {state: 'clean', at: null, error: null};
    selected = Math.min(selected, Math.max(0, published.segments.length - 1));
    refreshAll(); recomputeLater();
    toast('Đã quay về lịch ghim đang dùng.', 'info');
  }

  async function publish() {
    await flushSave();
    if (saveState.state === 'error') return;
    const current = doc();
    const counts = M.reviewCounts(current);
    const openSuggestions = suggestions.filter(s => !dismissed.has(suggestionKey(s)));
    const titleInput = h('input', {class: 'input', value: current.title, maxlength: '200'});
    const ok = await confirmDialog({
      title: 'Phát hành thành video trong thư viện?',
      text: 'Lịch ghim dùng được ngay cho LIVE. Mốc bị loại không được đưa vào. Sau khi phát hành vẫn chỉnh tiếp được trong Studio.',
      confirm: 'Phát hành', tone: 'primary',
      details: h('div', {class: 'col gap-3'}, field('Tên video trong thư viện', titleInput),
        h('div', {class: 'row wrap'}, chip(`${counts.approved} đúng`, 'ok'), chip(`${counts.edited} đã sửa`, 'info'), chip(`${counts.rejected} bị loại`, 'danger')),
        openSuggestions.length ? notice('warn', `Lời thoại gợi ý ${openSuggestions.length} chỗ có thể thiếu mốc`, 'Có thể phát hành rồi chỉnh sau, hoặc huỷ để xem tab Kiểm tra.') : null),
    });
    if (!ok) return;
    try {
      const out = await request(`/api/workspace/video-jobs/${encodeURIComponent(id)}/publish`, {method: 'POST', body: {title: titleInput.value.trim() || current.title, expected_revision: revision, operation_id: uid('pub').replace(/[^A-Za-z0-9_-]/g, '') + Math.random().toString(36).slice(2, 10)}});
      saveState = {state: 'clean', at: null, error: null};
      toast(out.da_chuan_hoa_moc_dau ? `Đã phát hành. Mốc đầu được đưa về 00:00 (trước đó ${clock(out.da_chuan_hoa_moc_dau.tu_ms)}).` : 'Đã phát hành video vào thư viện.', 'ok');
      ctx.navigate('video', 'studio', out.video_id);
    } catch (error) { toast(friendlyError(error), 'danger'); }
  }

  // ============================================================ phím tắt
  d.on(window, 'keydown', e => {
    if (!history || !els.studio || isOverlayOpen() || document.querySelector('.overlay')) return;
    if ((e.ctrlKey || e.metaKey) && !e.altKey) {
      const k = e.key.toLowerCase();
      if (k === 'z' && !e.shiftKey && !isTypingTarget(e.target)) { e.preventDefault(); undo(); return; }
      if ((k === 'y' || (k === 'z' && e.shiftKey)) && !isTypingTarget(e.target)) { e.preventDefault(); redo(); return; }
      if (k === 's') { e.preventDefault(); if (isReview) flushSave(); else apply(); return; }
      if (k === 'f') { e.preventDefault(); transcript.focusSearch(); return; }
      return;
    }
    if (isTypingTarget(e.target) || e.altKey || e.repeat && ['Enter', 'x', 'X', 'Delete'].includes(e.key)) return;
    // Nút / ô chọn đang có focus bằng bàn phím: để trình duyệt tự xử lý Enter/Space.
    if ((e.key === 'Enter' || e.key === ' ') && e.target.closest && e.target.closest('button, a, [role="button"], input, select, label')) return;
    const key = e.key;
    const done = () => e.preventDefault();
    if (key === ' ' || key === 'k' || key === 'K') { done(); player.toggle(); }
    else if (key === 'j' || key === 'J') { done(); player.seekBy(-5000); }
    else if (key === 'l' || key === 'L') { done(); player.seekBy(5000); }
    else if (key === 'ArrowLeft') { done(); player.seekBy(e.shiftKey ? -10000 : -1000); }
    else if (key === 'ArrowRight') { done(); player.seekBy(e.shiftKey ? 10000 : 1000); }
    else if (key === ',') { done(); player.stepFrame(-1); }
    else if (key === '.') { done(); player.stepFrame(1); }
    else if (key === '<') { player.slower(); }
    else if (key === '>') { player.faster(); }
    else if (key === '[') { done(); jumpSegment(-1); }
    else if (key === ']') { done(); jumpSegment(1); }
    else if (key === 'm' || key === 'M') { done(); pinWithPicker(playheadMs); }
    else if (key === 's' || key === 'S') { done(); splitHere(); }
    else if (key === 'z' || key === 'Z') { done(); const seg = M.withEnds(doc())[selected]; if (seg) timeline.zoomToRange(seg.start, seg.end); }
    else if (key === 'r' || key === 'R') { done(); replay(); }
    else if ((key === 'n' || key === 'N') && isReview) { done(); const n = M.nextPending(doc(), selected); if (n >= 0) goTo(n); else toast('Không còn mốc chưa duyệt.', 'info'); }
    else if (key === 'Enter' && isReview) { done(); decide('approved', {viaKey: true}); }
    else if ((key === 'x' || key === 'X') && isReview) { done(); decide('rejected', {viaKey: true}); }
    else if (key === 'Delete' || key === 'Backspace') {
      done();
      const seg = doc().segments[selected];
      if (isReview && seg && seg.ai) decide('rejected', {viaKey: true}); else removeSegment(selected);
    }
    else if (key === '+' || key === '=') { timeline.zoomIn(); }
    else if (key === '-' || key === '_') { timeline.zoomOut(); }
    else if (key === 'Home') { done(); seek(0); }
    else if (key === 'End') { done(); seek(doc().durationMs - 1000); }
    else if (/^[0-9]$/.test(key)) { done(); pinBag(key === '0' ? 10 : Number(key)); }
  });

  // Quay lại cửa sổ: giỏ hàng có thể vừa đổi số (tiện ích đồng bộ khi LIVE)
  // hoặc lịch ghim vừa được áp dụng ở cửa sổ khác. Không có thay đổi dở dang
  // thì lặng lẽ nạp bản mới — tránh cảnh báo "lệch giỏ" đã được xử lý rồi.
  let refreshing = false;
  async function refreshIfStale() {
    if (closed || !history || refreshing || document.visibilityState === 'hidden') return;
    refreshing = true;
    try {
      const cat = await loadCatalog({force: true, cart: catalogCart}).catch(() => null);
      if (closed) return;
      let reloaded = false;
      if (!isReview && saveState.state === 'clean' && M.sameContent(doc(), published)) {
        const detail = await request(`/api/workspace/videos/${encodeURIComponent(id)}`);
        if (closed) return;
        const rev = detail.revision || (detail.data || {}).revision || '';
        if (rev && rev !== revision) {
          revision = rev;
          appliedAt = (detail.data || {}).applied_at || appliedAt;
          published = M.fromLibrary({...((detail.data || {}).timeline || {}), video_id: id});
          history.reset(published);
          lastAutosaved = published;
          draftRevision = detail.data?.draft_revision || 0;
          selected = Math.min(selected, Math.max(0, published.segments.length - 1));
          reloaded = true;
        }
      }
      if (cat) catalog = cat;
      if (cat || reloaded) { refreshAll(); recomputeLater(); }
      if (reloaded) toast('Lịch ghim vừa được cập nhật ở nơi khác — Studio đã nạp bản mới.', 'info');
    } catch (_) { /* giữ nguyên màn hình */ } finally { refreshing = false; }
  }
  d.on(window, 'focus', refreshIfStale);
  d.on(document, 'visibilitychange', refreshIfStale);

  d.add(() => { clearTimeout(recoveryTimer); if (history && ['dirty', 'saving', 'error'].includes(saveState.state)) preserveRecovery().catch(() => {}); scheduleSave.cancel(); if (saveState.state === 'dirty') flushSave(); });
  d.add(() => { if (player) player.destroy(); if (timeline) timeline.destroy(); if (transcript && transcript.destroy) transcript.destroy(); });
  d.on(window, 'beforeunload', e => { if (['dirty', 'saving', 'error'].includes(saveState.state) || recoveryError) {preserveRecovery().catch(() => {}); flushSave(); e.preventDefault(); e.returnValue = ''; } });
  load();

  async function beforeLeave() {
    await flushSave();
    while (!closed && saveState.state === 'dirty') await flushSave();
    if (!['dirty', 'saving', 'error'].includes(saveState.state)) return true;
    try {await preserveRecovery();}
    catch (_) {return false;}
    return confirmDialog({title: 'Chưa lưu được bản nháp trên Kid TE',
      text: 'Công việc hiện tại đã được giữ trong bản phục hồi trên máy. Có thể ở lại thử lưu hoặc rời trang rồi mở lại mốc phục hồi.',
      confirm: 'Rời trang, giữ phục hồi', cancel: 'Ở lại thử lưu', tone: 'primary'});
  }
  return {fullBleed: true, isDirty: () => ['dirty', 'saving', 'error'].includes(saveState.state), beforeLeave, unmount: () => d.dispose()};
}
