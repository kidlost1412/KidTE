"use strict";
// Kịch bản thật: người vận hành mở tab LIVE Manager, KHÔNG bấm "Bắt đầu",
// AI tắt, tự rep tay. Trước bản này hệ thống thu được 0 dòng.
const assert = require("node:assert/strict");

global.__LIVE_RE_TEST_MODE__ = true;
global.KidTESchedule = require("./schedule.js");
global.window = global;
const listeners = {};
global.addEventListener = (name, callback) => { listeners[name] = callback; };

const storage = { liveReAiEnabled: false };   // AI TẮT
const observed = [];
let bridgeInstalled = 0;
let claimedBy = null;

global.chrome = {
  storage: {
    local: {
      async get(key) {
        if (typeof key === "string") return { [key]: storage[key] };
        if (Array.isArray(key)) {
          const out = {};
          for (const k of key) out[k] = storage[k];
          return out;
        }
        return { ...storage };
      },
      async set(value) { Object.assign(storage, value); }
    },
    onChanged: { addListener() {} }
  },
  runtime: {
    onMessage: { addListener() {} },
    async sendMessage(message) {
      if (message.type === "LIVE_RE_GET_STATE") {
        // KHÔNG có controllerTabId: chưa ai bấm "Bắt đầu".
        const state = { running: false, startedAtMs: 0, pausedElapsedMs: 0,
                        sessionId: "sess-1", controllerTabId: 0 };
        let isObserver;
        if (message.claimObserver) {
          if (claimedBy === null) claimedBy = "tab-1";
          isObserver = claimedBy === "tab-1";
        }
        return { ok: true, state, isController: false, isObserver };
      }
      if (message.type === "LIVE_RE_INSTALL_COMMENT_BRIDGE") {
        bridgeInstalled += 1;
        return { ok: true };
      }
      if (message.type === "LIVE_RE_OBSERVE") {
        observed.push(...(message.payload?.events || []));
        return { ok: true, written: message.payload.events.length, total: observed.length };
      }
      if (message.request?.action === "snapshot") {
        return { ok: true, snapshot: { room_id: "room-9", pin_id: "pin-9", products: [] } };
      }
      throw new Error("unexpected " + message.type);
    }
  }
};
require("./comments.js");

(async () => {
  const debug = global.__LIVE_RE_COMMENTS_DEBUG__;
  await new Promise((r) => setTimeout(r, 30));

  assert.equal(bridgeInstalled, 1, "phải cài cầu dù chưa bấm Bắt đầu và AI đang tắt");
  assert.equal(debug.getState().isObserver, true, "tab phải nhận vai quan sát");
  assert.equal(debug.getState().enabled, false, "AI vẫn phải đang tắt");

  debug.onRawComment({ comment: "đạm cá bao nhiêu v shop", comment_id: "m1",
    user_id: "u1", unique_id: "bacnam", nickname: "Bác Năm" });
  debug.onRawComment({ comment: "@bacnam gốc 289k nha anh, coi mã bên tài khoản mình",
    comment_id: "m2", user_id: "host-1", unique_id: "nongduoctoancauofficial",
    nickname: "Nông Dược Toàn Cầu", is_host: true });
  debug.onRawComment({ comment: "ok", comment_id: "m3", user_id: "u2", unique_id: "nguoihai" });
  await debug.flushObserve();

  assert.equal(observed.length, 3, "phải thu đủ 3 dòng khi AI tắt và đồng hồ chưa chạy");
  const [khach, chuPhong, ngan] = observed;
  assert.equal(khach.ai_action, "ai_off");
  assert.equal(khach.room_id, "room-9", "phải gắn room_id");
  assert.equal(khach.session_id, "sess-1", "phải gắn session_id để tách phiên");
  assert.equal(khach.data_source, "live_extension", "dữ liệu thật phải có nhãn nguồn");
  assert.equal(chuPhong.is_host, true, "phải nhận ra câu chủ phòng gõ tay");
  assert.equal(chuPhong.ai_action, "host_manual");
  // Việc tách "@bacnam" thành reply_to_username do backend làm (một nguồn sự
  // thật duy nhất); ở đây chỉ cần chắc nguyên văn có @ để backend ghép được.
  assert.match(chuPhong.text, /^@bacnam /);
  assert.equal(ngan.text, "ok", "câu ngắn vẫn phải vào kho dữ liệu");

  console.log("LIVE_RE_COLLECTOR_TEST_OK");
  process.exit(0);
})().catch((error) => { console.error(error); process.exit(1); });
