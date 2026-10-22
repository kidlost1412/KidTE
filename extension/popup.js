"use strict";

const SCHEDULE = globalThis.KidTESchedule;
let state = {
  running: false,
  startedAtMs: 0,
  pausedElapsedMs: 0,
  revision: 0,
  sessionId: ""
};
let busy = false;
let jumpMessage = "Nhập phút:giây, ví dụ 07:35 hoặc 27:15.";
let jumpIsError = false;
let aiEnabled = false;
let pageFresh = false;
let pageOK = false;
let observedProduct = '';

const $ = (selector) => document.querySelector(selector);

function elapsedMs(now = Date.now()) {
  if (state.running && state.startedAtMs) return Math.max(0, now - Number(state.startedAtMs));
  return Math.max(0, Number(state.pausedElapsedMs) || 0);
}

// Lịch ghim đang áp dụng = lịch của video mà trang TikTok đang dùng (bộ nghe nhận
// ra). Chưa có thì dùng lịch mặc định trong schedule.js.
let activeTitle = "";
async function loadActiveTimeline() {
  const stored = await chrome.storage.local.get(["liveReTimelines", "liveReActiveVideoId"]);
  const timeline = stored.liveReActiveVideoId && stored.liveReTimelines?.[stored.liveReActiveVideoId];
  if (timeline && SCHEDULE.load(timeline).ok) activeTitle = SCHEDULE.VIDEO_TITLE || stored.liveReActiveVideoId;
  render();
}

function render() {
  const resolved = SCHEDULE.resolve(elapsedMs());
  const next = SCHEDULE.nextBoundary(elapsedMs());
  $("#dot").classList.toggle("running", Boolean(state.running));
  $("#mode").textContent = state.running
    ? `Đang chạy · vòng ${resolved.loopIndex + 1}`
    : (state.startedAtMs ? "Đang tạm dừng" : "Đang dừng");
  $("#clock").textContent = pageFresh ? SCHEDULE.formatClock(resolved.positionMs) : '—';
  $("#current").textContent = !pageFresh ? 'Chưa xác nhận sản phẩm hiện tại' : observedProduct || (resolved.product ? 'Đang xác nhận sản phẩm đang ghim' : 'Không có sản phẩm đang ghim');
  $("#next").textContent = state.running || state.startedAtMs
    ? `Mốc kế: ${SCHEDULE.formatClock(next.atPositionMs)} · ${next.product?.shortName || "Bỏ ghim"}`
    : `Mốc đầu: 00:00 · ${SCHEDULE.resolve(0).product?.shortName || "Bỏ ghim"}`;
  const cycle = Number(SCHEDULE.CYCLE_MS);
  const repin = Number(SCHEDULE.REPIN_EVERY_MS);
  $("#timeline-info").textContent = Number.isFinite(cycle)
    ? `Lịch ghim: ${activeTitle || "mặc định"} · vòng ${SCHEDULE.formatClock(cycle)} · làm mới ghim mỗi ${Math.round(repin / 1000)} giây`
    : "Chưa đọc được lịch ghim đang áp dụng";
  $("#pause").textContent = state.running ? "Tạm dừng" : "Tiếp tục";
  $("#jump-error").textContent = jumpMessage;
  $("#jump-error").className = jumpIsError ? "bad" : "ok";
  $("#pause").disabled = busy || (!state.running && !state.startedAtMs);
  for (const button of document.querySelectorAll("button")) {
    if (button.id !== "pause" && button.id !== "ai-toggle" && button.getAttribute('role') !== 'tab') button.disabled = busy;
  }
  const aiButton = $("#ai-toggle");
  aiButton.setAttribute?.('aria-pressed', String(aiEnabled));
  aiButton.textContent = aiEnabled ? "TẮT AI" : "BẬT AI";
  aiButton.classList.toggle("on", aiEnabled);
  aiButton.disabled = busy;
}

async function readCollectorStatus(snapshot) {
  const stored = snapshot || await chrome.storage.local.get("liveReCollector");
  const box = document.querySelector("#collect-status");
  if (!box) return;
  const collector = stored.liveReCollector;
  const stale = !collector || Date.now() - Number(collector.updatedAtMs || 0) > 20000;
  box.textContent = collector && collector.ok && !stale
    ? `Bộ thu dữ liệu: đã ghi ${collector.rows} bình luận` +
      (collector.queued ? ` · chờ gửi ${collector.queued}` : "") +
      (collector.retried ? ` · gửi lại ${collector.retried}` : "")
    : `Bộ thu dữ liệu chưa chạy: ${collector?.error || "mở tab TikTok LIVE Manager để tiện ích tự cài cầu thu dữ liệu"}`;
  box.className = collector && collector.ok && !stale ? "ok" : "bad";
}

