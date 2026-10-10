import {h, clear, rafThrottle} from '../../core/dom.js';
import {icon} from '../../core/icons.js';
import {clock, shortDuration} from '../../core/format.js';
import {iconBtn, productAvatar, bagColor} from '../../ui/kit.js';
import {showTip, hideTip} from '../../ui/overlays.js';
import {withEnds, moveBounds} from './model.js';
import {resolveProduct} from './data.js';

const TICK_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
const MAX_PPS = 60;
const SNAP_PX = 9;

export function createTimeline(opts) {
  const {getDoc, getCatalog, getSelection, onSeek, onSelect, onCommitMove, onEditProduct, onScrubPreview, onMarker} = opts;
  let markers = [];
  let pps = 1;             // pixel / giây
  let fitPps = 1;
  let playheadMs = 0;
  let assets = null;
  let transcript = [];
  let snapOn = true;
  let follow = true;
  let dragging = null;
  let userScrolledAt = 0;

  // ---------- DOM ----------
  const zoomSlider = h('input', {type: 'range', class: 'zoom', min: '0', max: '1000', value: '0', 'aria-label': 'Mức phóng to', onInput: () => {
    const t = Number(zoomSlider.value) / 1000;
    zoomTo(fitPps * Math.pow(MAX_PPS / fitPps, t), playheadMs);
  }});
  const snapBtn = iconBtn('magnet', 'Bắt dính vào đầu câu thoại (giữ Alt để tắt tạm)', () => { snapOn = !snapOn; syncToggles(); }, {size: 'sm'});
  const followBtn = iconBtn('target', 'Timeline tự chạy theo video', () => { follow = !follow; syncToggles(); if (follow) ensureVisible(playheadMs, true); }, {size: 'sm'});
  const zoomLabel = h('span', {class: 'fs-xs muted mono', style: {minWidth: '72px'}});
  const hint = h('span', {class: 'fs-xs muted truncate grow', style: {textAlign: 'right'}}, 'Bấm để tua · kéo mép khối để đổi giờ · nháy đúp khối để đổi sản phẩm · Ctrl + lăn chuột để phóng to');
  const toolbar = h('div', {class: 'tl-toolbar'},
    iconBtn('zoomOut', 'Thu nhỏ (−)', () => zoomBy(1 / 1.6), {size: 'sm'}), zoomSlider,
    iconBtn('zoomIn', 'Phóng to (+)', () => zoomBy(1.6), {size: 'sm'}),
    iconBtn('expand', 'Xem toàn bộ video', () => fit(), {size: 'sm'}), zoomLabel,
    h('span', {class: 'sep'}), snapBtn, followBtn, hint);

  const ruler = h('div', {class: 'tl-ruler'});
  const film = h('div', {class: 'tl-film'});
  const segs = h('div', {class: 'tl-segs'});
  const markerLayer = h('div', {class: 'tl-markers'});
  const selBand = h('div', {class: 'sel-band', hidden: true});
  const waveCanvas = h('canvas');
  const speechCanvas = h('canvas', {class: 'tl-speech'});
  const wave = h('div', {class: 'tl-wave'}, waveCanvas, speechCanvas);
  const playhead = h('div', {class: 'playhead'});
  const hoverLine = h('div', {class: 'hover-line', hidden: true});
  const content = h('div', {class: 'tl-content'}, ruler, film, segs, wave, selBand, markerLayer, playhead, hoverLine);
  const scroller = h('div', {class: 'tl-scroll'}, content);
  const labels = h('div', {class: 'tl-labels'},
    h('div', {}, icon('clock'), 'Thời gian'), h('div', {}, icon('image'), 'Hình ảnh'),
    h('div', {}, icon('pin'), 'Ghim giỏ'), h('div', {}, icon('waves'), 'Âm thanh'));
  const resize = h('div', {class: 'tl-resize', title: 'Kéo để đổi chiều cao timeline'});
  const root = h('section', {class: 'tl', 'aria-label': 'Timeline'}, resize, toolbar, h('div', {class: 'tl-body'}, labels, scroller));

  function syncToggles() {
    snapBtn.setAttribute('aria-pressed', String(snapOn)); snapBtn.style.color = snapOn ? 'var(--studio-accent)' : '';
    followBtn.setAttribute('aria-pressed', String(follow)); followBtn.style.color = follow ? 'var(--studio-accent)' : '';
  }
  syncToggles();

  // ---------- toạ độ ----------
  const doc = () => getDoc();
  const durationMs = () => Math.max(1000, doc().durationMs || 1000);
  const xOf = ms => ms / 1000 * pps;
  const msOfX = x => Math.max(0, Math.min(durationMs(), Math.round(x / pps * 1000)));
  function eventMs(e) {
    const rect = content.getBoundingClientRect();
    return msOfX(e.clientX - rect.left);
  }
  function visibleRange() {
    const left = scroller.scrollLeft, width = scroller.clientWidth;
    return {left, width, t0: left / pps * 1000, t1: (left + width) / pps * 1000};
  }

  // ---------- zoom ----------
  function computeFit() {
    const width = Math.max(200, scroller.clientWidth - 16);
    fitPps = width / (durationMs() / 1000);
    return fitPps;
  }
  function zoomTo(value, anchorMs) {
    computeFit();
    const next = Math.max(fitPps, Math.min(MAX_PPS, value));
    const anchor = anchorMs === undefined ? visibleRange().t0 + (visibleRange().t1 - visibleRange().t0) / 2 : anchorMs;
    const anchorScreen = xOf(anchor) - scroller.scrollLeft;
    pps = next;
    layout();
    scroller.scrollLeft = Math.max(0, xOf(anchor) - anchorScreen);
    drawVisible();
  }
  function zoomBy(factor) { zoomTo(pps * factor, playheadMs); }
  function fit() { computeFit(); pps = fitPps; layout(); scroller.scrollLeft = 0; drawVisible(); }
  function syncZoomUi() {
    const t = fitPps >= MAX_PPS ? 0 : Math.log(pps / fitPps) / Math.log(MAX_PPS / fitPps);
    zoomSlider.value = String(Math.round(Math.max(0, Math.min(1, t)) * 1000));
    const visibleSec = scroller.clientWidth / pps;
    zoomLabel.textContent = `≈ ${shortDuration(visibleSec * 1000)}/màn`;
  }

  // ---------- vẽ ----------
  function layout() {
    content.style.width = Math.ceil(xOf(durationMs())) + 'px';
    renderSegments();
    syncZoomUi();
    setPlayhead(playheadMs, {scroll: false});
  }

  function renderSegments() {
    const d = doc();
    const catalog = getCatalog();
    const selected = getSelection();
    const items = [];
    const list = withEnds(d);
    if (list.length && list[0].start > 0) {
      const w = xOf(list[0].start);
      items.push(h('div', {class: 'tl-gap', style: {left: '0px', width: Math.max(2, w - 2) + 'px'}, title: 'Trước mốc đầu tiên: tiện ích ghim sản phẩm của đoạn đầu'}, w > 120 ? 'Chưa có mốc · sẽ ghim đoạn đầu' : ''));
    }
    list.forEach(seg => {
      const left = xOf(seg.start), width = Math.max(2, xOf(seg.end) - left - 2);
      const product = resolveProduct(d, seg.product, catalog);
      const classes = ['seg', !seg.product ? 'none' : '', seg.index === selected ? 'selected' : '', seg.review === 'rejected' ? 'rejected' : '', d.kind === 'review' && (seg.review || 'pending') === 'pending' ? 'pending' : ''].filter(Boolean).join(' ');
      const node = h('div', {class: classes, dataset: {index: seg.index}, style: {left: left + 'px', width: width + 'px', '--bag': product ? bagColor(product.bag_index) : 'var(--bag-none)'}});
      if (width >= 44) node.append(productAvatar(product, 34));
      if (width >= 88) node.append(h('span', {class: 'seg-text'},
        h('strong', {}, product ? `${product.bag_index ? '#' + product.bag_index + ' ' : ''}${product.name}` : 'Không ghim'),
        h('small', {}, `${clock(seg.start)} · ${shortDuration(seg.end - seg.start)}`)));
      if (product && (product.bagChanged || product.missing) && width >= 24) node.append(h('span', {style: {position: 'absolute', right: '5px', bottom: '7px', color: '#ffcf66'}, title: product.missing ? 'Không còn trong giỏ hàng hôm nay' : `Giỏ đã đổi #${product.timelineBag} → #${product.bag_index}`}, icon('alert', {size: 13})));
      if (seg.product && seg.alt && seg.altEvery >= 10000) {
        const alt = resolveProduct(d, seg.alt, catalog);
        const stripes = h('span', {class: 'alt-stripes'});
        const stepPx = xOf(seg.altEvery);
        const total = xOf(seg.end - seg.start);
        for (let x = 0, k = 0; x < total && k < 400; x += stepPx, k += 1) {
          stripes.append(h('i', {style: {width: Math.min(stepPx, total - x) + 'px', background: k % 2 ? bagColor(alt && alt.bag_index) : bagColor(product && product.bag_index)}}));
        }
        node.append(stripes);
      }
      items.push(node);
      const movable = d.kind === 'review' || seg.index > 0;
      if (movable) items.push(h('div', {class: 'seg-handle', dataset: {handle: seg.index}, style: {left: left + 'px'}, title: 'Kéo để đổi giờ bắt đầu'}));
    });
    clear(segs, items);
    const sel = list[selected];
    selBand.hidden = !sel;
    if (sel) { selBand.style.left = xOf(sel.start) + 'px'; selBand.style.width = Math.max(2, xOf(sel.end) - xOf(sel.start)) + 'px'; }
    renderMarkers();
  }

  function renderMarkers() {
    clear(markerLayer, markers.map((m, i) => {
      const left = xOf(m.ms);
      if (m.kind === 'evidence') {
        const width = Math.max(4, xOf(Math.max(m.endMs || m.ms, m.ms + 1500)) - left);
        return h('div', {class: 'mk-evidence' + (m.active ? ' active' : ''), style: {left: left + 'px', width: width + 'px'}, title: m.label || 'Chỗ AI nghe thấy'});
      }
      return h('button', {class: 'mk-suggest', type: 'button', style: {left: left + 'px'}, title: m.label, dataset: {marker: i}}, '!');
    }));
  }
  markerLayer.addEventListener('pointerdown', e => {
    const node = e.target.closest('.mk-suggest');
    if (!node) return;
    e.stopPropagation();
    e.preventDefault();
    if (onMarker) onMarker(markers[Number(node.dataset.marker)]);
  });

  function renderRuler(range) {
    const minLabelPx = 86;
    const step = TICK_STEPS.find(s => s * pps >= minLabelPx) || 3600;
    const minor = step >= 60 ? step / 6 : step / 5;
    const start = Math.floor(range.t0 / 1000 / minor) * minor;
    const end = range.t1 / 1000 + minor;
    const ticks = [];
    for (let t = Math.max(0, start); t <= end && t <= durationMs() / 1000; t += minor) {
      const major = Math.abs(t / step - Math.round(t / step)) < 1e-6;
      ticks.push(h('div', {class: 'tick' + (major ? ' major' : ''), style: {left: xOf(t * 1000) + 'px', top: major ? '0' : '16px'}}, major ? h('span', {}, clock(t * 1000)) : null));
    }
    clear(ruler, ticks);
  }

  function spriteStyle(indexFrame, tileH) {
    const fs = assets.filmstrip;
    const tileW = tileH * fs.frame_w / fs.frame_h;
    const col = indexFrame % fs.cols, row = Math.floor(indexFrame / fs.cols);
    return {
      tileW,
      style: {
        backgroundImage: `url("${assets.filmstrip_url}")`,
        backgroundSize: `${fs.cols * tileW}px ${fs.rows * tileH}px`,
        backgroundPosition: `${-col * tileW}px ${-row * tileH}px`,
      },
    };
  }

  function renderFilm(range) {
    if (!assets || !assets.filmstrip || !assets.filmstrip_url) {
      const text = !assets ? 'Đang chuẩn bị hình ảnh…'
        : assets.state === 'missing_media' ? 'Không có file video trên máy — không có hình xem trước'
        : assets.state === 'error' ? 'Không tạo được hình xem trước'
        : `Đang tạo hình xem trước… ${Math.round((assets.progress || 0) * 100)}%`;
      clear(film, h('div', {class: 'film-state', style: {left: (range.left + 10) + 'px'}}, assets && assets.state !== 'missing_media' && assets.state !== 'error' ? h('span', {class: 'spinner', style: {width: '14px', height: '14px'}}) : icon('image', {size: 14}), text));
      return;
    }
    const fs = assets.filmstrip;
    const tileH = 60;
    const tileW = tileH * fs.frame_w / fs.frame_h;
    const slotPx = fs.interval_s * pps;
    const every = Math.max(1, Math.ceil((tileW + 2) / slotPx));
    const first = Math.max(0, Math.floor(range.t0 / 1000 / fs.interval_s) - every);
    const last = Math.min(fs.count - 1, Math.ceil(range.t1 / 1000 / fs.interval_s) + every);
    const tiles = [];
    for (let i = first - (first % every); i <= last; i += every) {
      if (i < 0) continue;
      const {style} = spriteStyle(i, tileH);
      tiles.push(h('div', {class: 'frame', style: {...style, left: xOf(fs.times[i] * 1000) + 'px', width: tileW + 'px'}}));
    }
    clear(film, tiles);
  }

  function drawWave(range) {
    const width = Math.max(1, Math.round(range.width));
    const height = wave.clientHeight || 44;
    const dpr = window.devicePixelRatio || 1;
    [waveCanvas, speechCanvas].forEach(c => { c.style.left = range.left + 'px'; c.style.width = width + 'px'; });
    waveCanvas.width = width * dpr; waveCanvas.height = height * dpr; waveCanvas.style.height = height + 'px';
    const g = waveCanvas.getContext('2d');
    g.scale(dpr, dpr);
    g.clearRect(0, 0, width, height);
    const peaks = assets && assets.waveform && assets.waveform.peaks;
    if (peaks && peaks.length) {
      const bucketMs = assets.waveform.bucket_s * 1000;
      const mid = (height - 6) / 2;
      for (let x = 0; x < width; x += 2) {
        const t0 = (range.left + x) / pps * 1000, t1 = (range.left + x + 2) / pps * 1000;
        let i0 = Math.floor(t0 / bucketMs), i1 = Math.max(i0 + 1, Math.ceil(t1 / bucketMs));
        let peak = 0;
        for (let i = i0; i < i1 && i < peaks.length; i += 1) if (peaks[i] > peak) peak = peaks[i];
        const v = peak / 255;
        const bar = Math.max(1, v * mid);
        g.fillStyle = v > 0.18 ? 'rgba(61, 220, 151, .85)' : 'rgba(120, 140, 160, .45)';
        g.fillRect(x, mid - bar, 1.4, bar * 2);
      }
    } else {
      g.fillStyle = 'rgba(120,140,160,.5)';
      g.font = '11px Segoe UI';
      g.fillText(assets && assets.state === 'missing_media' ? 'Không có âm thanh (thiếu file video)' : 'Đang phân tích âm thanh…', 10, height / 2 + 4);
    }
    speechCanvas.width = width * dpr; speechCanvas.height = 4 * dpr; speechCanvas.style.height = '4px';
    const sg = speechCanvas.getContext('2d');
    sg.scale(dpr, dpr);
    sg.fillStyle = 'rgba(147, 188, 255, .85)';
    transcript.forEach(line => {
      const x0 = xOf(line.startMs) - range.left, x1 = xOf(line.endMs) - range.left;
      if (x1 < 0 || x0 > width) return;
      sg.fillRect(Math.max(0, x0), 0, Math.max(1, x1 - x0), 4);
    });
  }

  const drawVisible = rafThrottle(() => {
    const range = visibleRange();
    renderRuler(range);
    renderFilm(range);
    drawWave(range);
    syncZoomUi();
  });

  function setPlayhead(ms, {scroll = true} = {}) {
    playheadMs = ms;
    playhead.style.transform = `translateX(${xOf(ms)}px)`;
    if (scroll && follow && !dragging) ensureVisible(ms);
  }

  function ensureVisible(ms, center = false) {
    const x = xOf(ms), range = visibleRange();
    if (Date.now() - userScrolledAt < 2500 && !center) return;
    if (center || x < range.left + 20 || x > range.left + range.width - 40) {
      scroller.scrollLeft = Math.max(0, x - range.width * (center ? 0.5 : 0.25));
    }
  }

  // ---------- tương tác ----------
  function snap(ms, e, exclude = -1) {
    if (!snapOn || (e && e.altKey)) return {ms, snapped: false};
    const threshold = SNAP_PX / pps * 1000;
    let best = null;
    const consider = (t, kind) => { const dist = Math.abs(t - ms); if (dist <= threshold && (!best || dist < best.dist)) best = {t, dist, kind}; };
    transcript.forEach(line => consider(line.startMs, 'transcript'));
    consider(playheadMs, 'playhead');
    doc().segments.forEach((seg, i) => { if (i !== exclude) consider(seg.start, 'segment'); });
    return best ? {ms: best.t, snapped: best.kind} : {ms, snapped: false};
  }

  function frameAt(ms) {
    if (!assets || !assets.filmstrip || !assets.filmstrip_url) return null;
    const fs = assets.filmstrip;
    const index = Math.max(0, Math.min(fs.count - 1, Math.round(ms / 1000 / fs.interval_s)));
    const tileH = 160;
    const {style, tileW} = spriteStyle(index, tileH);
    return h('div', {class: 'tip-frame', style: {...style, width: tileW + 'px', height: tileH + 'px'}});
  }

  function tipContent(ms) {
    const d = doc();
    const i = withEnds(d).findIndex(seg => ms >= seg.start && ms < seg.end);
    const seg = i >= 0 ? d.segments[i] : null;
    const product = seg ? resolveProduct(d, seg.product, getCatalog()) : null;
    return h('div', {class: 'tl-tip'}, frameAt(ms),
      h('div', {class: 'tip-row'}, h('strong', {class: 'mono'}, clock(ms, {tenths: pps > 8}))),
      seg ? h('div', {class: 'tip-row'}, product ? productAvatar(product, 22, {showBag: false}) : null, h('span', {}, product ? `Giỏ #${product.bag_index} · ${product.name}` : 'Không ghim sản phẩm')) : null);
  }

  const hover = rafThrottle((clientX, top, ms) => {
    hoverLine.hidden = false;
    hoverLine.style.transform = `translateX(${xOf(ms)}px)`;
    showTip(tipContent(ms), clientX, top);
  });
  content.addEventListener('pointermove', e => {
    if (dragging || e.buttons) return;
    hover(e.clientX, content.getBoundingClientRect().top, eventMs(e));
  });
  content.addEventListener('pointerleave', () => { hoverLine.hidden = true; hideTip(); });

  // Bấm/kéo trên thước, hình, sóng âm = tua (scrub)
  function startScrub(e) {
    if (e.button !== 0) return;
    e.preventDefault();
    const target = e.currentTarget;
    target.setPointerCapture(e.pointerId);
    const move = rafThrottle(ev => onSeek(eventMs(ev)));
    onSeek(eventMs(e));
    const up = () => { target.removeEventListener('pointermove', move); target.removeEventListener('pointerup', up); target.removeEventListener('pointercancel', up); };
    target.addEventListener('pointermove', move);
    target.addEventListener('pointerup', up);
    target.addEventListener('pointercancel', up);
  }
  [ruler, film, wave].forEach(node => node.addEventListener('pointerdown', startScrub));

  segs.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    const handle = e.target.closest('.seg-handle');
    if (handle) { startDrag(e, Number(handle.dataset.handle), handle); return; }
    const block = e.target.closest('.seg');
    const ms = eventMs(e);
    if (block) onSelect(Number(block.dataset.index), {seekTo: ms});
    else onSeek(ms);
  });
  segs.addEventListener('dblclick', e => {
    const block = e.target.closest('.seg');
    if (block) onEditProduct(Number(block.dataset.index));
  });

  function startDrag(e, index, handle) {
    e.preventDefault();
    e.stopPropagation();
    hideTip();
    const d = doc();
    const {lo, hi} = moveBounds(index, d);
    const segNode = segs.querySelector(`.seg[data-index="${index}"]`);
    const prevNode = segs.querySelector(`.seg[data-index="${index - 1}"]`);
    const end = index + 1 < d.segments.length ? d.segments[index + 1].start : d.durationMs;
    const prevStart = index > 0 ? d.segments[index - 1].start : 0;
    const label = h('div', {class: 'drag-time'});
    segs.append(label);
    handle.classList.add('dragging');
    handle.setPointerCapture(e.pointerId);
    dragging = {index, ms: d.segments[index].start};
    const preview = rafThrottle(ms => onScrubPreview && onScrubPreview(ms));
    const move = ev => {
      let {ms, snapped} = snap(eventMs(ev), ev, index);
      ms = Math.max(lo, Math.min(hi, ms));
      dragging.ms = ms;
      const x = xOf(ms);
      handle.style.left = x + 'px';
      if (segNode) { segNode.style.left = x + 'px'; segNode.style.width = Math.max(2, xOf(end) - x - 2) + 'px'; }
      if (prevNode) prevNode.style.width = Math.max(2, x - xOf(prevStart) - 2) + 'px';
      label.style.left = x + 'px';
      label.textContent = clock(ms, {tenths: true}) + (snapped === 'transcript' ? ' · đầu câu' : snapped === 'playhead' ? ' · đầu phát' : '');
      preview(ms);
    };
    const up = () => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
      handle.removeEventListener('pointercancel', up);
      label.remove();
      handle.classList.remove('dragging');
      const result = dragging;
      dragging = null;
      if (result && result.ms !== d.segments[index].start) onCommitMove(index, result.ms);
      else renderSegments();
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up);
    handle.addEventListener('pointercancel', up);
  }

  scroller.addEventListener('wheel', e => {
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      const rect = content.getBoundingClientRect();
      const anchor = msOfX(e.clientX - rect.left);
      zoomTo(pps * (e.deltaY < 0 ? 1.25 : 0.8), anchor);
    } else if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
      e.preventDefault();
      scroller.scrollLeft += e.deltaY;
    }
  }, {passive: false});
  scroller.addEventListener('scroll', () => { if (!dragging) userScrolledAt = Date.now(); drawVisible(); hideTip(); });

  // Kéo mép trên để đổi chiều cao timeline
  resize.addEventListener('pointerdown', e => {
    e.preventDefault();
    const studio = root.closest('.studio');
    const startY = e.clientY, startH = root.getBoundingClientRect().height;
    resize.setPointerCapture(e.pointerId);
    const move = ev => {
      const next = Math.max(190, Math.min(window.innerHeight * 0.55, startH + (startY - ev.clientY)));
      studio.style.setProperty('--tl-h', next + 'px');
    };
    const up = () => { resize.removeEventListener('pointermove', move); resize.removeEventListener('pointerup', up); try { localStorage.setItem('lr-tl-h', studio.style.getPropertyValue('--tl-h')); } catch (_) { /* */ } drawVisible(); };
    resize.addEventListener('pointermove', move);
    resize.addEventListener('pointerup', up);
  });

  const ro = new ResizeObserver(() => {
    const wasFit = Math.abs(pps - fitPps) < 1e-6;
    computeFit();
    if (wasFit || pps < fitPps) pps = fitPps;
    layout();
    drawVisible();
  });
  ro.observe(scroller);

  return {
    root,
    render() { renderSegments(); },
    relayout() { layout(); drawVisible(); },
    setPlayhead,
    setAssets(state) { assets = state; drawVisible(); },
    setTranscript(lines) { transcript = lines; drawVisible(); },
    zoomIn: () => zoomBy(1.6), zoomOut: () => zoomBy(1 / 1.6), fit,
    setMarkers(list) { markers = list || []; renderMarkers(); },
    /** Phóng để đoạn [a,b] chiếm ~60% bề ngang và nằm giữa. */
    zoomToRange(a, b) {
      computeFit();
      const span = Math.max(4000, b - a);
      pps = Math.max(fitPps, Math.min(MAX_PPS, scroller.clientWidth * 0.6 / (span / 1000)));
      layout();
      scroller.scrollLeft = Math.max(0, xOf(a) - (scroller.clientWidth - xOf(span)) / 2);
      userScrolledAt = Date.now();
      drawVisible();
    },
    reveal: ms => ensureVisible(ms, true),
    isDragging: () => Boolean(dragging),
    destroy() { ro.disconnect(); hideTip(); },
  };
}
