"use strict";
// 3.11.11 — kịch bản chủ shop 20/10: MỘT máy, HAI trình duyệt (2 profile), mỗi bên
// đăng nhập một tài khoản TikTok khác nhau. A đang LIVE tài khoản 1, B chưa LIVE
// (tài khoản 2) quét giỏ trước LIVE. Mỗi profile = một bản background.js riêng
// (vm context riêng, chrome.storage riêng); backend lease giả lập theo đúng luật
// backend/src/tiktok_lease.py. Không bao giờ chạm cổng 8793 thật.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const DASH = "https://shop.tiktok.com/streamer/live/product/dashboard";
const SOURCE = fs.readFileSync(path.join(__dirname, "background.js"), "utf8");
let clock = 1_000_000;

// ---- backend lease giả (cùng luật tiktok_lease.py) ----
const TTL = 30000;
const leases = new Map(); // owner_key -> {holder:[profile,tab], expires, live}
const leaseLog = [];
function leaseHandle(body) {
  leaseLog.push(body);
  const me = `${body.profile_id}|${body.tab_id}`;
  for (const [key, value] of leases) if (clock >= value.expires) leases.delete(key);
  if (body.action === "release") {
    let released = false;
    for (const [key, value] of leases) if (value.holder === me) { leases.delete(key); released = true; }
    return { ok: true, granted: false, released };
  }
  const key = body.owner_key || "";
  const current = leases.get(key);
  if (!current || current.holder.split("|")[0] === body.profile_id || (body.live === true && !current.live)) {
    for (const [other, value] of leases) if (value.holder === me && other !== key) leases.delete(other);
    leases.set(key, { holder: me, expires: clock + TTL, live: body.live === true });
    return { ok: true, granted: true, ttl_ms: TTL };
  }
  return { ok: true, granted: false, retry_after_ms: current.expires - clock };
}
const catalogPosts = [];

function makeProfile(name) {
  const storage = {};
  const tabs = new Map();
  const injected = [];
  const tabMessages = [];
  const listeners = { message: null, removed: [], updated: [], windowRemoved: [] };
  // Phản hồi TikTok của từng tab (MAIN world) do test điều khiển.
  const tiktok = new Map(); // tabId -> {room_id, host}
  const chrome = {
    storage: { local: {
      get: async (keys) => {
        if (!keys) return { ...storage };
        const wanted = Array.isArray(keys) ? keys : [keys];
        return Object.fromEntries(wanted.map((key) => [key, storage[key]]));
      },
      set: async (value) => Object.assign(storage, value)
    }, onChanged: { addListener() {} } },
    tabs: {
      query: async () => [...tabs.values()],
      get: async (id) => { if (!tabs.has(id)) throw new Error("No tab"); return tabs.get(id); },
      sendMessage: async (tabId, message) => { tabMessages.push({ tabId, message }); return {}; },
      onRemoved: { addListener(cb) { listeners.removed.push(cb); } },
      onUpdated: { addListener(cb) { listeners.updated.push(cb); } }
    },
    windows: { onRemoved: { addListener(cb) { listeners.windowRemoved.push(cb); } } },
    action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
    scripting: { executeScript: async ({ target, args }) => {
      injected.push({ tabId: target.tabId, request: args?.[0] });
      const page = tiktok.get(target.tabId) || {};
      if (args?.[0]?.action === "snapshot") {
        return [{ result: { ok: true, api_calls: 1, snapshot: {
          room_id: page.room_id || "", phase: page.room_id ? "" : "prelive", host: page.host || null, products: [{ product_id: "p1" }]
        } } }];
      }
      return [{ result: { ok: true, api_calls: 1, room_id: page.room_id || "" } }];
    } },
    runtime: {
      id: "ext",
      onInstalled: { addListener() {} },
      onMessage: { addListener(cb) { listeners.message = cb; } }
    }
  };
  const fetch = async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : {};
    if (String(url).endsWith("/api/tiktok/lease")) {
      const result = leaseHandle(body);
      return { ok: true, json: async () => result };
    }
    if (String(url).endsWith("/api/catalog/sync")) {
      catalogPosts.push({ profile: name, body });
      return { ok: true, json: async () => ({ ok: true }) };
    }
    return { ok: true, json: async () => ({ ok: true }) };
  };
  const context = vm.createContext({
    chrome, fetch, console: { ...console, warn() {}, debug() {} },
    Date: class extends Date { static now() { return clock; } },
    setTimeout, clearTimeout, setInterval, clearInterval, Promise, URL, AbortSignal,
    crypto: { randomUUID: () => `profile-${name}-0001` },
    window: { webpackChunkecom_streamer: [] }
  });
  context.globalThis = context;
  vm.runInContext(SOURCE, context, { filename: `background-${name}.js` });
  const send = (message, tab) => new Promise((resolve) => listeners.message(message, { tab, id: "ext" }, resolve));
  return {
    name, storage, tabs, tiktok, injected, tabMessages,
    open(tabId, page) { const tab = { id: tabId, url: DASH }; tabs.set(tabId, tab); tiktok.set(tabId, page); return tab; },
    close(tabId) { tabs.delete(tabId); listeners.removed.forEach((cb) => cb(tabId, { isWindowClosing: true })); },
    closeWindow(tabId) { tabs.delete(tabId); listeners.windowRemoved.forEach((cb) => cb(1)); },
    api(tabId, request = { action: "snapshot" }) {
      return send({ type: "LIVE_RE_TIKTOK_API", request }, tabs.get(tabId) || { id: tabId, url: DASH });
    },
    sync(tabId, payload) { return send({ type: "LIVE_RE_CATALOG_SYNC", payload }, tabs.get(tabId)); }
  };
}
const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
const ACC1 = { user_id: "1001", unique_id: "shop_mot" };
const ACC2 = { user_id: "2002", unique_id: "shop_hai" };

