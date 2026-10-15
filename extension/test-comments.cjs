"use strict";

const assert = require("node:assert/strict");

global.__LIVE_RE_TEST_MODE__ = true;
global.KidTESchedule = require("./schedule.js");
global.window = global;
const listeners = {};
global.addEventListener = (name, callback) => { listeners[name] = callback; };

const storage = { liveReAiEnabled: false };
const changes = [];
const runtimeListeners = [];
const sent = [];
const observed = [];
let replyCalls = 0;
let catalogCount = 42;
let ackCalls = 0;
let snapshotContext = { room_id: "r1", pin_id: "p1", products: [] };
let sendChatFailure = "";
const deliveries = [];
let replyResult = {
  ok: true, should_send: true,
  reply: "Dạ, dùng theo hướng dẫn trên nhãn ạ.",
  interaction_id: "interaction-1", reply_id: "reply-1"
};
global.chrome = {
  storage: {
    local: {
      async get(key) {
        if (typeof key === "string") return { [key]: storage[key] };
        return { ...storage };
      },
      async set(value) { Object.assign(storage, value); }
    },
    onChanged: { addListener(callback) { changes.push(callback); } }
  },
  runtime: {
    onMessage: { addListener(callback) { runtimeListeners.push(callback); } },
    async sendMessage(message) {
      if (message.type === "LIVE_RE_GET_STATE") return {
        ok: true, isController: true,
        state: { running: true, startedAtMs: Date.now() - 45000, pausedElapsedMs: 0 }
      };
      if (message.type === "LIVE_RE_INSTALL_COMMENT_BRIDGE") return { ok: true };
      if (message.type === "LIVE_RE_AI_HEALTH") return { ok: true };
      if (message.type === "LIVE_RE_CATALOG_SYNC") {
        return { ok: true, product_count: catalogCount, synced_at: "2026-09-09T22:00:00+07:00" };
      }
      if (message.type === "LIVE_RE_AI_REPLY") {
        replyCalls += 1;
        return { ...replyResult };
      }
      if (message.type === "LIVE_RE_AI_ACK") {
        ackCalls += 1;
        return { ok: true, confirmed: true };
      }
      if (message.type === "LIVE_RE_AI_DELIVERY") {
        deliveries.push(message.payload);
        return { ok: true, updated: true };
      }
      if (message.type === "LIVE_RE_OBSERVE") {
        observed.push(...(message.payload?.events || []));
        return { ok: true, written: (message.payload?.events || []).length };
      }
      if (message.type === "LIVE_RE_HEARTBEAT") return { ok: true };
      if (message.request?.action === "snapshot") {
        return { ok: true, snapshot: { ...snapshotContext } };
      }
      if (message.request?.action === "send_chat") {
        if (sendChatFailure) return { ok: false, error: sendChatFailure };
        sent.push(message.request);
        return { ok: true };
      }
      throw new Error("unexpected message");
    }
  }
};
require("./comments.js");

