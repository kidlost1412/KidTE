import {createStore, startDashboardPoll} from './core/store.js';
import {createRouter} from './core/router.js';
import {createShell} from './shell.js';
import {h} from './core/dom.js';
import {guardStylesheets} from './core/asset_guard.js';
import {beginPageScope, endPageScope} from './core/api.js';
import {errorBox} from './ui/kit.js';
import {confirmDialog} from './ui/overlays.js';
import overview from './pages/overview.js';
import inbox from './pages/inbox.js';
import customers from './pages/customers.js';
import catalog from './pages/catalog.js';
import video from './pages/video/index.js';
import automation from './pages/automation.js';
import system from './pages/system.js';
import brain from './pages/brain.js';
import captcha from './pages/captcha.js';
import shopInfo from './pages/shop-info.js';
import playbook from './pages/playbook.js';

const PAGES = {overview, inbox, brain, customers, 'shop-info': shopInfo, playbook, catalog, video, automation, system, captcha};

const store = createStore({dashboard: null, dashboardError: null});
let mounted = null;
let pageScope = null;

const router = createRouter(route => mount(route));
const navigate = (page, sub, id, opts) => router.go(page, sub, id, opts);

const shell = createShell({
  goBack: () => router.back(),
  canGoBack: () => router.canBack(),
  navigate,
  commands: () => [
    {group: 'Đi tới', title: 'Tổng quan', sub: 'Tình hình phiên LIVE và việc cần làm', icon: 'home', run: () => navigate('overview')},
    {group: 'Đi tới', title: 'Hộp thư LIVE', sub: 'Bình luận, câu trả lời của AI và chủ shop', icon: 'message', keywords: 'binh luan comment tra loi', run: () => navigate('inbox')},
    {group: 'Đi tới', title: 'Bình luận bị bỏ sót', sub: 'Khách hỏi mà chưa ai trả lời', icon: 'alert', keywords: 'bo sot chua tra loi', run: () => navigate('inbox', 'unanswered')},
    {group: 'Đi tới', title: 'Thư viện video', sub: 'Video phát lại và lịch ghim sản phẩm', icon: 'film', keywords: 'video lich ghim timeline', run: () => navigate('video')},
    {group: 'Đi tới', title: 'Tải video mới để phân tích', sub: 'AI nghe lời nói, gợi ý mốc ghim', icon: 'upload', keywords: 'upload tai video', run: () => navigate('video', 'library', 'upload')},
    {group: 'Đi tới', title: 'Sản phẩm đang bán', sub: 'Giỏ hàng, giá, ảnh, hồ sơ', icon: 'box', keywords: 'san pham gio hang sku', run: () => navigate('catalog')},
    {group: 'Đi tới', title: 'Ánh xạ sản phẩm ↔ hồ sơ', sub: 'Gắn hồ sơ kỹ thuật cho từng listing', icon: 'link', keywords: 'anh xa mapping ho so', run: () => navigate('catalog', 'mapping')},
    {group: 'Đi tới', title: 'Hồ sơ kỹ thuật sản phẩm', sub: 'Công dụng, liều dùng AI được phép nói', icon: 'fileText', keywords: 'ho so lieu dung cong dung', run: () => navigate('catalog', 'profiles')},
    {group: 'Đi tới', title: 'Khách hàng', sub: 'Ai đã hỏi gì, bao nhiêu lần', icon: 'users', keywords: 'khach hang', run: () => navigate('customers')},
    {group: 'Dữ liệu', title: 'Thông tin shop', sub: 'Địa chỉ, hotline, giao hàng AI dùng khi khách hỏi', icon: 'home', keywords: 'thong tin shop dia chi hotline giao hang', run: () => navigate('shop-info')},
    {group: 'Dữ liệu', title: 'Sổ tay tình huống', sub: 'Cách AI xử lý bán chịu, hàng giả, chê đắt', icon: 'fileText', keywords: 'so tay tinh huong ban chiu', run: () => navigate('playbook')},
    {group: 'Đi tới', title: 'Phiên LIVE', sub: 'Lịch sử từng phiên', icon: 'activity', keywords: 'phien live session', run: () => navigate('customers', 'sessions')},
    {group: 'Đi tới', title: 'Trợ lý tự động', sub: 'Bật/tắt dịch vụ đồng bộ video, AI', icon: 'bot', run: () => navigate('automation')},
    {group: 'Đi tới', title: 'Trí nhớ AI', sub: 'Kiến thức AI đã học, phong cách người trực, học lại', icon: 'sparkles', keywords: 'hoc day ai kien thuc tri nho phong cach', run: () => navigate('brain')},
    {group: 'Đi tới', title: 'Tài liệu cho AI', sub: 'Tải tài liệu công ty, sản phẩm, kiến thức nông nghiệp để AI đọc', icon: 'fileText', keywords: 'tai lieu upload pdf word kien thuc', run: () => navigate('brain', 'docs')},
    {group: 'Đi tới', title: 'Thử AI', sub: 'Gõ câu khách hỏi, xem AI trả lời (không gửi)', icon: 'play', keywords: 'thu ai test', run: () => navigate('brain', 'try')},
    {group: 'Đi tới', title: 'Học lại toàn bộ', sub: 'Gộp kiến thức, viết lại phong cách từ mọi bản sửa', icon: 'refresh', keywords: 'hoc lai relearn', run: () => navigate('brain', 'relearn')},
    {group: 'Cài đặt', title: 'Cài đặt AI & tiện ích hai sàn', sub: 'Khoá API, model, tài khoản chủ shop, TikTok và Shopee', icon: 'key', keywords: 'deepseek api key model tiktok shopee', run: () => navigate('system', 'setup')},
    {group: 'Cài đặt', title: 'Nguồn LIVE theo sàn', sub: 'OBS cho Shopee, TikTok Studio cho TikTok, độ trễ riêng', icon: 'film', keywords: 'obs shopee tiktok studio dong bo video', run: () => navigate('system', 'live')},
    {group: 'Cài đặt', title: 'Giải CAPTCHA', sub: 'Solver độc lập và nhật ký SadCaptcha', icon: 'shield', keywords: 'captcha sadcaptcha solver', run: () => navigate('captcha')},
    {group: 'Cài đặt', title: 'Thiết bị âm thanh', sub: 'Chọn nguồn âm cho đồng bộ video', icon: 'volume', keywords: 'am thanh audio', run: () => navigate('system', 'audio')},
    {group: 'Cài đặt', title: 'Sao lưu dữ liệu', sub: 'Tạo và xem bản sao lưu', icon: 'archive', keywords: 'backup sao luu', run: () => navigate('system', 'backups')},
    {group: 'Cài đặt', title: 'Thùng rác', sub: 'Khôi phục nội dung đã xoá (30 ngày)', icon: 'trash', keywords: 'thung rac khoi phuc', run: () => navigate('system', 'trash')},
    {group: 'Cài đặt', title: 'Chẩn đoán hệ thống', sub: 'Kiểm tra cổng, tiện ích, dữ liệu', icon: 'shield', keywords: 'chan doan loi', run: () => navigate('system', 'diagnostics')},
    {group: 'Cài đặt', title: 'Nhật ký & sự cố', sub: 'Lỗi gần đây và cách xử lý', icon: 'terminal', keywords: 'log nhat ky loi su co', run: () => navigate('system', 'logs')},
    {group: 'Cài đặt', title: 'Model chép lời', sub: 'Tải model chép lời video hợp với máy', icon: 'mic', keywords: 'whisper chep loi model transcript', run: () => navigate('system', 'whisper')},
    {group: 'Trợ lý', title: 'Chính sách giá', sub: 'AI được nói giá nào, câu nào bị chặn', icon: 'tag', keywords: 'gia voucher ma chan', run: () => navigate('automation', 'chinh-sach-gia')},
    {group: 'Trợ lý', title: 'Chọn SKU từ câu hỏi', sub: 'AI hiểu khách hỏi sản phẩm nào', icon: 'target', keywords: 'sku san pham chon', run: () => navigate('automation', 'chon-san-pham')},
    {group: 'Trợ lý', title: 'Tính liều theo nhãn', sub: 'Sản phẩm nào AI tính được liều', icon: 'activity', keywords: 'lieu dung pha binh', run: () => navigate('automation', 'lieu-dung')},
    {group: 'Trợ lý', title: 'Nhà cung cấp DeepSeek', sub: 'Độ dài câu, độ sáng tạo, lời nhắc hệ thống', icon: 'bot', keywords: 'deepseek prompt temperature', run: () => navigate('automation', 'model-deepseek')},
    {group: 'Trợ lý', title: 'Thu dữ liệu phiên LIVE', sub: 'Luồng bình luận đã ghi', icon: 'database', keywords: 'du lieu raw stream', run: () => navigate('automation', 'thu-du-lieu')},
    {group: 'Trợ giúp', title: 'Xem phím tắt', sub: 'Phím ?', icon: 'keyboard', run: () => shell.showShortcuts()},
  ],
});

