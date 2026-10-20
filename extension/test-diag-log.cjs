"use strict";
// Nhật ký chẩn đoán captcha: không lộ dữ liệu nhạy cảm, phát hiện captcha bằng DOM giả,
// buffer khi backend lỗi rồi gửi bù, tóm tắt 5 phút. fetch luôn là mock (không chạm cổng 8793).
const assert = require("node:assert/strict");
const { createDiag } = require("./diag-log.js");

function makeChrome(storage = {}) {
  const listeners = [];
  return {
    storage: {
      local: {
        get: async (key) => ({ [key]: storage[key] }),
        set: async (value) => Object.assign(storage, value)
      },
      onChanged: { addListener: (fn) => listeners.push(fn) }
    },
    tabs: { get: async (id) => ({ id, active: id === 7 }) },
    runtime: { onMessage: { addListener() {} }, sendMessage: async () => ({}) },
    alarms: { create() {}, onAlarm: { addListener() {} } },
    _emit: (changes) => listeners.forEach((fn) => fn(changes, "local"))
  };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));
const jsonResponse = (ok, body) => ({ ok, json: async () => body });

(async () => {
  // 1) Dữ liệu nhạy cảm không lọt vào dòng log.
  {
    const posted = [];
    const storage = {};
    const diag = createDiag({ chrome: makeChrome(storage), fetch: async (url, init) => { posted.push([url, JSON.parse(init.body)]); return jsonResponse(true, { ok: true }); } });
    diag.initBackground();
    const request = { action: "send_chat", content: "Chào chị Lan, giá 99k ạ", userInitiated: false, cookie: "SECRET-COOKIE", headers: { Authorization: "Bearer TOK" }, at_uid: "u-12345" };
    const result = { ok: false, error: "verify msToken=abc123XYZ cookie: sessionid=ZZZ " + "A".repeat(200), response_code: 403, api_calls: 2, api_methods: ["GetLiveRoomInfo", "Chat"], msToken: "SECRETTOKEN", customer: "khach-1" };
    diag.api({ tabId: 7, request, result, startedAt: Date.now() - 120 });
    await tick(); await tick();
    await diag.flush();
    const entries = posted.flatMap(([, body]) => body.entries);
    const text = JSON.stringify(entries);
    for (const secret of ["SECRET", "abc123XYZ", "sessionid=ZZZ", "Bearer", "Chào chị", "u-12345", "khach-1", "99k"]) {
      assert.ok(!text.includes(secret), `lộ dữ liệu: ${secret}`);
    }
    const row = entries.find((e) => e.kind === "api");
    assert.equal(row.tab, 7); assert.equal(row.op, "GetLiveRoomInfo+Chat"); assert.equal(row.calls, 2);
    assert.equal(row.code, "403"); assert.equal(row.ok, false); assert.equal(row.visible, true); assert.equal(row.user, false);
    assert.equal(row.len, request.content.length); assert.match(row.hash, /^[0-9a-f]{8}$/);
    assert.ok(row.msg.length <= 120); assert.ok(row.ms >= 100);
    assert.equal(posted[0][0], "http://127.0.0.1:8793/api/diag/tiktok-browser");
    assert.deepEqual(storage.liveReDiagBuffer, []);
  }

  // 2) Chặn bởi guard/lease được ghi; URL bỏ query; tắt công tắc thì không ghi.
  {
    const chrome = makeChrome();
    const posted = [];
    const diag = createDiag({ chrome, fetch: async (u, init) => { posted.push(...JSON.parse(init.body).entries); return jsonResponse(true, { ok: true }); } });
    diag.initBackground();
    diag.api({ tabId: 8, request: { action: "pin" }, blocked: "guard_pause" });
    diag.record({ kind: "captcha", phase: "hide", url: "https://shop.tiktok.com/streamer/live?msToken=SEC&x=1#h", shown_ms: 5000 });
    await tick(); await tick(); await diag.flush();
    assert.equal(posted.find((e) => e.kind === "api").blocked, "guard_pause");
    assert.equal(posted.find((e) => e.kind === "captcha").url, "https://shop.tiktok.com/streamer/live");
    chrome._emit({ liveReDiagEnabled: { newValue: false } });
    assert.equal(diag.enabled, false);
    const before = diag.pending.length;
    diag.event("ai_toggle", { on: true });
    assert.equal(diag.pending.length, before, "tắt công tắc thì không ghi");
    chrome._emit({ liveReDiagEnabled: { newValue: true } });
    // Sự kiện AI/tự ghim/controller từ storage.onChanged.
    chrome._emit({ liveReAiEnabled: { newValue: true }, liveReState: { oldValue: { running: false, controllerTabId: 1 }, newValue: { running: true, controllerTabId: 2 } } });
    const names = diag.pending.map((e) => e.name);
    assert.deepEqual(names.filter(Boolean), ["ai_toggle", "autopin_toggle", "controller_change"]);
  }

  // 3) Backend lỗi: giữ buffer (tối đa 500 dòng), có lại thì gửi bù đủ và xoá buffer.
  {
    const storage = {};
    let up = false;
    const sent = [];
    const fetchMock = async (url, init) => {
      if (!up) throw new Error("backend offline");
      sent.push(...JSON.parse(init.body).entries);
      return jsonResponse(true, { ok: true });
    };
    const warnings = [];
    const realWarn = console.warn; console.warn = (...a) => warnings.push(a.join(" "));
    const diag = createDiag({ chrome: makeChrome(storage), fetch: fetchMock });
    diag.initBackground();
    for (let i = 0; i < 620; i += 1) diag.event("pulse", { phase: "start", ms: i });
    await diag.flush();
    console.warn = realWarn;
    assert.equal(storage.liveReDiagBuffer.length, 500, "buffer tối đa 500 dòng");
    assert.equal(storage.liveReDiagBuffer.at(-1).ms, 619, "giữ dòng mới nhất");
    assert.ok(warnings.length >= 1);
    up = true;
    diag.event("ai_toggle", { on: false });
    await diag.flush();
    assert.equal(sent.length, 500, "gửi bù đủ buffer (đã giới hạn 500)");
    assert.deepEqual(storage.liveReDiagBuffer, []);
  }

  // 4) Phát hiện captcha bằng DOM giả + tóm tắt 5 phút gửi ngay khi captcha hiện.
  {
    let clock = 1_000_000;
    const storage = {};
    const posted = [];
    const chrome = makeChrome(storage);
    const diag = createDiag({ chrome, now: () => clock, fetch: async (u, init) => { posted.push(...JSON.parse(init.body).entries); return jsonResponse(true, { ok: true }); } });
    diag.initBackground();
    const calls = [["ListLiveProduct", 7], ["ListLiveProduct", 7], ["PinLiveProduct", 7], ["Chat", 9]];
    for (const [method, tab] of calls) { diag.api({ tabId: tab, request: { action: "pin" }, result: { ok: true, api_calls: 1, api_methods: [method] }, startedAt: clock }); }
    await tick(); await tick();
    clock += 60_000;
    diag.api({ tabId: 7, request: { action: "snapshot" }, result: { ok: true, api_methods: ["ListLiveProduct"] } });
    await tick(); await tick();
    clock += 6 * 60_000; // các lượt trên ra khỏi cửa sổ 5 phút, chỉ còn lượt mới
    diag.api({ tabId: 9, request: { action: "snapshot" }, result: { ok: true, api_methods: ["GetLiveRoomInfo"] } });
    await tick(); await tick();

    // Content script (DOM giả) gửi sự kiện captcha qua runtime.sendMessage -> background ghi.
    const contentMessages = [];
    const contentChrome = makeChrome();
    contentChrome.runtime.sendMessage = async (message) => { contentMessages.push(message); };
    const content = createDiag({ chrome: contentChrome, now: () => clock });
    let observerCallback = null;
    class FakeObserver { constructor(cb) { observerCallback = cb; } observe() {} }
    const small = { id: "verified-badge", className: "", tagName: "SPAN", getBoundingClientRect: () => ({ width: 20, height: 20 }) };
    const captcha = { id: "captcha_container", className: "secsdk-captcha-wrapper", tagName: "DIV", getBoundingClientRect: () => ({ width: 340, height: 300 }) };
    let nodes = [small];
    const doc = { documentElement: {}, defaultView: { getComputedStyle: () => ({ display: "block", visibility: "visible" }) }, querySelectorAll: () => nodes };
    const handle = content.initContent({ document: doc, MutationObserver: FakeObserver, location: { href: "https://shop.tiktok.com/streamer/live/product/dashboard?msToken=SEC" } });
    handle.check();
    assert.equal(contentMessages.length, 0, "phần tử nhỏ 'verified' không phải captcha");
    nodes = [captcha, small];
    observerCallback([]); // MutationObserver kích hoạt
    handle.check();
    assert.equal(contentMessages.length, 1);
    assert.equal(contentMessages[0].type, "KIDTE_DIAG");
    assert.equal(contentMessages[0].entry.phase, "show");
    assert.equal(contentMessages[0].entry.url, "https://shop.tiktok.com/streamer/live/product/dashboard");
    handle.check(); assert.equal(contentMessages.length, 1, "không báo trùng khi vẫn hiện");
    clock += 4000; nodes = [small]; handle.check();
    assert.equal(contentMessages[1].entry.phase, "hide");
    assert.equal(contentMessages[1].entry.shown_ms, 4000);

    // Background nhận tin từ tab TikTok và gửi NGAY kèm tóm tắt.
    const bgListeners = [];
    chrome.runtime.onMessage.addListener = (fn) => bgListeners.push(fn);
    diag.initBackground();
    bgListeners[0](contentMessages[0], { tab: { id: 7, url: "https://shop.tiktok.com/x" } });
    bgListeners[0](contentMessages[0], { tab: { id: 7, url: "https://evil.example/x" } }); // bị bỏ qua
    await tick(); await tick(); await tick();
    await diag.flush();
    const shown = posted.filter((e) => e.kind === "captcha" && e.phase === "show");
    assert.equal(shown.length, 1);
    assert.equal(shown[0].tab, 7);
    assert.equal(shown[0].summary.window_s, 300);
    assert.equal(shown[0].summary.calls, 1);
    assert.deepEqual(shown[0].summary.by_method, { GetLiveRoomInfo: 1 });

    // Tóm tắt khi 5 phút gần nhất có đủ lượt.
    const d2 = createDiag({ chrome: makeChrome(), now: () => clock, fetch: async () => jsonResponse(true, { ok: true }) });
    d2.initBackground();
    for (const [method, tab] of calls) d2.api({ tabId: tab, request: { action: "x" }, result: { ok: true, api_methods: [method] } });
    const summary = d2.summarize(clock);
    assert.deepEqual(summary.by_method, { ListLiveProduct: 2, PinLiveProduct: 1, Chat: 1 });
    assert.equal(summary.writes, 2); assert.equal(summary.tabs, 2); assert.equal(summary.calls, 4);
  }

  // 5) Ghi log hỏng không ném lỗi / không làm chậm luồng chính.
  {
    const diag = createDiag({ chrome: { storage: { local: { get: async () => { throw new Error("boom"); } } } }, fetch: undefined });
    const realWarn = console.warn; console.warn = () => {};
    diag.initBackground();
    assert.doesNotThrow(() => { diag.record(null); diag.event("x", { reason: {} }); diag.api({}); diag.api({ tabId: 1, request: null, result: null }); });
    await diag.flush();
    console.warn = realWarn;
  }
  console.log("diag-log: OK");
})().catch((error) => { console.error(error); process.exit(1); });
