"use strict";
// Chỉ MỘT tab LIVE được gọi TikTok; TikTok đòi xác minh thì tạm dừng gọi tự động.
const assert = require("node:assert/strict");

const storage = {};
const DASH = "https://shop.tiktok.com/streamer/live/product/dashboard";
const tabs = new Map([
  [7, { id: 7, url: DASH }],
  [8, { id: 8, url: DASH }],
  [9, { id: 9, url: "https://shop.tiktok.com/workbench/orders" }]
]);
let messageListener = null;
const injected = [];
let nextResult = { ok: true, snapshot: { room_id: "room-1", products: [] }, api_calls: 1 };

global.window = { webpackChunkecom_streamer: [] };
global.chrome = {
  storage: { local: {
    get: async (keys) => {
      if (!keys) return { ...storage };
      const wanted = Array.isArray(keys) ? keys : [keys];
      return Object.fromEntries(wanted.map((key) => [key, storage[key]]));
    },
    set: async (value) => Object.assign(storage, value)
  } },
  tabs: {
    query: async () => [...tabs.values()],
    get: async (id) => { if (!tabs.has(id)) throw new Error("No tab"); return tabs.get(id); },
    sendMessage: async () => ({})
  },
  action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
  scripting: { executeScript: async ({ target }) => { injected.push(target.tabId); return [{ result: { ...nextResult } }]; } },
  runtime: {
    onInstalled: { addListener() {} },
    onMessage: { addListener(callback) { messageListener = callback; } }
  }
};
require("./background.js");

const api = (tabId, request = { action: "snapshot" }) => new Promise((resolve) => messageListener(
  { type: "LIVE_RE_TIKTOK_API", request },
  { tab: { id: tabId, url: tabs.get(tabId)?.url || DASH } }, resolve));

(async () => {
  // Hai tab LIVE dashboard gọi cùng lúc: đúng một tab thắng và giữ vai ổn định.
  const both = await Promise.all([api(7), api(8)]);
  assert.equal(both.filter((item) => item.ok).length, 1, "only one tab may call TikTok");
  const owner = both[0].ok ? 7 : 8;
  const other = owner === 7 ? 8 : 7;
  assert.equal((await api(other)).not_owner, true);
  assert.equal((await api(owner)).ok, true, "owner stays stable");
  assert.deepEqual([...new Set(injected)], [owner], "refused tab never reaches TikTok");

  // Trang không phải LIVE dashboard: luôn bị từ chối, kể cả thao tác tay.
  assert.equal((await api(9)).not_owner, true);
  assert.equal((await api(9, { action: "snapshot", userInitiated: true })).not_owner, true);

  // Thao tác tay (quét giỏ từ popup) từ tab dashboard khác vẫn chạy.
  assert.equal((await api(other, { action: "snapshot", userInitiated: true })).ok, true);

  // Tab điều khiển luôn được ưu tiên.
  storage.liveReState = { running: true, controllerTabId: other };
  assert.equal((await api(other)).ok, true);
  assert.equal((await api(owner)).not_owner, true, "controller tab wins");

  // Tab điều khiển đóng: tab còn lại nhận vai.
  tabs.delete(other);
  assert.equal((await api(owner)).ok, true, "survivor takes over");
  storage.liveReState = { running: true, controllerTabId: 0 };
  tabs.set(other, { id: other, url: DASH });
  assert.equal((await api(other)).not_owner, true, "returning tab does not steal the role");

  // Tab giữ vai chuyển sang trang khác: tab còn lại nhận vai.
  tabs.set(owner, { id: owner, url: "https://shop.tiktok.com/workbench/orders" });
  assert.equal((await api(other)).ok, true, "tab that left LIVE page loses the role");
  tabs.set(owner, { id: owner, url: DASH });

  // TikTok đòi xác minh: tạm dừng gọi tự động >= 2 phút, không chạm TikTok nữa.
  nextResult = { ok: false, error: "Please verify captcha", api_calls: 1 };
  const verify = await api(other);
  assert.equal(verify.ok, false);
  assert.equal(verify.paused, true);
  assert.ok(verify.paused_until_ms - Date.now() >= 119000);
  nextResult = { ok: true, snapshot: { room_id: "room-1" }, api_calls: 1 };
  const before = injected.length;
  const blocked = await api(other, { action: "pin", productId: "p1" });
  assert.equal(blocked.paused, true);
  assert.match(blocked.error, /xác minh – tạm dừng gọi tự động/);
  assert.equal(injected.length, before, "paused tab must not call TikTok");
  // Thao tác tay vẫn được thử.
  assert.equal((await api(other, { action: "snapshot", userInitiated: true })).ok, true);
  // Bộ đếm request/phút cho popup.
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.ok(storage.liveReApiGuard.perMinute >= 5);
  assert.ok(storage.liveReApiGuard.pausedUntil > Date.now());

  console.log("LIVE_RE_API_GUARD_TEST_OK");
})().catch((error) => { console.error(error); process.exitCode = 1; });
