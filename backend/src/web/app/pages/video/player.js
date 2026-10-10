import {h, clear} from '../../core/dom.js';
import {icon} from '../../core/icons.js';
import {clock} from '../../core/format.js';
import {iconBtn} from '../../ui/kit.js';

const RATES = [0.75, 1, 1.25, 1.5, 2, 3];

/**
 * Trình phát của Studio. Không dựng lại khi đổi đoạn/lựa chọn; chỉ phát
 * sự kiện thời gian qua onTime (requestAnimationFrame khi đang phát để
 * playhead mượt, timeupdate chỉ ~4 lần/giây).
 */
export function createPlayer({src, durationMs = 0, fps = 30, onTime, onState, onMeta}) {
  let duration = durationMs;
  let frameMs = 1000 / (fps || 30);
  let raf = 0;
  let loop = null; // {start, end}
  const video = h('video', {preload: 'auto', playsinline: true, 'aria-label': 'Video đang chỉnh'});
  const errorBox = h('div', {class: 'video-error', hidden: true});
  const centerPlay = h('button', {class: 'video-center-play show', type: 'button', 'aria-label': 'Phát', onClick: () => toggle()}, h('span', {}, icon('play', {fill: true})));
  const overlay = h('div', {class: 'video-overlay'});
  const box = h('div', {class: 'video-box'}, video, centerPlay, overlay, errorBox);
  const stageVideo = h('div', {class: 'stage-video'}, box);

  const timecode = h('div', {class: 'timecode'}, '00:00');
  const playBtn = h('button', {class: 'btn play', type: 'button', title: 'Phát / dừng (Space)', 'aria-label': 'Phát', onClick: () => toggle()}, icon('play', {fill: true}));
  const rateGroup = h('select', {class: 'select speed-select', 'aria-label': 'Tốc độ phát', title: 'Tốc độ phát (< >)', onChange: e => setRate(Number(e.target.value))});
  const muteBtn = iconBtn('volume', 'Tắt tiếng', () => { video.muted = !video.muted; syncMute(); }, {size: 'sm'});
  function syncMute() { clear(muteBtn, icon(video.muted ? 'volumeX' : 'volume')); muteBtn.title = video.muted ? 'Bật tiếng' : 'Tắt tiếng'; }
  function renderRates() {
    clear(rateGroup, RATES.map(r => h('option', {value: String(r), selected: video.playbackRate === r}, `${r}x`)));
  }
  const small = (iconName, label, fn, text) => h('button', {class: 'btn ghost sm tbtn', type: 'button', title: label, 'aria-label': label, onClick: fn}, icon(iconName), text ? h('span', {class: 'tlabel'}, text) : null);
  // MỘT hàng điều khiển: mỗi pixel chiều cao nhường cho video dọc 9:16 là
  // thêm ~0,56 px chiều rộng. Lùi/tiến từng khung hình vẫn có phím , và .
  const transport = h('div', {class: 'transport'},
    h('div', {class: 'transport-row'},
      timecode,
      h('span', {class: 'grow'}),
      small('skipBack', 'Về đầu đoạn trước ([)', () => api.onPrevSegment && api.onPrevSegment()),
      small('rewind', 'Lùi 5 giây (J) · lùi 1 khung hình: phím ,', () => seekBy(-5000)),
      playBtn,
      small('fastForward', 'Tiến 5 giây (L) · tiến 1 khung hình: phím .', () => seekBy(5000)),
      small('skipForward', 'Tới đầu đoạn sau (])', () => api.onNextSegment && api.onNextSegment()),
      h('span', {class: 'grow'}),
      rateGroup, muteBtn));
  const root = h('div', {class: 'stage'}, stageVideo, transport);

  function emitTime() {
    const ms = currentMs();
    if (loop && !video.paused && ms >= loop.end) { video.currentTime = loop.start / 1000; }
    timecode.textContent = clock(ms, {tenths: true});
    timecode.title = 'Tổng thời lượng ' + clock(duration);
    if (onTime) onTime(ms);
  }
  function frame() { emitTime(); raf = video.paused ? 0 : requestAnimationFrame(frame); }
  function syncPlayState() {
    const paused = video.paused;
    clear(playBtn, icon(paused ? 'play' : 'pause', {fill: true}));
    playBtn.setAttribute('aria-label', paused ? 'Phát' : 'Dừng');
    centerPlay.classList.toggle('show', paused);
    if (!paused && !raf) raf = requestAnimationFrame(frame);
    if (onState) onState({paused});
  }

  video.addEventListener('play', syncPlayState);
  video.addEventListener('pause', () => { syncPlayState(); emitTime(); });
  video.addEventListener('seeked', emitTime);
  video.addEventListener('timeupdate', () => { if (video.paused) emitTime(); });
  video.addEventListener('ratechange', renderRates);
  video.addEventListener('loadedmetadata', () => {
    if (video.duration && Number.isFinite(video.duration)) duration = Math.round(video.duration * 1000);
    if (video.videoWidth && video.videoHeight) box.style.setProperty('--ar', `${video.videoWidth} / ${video.videoHeight}`);
    emitTime();
    if (onMeta) onMeta({durationMs: duration, width: video.videoWidth, height: video.videoHeight});
  });
  video.addEventListener('error', () => {
    errorBox.hidden = false;
    clear(errorBox, h('div', {}, icon('eyeOff'), h('strong', {}, 'Không mở được file video trên máy'),
      h('p', {class: 'fs-xs', style: {marginTop: '6px', maxWidth: '240px'}}, 'Vẫn chỉnh được lịch ghim bằng timeline và lời thoại. Muốn xem hình, đặt lại file gốc vào thư mục Tải xuống hoặc tải video lên lại.')));
    centerPlay.hidden = true;
  });
  box.addEventListener('dblclick', () => { if (document.fullscreenElement) document.exitFullscreen(); else box.requestFullscreen?.(); });
  video.addEventListener('click', () => toggle());

  // Cột trình phát rộng đúng bằng video (dọc 9:16 thì hẹp), phần còn lại cho bảng chỉnh.
  let aspect = 9 / 16;
  const fitStage = () => {
    const hgt = stageVideo.clientHeight;
    if (!hgt) return;
    const width = Math.round(Math.max(340, Math.min(window.innerWidth * 0.46, hgt * aspect + 20)));
    const host = root.closest('.studio');
    if (host) host.style.setProperty('--stage-w', width + 'px');
  };
  const stageObserver = new ResizeObserver(fitStage);
  stageObserver.observe(stageVideo);
  video.addEventListener('loadedmetadata', () => { if (video.videoWidth && video.videoHeight) { aspect = video.videoWidth / video.videoHeight; fitStage(); } });

  if (src) video.src = src; else video.dispatchEvent(new Event('error'));
  renderRates();
  syncMute();

  function currentMs() { return Math.round((video.currentTime || 0) * 1000); }
  function seek(ms) {
    const clamped = Math.max(0, Math.min(ms, Math.max(0, duration - 50)));
    if (video.readyState === 0 && !src) { lastSeek = clamped; emitVirtual(clamped); return; }
    video.currentTime = clamped / 1000;
    if (video.error) emitVirtual(clamped);
  }
  let lastSeek = 0;
  function emitVirtual(ms) { lastSeek = ms; timecode.textContent = clock(ms, {tenths: true}); if (onTime) onTime(ms); }
  function seekBy(delta) { seek((video.error ? lastSeek : currentMs()) + delta); }
  function stepFrame(n) { if (!video.paused) video.pause(); seekBy(n * frameMs); }
  function toggle() { if (video.error) return; if (video.paused) video.play().catch(() => {}); else video.pause(); }
  function setRate(r) { video.playbackRate = r; }

  const api = {
    root, video, overlay, box,
    currentMs: () => (video.error ? lastSeek : currentMs()),
    durationMs: () => duration,
    seek, seekBy, stepFrame, toggle, setRate,
    play: () => video.play().catch(() => {}), pause: () => video.pause(),
    paused: () => video.paused,
    faster() { const i = RATES.indexOf(video.playbackRate); setRate(RATES[Math.min(RATES.length - 1, i + 1)] || 1); },
    slower() { const i = RATES.indexOf(video.playbackRate); setRate(RATES[Math.max(0, i - 1)] || 1); },
    setLoop(range) { loop = range; },
    setFps(value) { frameMs = 1000 / (value || 30); },
    hasMedia: () => !video.error && Boolean(src),
    onPrevSegment: null, onNextSegment: null,
    destroy() { cancelAnimationFrame(raf); stageObserver.disconnect(); video.pause(); video.removeAttribute('src'); video.load(); },
  };
  return api;
}
