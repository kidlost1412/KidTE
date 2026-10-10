"use strict";
// Tự nhận điều khiển: background quyết định bắt đầu / nối lại / tôn trọng Dừng.
const assert = require("node:assert/strict");

const storage = {};
const DASH = "https://shop.tiktok.com/streamer/live/product/dashboard";
const tabs = new Map([[7, { id: 7, url: DASH }], [8, { id: 8, url: DASH }]]);
let messageListener = null;
const broadcasts = [];

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
    sendMessage: async (id, message) => { broadcasts.push([id, message]); return {}; }
  },
  action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
  scripting: { executeScript: async () => [] },
  runtime: {
    onInstalled: { addListener() {} },
    onMessage: { addListener(callback) { messageListener = callback; } }
  }
};
require("./background.js");

const send = (message, tabId) => new Promise((resolve) => messageListener(message,
  { tab: { id: tabId, url: tabs.get(tabId)?.url || DASH } }, resolve));
const auto = (tabId, roomId) => send({ type: "LIVE_RE_AUTO_START", roomId }, tabId);

(async () => {
  // Phòng được thấy: tự bắt đầu một lần, tab đó là controller, AI không tự bật.
  storage.liveReAiEnabled = false;
  let result = await auto(7, "room-1");
  assert.equal(result.started, true);
  assert.equal(storage.liveReState.running, true);
  assert.equal(storage.liveReState.controllerTabId, 7);
  assert.equal(storage.liveReAiEnabled, false, "auto start must not enable AI");
  const first = { ...storage.liveReState };

  // Không bắt đầu lần hai, không đặt lại đồng hồ.
  result = await auto(7, "room-1");
  assert.equal(result.started, false);
  assert.deepEqual(storage.liveReState, first);

  // Tab thứ hai không thành controller khi tab đầu còn sống.
  result = await auto(8, "room-1");
  assert.equal(result.started, false);
  assert.equal(result.reason, "other_controller");
  assert.equal(storage.liveReState.controllerTabId, 7);

  // Hai tab báo cùng lúc từ trạng thái trống: chỉ một tab thắng.
  for (const key of Object.keys(storage)) delete storage[key];
  const both = await Promise.all([auto(7, "room-2"), auto(8, "room-2")]);
  assert.equal(both.filter((item) => item.started).length, 1);
  const winner = storage.liveReState.controllerTabId;
  assert.ok([7, 8].includes(winner));

  // Tải lại extension/tab: controller cũ đã chết, phiên đang chạy được nối lại, giữ nguyên elapsed.
  const runningState = { ...storage.liveReState, startedAtMs: Date.now() - 600000 };
  storage.liveReState = runningState;
  const loser = winner === 7 ? 8 : 7;
  tabs.delete(winner);
  result = await auto(loser, "room-2");
  assert.equal(result.started, false);
  assert.equal(result.resumed, true);
  assert.equal(storage.liveReState.controllerTabId, loser);
  assert.equal(storage.liveReState.startedAtMs, runningState.startedAtMs, "resume must keep elapsed");
  assert.equal(storage.liveReState.sessionId, runningState.sessionId);
  assert.equal(storage.liveReState.running, true);

  // Người vận hành bấm Dừng: không tự bắt đầu lại cho cùng phòng.
  await send({ type: "LIVE_RE_STOP" }, loser);
  assert.equal(storage.liveReState.running, false);
  result = await auto(loser, "room-2");
  assert.equal(result.started, false);
  assert.equal(storage.liveReState.running, false);

  // Phòng đổi thì tự bắt đầu lại.
  result = await auto(loser, "room-3");
  assert.equal(result.started, true);
  assert.equal(storage.liveReState.running, true);
  assert.notEqual(storage.liveReState.sessionId, runningState.sessionId);

  // Không có phòng hoặc tab lạ thì không làm gì.
  await send({ type: "LIVE_RE_STOP" }, loser);
  assert.equal((await auto(loser, "")).started, false);
  tabs.set(9, { id: 9, url: "https://shop.tiktok.com/other" });
  assert.equal((await auto(9, "room-4")).started, false);
  assert.equal(storage.liveReState.running, false);

  console.log("LIVE_RE_AUTO_START_TEST_OK");
})().catch((error) => { console.error(error); process.exitCode = 1; });