let backendPing = {at: 0, ok: true};
async function backendAlive() {
  if (Date.now() - backendPing.at < 4000) return backendPing.ok;
  let ok = false;
  try {
    const reply = await Promise.race([
      chrome.runtime.sendMessage({type: "LIVE_RE_AI_HEALTH"}),
      new Promise(resolve => setTimeout(() => resolve(null), 2500))
    ]);
    ok = Boolean(reply) && reply.ok !== false;
  } catch { ok = false; }
  backendPing = {at: Date.now(), ok};
  return ok;
}

async function readAiStatus(snapshot) {
  const stored = snapshot || await chrome.storage.local.get(["liveReAiEnabled", "liveReAiStatus", "liveRePageStatus"]);
  aiEnabled = stored.liveReAiEnabled === true;
  const status = stored.liveReAiStatus;
  const box = $("#ai-status");
  const fresh = status && Date.now() - Number(status.updatedAtMs || 0) < 20000;
  let message = 'Đang tắt · bật AI để theo dõi bình luận';
  if (aiEnabled && fresh) message = status.message;
  else if (aiEnabled) {
    // Không có cập nhật mới: nói rõ thiếu tab TikTok hay backend chưa chạy.
    const page = stored.liveRePageStatus;
    const tabSeen = page && Date.now() - Number(page.updatedAtMs || 0) <= 5000;
    if (!tabSeen) message = "Chưa thấy tab TikTok LIVE — mở hoặc F5 tab";
    else message = (await backendAlive()) ? "Đã thấy tab TikTok LIVE · đang chờ bình luận đầu tiên…" : "KidTE.exe chưa chạy";
  }
  box.textContent = message;
  box.classList.toggle("bad", Boolean(aiEnabled && (fresh ? !status.ok : /chưa chạy|Chưa thấy tab/.test(message))));
  render();
}

// Bộ đếm request TikTok/phút và trạng thái tạm dừng do TikTok đòi xác minh.
function readApiGuard(stored) {
  const box = $("#api-guard");
  if (!box) return;
  const guard = stored?.liveReApiGuard;
  const fresh = guard && Date.now() - Number(guard.updatedAtMs || 0) < 120000;
  const pausedMs = fresh ? Number(guard.pausedUntil || 0) - Date.now() : 0;
  const otherBrowser = fresh ? String(guard.otherBrowser || "") : "";
  box.classList.toggle("bad", pausedMs > 0 || Boolean(otherBrowser));
  if (otherBrowser) {
    box.textContent = otherBrowser;
  } else if (pausedMs > 0) {
    box.textContent = `TikTok đang yêu cầu xác minh – tạm dừng gọi tự động (còn ${Math.ceil(pausedMs / 1000)} giây)`;
  } else {
    box.textContent = `Gọi TikTok: ${fresh ? Number(guard.perMinute || 0) : 0} request/phút`;
  }
}

async function readPageStatus(snapshot) {
  const stored = snapshot || await chrome.storage.local.get("liveRePageStatus");
  const page = stored.liveRePageStatus;
  const box = $("#page-status");
  box.className = "";
  if (!page || Date.now() - Number(page.updatedAtMs || 0) > 5000) {
    pageFresh = false;
    pageOK = false;
    globalThis.KidTEPopupUX?.state('stale');
    box.textContent = "Chưa thấy trang TikTok LIVE Manager đang kết nối.";
    box.classList.add("bad");
    $("#source-label").textContent = 'Mất cập nhật · chưa xác nhận nguồn TikTok';
    setBadges({state: 'stale'}, 0, '');
    $("#video-title").textContent = activeTitle || 'Chờ nhận diện video';
    return;
  }
  pageFresh = true;
  observedProduct = Date.now() - Number(page.pinObservedAtMs || 0) < 15000 ? page.pinnedProductTitle || '' : '';
  pageOK = Boolean(page.ok);
  globalThis.KidTEPopupUX?.state(pageOK ? 'connected' : 'error');
  box.textContent = page.message || "Trang TikTok đã kết nối; đang chờ bắt đầu.";
  box.classList.add(page.ok ? "ok" : "bad");
  $("#video-title").textContent = activeTitle || (page.videoSync?.videoId === SCHEDULE.VIDEO_ID ? SCHEDULE.VIDEO_TITLE : '') || 'Chờ nhận diện video';
  $("#source-label").textContent = page.videoSync?.state === 'locked'
    ? 'Đã nhận diện · nguồn riêng TikTok Studio'
    : 'Chờ nguồn riêng TikTok · ' + (page.videoSync?.state || 'chưa có tín hiệu');
  setBadges(page.videoSync || {state: 'stale'}, page.pinObservedAtMs, page.pinnedProductTitle || '');
  fetchPosition('tiktok').then(pos => { if (pos) setBadges(pos, page.pinObservedAtMs, page.pinnedProductTitle || ''); });
}

