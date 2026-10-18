import {aiStatus} from './core/ai-status.js';
import {connectionLost, shortPill, OFFLINE_LABEL, OFFLINE_TITLE} from './core/status-model.js';
import {h, clear, isTypingTarget} from './core/dom.js';
import {icon} from './core/icons.js';
import {fold, relative} from './core/format.js';
import {openModal, isOverlayOpen} from './ui/overlays.js';
import {request} from './core/api.js';
import {updateNotice, extensionNotice, updateComponents, DISMISS_KEY} from './core/update_notice.js';

const NAV = [
  {group: 'Vận hành'},
  {page: 'overview', label: 'Trung tâm LIVE', icon: 'home'},
  {page: 'inbox', label: 'Hộp thư LIVE', icon: 'message', badge: d => d?.session?.todo ?? d?.session?.unanswered, tone: 'danger'},
  {page: 'brain', label: 'Trí nhớ AI', icon: 'sparkles', badge: d => d?.learning?.suggested, tone: 'warn'},
  {page: 'video', label: 'Video & lịch ghim', icon: 'film', badge: d => d?.video_review?.pending_jobs, tone: 'warn'},
  {group: 'Dữ liệu'},
  {page: 'catalog', label: 'Sản phẩm', icon: 'box', badge: d => d?.catalog?.unmapped, tone: 'warn'},
  {page: 'customers', label: 'Khách hàng', icon: 'users'},
  {page: 'shop-info', label: 'Thông tin shop', icon: 'home'},
  {page: 'playbook', label: 'Sổ tay tình huống', icon: 'fileText'},
  {group: 'Hệ thống'},
  {page: 'captcha', label: 'Giải CAPTCHA', icon: 'shield'},
  {page: 'automation', label: 'Trợ lý tự động', icon: 'bot'},
  {page: 'system', label: 'Cài đặt & hệ thống', icon: 'settings', badge: d => (d && d.backup && d.backup.overdue) ? '!' : 0, tone: 'warn'},
];

