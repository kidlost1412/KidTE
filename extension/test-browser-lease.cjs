"use strict";
// Lease liên trình duyệt (Chrome + Cốc Cốc): backend Kid TE quyết profile nào gọi TikTok.
const assert = require("node:assert/strict");

const DASH = "https://shop.tiktok.com/streamer/live/product/dashboard";
const storage = {};
const tabs = new Map([[7, { id: 7, url: DASH }]]);
const injected = [];
const leaseRequests = [];
let backend = "grant"; // grant | deny | down
let messageListener = null, onRemoved = null, onUpdated = null;
let clock = Date.now();
Date.now = () => clock;
const warnings = [];
console.warn = (...args) => warnings.push(args.join(" "));

// Không bao giờ chạm backend thật (cổng 8793) khi test.
global.fetch = async (url, options = {}) => {
  if (!String(url).endsWith("/api/tiktok/lease")) return { ok: true, json: async () => ({ ok: true }) };
  const body = JSON.parse(options.body);
  leaseRequests.push(body);
  if (backend === "down") throw new Error("connect ECONNREFUSED 127.0.0.1:8793");
  if (body.action === "release") return { ok: true, json: async () => ({ ok: true, released: true }) };
  return { ok: true, json: async () => ({ ok: true, granted: backend === "grant" }) };
};
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
    sendMessage: async () => ({}),
    onRemoved: { addListener(cb) { if (!onRemoved || String(cb).includes("releaseBrowserLease")) onRemoved = cb; } },
    onUpdated: { addListener(cb) { onUpdated = cb; } }
  },
  action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
  scripting: { executeScript: async ({ target }) => { injected.push(target.tabId); return [{ result: { ok: true, snapshot: { room_id: "r1", host: { user_id: "u1" } }, api_calls: 1 } }]; } },
  runtime: {
    id: "ext",
    onInstalled: { addListener() {} },
    onMessage: { addListener(callback) { messageListener = callback; } }
  }
};
require("./background.js");

const api = (request = { action: "snapshot" }, tabId = 7) => new Promise((resolve) => messageListener(
  { type: "LIVE_RE_TIKTOK_API", request }, { tab: { id: tabId, url: DASH } }, resolve));
const tick = () => new Promise((resolve) => setTimeout(resolve, 5));

(async () => {
  // 3.11.11: snapshot đầu tiên (chưa biết tài khoản) chỉ ĐỌC để học tài khoản, không giành lease.
  assert.equal((await api()).ok, true);
  assert.equal(leaseRequests.length, 0, "unknown account snapshot does not claim the shared key");
  // Thắng lease: được gọi TikTok; định danh = id profile (lưu storage) + tabId + tài khoản.
  assert.equal((await api()).ok, true);
  assert.equal(leaseRequests.length, 1);
  assert.equal(leaseRequests[0].action, "claim");
  assert.equal(leaseRequests[0].tab_id, "7");
  assert.ok(/^[A-Za-z0-9_-]{8,80}$/.test(storage.liveReProfileId));
  assert.equal(leaseRequests[0].profile_id, storage.liveReProfileId);
  assert.equal(leaseRequests[0].owner_key, "uid:u1");
  assert.equal(leaseRequests[0].live, true);
  // Kết quả cache vài giây: không gọi backend mỗi request.
  await api();
  assert.equal(leaseRequests.length, 1, "lease result cached");

  // Trình duyệt khác giữ lease: từ chối, không chạm TikTok, popup/widget có thông báo.
  backend = "deny";
  clock += 6000;
  const before = injected.length;
  const denied = await api();
  assert.equal(denied.ok, false);
  assert.equal(denied.not_owner, true);
  assert.equal(denied.other_browser, true);
  assert.match(denied.error, /Trình duyệt khác đang LIVE/);
  assert.equal(injected.length, before, "denied profile must not call TikTok");
  await tick();
  assert.match(storage.liveReApiGuard.otherBrowser, /Trình duyệt khác đang LIVE/);
  // Thao tác tay vẫn được phép.
  assert.equal((await api({ action: "snapshot", userInitiated: true })).ok, true);

  // Backend lỗi ngay sau khi bị từ chối: vẫn chặn (trình duyệt khác vừa giữ quyền).
  backend = "down";
  clock += 6000;
  assert.equal((await api()).other_browser, true);
  assert.ok(warnings.some((w) => /giữ chặn/.test(w)), "warns instead of failing silently");
  // Quá 15 giây không liên lạc được backend: fail-open để không tê liệt LIVE (3.11.11: 2 phút -> 15s).
  clock += 61000; // > 15s hold (và > 60s nhịp cảnh báo)
  warnings.length = 0;
  assert.equal((await api()).ok, true);
  assert.ok(warnings.some((w) => /fail-open/.test(w)));

  // Đang giữ quyền rồi backend lỗi: fail-open, LIVE tiếp tục.
  backend = "grant";
  clock += 6000;
  assert.equal((await api()).ok, true);
  backend = "down";
  clock += 6000;
  assert.equal((await api()).ok, true, "holder keeps calling while backend is down");
  await tick();
  assert.equal(storage.liveReApiGuard.otherBrowser, "");
  assert.equal(storage.liveReApiGuard.leaseFailOpen, true);

  // Heartbeat 10s gia hạn lease của tab đang giữ.
  backend = "grant";
  clock += 6000;
  assert.equal((await api()).ok, true);
  const count = leaseRequests.length;
  clock += 10000;
  await new Promise((resolve) => messageListener({ type: "LIVE_RE_HEARTBEAT", payload: {} },
    { tab: { id: 7, url: DASH } }, resolve));
  await tick();
  assert.equal(leaseRequests.length, count + 1, "heartbeat renews the lease");

  // Tab rời LIVE dashboard / đóng: trả lease ngay.
  onUpdated(7, { url: "https://shop.tiktok.com/workbench/orders" });
  await tick();
  assert.equal(leaseRequests.at(-1).action, "release");
  assert.equal(leaseRequests.at(-1).tab_id, "7");
  clock += 1000;
  assert.equal((await api()).ok, true);
  onRemoved(7);
  await tick();
  assert.equal(leaseRequests.at(-1).action, "release");

  console.log("LIVE_RE_BROWSER_LEASE_TEST_OK");
})().catch((error) => { console.error(error); process.exitCode = 1; });