function setBadges(pos, pinAt, pinTitle) {
  const UX = globalThis.KidTEPopupUX;
  if (!UX?.videoSourceBadge) return;
  for (const [id, b] of [['#source-badge', UX.videoSourceBadge(pos)], ['#pin-badge', UX.pinBadge(pinAt, pinTitle)]]) {
    const el = $(id); if (!el) continue;
    el.textContent = b.text; el.className = b.className; el.dataset.kind = b.kind;
  }
}

async function fetchPosition(platform) {
  try {
    const r = await fetch('http://127.0.0.1:8793/api/video/position?platform=' + platform, {cache: 'no-store'});
    return r.ok ? await r.json() : null;
  } catch { return null; }
}

async function command(type, extra = {}) {
  if (busy) return false;
  busy = true;
  let succeeded = false;
  render();
  try {
    const response = await chrome.runtime.sendMessage({ type, ...extra });
    if (!response?.ok) throw new Error(response?.error || "Không có phản hồi");
    state = { ...state, ...response.state };
    succeeded = true;
  } catch (error) {
    const box = $("#page-status");
    box.textContent = `Lỗi tiện ích: ${error.message}`;
    box.className = "bad";
    globalThis.KidTEPopupUX?.notify(`Thao tác chưa hoàn tất: ${error.message}`, true);
  } finally {
    busy = false;
    render();
    await readPageStatus().catch(() => {});
  }
  return succeeded;
}

// Hai nút này làm lệch cả phiên LIVE nếu bấm nhầm: bấm lần 1 chỉ "lên đạn", bấm lại trong 3 giây mới chạy.
function confirmTwice(button, armedLabel, run) {
  const original = button.textContent;
  let timer = 0;
  const disarm = () => { clearTimeout(timer); timer = 0; button.textContent = original; delete button.dataset.armed; };
  button.addEventListener("click", () => {
    if (timer) { disarm(); run(); return; }
    button.textContent = armedLabel;
    button.dataset.armed = "1";
    globalThis.KidTEPopupUX?.notify(`${armedLabel} (trong 3 giây)`);
    timer = setTimeout(disarm, 3000);
  });
}
confirmTwice($("#start"), "Bấm lại để đặt về 00:00", () => command("LIVE_RE_START"));
confirmTwice($("#stop"), "Bấm lại để dừng và bỏ ghim", () => command("LIVE_RE_STOP"));
$("#adjust-back").addEventListener("click", () => command("LIVE_RE_ADJUST", { deltaMs: -1000 }));
$("#adjust-forward").addEventListener("click", () => command("LIVE_RE_ADJUST", { deltaMs: 1000 }));
$("#pause").addEventListener("click", async () => {
  // Mốc người dùng bấm: lệnh app phát ra TRƯỚC mốc này không được ghi đè (comments.js).
  await chrome.storage.local.set({ liveRePinChangedAtMs: Date.now() }).catch(() => {});
  return command(state.running ? "LIVE_RE_PAUSE" : "LIVE_RE_RESUME");
});
$("#ai-toggle").addEventListener("click", async () => {
  if (busy) return;
  busy = true;
  render();
  try {
    const next = !aiEnabled;
    await chrome.storage.local.set({ liveReAiEnabled: next, liveReAiChangedAtMs: Date.now() });
    aiEnabled = next;
    globalThis.KidTEPopupUX?.notify(next ? 'Đã bật AI · đang chờ bình luận' : 'Đã tắt trả lời tự động');
  } catch (error) { globalThis.KidTEPopupUX?.notify(error.message, true); }
  finally { busy = false; render(); }
  setTimeout(() => readAiStatus().catch(() => {}), 350);
});