function getTheme() {
  try { return localStorage.getItem('lr-theme') || 'system'; } catch (_) { return 'system'; }
}
export function applyTheme(theme) {
  try { if (theme === 'system') localStorage.removeItem('lr-theme'); else localStorage.setItem('lr-theme', theme); } catch (_) { /* */ }
  if (theme === 'system') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = theme;
}
function effectiveTheme() {
  const t = getTheme();
  if (t !== 'system') return t;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

const TO_TOP_AFTER = 400;

export function createShell({navigate, commands, goBack = () => false, canGoBack = () => false}) {
  const root = document.getElementById('app');
  const navButtons = new Map();
  let collapsed = false;
  try { collapsed = localStorage.getItem('lr-sidebar') === 'collapsed'; } catch (_) { /* */ }

  const nav = h('nav', {class: 'nav', 'aria-label': 'Điều hướng chính'});
  NAV.forEach(item => {
    if (item.group) { nav.append(h('div', {class: 'nav-group'}, item.group)); return; }
    const badge = h('span', {class: 'nav-badge', hidden: true});
    const update = item.page === 'system' ? h('i', {class: 'nav-update', hidden: true, title: 'Có bản cập nhật mới'}) : null;
    const button = h('button', {class: 'nav-item', type: 'button', title: item.label, onClick: () => navigate(item.page)},
      icon(item.icon), h('span', {class: 'nav-label'}, item.label), badge, h('i', {class: 'nav-dot', hidden: true}), update);
    navButtons.set(item.page, {button, badge, item, update});
    nav.append(button);
  });

  const themeBtn = h('button', {class: 'nav-item', type: 'button', onClick: () => { applyTheme(effectiveTheme() === 'dark' ? 'light' : 'dark'); renderThemeBtn(); }});
  function renderThemeBtn() {
    const dark = effectiveTheme() === 'dark';
    clear(themeBtn, icon(dark ? 'sun' : 'moon'), h('span', {class: 'nav-label'}, dark ? 'Giao diện sáng' : 'Giao diện tối'));
    themeBtn.title = dark ? 'Chuyển giao diện sáng' : 'Chuyển giao diện tối';
  }
  renderThemeBtn();
  const collapseBtn = h('button', {class: 'nav-item', type: 'button', onClick: () => setCollapsed(!collapsed)});
  function setCollapsed(value) {
    collapsed = value;
    shell.classList.toggle('collapsed', collapsed);
    try { localStorage.setItem('lr-sidebar', collapsed ? 'collapsed' : 'open'); } catch (_) { /* */ }
    clear(collapseBtn, icon('sidebar'), h('span', {class: 'nav-label'}, collapsed ? 'Mở rộng thanh bên' : 'Thu gọn thanh bên'));
    collapseBtn.title = collapsed ? 'Mở rộng thanh bên' : 'Thu gọn thanh bên';
  }

  const sidebar = h('aside', {class: 'sidebar'},
    h('a', {class: 'brand', href: '#overview', 'aria-label': 'Kid TE — Tổng quan'},
      h('span', {class: 'brand-mark'}, h('img', {src: '/assets/kid-te-icon.png', alt: '', width: '32', height: '32'})),
      h('span', {class: 'brand-copy'}, h('strong', {class:'brand-wordmark','aria-label':'Kid TE'}, h('span',{},'K'),h('b',{},'T'),h('span',{},'i'),h('b',{},'E'),h('span',{},'d')), h('small', {}, 'Trợ lý vận hành LIVE'))),
    nav,
    h('div', {class: 'sidebar-foot'}, themeBtn, collapseBtn));

  const statusStrip = h('div', {class: 'status-strip', 'aria-label': 'Trạng thái vận hành'});
  const backBtn = h('button', {class: 'topbar-back', type: 'button', 'aria-label': 'Quay lại trang trước', title: 'Quay lại trang trước (Alt+←)', disabled: true,
    onClick: () => goBackNow()}, icon('arrowLeft'));
  function goBackNow() { if (canGoBack()) goBack(); }
  const trigger = h('button', {class: 'command-trigger', type: 'button', onClick: () => openPalette()},
    icon('search'), h('span', {}, 'Tìm trang, lệnh…'), h('span', {class: 'kbd'}, 'Ctrl K'));
  // Banner cập nhật: ẩn theo phiên bản đã đóng trong phiên này (sessionStorage); badge trên sidebar vẫn hiện.
  let updateState = null, updateDismissed = '';
  try { updateDismissed = sessionStorage.getItem(DISMISS_KEY) || ''; } catch (_) { /* */ }
  const updateText = h('span', {class: 'update-banner-text'});
  const updateBanner = h('div', {class: 'update-banner', role: 'status', hidden: true},
    h('button', {class: 'update-banner-go', type: 'button', onClick: async () => {
      // Cùng một hành động "Cập nhật" như trang Cập nhật: bắt đầu tải rồi mở trang để xem tiến trình.
      try {
        if (updateState && updateState.update_available && !updateState.restart_required && !updateState.busy) {
          await request('/api/updates/install', {method: 'POST', body: {components: updateComponents(updateState)}});
        }
      } catch (_) { /* trang Cập nhật sẽ hiện lỗi bằng lời dễ hiểu */ }
      navigate('system', 'updates');
    }}, updateText),
    h('button', {class: 'update-banner-x', type: 'button', 'aria-label': 'Ẩn thông báo trong phiên này', title: 'Ẩn trong phiên này',
      onClick: () => {
        updateDismissed = updateNotice(updateState).version || '';
        try { sessionStorage.setItem(DISMISS_KEY, updateDismissed); } catch (_) { /* */ }
        renderUpdate();
      }}, '×'));
  const extText = h('span', {class: 'update-banner-text'});
  const extBanner = h('div', {class: 'update-banner', role: 'status', hidden: true}, h('span', {class: 'update-banner-go'}, extText),
    h('button', {class: 'update-banner-x', type: 'button', 'aria-label': 'Đã tải lại tiện ích, ẩn thông báo', title: 'Đã tải lại — ẩn thông báo',
      onClick: async () => { try { await request('/api/updates/ack_extension_notice', {method: 'POST', body: {}}); } catch (_) { /* */ } extBanner.hidden = true; }}, '×'));
  const topbar = h('header', {class: 'topbar'}, backBtn, trigger, updateBanner, extBanner, statusStrip);
  const main = h('main', {class: 'main', id: 'main', tabindex: '-1'});
  const toTop = h('button', {class: 'to-top', type: 'button', 'aria-label': 'Lên đầu trang', title: 'Lên đầu trang', hidden: true,
    onClick: () => main.scrollTo({top: 0, behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'})}, icon('arrowUp'));
  const shell = h('div', {class: 'shell'}, sidebar, topbar, main, toTop);
  root.replaceChildren(shell);
  document.querySelector('.skip-link')?.addEventListener('click', e => { e.preventDefault(); main.focus(); });
  main.addEventListener('scroll', syncToTop, {passive: true});
  function syncToTop() { toTop.hidden = main.scrollTop <= TO_TOP_AFTER; }
  setCollapsed(collapsed);

  function pill(label, tone, onClick, {title, live, short} = {}) {
    return h('button', {class: 'status-pill' + (live ? ' live-on' : ''), type: 'button', title: title || label, 'aria-label': label, onClick},
      h('i', {class: 'status-dot ' + tone}), h('span', {class: 'pill-label'}, label),
      short ? h('span', {class: 'pill-short', 'aria-hidden': 'true'}, short) : null);
  }

  function setStatus(dash, error) {
    if (connectionLost(error)) {
      clear(statusStrip, pill(OFFLINE_LABEL, 'danger', () => location.reload(), {title: OFFLINE_TITLE, short: 'Mất kết nối ✗'}));
      return;
    }
    const ext = (dash && dash.extension) || {};
    const connected = Boolean(ext.connected);
    const live = connected && ext.live_running;
    const ai = (dash && dash.ai) || {};
    const video = (dash && dash.video) || null;
    const videoOk = video && ['watching', 'detected', 'running', 'tracking'].includes(video.state);
    const captcha = (dash && dash.captcha) || {};
    clear(statusStrip,
      ...['tiktok', 'shopee'].map(platform => {
        const channel = dash?.live_channels?.[platform] || {}, ext = channel.extension || {};
        const live = ext.connected && ext.live_running;
        const name = platform === 'shopee' ? 'Shopee' : 'TikTok';
        const tone = live ? 'live' : 'warn';
        return pill(`${name}: ${live ? 'LIVE' : ext.connected ? 'đã nối' : 'chưa nối'}`, tone,
          () => navigate('system', 'live'), {live, short: shortPill(name, live || ext.connected ? 'ok' : 'warn'), title: `${name} · ${channel.position?.available ? 'đã nhận diện video riêng' : 'chờ nguồn riêng'}`});
      }),
      pill(ai.deepseek_available ? 'DeepSeek sẵn sàng' : aiStatus(ai).checking ? aiStatus(ai).text : 'AI chưa sẵn sàng', aiStatus(ai).kind, () => navigate('system', 'setup'),
        {short: shortPill('AI', ai.deepseek_available ? 'ok' : aiStatus(ai).kind)}),
      pill(captcha.running ? 'CAPTCHA đang quét' : captcha.key_rejected ? 'CAPTCHA: khoá lỗi' : 'CAPTCHA chưa chạy',
        captcha.running ? 'ok' : captcha.key_rejected ? 'danger' : 'warn', () => navigate('captcha'),
        {short: shortPill('CAPTCHA', captcha.running ? 'ok' : captcha.key_rejected ? 'danger' : 'warn')}));
    navButtons.forEach(({badge, item, button}) => {
      const value = item.badge ? item.badge(dash) : 0;
      const show = Boolean(value) && value !== '0';
      badge.hidden = !show;
      badge.textContent = show ? (Number(value) > 99 ? '99+' : String(value)) : '';
      badge.className = 'nav-badge ' + (item.tone === 'danger' ? '' : item.tone || '');
      button.querySelector('.nav-dot').hidden = !show;
    });
  }

  // -------- Thông báo bản cập nhật (đọc trạng thái, không cài) --------
  function renderUpdate() {
    const notice = updateNotice(updateState, updateDismissed);
    updateBanner.hidden = !notice.visible;
    updateText.textContent = notice.text;
    const ext = extensionNotice(updateState);
    extBanner.hidden = !ext.visible;
    extText.textContent = ext.text;
    navButtons.forEach(({update}) => { if (update) update.hidden = !notice.badge; });
  }
  async function refreshUpdates() {
    try { updateState = await request('/api/updates'); } catch (_) { return; }
    renderUpdate();
  }
  refreshUpdates();
  setInterval(refreshUpdates, 60 * 1000);

  // -------- Command palette (Ctrl+K) --------
  let paletteOpen = null;
  function openPalette() {
    if (paletteOpen) return;
    const items = commands();
    const inputNode = h('input', {placeholder: 'Gõ để tìm: "video", "khách", "cài đặt", "giỏ 3"…', 'aria-label': 'Tìm lệnh'});
    const list = h('div', {class: 'palette-list', role: 'listbox'});
    let index = 0, filtered = items;
    function render() {
      list.replaceChildren();
      if (!filtered.length) { list.append(h('div', {class: 'empty'}, h('strong', {}, 'Không có kết quả'), h('p', {}, 'Thử từ khoá khác.'))); return; }
      let group = '';
      filtered.forEach((cmd, i) => {
        if (cmd.group !== group) { group = cmd.group; list.append(h('div', {class: 'palette-group'}, group)); }
        const node = h('button', {class: 'palette-item', type: 'button', role: 'option', 'aria-selected': String(i === index), onClick: () => run(cmd), onMousemove: () => { if (index !== i) { index = i; render(); } }},
          h('span', {class: 'pi-icon'}, icon(cmd.icon || 'arrowRight')), h('span', {class: 'grow'}, h('strong', {}, cmd.title), cmd.sub ? h('small', {}, cmd.sub) : null));
        list.append(node);
      });
      list.querySelector('[aria-selected="true"]')?.scrollIntoView({block: 'nearest'});
    }
    function run(cmd) { entry.close(); paletteOpen = null; cmd.run(); }
    inputNode.addEventListener('input', () => {
      const q = fold(inputNode.value);
      filtered = !q ? items : items.filter(cmd => fold(`${cmd.title} ${cmd.sub || ''} ${cmd.keywords || ''} ${cmd.group}`).includes(q));
      index = 0; render();
    });
    inputNode.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown') { e.preventDefault(); index = Math.min(filtered.length - 1, index + 1); render(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); index = Math.max(0, index - 1); render(); }
      else if (e.key === 'Enter' && filtered[index]) { e.preventDefault(); run(filtered[index]); }
    });
    const palette = h('div', {class: 'palette'}, h('div', {class: 'palette-search'}, icon('search'), inputNode), list);
    const overlay = h('div', {class: 'overlay', style: {placeItems: 'start center'}}, palette);
    const entry = {close() { overlay.remove(); document.removeEventListener('keydown', esc, true); }};
    const esc = e => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); entry.close(); paletteOpen = null; } };
    document.addEventListener('keydown', esc, true);
    overlay.addEventListener('mousedown', e => { if (e.target === overlay) { entry.close(); paletteOpen = null; } });
    document.body.append(overlay);
    paletteOpen = entry;
    render();
    inputNode.focus();
  }

  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); }
    else if (e.altKey && !e.ctrlKey && !e.metaKey && e.key === 'ArrowLeft' && !isOverlayOpen() && !paletteOpen) { e.preventDefault(); goBackNow(); }
    else if (e.key === '?' && !isTypingTarget(e.target) && !isOverlayOpen() && !paletteOpen) { showShortcuts(); }
  });

  function showShortcuts() {
    const rows = [
      ['Ctrl K', 'Mở tìm kiếm nhanh / lệnh'], ['?', 'Xem phím tắt'],
      ['Space', 'Studio: phát / dừng'], ['J / L', 'Lùi / tiến 5 giây'], ['← / →', 'Lùi / tiến 1 giây (giữ Shift: 10 giây)'],
      [', / .', 'Lùi / tiến 1 khung hình'], ['< / >', 'Giảm / tăng tốc độ phát'],
      ['1 … 9, 0', 'Ghim giỏ số đó TỪ GIÂY ĐANG XEM (0 = giỏ 10)'], ['M', 'Ghim sản phẩm tại đây (chọn từ danh sách)'],
      ['S', 'Cắt đoạn tại giây đang xem'], ['[ / ]', 'Tới đoạn trước / sau'], ['Z', 'Phóng timeline vào đoạn đang chọn'],
      ['Delete', 'Xoá mốc (khi duyệt AI: loại mốc)'], ['Ctrl Z / Ctrl Y', 'Hoàn tác / làm lại'], ['+ / −', 'Phóng to / thu nhỏ timeline'],
      ['Enter', 'Duyệt AI: mốc này ĐÚNG → sang mốc chưa duyệt kế tiếp'], ['X', 'Duyệt AI: mốc này SAI → sang mốc kế tiếp'],
      ['N', 'Duyệt AI: tới mốc chưa duyệt'], ['R', 'Nghe lại mốc từ 3 giây trước'],
    ];
    openModal({title: 'Phím tắt', size: 'wide', body: h('table', {class: 'table'}, h('tbody', {}, rows.map(([k, v]) => h('tr', {}, h('td', {style: {width: '160px'}}, k.split(' / ').flatMap((part, i) => [i ? ' / ' : '', h('span', {class: 'kbd'}, part)])), h('td', {}, v)))))});
  }

  return {
    main,
    setRoute(route, {fullBleed = false} = {}) {
      navButtons.forEach(({button, item}) => {
        if (item.page === route.page) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
      });
      main.classList.toggle('full', fullBleed);
      shell.classList.toggle('collapsed', fullBleed || collapsed);
      shell.classList.toggle('studio-mode', fullBleed);
      backBtn.disabled = !canGoBack();
      syncToTop();
    },
    setFullBleed(value) {
      main.classList.toggle('full', value);
      // Studio cần tối đa bề ngang: thu gọn thanh bên tạm thời, không ghi nhớ.
      shell.classList.toggle('collapsed', value || collapsed);
      shell.classList.toggle('studio-mode', value);
    },
    setStatus,
    refreshUpdates,
    openPalette,
    showShortcuts,
    relative,
  };
}