let asking = false;
router.setGuard(target => {
  if (!mounted || typeof mounted.isDirty !== 'function' || !mounted.isDirty()) return true;
  if (asking) return false;
  asking = true;
  if (typeof mounted.beforeLeave === 'function') {
    const current = mounted;
    Promise.resolve().then(() => current.beforeLeave()).then(ok => {
      if (!ok || mounted !== current) return;
      current.isDirty = () => false;
      history.pushState(null, '', target);
      router.start();
    }).catch(error => console.error(error)).finally(() => {asking = false;});
    return false;
  }
  confirmDialog({
    title: 'Rời trang khi còn thay đổi chưa lưu?',
    text: 'Các thay đổi chưa lưu trên trang này sẽ mất.',
    confirm: 'Rời trang', tone: 'danger', cancel: 'Ở lại',
  }).then(ok => {
    asking = false;
    if (!ok) return;
    if (mounted) mounted.isDirty = () => false;
    history.pushState(null, '', target);
    router.start();
  });
  return false;
});

function mount(route) {
  if (mounted && mounted.unmount) { try { mounted.unmount(); } catch (e) { console.error(e); } }
  mounted = null;
  endPageScope(pageScope);
  pageScope = beginPageScope();
  const page = PAGES[route.page] || overview;
  shell.setRoute(route, {fullBleed: false});
  shell.main.scrollTop = 0;
  const context = {
    route, store, shell, navigate,
    replaceSilently: (page_, sub, id) => router.replaceSilently(page_, sub, id),
  };
  try {
    mounted = page.mount(context) || null;
    if (mounted && mounted.fullBleed) shell.setFullBleed(true);
  } catch (error) {
    console.error(error);
    shell.main.replaceChildren(h('div', {class: 'page'}, errorBox('Trang này gặp lỗi khi khởi tạo: ' + (error && error.message), () => location.reload())));
  }
}

window.addEventListener('beforeunload', e => {
  if (mounted && typeof mounted.isDirty === 'function' && mounted.isDirty()) { e.preventDefault(); e.returnValue = ''; }
});

store.subscribe(state => shell.setStatus(state.dashboard, state.dashboardError));
const poll = startDashboardPoll(store, 5000);
window.__kidTe = {store, poll, navigate};
window.__liveRe = window.__kidTe; // Legacy integration alias.
router.start();
try { guardStylesheets(); window.addEventListener('load', () => guardStylesheets()); } catch (error) { console.error(error); }
