"use strict";
// Chạy bởi backend/tests/test_live_control_e2e.py: nhận phản hồi heartbeat THẬT của backend
// (argv[2] = JSON), chạy comments.js với chrome giả (không fetch mạng) và in kết quả.
global.__LIVE_RE_TEST_MODE__ = true;
global.KidTESchedule = require("./schedule.js");
global.window = global;
global.addEventListener = () => {};
const response = JSON.parse(process.argv[2]);
const pinRunning = process.argv[3] === "running";
const storage = { liveReAiEnabled: false };
const beats = [], pinCalls = [];
let state = { running: pinRunning, startedAtMs: Date.now() - 60000, pausedElapsedMs: 0 };
global.chrome = {
  storage: { local: {
    async get(key) { return typeof key === "string" ? { [key]: storage[key] } : { ...storage }; },
    async set(value) { Object.assign(storage, value); } },
    onChanged: { addListener() {} } },
  runtime: {
    onMessage: { addListener() {} },
    async sendMessage(message) {
      if (message.type === "LIVE_RE_GET_STATE") return { ok: true, isController: true, isObserver: true, state };
      if (message.type === "LIVE_RE_PAUSE" || message.type === "LIVE_RE_RESUME") {
        pinCalls.push(message.type);
        state = { ...state, running: message.type === "LIVE_RE_RESUME" };
        return { ok: true, state };
      }
      if (message.type === "LIVE_RE_INSTALL_COMMENT_BRIDGE") return { ok: true };
      if (message.type === "LIVE_RE_AI_HEALTH") return { ok: true };
      if (message.type === "LIVE_RE_CATALOG_SYNC") return { ok: true, product_count: 3, synced_at: "x" };
      if (message.request?.action === "snapshot") return { ok: true, snapshot: { room_id: "r1", products: [{ product_id: "p1" }] } };
      if (message.type === "LIVE_RE_HEARTBEAT") { beats.push(message.payload); return response; }
      return { ok: true };
    }
  }
};
require("./comments.js");
(async () => {
  const debug = global.__LIVE_RE_COMMENTS_DEBUG__;
  debug.setRuntimeForTest({ isController: true, isObserver: true });
  await debug.sendHeartbeat();
  await new Promise(r => setTimeout(r, 30));
  await debug.sendHeartbeat();
  process.stdout.write(JSON.stringify({ storage, pinCalls, last: beats.at(-1) }));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
