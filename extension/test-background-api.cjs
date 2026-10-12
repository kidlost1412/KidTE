"use strict";

const assert = require("node:assert/strict");

const calls = [];
let pinResponse = { code: 0 };
let chatResponse = { code: 0 };
let installedListener = null;
let messageListener = null;
const storage = {};
const client = {
  async GetLiveRoomInfo(payload) {
    calls.push(["room", payload]);
    return { code: 0, data: { room_id: "room-19", anchor_info: { user: { id: "777", sec_uid: "SEC9", display_id: "shop_abc", nickname: "Shop ABC" } } } };
  },
  async ListLiveProduct(payload) {
    calls.push(["list", payload]);
    return {
      code: 0,
      data: {
        introduce_id: "p3",
        card_type: 1,
        server_time: "123456",
        products: [
          {
            product_id: "p1", title: "Kích Rễ Cực Mạnh TC01", index: 1,
            format_available_price: "90.000₫",
            format_origin_price: "120.000₫",
            skus: [{
              base: { sku_id: "s1", sku_name: "Chai 100ml" },
              price: {
                formatted_available_price: "90.000₫",
                formatted_original_price: "120.000₫"
              }
            }],
            promotion_info: { name: "Mua 1 tặng 1" }
          },
          { product_id: "p3", title: "BO TRI ONE", index: 3, isPined: true }
        ]
      }
    };
  },
  async PinLiveProduct(payload) {
    calls.push(["pin", payload]);
    return pinResponse;
  },
  async Chat(payload) {
    calls.push(["chat", payload]);
    return chatResponse;
  }
};

const webpackRequire = (id) => {
  assert.equal(id, 591595);
  return { gO: client, hW: { Pin: 1, Unpin: 2 } };
};
webpackRequire.m = {};
const chunk = [];
chunk.push = (entry) => {
  entry[2](webpackRequire);
  return 1;
};
global.window = { webpackChunkecom_streamer: chunk };

global.chrome = {
  storage: {
    local: {
      get: async (keys) => {
        if (!keys) return { ...storage };
        const wanted = Array.isArray(keys) ? keys : [keys];
        return Object.fromEntries(wanted.map((key) => [key, storage[key]]));
      },
      set: async (value) => Object.assign(storage, value)
    }
  },
  tabs: {
    query: async (query) => query?.active
      ? [{ id: 5, url: "https://shop.tiktok.com/other" }]
      : [{ id: 7, url: "https://shop.tiktok.com/streamer/live/product/dashboard?region=us" }],
    get: async (tabId) => {
      if (tabId === 7) {
        return { id: 7, url: "https://shop.tiktok.com/streamer/live/product/dashboard?region=us" };
      }
      throw new Error("No tab");
    },
    sendMessage: async (tabId, message) => ({
      ok: true, product_count: 41, tabId, forwardedType: message.type
    })
  },
  action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
  scripting: { executeScript: async () => [] },
  runtime: {
    onInstalled: { addListener(callback) { installedListener = callback; } },
    onMessage: { addListener(callback) { messageListener = callback; } }
  }
};

const { runTikTokLiveApi, chooseControllerTab, claimObserver } = require("./background.js");