// Công tắc nhật ký chẩn đoán captcha (mặc định BẬT khi chưa từng đặt).
chrome.storage.local.get("liveReDiagEnabled").then((stored) => {
  $("#diag-log").checked = stored?.liveReDiagEnabled !== false;
}).catch(() => {});
$("#diag-log").addEventListener("change", (event) => {
  Promise.resolve(chrome.storage.local.set({ liveReDiagEnabled: event.target.checked })).catch((error) => console.warn("Kid TE diag", error));
});

function defaultCartName(now = new Date()) {
  const two = (n) => String(n).padStart(2, "0");
  return `Giỏ ${two(now.getHours())}:${two(now.getMinutes())} ${two(now.getDate())}/${two(now.getMonth() + 1)}`;
}

// Danh sách giỏ cho "Lưu vào" + dòng "Buổi LIVE đang dùng giỏ «…»".
// Đang LIVE mà AI dùng giỏ của tài khoản khác: cảnh báo nổi bật, không tự đổi giỏ.
function liveMismatchText(response) {
  if (!response?.live_mismatch) return "";
  const live = (response.carts || []).find((c) => c.key === response.live) || {};
  const why = response.active_source === "video" ? " (theo video trong Studio)" : "";
  return `Đang LIVE ${response.live_name || live.name || response.live} nhưng AI đang dùng giỏ «${response.active_name || response.active}»${why}`;
}

// Dòng "Buổi LIVE đang dùng giỏ «…»": nói rõ khi giỏ lấy theo video đang phát trong Studio.
function activeCartNote(response) {
  if (!response?.active_name) return "";
  const why = response.active_source === "video" ? " (theo video trong Studio)" : "";
  return `Buổi LIVE đang dùng giỏ «${response.active_name}»${why}`;
}

function renderCartWarning(response) {
  const box = $("#live-cart-warn");
  if (!box) return;
  const text = liveMismatchText(response);
  box.hidden = !text;
  $("#live-cart-warn-text").textContent = text;
  // Giỏ theo video Studio: nút "Dùng giỏ của tài khoản LIVE" không đổi được -> ẩn (đổi giỏ trong Studio).
  const button = $("#use-live-cart");
  if (button) button.hidden = response?.active_source === "video";
}

$("#use-live-cart")?.addEventListener("click", async () => {
  const response = await chrome.runtime.sendMessage({ type: "LIVE_RE_LIST_CARTS", useLive: true }).catch(() => null);
  if (response?.ok) await loadCartChoices().catch(() => {});
});

async function loadCartChoices() {
  const nameBox = $("#cart-name");
  // Để trống = Kid TE đặt tên theo tài khoản TikTok (nếu biết), không thì "Giỏ HH:mm dd/mm".
  if (nameBox) nameBox.placeholder = `@tài khoản hoặc ${defaultCartName()}`;
  const select = $("#cart-target");
  if (!select) return;
  const response = await chrome.runtime.sendMessage({ type: "LIVE_RE_LIST_CARTS" }).catch(() => null);
  if (!response?.ok) return;
  renderCartWarning(response);
  const chosen = select.value;
  select.textContent = "";
  const add = (value, label) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.appendChild(option);
  };
  add("new", "Giỏ mới");
  for (const cart of response.carts || []) {
    add(String(cart.key), `Ghi đè «${cart.name}» (${Number(cart.product_count || 0)} SP)`);
  }
  select.value = [...select.options].some((o) => o.value === chosen) ? chosen : "new";
  const note = $("#live-cart-note");
  if (note) note.textContent = activeCartNote(response);
}
loadCartChoices().catch(() => {});

async function scanCatalog(options) {
  const response = await chrome.runtime.sendMessage({ type: "LIVE_RE_SCAN_CATALOG", ...options });
  if (response?.needs_confirm && !options.confirmed) {
    const name = String(response.cart_name || "");
    const ok = globalThis.confirm(
      `Ghi đè giỏ «${name}»?
Hiện có ${Number(response.old_count || 0)} sản phẩm → thay bằng ${Number(response.new_count || 0)} sản phẩm vừa quét.
Bản cũ được giữ trong lịch sử (trang Sản phẩm → "Khôi phục bản trước").`);
    if (!ok) return { ok: false, cancelled: true };
    return scanCatalog({ ...options, confirmed: true });
  }
  return response;
}