(async () => {
  const A = makeProfile("A");
  const B = makeProfile("B");

  // A LIVE tài khoản 1: học tài khoản ở snapshot đầu, rồi giữ lease khoá uid:1001.
  A.open(7, { room_id: "7300000000000000001", host: ACC1 });
  assert.equal((await A.api(7)).ok, true);
  assert.equal((await A.api(7)).ok, true);
  assert.equal(leases.get("uid:1001")?.holder.startsWith("profile-A"), true, "A holds account 1");

  // B (tài khoản 2, chưa LIVE) đứng ở dashboard: probe snapshot + quét tay đều được,
  // không đụng lease của tài khoản 1.
  B.open(9, { room_id: "", host: ACC2 });
  assert.equal((await B.api(9)).ok, true, "B prelive probe allowed");
  assert.equal((await B.api(9, { action: "snapshot", userInitiated: true })).ok, true, "B manual scan allowed");
  assert.equal((await B.api(9)).ok, true, "B keyed on its own account");
  assert.equal(leases.get("uid:2002")?.holder.startsWith("profile-B"), true);
  assert.equal(leases.get("uid:1001")?.holder.startsWith("profile-A"), true, "A not displaced by B");
  clock += 6000;
  assert.equal((await A.api(7)).ok, true, "A keeps calling while B is open");
  // Payload quét giỏ mang profile id để backend không đổ giỏ B vào giỏ LIVE của A.
  await B.sync(9, { live_context: { room_id: "", products: [] }, user_initiated: true });
  assert.equal(catalogPosts.at(-1).body.live_context.profile_id, "profile-B-0001");

  // B không có host (TikTok không trả chủ phòng trước LIVE): snapshot vẫn đọc được,
  // không giành khoá chung -> không chặn A.
  const C = makeProfile("C"); // trình duyệt thứ ba, cũng chưa LIVE, không có host
  C.open(10, { room_id: "", host: null });
  assert.equal((await C.api(10)).ok, true);
  clock += 6000;
  assert.equal((await C.api(10)).ok, true);
  assert.equal(leases.has(""), false, "unknown-account prelive tab never holds the shared key");

  // A đóng tab (trình duyệt A tắt): lease tài khoản 1 trả ngay; B không giữ khoá đó.
  A.close(7);
  await tick();
  assert.equal(leases.has("uid:1001"), false, "A released on close");
  // B đóng cả cửa sổ: lease được trả nhờ windows.onRemoved.
  B.closeWindow(9);
  await tick();
  assert.equal([...leases.values()].some((v) => v.holder.startsWith("profile-B")), false,
    "B released when its window closed");

  // A mở lại tab LIVE: nhận ngay (không cần F5), không có giãn cách nào.
  A.open(11, { room_id: "7300000000000000001", host: ACC1 });
  const firstCall = clock;
  assert.equal((await A.api(11)).ok, true);
  assert.equal((await A.api(11)).ok, true);
  assert.equal(clock - firstCall <= 10000, true);
  assert.equal(leases.get("uid:1001")?.holder, "profile-A-0001|11", "reopened A owns lease immediately");

  // CÙNG tài khoản ở 2 trình duyệt: chỉ một bên. B (tài khoản 1, chưa LIVE) mở dashboard
  // khi A đang LIVE -> bị chặn, và chặn trả blocked (không phải lỗi TikTok).
  B.open(12, { room_id: "", host: ACC1 });
  assert.equal((await B.api(12)).ok, true, "first read learns the account");
  clock += 6000;
  const sameDenied = await B.api(12);
  assert.equal(sameDenied.ok, false);
  assert.equal(sameDenied.blocked, true);
  assert.equal(sameDenied.other_browser, true);
  const injectedBefore = B.injected.length;
  clock += 6000;
  await B.api(12);
  assert.equal(B.injected.length, injectedBefore, "blocked tab never calls TikTok");

  // A crash (không kịp release), B (chưa LIVE) chiếm lease tài khoản 1 sau khi hết hạn;
  // A mở lại đang LIVE -> giành lại NGAY, B chưa LIVE phải nhường.
  A.tabs.delete(11); // không gọi onRemoved: mô phỏng trình duyệt bị kill
  clock += TTL + 1000;
  assert.equal((await B.api(12)).ok, true, "B takes the expired lease");
  assert.equal(leases.get("uid:1001")?.holder.startsWith("profile-B"), true);
  A.open(13, { room_id: "7300000000000000001", host: ACC1 });
  assert.equal((await A.api(13)).ok, true);
  assert.equal((await A.api(13)).ok, true, "live tab preempts non-live holder");
  assert.equal(leases.get("uid:1001")?.holder, "profile-A-0001|13");
  clock += 6000;
  assert.equal((await B.api(12)).blocked, true, "non-live B yields to live A");

  // Hai tab LIVE cùng tài khoản: tab sau không giành được của tab LIVE trước.
  B.tiktok.set(12, { room_id: "7300000000000000001", host: ACC1 });
  clock += 6000;
  // B chỉ biết mình LIVE sau một lượt đọc thành công; lượt đọc bị chặn nên vẫn chặn.
  assert.equal((await B.api(12)).blocked, true, "only one browser per account");

  // Lease được cấp lại sau khi bị chặn: background báo tab bỏ giãn cách.
  A.close(13);
  await tick();
  clock += 6000;
  B.tabMessages.length = 0;
  assert.equal((await B.api(12)).ok, true);
  await tick();
  assert.equal(B.tabMessages.some((m) => m.tabId === 12 && m.message.type === "LIVE_RE_LEASE_GRANTED"), true);

  console.log("LIVE_RE_TWO_BROWSERS_TEST_OK");
})().catch((error) => { console.error(error); process.exitCode = 1; });
