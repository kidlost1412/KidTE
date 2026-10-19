"use strict";
const $ = id => document.getElementById(id);
let busy = false;
function message(value, error = false) { $("status").textContent = value; $("status").className = error ? "error" : ""; }
let refreshPromise = null, refreshAgain = false;
async function refreshStatus() {
  if (refreshPromise) { refreshAgain = true; return refreshPromise; }
  refreshPromise = (async () => {
    do { refreshAgain = false; await readStatus(); } while (refreshAgain);
  })();
  try { return await refreshPromise; }
  catch (error) {
    globalThis.KidTEPopupUX?.state('error');
    $("clock").textContent = '—';
    $("current").textContent = 'Chưa xác nhận sản phẩm hiện tại';
    message(error.message || 'Không đọc được trạng thái tiện ích', true);
    throw error;
  } finally { refreshPromise = null; }
}
let lastPosition = null;
function paintBadges(fresh, recent) {
  const UX = globalThis.KidTEPopupUX;
  if (!UX?.videoSourceBadge) return;
  const src = UX.videoSourceBadge(fresh ? lastPosition || {state: 'stale'} : {state: 'stale'});
  const pin = UX.pinBadge(fresh && recent?.confirmed ? recent.at : 0, fresh ? recent?.productTitle || '' : '');
  for (const [id, b] of [['source-badge', src], ['pin-badge', pin]]) { const el = $(id); if (!el) continue; el.textContent = b.text; el.className = b.className; el.dataset.kind = b.kind; }
}
async function readStatus() {
  const data = await chrome.storage.local.get(["shopeeConfig", "shopeeAiRequested", "shopeeStatus", "shopeePaused", "shopeeAiStatus", "shopeeAiLastActivity"]);
  const config = {...data.shopeeConfig};
  if (typeof data.shopeeAiRequested === 'boolean') config.aiEnabled = data.shopeeAiRequested;
  const recent = data.shopeeStatus;
  const fresh = Boolean(recent?.at && Date.now() - recent.at >= 0 && Date.now() - recent.at < 12000);
  globalThis.KidTEPopupUX?.state(!fresh ? 'stale' : recent.ok === false ? 'error' : 'connected');
  message(fresh ? (recent.error || (recent.confirmed ? 'Đã kết nối · UI xác nhận sản phẩm' : recent.label)) : "Chưa có cập nhật · mở trang quản lý Shopee LIVE để nối lại.", fresh && recent?.ok === false);
  $("connection").textContent = !fresh ? 'Mất cập nhật' : data.shopeePaused ? 'Đã tạm dừng' : recent.ok ? 'Đang theo dõi LIVE' : 'Chờ phục hồi';
  $("clock").textContent = fresh && Number.isFinite(recent.positionS) ? `${Math.floor(recent.positionS / 60)}:${String(Math.floor(recent.positionS % 60)).padStart(2,'0')}` : '—';
  $("video-title").textContent = fresh && recent?.videoTitle ? recent.videoTitle : 'Chờ nhận diện video';
  $("source-label").textContent = fresh && recent.ok ? 'OBS Media Source · đang cập nhật' : 'OBS Media Source · chờ nguồn';
  paintBadges(fresh, recent);
  try {
    fetch('http://127.0.0.1:8793/api/video/position?platform=shopee', {cache: 'no-store'})
      .then(r => r.ok ? r.json() : null).then(pos => { if (pos) { lastPosition = pos; paintBadges(fresh, recent); } }).catch(() => {});
  } catch {}
  $("current").textContent = fresh && recent.confirmed ? recent.productTitle || 'Không có sản phẩm đang ghim' : 'Chưa xác nhận sản phẩm hiện tại';
  $("next").textContent = fresh && Number.isFinite(recent.nextAtMs) ? 'Mốc kế · ' + Math.floor(recent.nextAtMs / 60000) + ':' + String(Math.floor(recent.nextAtMs / 1000) % 60).padStart(2,'0') : 'Mốc tiếp theo theo kịch bản Kid TE';
  $("pause").hidden = Boolean(data.shopeePaused); $("resume").hidden = !data.shopeePaused;
  $("ai-start").hidden = Boolean(config.aiEnabled); $("ai-stop").hidden = !config.aiEnabled;
  $("pause").disabled = busy || Boolean(data.shopeePaused);
  $("resume").disabled = busy || !data.shopeePaused;
  $("ai-start").disabled = busy;
  $("ai-stop").disabled = busy;
  $("runtime").textContent = (recent?.productCount ? `Giỏ · ${recent.productCount} sản phẩm\n` : '') + (data.shopeePaused ? "Tự ghim tạm dừng." : "Tự ghim đã bật.") +
    (config.aiEnabled ? "\nDeepSeek đã bật · xem trạng thái trong Điều hành LIVE." : "\nDeepSeek đang tắt.") +
    (data.shopeeAiStatus?.error ? `\nLỗi AI: ${data.shopeeAiStatus.error}` : "");
  const activity = data.shopeeAiLastActivity?.sessionId === config.sessionId && config.sessionId ? data.shopeeAiLastActivity : null;
  const aiFresh = config.sessionId && data.shopeeAiStatus?.sessionId === config.sessionId && data.shopeeAiStatus?.at && Date.now() - data.shopeeAiStatus.at >= 0 && Date.now() - data.shopeeAiStatus.at < 20000;
  $("ai-status").textContent = (config.aiEnabled ? aiFresh ? data.shopeeAiStatus.error || data.shopeeAiStatus.label || "Đang chờ bình luận" : "Đã bật · chờ cập nhật từ trang LIVE" : "Đang tắt · bật AI để trả lời bình luận") + (activity?.at
    ? `\nLần xử lý gần nhất (${new Date(activity.at).toLocaleTimeString("vi-VN")}): ${activity.error || activity.label}` : "");
}
// confirmLabel: nút nguy hiểm — bấm lần 1 chỉ "lên đạn", bấm lại trong 3 giây mới chạy.
const armedTimers = new Map();
function action(id, handler, success, confirmLabel) {
  $(id).addEventListener('click', async () => {
    if (busy) return;
    if (confirmLabel) {
      const button = $(id);
      if (!armedTimers.has(id)) {
        const original = button.textContent;
        button.textContent = confirmLabel;
        globalThis.KidTEPopupUX?.notify(`${confirmLabel} (trong 3 giây)`);
        armedTimers.set(id, setTimeout(() => { armedTimers.delete(id); button.textContent = original; }, 3000));
        return;
      }
      clearTimeout(armedTimers.get(id)); armedTimers.delete(id);
      button.textContent = 'Tắt AI';
    }
    busy = true;
    for (const name of ['pause','resume','ai-start','ai-stop']) $(name).disabled = true;
    try {
      await handler();
      globalThis.KidTEPopupUX?.notify(success);
    } catch (error) {
      message(error.message, true);
      globalThis.KidTEPopupUX?.notify(error.message, true);
    } finally {
      busy = false;
      await refreshStatus().catch(() => {});
    }
  });
}
let statusPoll;
statusPoll = setInterval?.(() => refreshStatus().catch(() => {}), 3000);
action("pause", async () => {
  await chrome.storage.local.set({shopeePaused: true});
  await refreshStatus();
}, 'Đã tạm dừng tự ghim');
action("resume", async () => {
  await chrome.storage.local.set({shopeePaused: false});
  await refreshStatus();
}, 'Đã tiếp tục tự ghim');
action("ai-start", async () => {
    const {shopeeConfig: config = {}} = await chrome.storage.local.get("shopeeConfig");
    const tab = await KidTEShopeeCore.findLiveTab(chrome.tabs, config.tabId);
    const blocked = await KidTEShopeeCore.aiStartCheck({config, tabId: tab.id, sessionId: KidTEShopeeCore.liveSession(tab.url),
      aiStatus: () => chrome.runtime.sendMessage({type: "KIDTE_SHOPEE_BACKEND", resource: "ai_status"}),
      latestConfig: async () => (await chrome.storage.local.get("shopeeConfig")).shopeeConfig});
    if (blocked) throw new Error(blocked);
    await chrome.storage.local.set({shopeeAiRequested: true, shopeeAiChangedAtMs: Date.now()});
    await refreshStatus();
}, 'Đã bật AI · đang chờ bình luận');
action("ai-stop", async () => {
  await chrome.storage.local.set({shopeeAiRequested: false, shopeeAiChangedAtMs: Date.now()});
  await refreshStatus();
}, 'Đã tắt trả lời tự động', 'Bấm lại để tắt AI');
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && ['shopeeConfig','shopeeAiRequested','shopeeStatus','shopeePaused','shopeeAiStatus','shopeeAiLastActivity'].some(key => key in changes)) refreshStatus().catch(() => {});
});
refreshStatus().catch(error => message(error.message, true));
setInterval(() => refreshStatus().catch(() => {}), 1000);