(async () => {
  const debug = global.__LIVE_RE_COMMENTS_DEBUG__;
  assert.equal(debug.taggedReply({ unique_id: "nongdan" }, "@sai Câu trả lời"), "@nongdan Câu trả lời");
  await new Promise((resolve) => setTimeout(resolve, 0));
  await debug.applyEnabled(true);
  const scanned = await new Promise((resolve) => {
    const handled = runtimeListeners[0]({ type: "LIVE_RE_SCAN_CATALOG_PAGE" }, {}, resolve);
    assert.equal(handled, true);
  });
  assert.equal(scanned.product_count, 42, "manual pre-LIVE scan must persist catalog");
  debug.acceptComment({
    comment: "Giá bao nhiêu?", comment_id: "m1", user_id: "u1",
    unique_id: "nongdan", nickname: "Nông dân"
  });
  const deadline = Date.now() + 2000;
  while (!sent.length && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  assert.equal(sent.length, 1);
  assert.equal(sent[0].at_uid, "u1");
  assert.match(sent[0].content, /^@nongdan /);
  assert.ok(sent[0].content.length <= 100);

  // TikTok thường phát lại chính Chat của chủ phòng nhưng không luôn gắn
  // is_host. Câu echo phải bị chặn và ID đó được nhớ để chặn câu host sau.
  debug.acceptComment({
    comment: sent[0].content, comment_id: "host-echo-1", user_id: "host-1",
    unique_id: "nongduoctoancauofficial", nickname: "Nông Dược Toàn Cầu"
  });
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(replyCalls, 1);
  assert.ok(debug.getState().hostIdentities.includes("id:host-1"));
  // Chủ phòng tự nhận từ room info: phòng mới, tài khoản khác, không cấu hình.
  debug.setRuntimeForTest({ roomId: "room-auto" });
  debug.learnHostFromSnapshot({ snapshot: { room_id: "room-auto", host: {
    user_id: "auto-9", unique_id: "TaiKhoanMoi", nickname: "Moi", source: "room_info" } } });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(debug.getState().roomHost.unique_id, "TaiKhoanMoi");
  assert.equal(debug.observeAction({ comment: "xin chao ca nha", user_id: "auto-9", unique_id: "taikhoanmoi" }), "host_manual");
  assert.notEqual(debug.observeAction({ comment: "xin chao ca nha", user_id: "khach-1", unique_id: "khach" }), "host_manual");
  assert.ok(storage.liveReRoomHosts["room-auto"]);
  debug.setRuntimeForTest({ roomId: "room-other" });
  assert.equal(debug.getState().roomHost, null);
  debug.setRuntimeForTest({ roomId: "" });
  debug.acceptComment({
    comment: "Câu chủ phòng gõ tay", comment_id: "host-manual-2", user_id: "host-1",
    unique_id: "nongduoctoancauofficial", nickname: "Nông Dược Toàn Cầu"
  });
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(replyCalls, 1);

  debug.acceptComment({
    comment: "Giá bao nhiêu?", comment_id: "m1", user_id: "u1",
    unique_id: "nongdan", nickname: "Nông dân"
  });
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(sent.length, 1);

  await debug.applyEnabled(false);
  catalogCount = 0;
  await debug.applyEnabled(true);
  assert.equal(debug.getState().enabled, false);
  // installed = cầu ChatMessage (vẫn phải sống để thu dữ liệu).
  // aiReady = đã kiểm tra backend + danh mục (phải tắt khi bật AI thất bại).
  assert.equal(debug.getState().aiReady, false);
  assert.equal(debug.getState().installed, true);
  assert.equal(storage.liveReAiEnabled, false);
  debug.acceptComment({
    comment: "Có gửi nhầm không?", comment_id: "m2", user_id: "u2",
    unique_id: "nguoi2", nickname: "Người 2"
  });
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(sent.length, 1);
  // --- Chế độ thu thập: ghi mọi thứ kể cả khi AI đã tắt ---
  observed.length = 0;
  assert.equal(debug.getState().enabled, false);
  debug.onRawComment({
    comment: "ok", comment_id: "obs-1", user_id: "u3", unique_id: "nguoi3"
  });
  debug.onRawComment({
    comment: "@nguoi3 289k nha anh, coi mã bên tài khoản mình nha",
    comment_id: "obs-2", user_id: "host-1",
    unique_id: "nongduoctoancauofficial", nickname: "Nông Dược Toàn Cầu"
  });
  await debug.flushObserve();
  // Gồm cả câu AI đã gửi ở phần trên: raw stream dựng lại được trọn phiên.
  const replied = observed.find((row) => row.ai_action === "ai_replied");
  assert.ok(replied, "phải ghi lại chính câu AI đã gửi");
  assert.equal(replied.reply_to_username, "nongdan");
  assert.match(replied.text, /^@nongdan /);
  const short = observed.find((row) => row.text === "ok");
  assert.ok(short, "phải thu câu ngắn khi AI đang tắt");
  assert.equal(short.ai_enabled, false);
  assert.equal(short.ai_action, "ai_off");
  const manual = observed.find((row) => row.ai_action === "host_manual");
  assert.ok(manual, "phải thu câu chủ phòng gõ tay");
  assert.equal(manual.is_host, true);
  assert.equal(manual.reply_to_username || "", "");
  assert.equal(sent.length, 1, "chế độ thu thập không được gửi thêm câu nào");

  // Bình luận dưới 2 ký tự vẫn phải vào kho dữ liệu.
  observed.length = 0;
  debug.setRuntimeForTest({ enabled: true, aiReady: true, liveRunning: true });
  debug.onRawComment({ comment: "e", comment_id: "obs-3", user_id: "u4", unique_id: "nguoi4" });
  await debug.flushObserve();
  assert.equal(observed.length, 1);
  assert.equal(observed[0].ai_action, "too_short");

  // Xác nhận xóa bộ nhớ không tạo interaction mới nên không gửi ACK rỗng.
  replyResult = {
    ok: true, should_send: true, requires_ack: false,
    reply: "Dạ, em đã xóa dữ liệu ghi nhớ của mình rồi ạ."
  };
  const ackBeforeForget = ackCalls;
  debug.setRuntimeForTest({ enabled: true, aiReady: true, liveRunning: true, lastSendAt: 0 });
  await debug.processOne({
    comment: "xóa dữ liệu của tôi", comment_id: "forget-1", user_id: "u5",
    unique_id: "nguoi5", nickname: "Người 5"
  });
  assert.equal(ackCalls, ackBeforeForget, "forget confirmation must not send an empty ACK");
  assert.match(sent.at(-1).content, /đã xóa dữ liệu/);

  // AI bị tắt trong lúc model đang soạn phải chốt bản nháp là cancelled.
  replyResult = {
    ok: true, should_send: true, reply: "Bản nháp không được gửi",
    interaction_id: "interaction-cancel", reply_id: "reply-cancel"
  };
  debug.setRuntimeForTest({ enabled: false, aiReady: true, liveRunning: true, lastSendAt: 0 });
  await debug.processOne({
    comment: "giá bao nhiêu", comment_id: "cancel-1", user_id: "u6",
    unique_id: "nguoi6", nickname: "Người 6"
  });
  assert.equal(deliveries.at(-1).status, "cancelled");

  // --- Tiếng vọng câu AI KHÔNG được ghi là "chủ phòng gõ tay" ---
  // Dữ liệu này để sau học giọng chủ phòng. Ghi câu máy thành câu người là
  // đầu độc chính bộ dữ liệu đó.
  observed.length = 0;
  debug.setRuntimeForTest({ enabled: true, aiReady: true, liveRunning: true, lastSendAt: 0 });
  const echoText = sent[0].content;
  debug.onRawComment({
    comment: echoText, comment_id: "echo-label-1", user_id: "host-1",
    unique_id: "nongduoctoancauofficial", nickname: "Nông Dược Toàn Cầu",
    is_host: true
  });
  await debug.flushObserve();
  const echoRow = observed.find((row) => row.text === echoText);
  assert.ok(echoRow, "tiếng vọng vẫn phải được ghi lại");
  assert.equal(echoRow.ai_action, "self_echo",
    "câu AI vọng về không được gán là host_manual");

  // TikTok vọng lại bằng @NICKNAME (nhiều từ + emoji), không phải @unique_id.
  observed.length = 0;
  const body = echoText.replace(/^@\S+\s+/, "");
  debug.onRawComment({
    comment: "@Vũ style \u{1F1FB}\u{1F1F3} " + body, comment_id: "echo-nick-1", user_id: "host-1",
    unique_id: "nongduoctoancauofficial", nickname: "Nông Dược Toàn Cầu", is_host: true
  });
  debug.onRawComment({
    comment: "@Vũ style \u{1F1FB}\u{1F1F3} " + body.slice(0, 20) + "...", comment_id: "echo-nick-2",
    user_id: "host-1", unique_id: "nongduoctoancauofficial", nickname: "Nông Dược Toàn Cầu", is_host: true
  });
  await debug.flushObserve();
  assert.deepEqual(observed.map((row) => row.ai_action), ["self_echo", "self_echo"],
    "echo theo @nickname không được coi là host_manual");

  // Câu chủ phòng gõ tay thật thì vẫn phải là host_manual.
  observed.length = 0;
  debug.onRawComment({
    comment: "câu này anh tự gõ nha bà con", comment_id: "that-su-host",
    user_id: "host-1", unique_id: "nongduoctoancauofficial",
    nickname: "Nông Dược Toàn Cầu", is_host: true
  });
  await debug.flushObserve();
  assert.equal(
    observed.find((r) => r.text === "câu này anh tự gõ nha bà con")?.ai_action,
    "host_manual", "câu chủ phòng gõ tay vẫn phải là host_manual");

  // --- TikTok đòi xác minh (captcha): phải DỪNG GỬI, không đốt hàng chờ ---
  // Bản cũ chỉ ghi một dòng lỗi rồi xử lý bình luận kế tiếp, gửi tiếp, hỏng
  // tiếp — vừa không ai được trả lời, vừa càng bị TikTok siết.
  replyResult = {
    ok: true, should_send: true, reply: "Câu này sẽ bị TikTok chặn",
    interaction_id: "interaction-cap", reply_id: "reply-cap"
  };
  sendChatFailure = "Chat failed: captcha verification required";
  const sentBeforeCaptcha = sent.length;
  debug.setRuntimeForTest({ enabled: true, aiReady: true, liveRunning: true, lastSendAt: 0 });
  await debug.processOne({
    comment: "giá bao nhiêu", comment_id: "cap-1", user_id: "u7",
    unique_id: "nguoi7", nickname: "Người 7"
  }).catch(() => {});
  const capDelivery = deliveries.at(-1);
  assert.equal(capDelivery.status, "failed");
  assert.match(capDelivery.detail, /xác minh|captcha/i,
    "phải ghi rõ là TikTok đòi xác minh, không chỉ là lỗi chung chung");
  assert.equal(storage.liveReAiStatus.sendPaused, true, "phải bật cờ tạm dừng");
  assert.ok(storage.liveReAiStatus.sendPausedUntilMs > Date.now(),
    "phải đặt mốc thời gian thử lại");

  // Trong lúc tạm dừng, bình luận mới KHÔNG được gửi đi.
  const sentDuringPause = sent.length;
  debug.acceptComment({
    comment: "còn hàng không shop", comment_id: "cap-2", user_id: "u8",
    unique_id: "nguoi8", nickname: "Người 8"
  });
  await new Promise((resolve) => setTimeout(resolve, 300));
  assert.equal(sent.length, sentDuringPause,
    "đang bị chặn mà vẫn gửi tiếp là làm TikTok siết chặt thêm");
  assert.equal(sent.length, sentBeforeCaptcha,
    "không câu nào được gửi kể từ lúc bị chặn");
  sendChatFailure = "";

  // ---------------------------------------------------------------
  // Chống trùng phải có phạm vi phòng, và phải phân biệt khách gửi lại
  // với sự kiện mạng phát lại.
  //
  // Khoá cũ: `comment_id || user_id|created_at|comment` — không biết phòng
  // nào cả. Trong dữ liệu thật, 203/652 bản ghi không có comment_id, và có
  // sẵn những cặp cùng khách + cùng nội dung ("ey", "Alo").
  // ---------------------------------------------------------------
  debug.clearSeenForTest();
  debug.setRuntimeForTest({ roomId: "phong-A" });

  const truocKhiThu = debug.getQueue().length;
  // Cùng một bình luận có comment_id, gửi hai lần: lần hai là phát lại.
  debug.acceptComment({
    comment: "gia nhieu shop", comment_id: "m-1", user_id: "u1",
    unique_id: "khach1", nickname: "Khách 1"
  });
  debug.acceptComment({
    comment: "gia nhieu shop", comment_id: "m-1", user_id: "u1",
    unique_id: "khach1", nickname: "Khách 1"
  });
  assert.equal(debug.getQueue().length - truocKhiThu, 1,
    "cùng comment_id trong cùng phòng là phát lại, chỉ nhận một lần");

  // Cùng comment_id nhưng ĐỔI PHÒNG: là bình luận khác, phải nhận.
  const truocDoiPhong = debug.getQueue().length;
  debug.setRuntimeForTest({ roomId: "phong-B" });
  debug.acceptComment({
    comment: "gia nhieu shop", comment_id: "m-1", user_id: "u1",
    unique_id: "khach1", nickname: "Khách 1"
  });
  assert.equal(debug.getQueue().length - truocDoiPhong, 1,
    "đổi phòng thì khoá chống trùng cũ không được chặn bình luận mới");

  // Không có comment_id: khách gửi lại đúng câu đó ở thời điểm khác.
  debug.setRuntimeForTest({ roomId: "phong-C" });
  const truocGuiLai = debug.getQueue().length;
  debug.acceptComment({
    comment: "alo shop oi", user_id: "u2", created_at: "2026-09-15T10:00:00Z",
    unique_id: "khach2", nickname: "Khách 2"
  });
  debug.acceptComment({
    comment: "alo shop oi", user_id: "u2", created_at: "2026-09-15T10:00:00Z",
    unique_id: "khach2", nickname: "Khách 2"
  });
  assert.equal(debug.getQueue().length - truocGuiLai, 1,
    "cùng mốc thời gian sự kiện = phát lại, chỉ nhận một lần");
  debug.acceptComment({
    comment: "alo shop oi", user_id: "u2", created_at: "2026-09-15T10:00:07Z",
    unique_id: "khach2", nickname: "Khách 2"
  });
  assert.equal(debug.getQueue().length - truocGuiLai, 1,
    "cùng khách hỏi lại đúng câu trong 3 phút: chỉ trả lời một lần");
  debug.acceptComment({
    comment: "alo shop oi nha", user_id: "u2", created_at: "2026-09-15T10:00:09Z",
    unique_id: "khach2", nickname: "Khách 2"
  });
  assert.equal(debug.getQueue().length - truocGuiLai, 2,
    "câu khác của cùng khách vẫn phải nhận");

  debug.setRuntimeForTest({enabled:false});
  await debug.waitForAiPersistence();
  const savedPending = storage.liveReAiPending.map(row=>({...row}));
  assert.ok(savedPending.length > 0);
  storage.liveReAiPending.push({room_id:'old', comment_id:'expired', received_at_ms:Date.now()-120000});
  storage.liveReAiEnabled = true;
  const originalSend = chrome.runtime.sendMessage;
  chrome.runtime.sendMessage = message => message.type === 'LIVE_RE_GET_STATE'
    ? Promise.resolve({ok:true,isController:true,state:{running:false}}) : originalSend(message);
  global.__LIVE_RE_COMMENTS_LOADED__ = false;
  delete require.cache[require.resolve('./comments.js')];
  require('./comments.js');
  await new Promise(resolve=>setImmediate(resolve));
  await new Promise(resolve=>setImmediate(resolve));
  const restored = global.__LIVE_RE_COMMENTS_DEBUG__.getQueue();
  assert.deepEqual(restored.map(row=>row.received_at_ms), savedPending.map(row=>row.received_at_ms));
  assert.equal(restored.some(row=>row.comment_id==='expired'), false);
  assert.deepEqual(restored.map(row=>row.comment_id), savedPending.map(row=>row.comment_id));

  // WP2: bản sao phụ lên hàng chờ backend, không đụng đường gửi.
  {
    const d2 = global.__LIVE_RE_COMMENTS_DEBUG__;
    const qCalls = [];
    let qFail = true;
    const before = chrome.runtime.sendMessage;
    chrome.runtime.sendMessage = async message => {
      if (message.type === 'LIVE_RE_QUEUE_ENQUEUE') {
        qCalls.push(message.payload);
        if (qFail) throw new Error('backend down');
        return {ok: true, queue_id: `q-${message.payload.comment_payload.comment_id}`, state: 'queued'};
      }
      return before(message);
    };
    const feed = (id, text) => {
      d2.setRuntimeForTest({enabled: true, aiReady: true, liveRunning: true, isController: true, roomId: 'room-q'});
      d2.acceptComment({comment: text, comment_id: id, user_id: 'uq', unique_id: 'khachq', nickname: 'Q'});
      d2.setRuntimeForTest({liveRunning: false}); // giữ hàng chờ nguyên để kiểm tra
    };
    d2.setRuntimeForTest({queueApi: false});
    feed('qc0', 'khong co api');
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.equal(qCalls.some(item => item.comment_payload.comment_id === 'qc0'), false,
      'thiếu comment_queue_api thì bỏ qua im lặng');
    d2.setRuntimeForTest({queueApi: true});
    feed('qc1', 'hang cho backend');
    await new Promise(resolve => setTimeout(resolve, 30));
    const first = qCalls.find(item => item.comment_payload.comment_id === 'qc1');
    assert.ok(first, 'comment mới phải được báo lên hàng chờ backend');
    assert.equal(first.room_id, 'room-q');
    assert.equal(first.comment_payload.comment_id, 'qc1');
    assert.equal(first.comment_payload.live_context.room_id, 'room-q');
    // Cùng ngữ cảnh /api/reply: snapshot đang ghim + video_context theo đồng hồ lịch.
    assert.equal(first.comment_payload.live_context.pin_id, 'p1');
    assert.deepEqual(first.comment_payload.live_context.products, []);
    const mirrored = first.comment_payload.video_context;
    assert.equal(mirrored.sync_confidence, 'manual_clock');
    assert.equal(typeof mirrored.position_s, 'number');
    assert.deepEqual(Object.keys(mirrored).sort(),
      ['current_topic', 'position_s', 'reviewed_script_facts', 'scripted_product_id', 'sync_confidence']);
    assert.equal(first.comment_payload.data_source, 'live_extension');
    await new Promise(resolve => setTimeout(resolve, 30));
    const row = d2.getQueue().find(item => item.comment_id === 'qc1');
    assert.ok(row && !row.backend_queue_id, 'lỗi backend: giữ hàng, chưa đánh dấu');
    qFail = false;
    await d2.syncBackendQueue();
    const retry = qCalls.filter(item => item.comment_payload.comment_id === 'qc1').at(-1);
    assert.equal(retry.comment_payload.received_at_ms, first.comment_payload.received_at_ms,
      'thử lại phải mang received_at_ms gốc');
    assert.equal(d2.getQueue().find(item => item.comment_id === 'qc1').backend_queue_id, 'q-qc1');
    await d2.waitForAiPersistence();
    assert.equal(storage.liveReAiPending.find(item => item.comment_id === 'qc1').backend_queue_id, 'q-qc1');
    await d2.syncBackendQueue();
    assert.equal(qCalls.filter(item => item.comment_payload.comment_id === 'qc1').length, 2,
      'đã có backend_queue_id thì không gửi lại');
    chrome.runtime.sendMessage = before;
  }

  // WP3: đường gửi qua backend queue/next (cùng cờ liveReBackendQueueMirror).
  {
    const d3 = global.__LIVE_RE_COMMENTS_DEBUG__;
    const before = chrome.runtime.sendMessage;
    const calls = [];
    let nextScript = [];
    const turnsSent = [];
    let sendTurnFailures = 0;
    let stopNext = '';
    global.KidTEMessageDelivery = {
      async sendTurn(result, hooks) {
        if (stopNext) { const stopped = stopNext; stopNext = ''; return {ok: false, stopped, sent: 0}; }
        if (sendTurnFailures > 0) { sendTurnFailures -= 1; throw new Error('send failed'); }
        turnsSent.push(result);
      }
    };
    chrome.runtime.sendMessage = async message => {
      if (message.type === 'LIVE_RE_QUEUE_NEXT') {
        calls.push(['next', message.payload]);
        const step = nextScript.shift();
        if (step instanceof Error) throw step;
        return step || {ok: true, available: false};
      }
      if (message.type === 'LIVE_RE_QUEUE_CANCEL') { calls.push(['cancel', message.payload]); return {ok: true}; }
      if (message.type === 'LIVE_RE_HEARTBEAT') { calls.push(['heartbeat']); return {ok: true}; }
      if (message.type === 'LIVE_RE_QUEUE_ENQUEUE') return {ok: true, queue_id: 'x', state: 'queued'};
      if (message.type === 'LIVE_RE_AI_REPLY') { calls.push(['local_reply']); }
      return before(message);
    };
    const turn = (id, extra = {}) => ({ok: true, available: true, queue_id: `q-${id}`,
      result: {ok: true, should_send: true, protocol_version: 2, turn_id: `t-${id}`, room_id: 'r1',
        interaction_id: `i-${id}`, messages: [{content: `reply ${id}`}], ...extra},
      source_comment: {comment_id: id, user_id: `u-${id}`, room_id: 'r1'}});
    const feed = (id) => {
      d3.setRuntimeForTest({enabled: true, aiReady: true, liveRunning: true, isController: true, roomId: 'r1'});
      d3.acceptComment({comment: `hoi ${id}`, comment_id: id, user_id: `u-${id}`, unique_id: `k${id}`, nickname: id});
      d3.setRuntimeForTest({liveRunning: false});
    };
    const run = async () => { d3.setRuntimeForTest({liveRunning: true}); await d3.drain(); d3.setRuntimeForTest({liveRunning: false}); };
    const ids = () => d3.getQueue().map(row => row.comment_id);

    // Hàng chờ cũ còn sót: bị bỏ cục bộ (hết hạn) -> phải huỷ ở backend.
    d3.setRuntimeForTest({queueApi: true, protocolV2: true});
    const realNow = Date.now;
    Date.now = () => realNow() + 130000;
    await run();
    Date.now = realNow;
    assert.deepEqual(ids(), [], 'hàng hết hạn bị bỏ cục bộ');
    assert.ok(calls.some(c => c[0] === 'cancel' && c[1].comment_id === 'qc1' && c[1].room_id === 'room-q'),
      'bỏ cục bộ khi BẬT phải gọi queue/cancel');
    calls.length = 0;

    // OFF: không đụng queue/next, đi đường cục bộ cũ.
    d3.setRuntimeForTest({queueApi: false});
    feed('off1');
    await run();
    assert.equal(calls.some(c => c[0] === 'next' || c[0] === 'cancel'), false, 'OFF: không gọi queue/next');
    assert.ok(calls.some(c => c[0] === 'local_reply'), 'OFF: vẫn dùng LIVE_RE_AI_REPLY');
    d3.setRuntimeForTest({queueApi: true});
    calls.length = 0;
    turnsSent.length = 0;

    // FIFO từ backend, gửi bằng sendTurn với source_comment, xoá hàng cục bộ khớp comment_id.
    feed('a1'); feed('a2');
    nextScript = [turn('a1'), turn('a2')];
    await run();
    assert.deepEqual(turnsSent.map(item => item.turn_id), ['t-a1', 't-a2']);
    assert.deepEqual(ids(), [], 'gửi xong thì xoá hàng cục bộ');
    assert.equal(calls.filter(c => c[0] === 'local_reply').length, 0, 'không gửi cục bộ khi BẬT');
    assert.deepEqual(calls.filter(c => c[0] === 'next').map(c => c[1]), [
      {room_id: 'r1'}, {room_id: 'r1'}], 'room_id lấy từ snapshot');
    assert.equal(calls[0][0], 'heartbeat', 'heartbeat trước mỗi next');
    assert.equal(calls[1][0], 'next');

    // B06: facts changed before the first send -> turn stopped, nothing reported as sent;
    // the backend serves the recomposed turn on the next request.
    feed('rv1');
    nextScript = [turn('rv1')];
    stopNext = 'revision_changed';
    await run();
    assert.equal(turnsSent.length, 2, 'lượt bị dừng không được tính là đã gửi');
    assert.match(storage.liveReAiStatus.message, /Dữ kiện đã đổi/);
    assert.equal(storage.liveReAiStatus.message.includes('Đã gửi đủ'), false);
    assert.deepEqual(ids(), [], 'backend giữ lượt soạn lại, hàng cục bộ không bị kẹt');

    // available:false -> không làm gì, giữ hàng.
    calls.length = 0; turnsSent.length = 0;
    feed('b1');
    nextScript = [{ok: true, available: false}];
    await run();
    assert.equal(turnsSent.length, 0);
    assert.deepEqual(ids(), ['b1']);

    // Lỗi mạng / delivery_control_inactive -> không gửi cục bộ, giữ hàng.
    calls.length = 0;
    nextScript = [new Error('net down')];
    await run();
    nextScript = [{ok: false, error: 'delivery_control_inactive'}];
    await run();
    assert.equal(turnsSent.length, 0);
    assert.equal(calls.some(c => c[0] === 'local_reply'), false, 'không fallback gửi cục bộ');
    assert.deepEqual(ids(), ['b1']);

    // Lượt phục hồi: lần gửi đầu lỗi (giữ hàng), nhịp sau nhận lại đúng lượt và gửi một lần.
    sendTurnFailures = 1;
    nextScript = [turn('b1', {recovered: true}), {...turn('b1'), recovered: true}];
    await run();
    assert.deepEqual(ids(), ['b1'], 'gửi lỗi thì giữ hàng');
    await run();
    assert.deepEqual(turnsSent.map(item => item.turn_id), ['t-b1']);
    assert.deepEqual(ids(), []);
    await run();
    assert.equal(turnsSent.length, 1, 'không gửi lại sau khi đã xoá hàng');

    // Hết hạn / backend huỷ: không gửi, hàng cục bộ bị xoá.
    feed('e1');
    nextScript = [{ok: true, available: true, queue_id: 'q-e1',
      result: {ok: true, should_send: false, reason: 'expired'}, source_comment: {comment_id: 'e1'}}];
    await run();
    assert.equal(turnsSent.length, 1);
    assert.deepEqual(ids(), []);

    // E1: queue mode — lỗi GỬI kiểu TikTok chặn (captcha) kích hoạt tạm dừng; khi đang
    // tạm dừng không gọi queue/next; lỗi mạng backend không tạm dừng; gửi ok thì reset.
    global.KidTEMessageDelivery = {
      async sendTurn(result, hooks) {
        await hooks.send(result.messages[0], {turn_id: result.turn_id});
        turnsSent.push(result);
        return {ok: true, sent: 1};
      }
    };
    d3.setRuntimeForTest({sendPausedUntil: 0, sendFailStreak: 0});
    feed('p1');
    sendChatFailure = 'Vui lòng hoàn tất xác minh captcha';
    nextScript = [turn('p1')];
    calls.length = 0;
    await run();
    sendChatFailure = '';
    assert.ok(d3.getState().sendPausedUntil > Date.now(), 'bị chặn khi gửi qua queue -> tạm dừng');
    assert.match(storage.liveReAiStatus.message, /TẠM DỪNG GỬI/);
    assert.deepEqual(ids(), ['p1'], 'gửi lỗi thì giữ hàng');
    calls.length = 0;
    nextScript = [turn('p1')];
    await run();
    assert.equal(calls.some(c => c[0] === 'next'), false, 'đang tạm dừng: không gọi queue/next');
    // Hết tạm dừng: lỗi mạng backend (queue/next, control) KHÔNG tính là bị chặn.
    d3.setRuntimeForTest({sendPausedUntil: 0, sendFailStreak: 0});
    for (let i = 0; i < 4; i += 1) {
      nextScript = [i % 2 ? {ok: false, error: 'delivery_control_inactive'} : new Error('net down')];
      await run();
    }
    assert.equal(d3.getState().sendPausedUntil, 0, 'lỗi mạng backend không tạm dừng');
    assert.equal(d3.getState().sendFailStreak, 0);
    // Gửi lỗi một lần (không phải captcha) rồi gửi ok -> streak reset.
    sendChatFailure = 'HTTP 500';
    nextScript = [turn('p1')];
    await run();
    sendChatFailure = '';
    assert.equal(d3.getState().sendFailStreak, 1);
    assert.equal(d3.getState().sendPausedUntil, 0, 'một lỗi thường chưa tạm dừng');
    nextScript = [turn('p1')];
    await run();
    assert.equal(d3.getState().sendFailStreak, 0, 'gửi ok -> reset');
    assert.deepEqual(ids(), []);

    chrome.runtime.sendMessage = before;
    d3.setRuntimeForTest({queueApi: false, protocolV2: false});
    delete global.KidTEMessageDelivery;

    // E2: tab không điều khiển/quan sát, AI tắt -> không gửi heartbeat; có tab_id.
    {
      const beats = [];
      const prior = chrome.runtime.sendMessage;
      chrome.runtime.sendMessage = async m => {
        if (m.type === 'LIVE_RE_HEARTBEAT') { beats.push(m.payload); return {ok: true}; }
        return prior(m);
      };
      d3.setRuntimeForTest({isController: false, isObserver: false, enabled: false, heartbeatEverSent: false});
      await d3.sendHeartbeat();
      assert.equal(beats.length, 0, 'tab không điều khiển không gửi heartbeat');
      d3.setRuntimeForTest({isController: true, enabled: true});
      await d3.sendHeartbeat();
      assert.equal(beats.length, 1);
      assert.ok(beats[0].tab_id && beats[0].tab_id === d3.getState().heartbeatTabId);
      d3.setRuntimeForTest({isController: false, enabled: false});
      await d3.sendHeartbeat();
      assert.equal(beats.length, 2, 'tab từng điều khiển vẫn báo tắt AI ngay');
      assert.equal(beats[1].ai_enabled, false);
      chrome.runtime.sendMessage = prior;
    }
  }

  // F02: privacy_purge from the heartbeat removes matching local rows, keeps others, then acks.
  {
    const priorSend = chrome.runtime.sendMessage;
    const acks = [];
    storage.liveReReceiptOutbox = {
      r1: { type: "LIVE_RE_AI_ACK", payload: { text: "@Nguyễn Văn Nông Dân dạ giá 50k", interaction_id: "i1" } },
      r2: { type: "LIVE_RE_AI_ACK", payload: { text: "@khac dạ giá 60k", interaction_id: "i2" } }
    };
    storage.liveReChatIntents = { c1: { user_id: "nongdan9999", reply_id: "x" }, c2: { user_id: "other1", reply_id: "y" } };
    chrome.runtime.sendMessage = async (message) => {
      if (message.type === "LIVE_RE_HEARTBEAT") {
        return { ok: true, privacy_purge: [
          { id: "purge-1", platform: "tiktok", identifiers: ["nguyen van nong dan", "nongdan9999"] },
          { id: "purge-short", platform: "tiktok", identifiers: [] }
        ] };
      }
      if (message.type === "LIVE_RE_PRIVACY_PURGE_ACK") { acks.push(message.payload); return { ok: true }; }
      return priorSend(message);
    };
    await debug.sendHeartbeat();
    assert.deepEqual(Object.keys(storage.liveReReceiptOutbox), ["r2"]);
    assert.deepEqual(Object.keys(storage.liveReChatIntents), ["c2"]);
    assert.deepEqual(acks, [{ ids: ["purge-1"], platform: "tiktok" }]);
    // ordinary heartbeat without the extra field keeps working and acks nothing
    chrome.runtime.sendMessage = async (message) => message.type === "LIVE_RE_HEARTBEAT" ? { ok: true } : priorSend(message);
    await debug.sendHeartbeat();
    assert.equal(acks.length, 1);
    // an "id-looking" substring inside a longer token must not match
    await debug.applyPrivacyPurge([]);
    chrome.runtime.sendMessage = priorSend;
  }

  // Lệnh từ app qua heartbeat: seq mới áp một lần qua đường popup, seq cũ không áp lại.
  {
    const priorSend = chrome.runtime.sendMessage;
    const beats = [], pinCalls = [];
    let control = { seq: 3, ai_enabled: true, auto_pin: false, issued_at: Date.now() / 1000 };
    delete storage.liveReControlSeq;
    storage.liveReAiEnabled = false;
    chrome.runtime.sendMessage = async (message) => {
      if (message.type === "LIVE_RE_HEARTBEAT") { beats.push(message.payload); return { ok: true, control }; }
      if (message.type === "LIVE_RE_PAUSE" || message.type === "LIVE_RE_RESUME") { pinCalls.push(message.type); return { ok: true }; }
      if (message.type === "LIVE_RE_GET_STATE") return { ok: true, state: { running: pinCalls.length === 0, startedAtMs: 1 } };
      return priorSend(message);
    };
    debug.setRuntimeForTest({ isController: true });
    await debug.sendHeartbeat();
    assert.equal(storage.liveReAiEnabled, true, "seq mới bật AI qua liveReAiEnabled như popup");
    assert.deepEqual(pinCalls, ["LIVE_RE_PAUSE"], "auto_pin=false tạm dừng ghim như nút popup");
    assert.equal(storage.liveReControlSeq, 3);
    assert.equal(beats[0].control_applied_seq, undefined);
    // Người dùng tắt trong popup: heartbeat sau vẫn mang seq 3 -> không ép bật lại.
    storage.liveReAiEnabled = false;
    await debug.sendHeartbeat();
    assert.equal(storage.liveReAiEnabled, false, "lệnh cũ không ép lại");
    assert.equal(beats[1].control_applied_seq, 3);
    assert.equal(pinCalls.length, 1);
    // seq cũ hơn cũng bị bỏ qua; seq mới hơn áp tiếp.
    assert.equal(await debug.applyAppControl({ seq: 2, ai_enabled: true }), false);
    control = { seq: 4, ai_enabled: false, auto_pin: null, issued_at: Date.now() / 1000 };
    storage.liveReAiEnabled = true;
    await debug.sendHeartbeat();
    assert.equal(storage.liveReAiEnabled, false);
    assert.equal(storage.liveReControlSeq, 4);
    assert.equal(typeof beats[1].pin_running, "boolean");
    // Lệnh quá 10 phút (cài lại tiện ích): ghi nhận seq, không bật AI, báo control_error.
    assert.equal(await debug.applyAppControl({ seq: 9, ai_enabled: true, issued_at: Date.now() / 1000 - 11 * 60 }), false);
    assert.equal(storage.liveReAiEnabled, false);
    assert.equal(storage.liveReControlSeq, 9);
    await debug.sendHeartbeat();
    assert.match(beats.at(-1).control_error, /quá hạn/);
    chrome.runtime.sendMessage = priorSend;
  }

  {
    // Chưa LIVE + AI tắt: nút Quét giỏ vẫn lưu giỏ prelive, AI không được bật.
    await debug.applyEnabled(false);
    catalogCount = 42;
    snapshotContext = { room_id: "", phase: "prelive", products: [{ product_id: "x" }] };
    const prelive = await new Promise((resolve) => {
      runtimeListeners[0]({ type: "LIVE_RE_SCAN_CATALOG_PAGE" }, {}, resolve);
    });
    assert.equal(prelive.product_count, 42, "prelive scan must work with AI off");
    assert.equal(prelive.live_room_id, "");
    assert.equal(prelive.live_phase, "prelive");
    await debug.applyEnabled(true);
    assert.equal(storage.liveReAiEnabled, false, "AI must not turn on without room_id");
    assert.match(storage.liveReAiStatus.message, /Chưa LIVE/);
    snapshotContext = { room_id: "r1", pin_id: "p1", products: [] };
    // AI tắt: tab trang khác không làm mới định kỳ; tab dashboard đang hiện thì có.
    debug.setRuntimeForTest({ enabled: false, aiReady: false });
    global.location = { pathname: "/workbench/orders" };
    global.document = { visibilityState: "visible" };
    assert.equal(debug.shouldPeriodicSync(), false);
    global.location = { pathname: "/streamer/live/product/dashboard" };
    global.document = { visibilityState: "hidden" };
    assert.equal(debug.shouldPeriodicSync(), false);
    global.document = { visibilityState: "visible" };
    assert.equal(debug.shouldPeriodicSync(), true);
    debug.setRuntimeForTest({ enabled: true, aiReady: true });
    global.location = { pathname: "/workbench/orders" };
    assert.equal(debug.shouldPeriodicSync(), true);
    delete global.location; delete global.document;
  }

  console.log("LIVE_RE_COMMENTS_TEST_OK");
  process.exit(0);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