(async () => {
  storage.liveReAiEnabled = true;
  await installedListener({reason:'install'});
  assert.equal(storage.liveReAiEnabled, false, "install must default AI to OFF");
  storage.liveReAiEnabled = true;
  await installedListener({reason:'update'});
  assert.equal(storage.liveReAiEnabled, true, 'updating must preserve AI ON');
  storage.liveReAiEnabled = false;
  await installedListener({reason:'update'});
  assert.equal(storage.liveReAiEnabled, false, 'updating must preserve AI OFF');
  const dispatch = (message) => new Promise((resolve, reject) => {
    try {
      if (messageListener(message, {}, resolve) !== true) {
        reject(new Error("message was not handled asynchronously"));
      }
    } catch (error) { reject(error); }
  });
  storage.liveReAiEnabled = false;
  await dispatch({ type: "LIVE_RE_START" });
  assert.equal(storage.liveReAiEnabled, false, "starting clock must preserve AI OFF");
  await dispatch({ type: "LIVE_RE_START_AT", elapsedMs: 45000 });
  assert.equal(storage.liveReAiEnabled, false, "jumping clock must preserve AI OFF");
  const scan = await dispatch({ type: "LIVE_RE_SCAN_CATALOG" });
  assert.equal(scan.product_count, 41);
  assert.equal(scan.tabId, 7);
  assert.equal(scan.forwardedType, "LIVE_RE_SCAN_CATALOG_PAGE");

  assert.equal(await chooseControllerTab(), 7, "controller must be the LIVE dashboard tab");
  storage.liveReState = { running: false, controllerTabId: 99 };
  storage.liveReObserverTabId = 99;
  storage.liveReObserverAt = Date.now();
  assert.equal(await claimObserver(7), true, "stale controller must not lock the collector");
  assert.equal(storage.liveReState.controllerTabId, 0);
  assert.equal(storage.liveReObserverTabId, 7);
  const snapshotResult = await runTikTokLiveApi({
    action: "snapshot",
    includeCatalogRaw: true
  });
  assert.equal(snapshotResult.ok, true);
  assert.equal(snapshotResult.snapshot.room_id, "room-19");
  assert.equal(snapshotResult.snapshot.pin_id, "p3");
  assert.deepEqual(snapshotResult.snapshot.host, { user_id: "777", sec_uid: "SEC9", unique_id: "shop_abc", nickname: "Shop ABC", source: "room_info" });
  assert.equal(snapshotResult.snapshot.products[0].bag_index, 1);
  assert.equal(snapshotResult.snapshot.products[0].original_price, "120.000₫");
  assert.equal(snapshotResult.snapshot.products[0].sale_price, "90.000₫");
  assert.equal(snapshotResult.snapshot.products[0].skus[0].sku_id, "s1");
  assert.match(snapshotResult.snapshot.products[0].promotion, /Mua 1 tặng 1/);
  assert.ok(snapshotResult.snapshot.raw_api_data.products);
  assert.equal(snapshotResult.snapshot.products[1].is_pinned, true);

  const pinResult = await runTikTokLiveApi({
    action: "pin",
    roomId: "room-19",
    productId: "p1"
  });
  assert.deepEqual(
    calls.at(-1),
    ["pin", { room_id: "room-19", product_id: "p1", op: 1 }]
  );
  assert.equal(pinResult.ok, true);

  const unpinResult = await runTikTokLiveApi({
    action: "unpin",
    roomId: "room-19",
    productId: "p1"
  });
  assert.deepEqual(
    calls.at(-1),
    ["pin", { room_id: "room-19", product_id: "p1", op: 2 }]
  );
  assert.equal(unpinResult.ok, true);

  pinResponse = {};
  const ambiguousPin = await runTikTokLiveApi({
    action: "pin", roomId: "room-19", productId: "p1"
  });
  assert.equal(ambiguousPin.ok, false, "missing TikTok success code must fail closed");
  assert.equal(ambiguousPin.response_code, -1);
  pinResponse = { code: 0 };

  const chatResult = await runTikTokLiveApi({
    action: "send_chat",
    roomId: "room-19",
    content: "@nongdan Dạ, chai đang ghim giá 205.800đ ạ.",
    at_uid: "user-88",
    app_id: 734
  });
  const chatPayload = calls.at(-1)[1];
  assert.equal(calls.at(-1)[0], "chat");
  assert.equal(chatPayload.content, "@nongdan Dạ, chai đang ghim giá 205.800đ ạ.");
  assert.equal(chatPayload.at_uid, "user-88");
  assert.equal(chatPayload.meta.room_id, "room-19");
  assert.equal(chatPayload.meta.app_id, 734);
  assert.equal(typeof chatPayload.client_start_time_stamp_millisecond, "string");
  assert.equal(chatResult.ok, true);

  const longChat = await runTikTokLiveApi({
    action: "send_chat", roomId: "room-19", content: "a".repeat(140)
  });
  assert.equal(longChat.ok, true);
  assert.equal(calls.at(-1)[1].content.length, 140, "chat must not be silently cut at 100");
  const sentCount = calls.length;
  const oversized = await runTikTokLiveApi({
    action: "send_chat", roomId: "room-19", content: "a".repeat(151)
  });
  assert.equal(oversized.ok, false);
  assert.equal(calls.length, sentCount, "oversized chat must never reach TikTok");
  for (const code of [null, false, ""]) {
    chatResponse = { code };
    const invalid = await runTikTokLiveApi({
      action: "send_chat", roomId: "room-19", content: "hello"
    });
    assert.equal(invalid.ok, false, `ambiguous success code ${String(code)} must fail`);
  }

  let receiptFailure = true;
  const receiptRequests = [];
  global.fetch = async (url, options) => {
    receiptRequests.push([url, options]);
    if (url.endsWith('/api/reply/ack') && receiptFailure) throw new Error('backend unavailable');
    return {ok:true,json:async()=>({ok:true,confirmed:true})};
  };
  const fromPage = message => new Promise(resolve => messageListener(message,
    {tab:{id:7,url:'https://shop.tiktok.com/streamer/live/product/dashboard'}}, resolve));
  const ack = {type:'LIVE_RE_AI_ACK',payload:{interaction_id:'i',reply_id:'turn',
    receipt:{room_id:'room-19',response_code:0}}};
  assert.equal((await fromPage(ack)).receipt_saved, true);
  assert.equal(storage.liveReReceiptOutbox.turn.payload.receipt.room_id, 'room-19');
  storage.liveReReceiptOutbox.turn.nextAt = 0;
  receiptFailure = false;
  delete require.cache[require.resolve('./background.js')];
  require('./background.js');
  const chatsBeforeRetry = calls.filter(item=>item[0]==='chat').length;
  await fromPage({type:'LIVE_RE_AI_HEALTH'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(storage.liveReReceiptOutbox.turn, undefined);
  assert.equal(calls.filter(item=>item[0]==='chat').length, chatsBeforeRetry,
    'saved receipt retry must never send chat again');
  assert.equal(receiptRequests.filter(([url])=>url.endsWith('/api/reply/ack')).length, 2);

  const queued = await fromPage({type:'LIVE_RE_QUEUE_ENQUEUE',payload:{room_id:'room-19',
    comment_payload:{comment_id:'c1',received_at_ms:12345}}});
  assert.equal(queued.ok, true);
  const queueRequest = receiptRequests.find(([url]) => url.endsWith('/api/tiktok/queue/enqueue'));
  assert.ok(queueRequest, 'LIVE_RE_QUEUE_ENQUEUE must reach /api/tiktok/queue/enqueue');
  const queueBody = JSON.parse(queueRequest[1].body);
  assert.equal(queueBody.room_id, 'room-19');
  assert.equal(queueBody.comment_payload.received_at_ms, 12345);
  assert.equal(queueBody.comment_payload.comment_id, 'c1');
  for (const [type, op] of [['LIVE_RE_QUEUE_NEXT', 'next'], ['LIVE_RE_QUEUE_CANCEL', 'cancel']]) {
    assert.equal((await fromPage({type, payload: {room_id: 'room-19', comment_id: 'c1'}})).ok, true);
    const request = receiptRequests.find(([url]) => url.endsWith(`/api/tiktok/queue/${op}`));
    assert.ok(request, `${type} must reach /api/tiktok/queue/${op}`);
    assert.equal(JSON.parse(request[1].body).room_id, 'room-19');
  }

  chatResponse = {code: 0};
  storage.liveReAiEnabled = true;
  storage.liveReState = {running: true, controllerTabId: 7};
  chrome.scripting.executeScript = async ({func, args}) => [{result: await func(...args)}];
  const durable = {type:'LIVE_RE_TIKTOK_API',request:{action:'send_chat',roomId:'room-19',
    expected_room_id:'room-19',content:'@Mai Đúng hướng dẫn nha',
    delivery:{interaction_id:'durable-i',reply_id:'durable-turn'}}};
  receiptFailure = true;
  const sent = await fromPage(durable);
  assert.equal(sent.ok, true);
  assert.equal(storage.liveReChatIntents['durable-turn'].state, 'sent');
  assert.equal(storage.liveReReceiptOutbox['durable-turn'].payload.receipt.sent_at_ms, sent.sent_at_ms);
  const chatCount = calls.filter(item=>item[0]==='chat').length;
  delete require.cache[require.resolve('./background.js')];
  require('./background.js');
  assert.equal((await fromPage(durable)).replayed, true);
  assert.equal(calls.filter(item=>item[0]==='chat').length, chatCount,
    'persisted send intent must block a duplicate API call after worker restart');
  const changedRoom = await runTikTokLiveApi({action:'send_chat',roomId:'room-19',
    expected_room_id:'other-room',content:'hello'});
  assert.equal(changedRoom.ok, false);
  assert.equal(calls.filter(item=>item[0]==='chat').length, chatCount);
  storage.liveReChatIntents['lost-intent'] = {state:'sending',interaction_id:'lost-i',room_id:'room-19',at:Date.now()};
  delete require.cache[require.resolve('./background.js')];
  require('./background.js');
  await new Promise(resolve=>setImmediate(resolve));
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(storage.liveReChatIntents['lost-intent'].state, 'unknown');
  assert.ok(receiptRequests.some(([url, options]) => url.endsWith('/api/reply/delivery') &&
    JSON.parse(options.body).reply_id === 'lost-intent' && JSON.parse(options.body).status === 'unknown'));
  assert.equal(calls.filter(item=>item[0]==='chat').length, chatCount);

  // WP6: lịch thử lại receipt với đồng hồ giả: 1,2,5,10,30s rồi 60s, dừng sau 24h nhưng giữ bản ghi.
  {
    const realNow = Date.now, realSetTimeout = global.setTimeout, realClear = global.clearTimeout;
    let now = 1_700_000_000_000;
    Date.now = () => now;
    const timers = [], alarms = [], alarmListeners = [];
    global.setTimeout = (fn, ms) => { timers.push({fn, ms}); return timers.length; };
    global.clearTimeout = () => {};
    chrome.alarms = {create: (name, info) => alarms.push({name, ...info}),
      onAlarm: {addListener: fn => alarmListeners.push(fn)}};
    storage.liveReChatIntents = {};
    storage.liveReReceiptOutbox = {};
    let ackUp = false, ackAttempts = 0;
    global.fetch = async url => {
      if (url.endsWith('/api/reply/ack')) {
        ackAttempts++;
        if (!ackUp) throw new Error('backend unavailable');
      }
      return {ok: true, json: async () => ({ok: true})};
    };
    delete require.cache[require.resolve('./background.js')];
    require('./background.js');
    const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };
    const alarm = async () => { alarmListeners.forEach(fn => fn({name: 'tiktok-receipts'})); await settle(); };
    const first = await fromPage({type: 'LIVE_RE_AI_ACK', payload: {interaction_id: 'i', reply_id: 'fake-clock',
      receipt: {room_id: 'room-19', response_code: 0}}});
    assert.equal(first.receipt_saved, true);
    const created = now;
    assert.equal(storage.liveReReceiptOutbox['fake-clock'].createdAt, created);
    const delays = [];
    for (const expected of [1000, 2000, 5000, 10000]) {
      const timer = timers.at(-1);
      assert.equal(timer.ms, expected, 'bước <30s dùng setTimeout trong bộ nhớ');
      delays.push(timer.ms);
      now += expected;
      timer.fn(); await settle();
    }
    assert.deepEqual(delays, [1000, 2000, 5000, 10000]);
    const timerCount = timers.length;
    assert.equal(alarms.at(-1).when, now + 30000, 'bước 30s dựa vào alarm');
    assert.equal(timers.length, timerCount, 'không dùng setTimeout từ 30s');
    now += 30000; await alarm();
    assert.equal(alarms.at(-1).when, now + 60000);
    now += 60000; await alarm();
    assert.equal(alarms.at(-1).when, now + 60000, 'sau đó mỗi 60s');
    // Sát mốc 24h vẫn còn thử, quá mốc thì dừng nhưng giữ bản ghi.
    now = created + 86400000 - 1000;
    const before = ackAttempts; await alarm();
    assert.equal(ackAttempts, before + 1);
    now = created + 86400000 + 1;
    const afterLimit = ackAttempts;
    await alarm(); await alarm();
    assert.equal(ackAttempts, afterLimit, 'quá 24h không thử nữa');
    assert.equal(storage.liveReReceiptOutbox['fake-clock'].expired_for_reconcile, true);
    assert.equal(storage.liveReReceiptOutbox['fake-clock'].payload.reply_id, 'fake-clock', 'giữ bản ghi, không xoá');
    // Thành công thì xoá bản ghi.
    await fromPage({type: 'LIVE_RE_AI_ACK', payload: {interaction_id: 'i2', reply_id: 'will-succeed',
      receipt: {room_id: 'room-19', response_code: 0}}});
    assert.ok(storage.liveReReceiptOutbox['will-succeed']);
    ackUp = true;
    now += 1000; timers.at(-1).fn(); await settle();
    assert.equal(storage.liveReReceiptOutbox['will-succeed'], undefined);
    Date.now = realNow; global.setTimeout = realSetTimeout; global.clearTimeout = realClear;
    delete chrome.alarms;
  }

  console.log("LIVE_RE_BACKGROUND_API_TEST_OK");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
