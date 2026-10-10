// Định dạng hiển thị cho người vận hành Việt Nam. Module THUẦN (không DOM)
// để kiểm thử bằng node --test.

export function pad2(n) { return String(Math.floor(Math.abs(n))).padStart(2, '0'); }

/** 3014536 -> "50:14"; 3725000 -> "1:02:05". Không bao giờ hiện mili-giây thô. */
export function clock(ms, {tenths = false, forceHours = false} = {}) {
  const value = Math.max(0, Number(ms) || 0);
  const totalTenths = Math.floor(value / 100);
  const totalSeconds = Math.floor(totalTenths / 10);
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const sec = totalSeconds % 60;
  let out = (h > 0 || forceHours) ? `${h}:${pad2(m)}:${pad2(sec)}` : `${pad2(m)}:${pad2(sec)}`;
  if (tenths) out += '.' + (totalTenths % 10);
  return out;
}

/** "2:47" | "02:47.5" | "1:02:05" | "167" (giây) | "2p47" | "2m 47s" -> ms; sai -> null */
export function parseClock(input) {
  const text = String(input ?? '').trim().toLowerCase().replace(/,/g, '.');
  if (!text) return null;
  let match = text.match(/^(\d+)\s*(?:p|m|ph|phút)\s*(\d+(?:\.\d+)?)?\s*(?:s|giây)?$/);
  if (match) return Math.round((Number(match[1]) * 60 + Number(match[2] || 0)) * 1000);
  if (/^\d+(?:\.\d+)?\s*(?:s|giây)?$/.test(text)) return Math.round(parseFloat(text) * 1000);
  const parts = text.split(':');
  if (parts.length < 2 || parts.length > 3 || parts.some(p => !/^\d+(?:\.\d+)?$/.test(p))) return null;
  const nums = parts.map(Number);
  if (nums.slice(1).some(n => n >= 60)) return null;
  const seconds = parts.length === 3 ? nums[0] * 3600 + nums[1] * 60 + nums[2] : nums[0] * 60 + nums[1];
  return Math.round(seconds * 1000);
}

/** Thời lượng dạng chữ: 3014536 -> "50 phút 14 giây" */
export function durationText(ms) {
  const total = Math.round(Math.max(0, Number(ms) || 0) / 1000);
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), sec = total % 60;
  const parts = [];
  if (h) parts.push(`${h} giờ`);
  if (m) parts.push(`${m} phút`);
  if (sec || !parts.length) parts.push(`${sec} giây`);
  return parts.join(' ');
}

export function shortDuration(ms) {
  const total = Math.round(Math.max(0, Number(ms) || 0) / 1000);
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60), sec = total % 60;
  if (m < 60) return sec ? `${m}p${pad2(sec)}` : `${m}p`;
  return `${Math.floor(m / 60)}g${pad2(m % 60)}`;
}

export function toDate(value) {
  if (!value) return null;
  if (value instanceof Date) return Number.isNaN(+value) ? null : value;
  const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
  return Number.isNaN(+date) ? null : date;
}

export function dateTime(value) {
  const date = toDate(value);
  if (!date) return '—';
  return date.toLocaleString('vi-VN', {hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit', year: 'numeric'});
}

export function timeOnly(value) {
  const date = toDate(value);
  return date ? date.toLocaleTimeString('vi-VN', {hour: '2-digit', minute: '2-digit'}) : '—';
}

/** "lúc 15:10" nếu là hôm nay, ngược lại "12/09 08:30" — cho nhãn trạng thái. */
export function timeOrDate(value, now = new Date()) {
  const date = toDate(value);
  if (!date) return '';
  if (date.toDateString() === now.toDateString()) return 'lúc ' + timeOnly(date);
  return date.toLocaleString('vi-VN', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'});
}

/** "vừa xong", "5 phút trước", "hôm qua 14:20", "12/09 08:30" */
export function relative(value, now = new Date()) {
  const date = toDate(value);
  if (!date) return '—';
  const diff = (now - date) / 1000;
  if (diff < 0 && diff > -60) return 'vừa xong';
  if (diff < 0) return dateTime(date);
  if (diff < 45) return 'vừa xong';
  if (diff < 3600) return `${Math.max(1, Math.round(diff / 60))} phút trước`;
  const sameDay = date.toDateString() === now.toDateString();
  if (sameDay) return `${Math.round(diff / 3600)} giờ trước`;
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return `hôm qua ${timeOnly(date)}`;
  if (diff < 7 * 86400) return `${Math.round(diff / 86400)} ngày trước`;
  return date.toLocaleString('vi-VN', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'});
}

export function number(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString('vi-VN') : '—';
}

export function bytes(value) {
  let n = Number(value) || 0;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

/** Bỏ dấu tiếng Việt để tìm kiếm: "Đạm cá" -> "dam ca" */
export function fold(value) {
  return String(value ?? '').toLowerCase().replace(/đ/g, 'd').normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/\s+/g, ' ').trim();
}

export function initials(name) {
  const words = String(name || '?').trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '?';
  return (words.length === 1 ? words[0].slice(0, 2) : words[0][0] + words[words.length - 1][0]).toUpperCase();
}

export function hashHue(value) {
  let hash = 0;
  for (const ch of String(value || '')) hash = (hash * 31 + ch.charCodeAt(0)) | 0;
  return Math.abs(hash) % 360;
}

/** Mọi quy cách trong một chuỗi, chuẩn hoá: "Chai 250-500ml" -> ["250ml","500ml"], "Can 5 Lít" -> ["5l"]. */
export function packSizes(text) {
  const t = fold(text).replace(/,/g, '.');
  const out = new Set();
  const norm = (n, u) => {
    if (u === 'lit') u = 'l';
    if (u === 'gr' || u === 'gram' || u === 'gam') u = 'g';
    if (u === 'l' && n < 1) { n = Math.round(n * 1000); u = 'ml'; }
    if (u === 'kg' && n < 1) { n = Math.round(n * 1000); u = 'g'; }
    return `${n}${u}`;
  };
  const re = /(\d+(?:\.\d+)?)(?:\s*[-–\/]\s*(\d+(?:\.\d+)?))?\s*(ml|l|lit|kg|gr|g|gram|gam)\b/g;
  let m;
  while ((m = re.exec(t))) {
    out.add(norm(Number(m[1]), m[3]));
    if (m[2]) out.add(norm(Number(m[2]), m[3]));
  }
  return [...out];
}

export function packSize(text) { return packSizes(text)[0] || ''; }

/** Listing và hồ sơ ghi quy cách khác nhau? Chỉ khi CẢ HAI có ghi và không có cỡ chung. */
export function sizeMismatch(listingTitle, profileName) {
  const a = packSizes(listingTitle), b = packSizes(profileName);
  if (!a.length || !b.length || a.some(x => b.includes(x))) return null;
  return {listing: a.join('/'), profile: b.join('/')};
}