$("#scan-catalog").addEventListener("click", async () => {
  if (busy) return;
  busy = true;
  render();
  const box = $("#catalog-status");
  box.className = "";
  box.textContent = "Đang đọc Product ID và thứ tự giỏ từ TikTok…";
  try {
    const cartTarget = String($("#cart-target")?.value || "new");
    const cartName = String($("#cart-name")?.value || "").trim();
    const response = await scanCatalog({ cartTarget, cartName, confirmed: false });
    if (response?.cancelled) {
      box.textContent = "Đã huỷ, giỏ cũ giữ nguyên.";
      return;
    }
    if (!response?.ok) throw new Error(response?.error || "Không quét được giỏ");
    const saved = Number(response.product_count || 0);
    const fromTikTok = Number(response.tiktok_count ?? response.received_count ?? saved);
    if (saved <= 0) throw new Error("Kid TE chưa lưu được sản phẩm nào");
    const savedName = String(response.cart_name || "").trim();
    box.textContent = `Đã lưu ${saved} sản phẩm vào giỏ «${savedName}».` +
      (fromTikTok > saved ? ` (TikTok trả ${fromTikTok}, ${fromTikTok - saved} bị loại khỏi AI.)` : "") +
      (response.active_cart_name ? ` Buổi LIVE đang dùng giỏ «${response.active_cart_name}».` : "");
    box.className = "ok";
    $("#cart-name").value = "";
    loadCartChoices().catch(() => {});
  } catch (error) {
    box.textContent = `Chưa quét được: ${error.message}`;
    box.className = "bad";
  } finally {
    busy = false;
    render();
  }
});

async function jumpToEnteredTime() {
  const entered = $("#jump-time").value;
  const requestedElapsedMs = SCHEDULE.parseClock(entered);
  if (requestedElapsedMs === null) {
    jumpMessage = "Mốc không hợp lệ. Hãy nhập dạng phút:giây, ví dụ 07:35.";
    jumpIsError = true;
    render();
    $("#jump-time").focus();
    return;
  }
  jumpMessage = `Đang đồng bộ từ ${SCHEDULE.formatClock(requestedElapsedMs)}…`;
  jumpIsError = false;
  const succeeded = await command("LIVE_RE_START_AT", { elapsedMs: requestedElapsedMs });
  if (succeeded) {
    jumpMessage = `Đã đồng bộ từ ${SCHEDULE.formatClock(requestedElapsedMs)}; LIVE không cần khởi động lại.`;
    render();
  }
}

$("#jump").addEventListener("click", jumpToEnteredTime);
$("#jump-time").addEventListener("keydown", (event) => {
  if (event.key === "Enter") jumpToEnteredTime();
});

let refreshPromise = null, refreshAgain = false;
async function refreshSnapshot() {
  if (refreshPromise) { refreshAgain = true; return refreshPromise; }
  refreshPromise = (async () => {
    do {
      refreshAgain = false;
      const stored = await chrome.storage.local.get(['liveReState','liveRePageStatus','liveReAiEnabled','liveReAiStatus','liveReCollector','liveReApiGuard']);
      readApiGuard(stored);
      if (stored.liveReState) state = {...state,...stored.liveReState};
      await readPageStatus(stored);
      await readCollectorStatus(stored);
      await readAiStatus(stored);
      render();
    } while (refreshAgain);
  })();
  try { return await refreshPromise; }
  catch (error) {
    pageFresh = false; observedProduct = '';
    globalThis.KidTEPopupUX?.state('error');
    $('#page-status').textContent = error.message || 'Không đọc được trạng thái tiện ích';
    $('#page-status').className = 'bad';
    render();
  } finally { refreshPromise = null; }
}
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && ['liveReState','liveRePageStatus','liveReAiEnabled','liveReAiStatus','liveReCollector','liveReApiGuard'].some(key => key in changes)) refreshSnapshot();
});

chrome.runtime.sendMessage({ type: "LIVE_RE_GET_STATE" })
  .then((response) => {
    if (response?.state) state = { ...state, ...response.state };
    render();
  })
  .catch(() => render());

render();
refreshSnapshot();
setInterval(() => { render(); refreshSnapshot(); }, 1000);
loadActiveTimeline().catch(error => globalThis.KidTEPopupUX?.notify(error.message, true));
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && (changes.liveReActiveVideoId || changes.liveReTimelines)) loadActiveTimeline().catch(error => globalThis.KidTEPopupUX?.notify(error.message, true));
});
