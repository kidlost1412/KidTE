// Bảo vệ stylesheet: WebView2 đôi khi mất một request CSS lúc khởi động (kết nối bị reset)
// và trang không tự hồi phục. Kiểm tra từng <link rel=stylesheet>, tải lại có query chống cache.

export function sheetLoaded(link) {
  const sheet = link && link.sheet;
  if (!sheet) return false;
  try {
    return Boolean(sheet.cssRules && sheet.cssRules.length > 0);
  } catch (_) {
    return true; // cross-origin: không đọc được nhưng đã nạp
  }
}

export function bustUrl(href, attempt, now = Date.now()) {
  const base = String(href).replace(/([?&])(retry|t)=[^&]*/g, '$1').replace(/[?&]+$/, '');
  return `${base}${base.includes('?') ? '&' : '?'}retry=${attempt}&t=${now}`;
}

export function guardStylesheets({doc = document, retries = 4, delay = 600, setTimer = setTimeout, now = Date.now} = {}) {
  const attempts = new Map();
  const rootOf = href => String(href).replace(/[?&]retry=.*$/, '');
  const check = link => {
    if (sheetLoaded(link)) return;
    const href = link.getAttribute('href');
    const root = rootOf(href);
    const used = attempts.get(root) || 0;
    if (used >= retries) return;
    attempts.set(root, used + 1);
    const fresh = doc.createElement('link');
    fresh.rel = 'stylesheet';
    fresh.href = bustUrl(href, used + 1, now());
    const after = () => setTimer(() => {
      if (sheetLoaded(fresh)) { if (link.remove) link.remove(); } else check(fresh);
    }, 0);
    fresh.addEventListener('load', after);
    fresh.addEventListener('error', () => setTimer(after, delay * (used + 1)));
    if (link.after) link.after(fresh); else doc.head.appendChild(fresh);
  };
  const scan = () => doc.querySelectorAll('link[rel="stylesheet"]').forEach(check);
  scan();
  for (let i = 1; i <= retries; i += 1) setTimer(scan, delay * i);
  return {scan, attempts};
}
