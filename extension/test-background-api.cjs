"use strict";

const assert = require("node:assert/strict");
// Không bao giờ chạm backend thật (cổng 8793) khi test.
global.fetch = async () => { throw new Error("backend offline (test)"); };

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
      if (tabId === 5 || tabId === 6) return { id: tabId, url: "https://shop.tiktok.com/other" };
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
  // 3.11.8: tab khác trang không giành vai của tab LIVE dashboard còn sống...
  assert.equal(await claimObserver(5), false, "non-dashboard tab must not take the observer role");
  // ...nhưng tab LIVE dashboard giành vai từ tab khác trang đang giữ.
  storage.liveReObserverTabId = 6;
  storage.liveReObserverAt = Date.now();
  assert.equal(await claimObserver(7), true, "dashboard tab takes the role from a non-dashboard holder");
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

  {
    // Chưa LIVE: GetLiveRoomInfo không có room_id, ListLiveProduct({}) trả giỏ.
    const saved = { room: client.GetLiveRoomInfo, list: client.ListLiveProduct };
    const writeCalls = [];
    for (const name of ["PinLiveProduct", "AddLiveProduct", "DeleteLiveProduct", "Chat"]) {
      const prior = client[name];
      client[name] = async (payload) => { writeCalls.push(name); return prior ? prior(payload) : { code: 0 }; };
    }
    client.GetLiveRoomInfo = async () => ({ code: 0, message: "success", data: { anchor_info: { user: { id: "777", display_id: "shop_abc" } } } });
    let listShape = "product_list";
    client.ListLiveProduct = async (payload) => {
      assert.equal(payload.room_id, undefined, "prelive must not invent room_id");
      if (listShape === "product_list") return { code: 0, data: { product_list: [
        { product_id: "a1", title: "Sp A", format_price: "10₫", cover: {url_list: ["https://img.example/a1"]} }, { product_id: "a2", title: "Sp B" }] } };
      if (listShape === "nested") return { code: 0, data: { sections: [{ live_products: [{ base: { product_id: "n1", title: "Lồng", cover: {url_list: ["https://img.example/n1"]} } }] }] } };
      return { code: 0, message: "success", data: { total: 0, has_more: false } };
    };
    for (const shape of ["product_list", "nested"]) {
      listShape = shape;
      const pre = await runTikTokLiveApi({ action: "snapshot" });
      assert.equal(pre.ok, true, shape);
      assert.equal(pre.snapshot.room_id, "");
      assert.equal(pre.snapshot.phase, "prelive");
      assert.ok(pre.snapshot.products.length >= 1);
      assert.equal(pre.snapshot.host.user_id, "777");
      assert.deepEqual(pre.snapshot.raw_api_data, {}, "ordinary snapshots stay compact");
      const withImages = await runTikTokLiveApi({ action: "snapshot", includeCatalogRaw: true });
      const rawProduct = withImages.snapshot.raw_api_data.products[0];
      assert.equal((rawProduct.cover || rawProduct.base.cover).url_list[0],
        shape === "nested" ? "https://img.example/n1" : "https://img.example/a1",
        "manual prelive scan must retain API covers");
    }
    // Không có sản phẩm: thử method đọc khác, rồi báo lỗi có chẩn đoán.
    listShape = "empty";
    let readCalled = 0;
    client.GetLiveProductList = async () => { readCalled += 1; return { code: 0, data: { secret_token: "TOKEN-XYZ" } }; };
    client.UpdateProductList = async () => { writeCalls.push("UpdateProductList"); return { code: 0 }; };
    client.ListShopProduct = async () => ({ code: 0, data: { products: [{ product_id: "shop-all" }] } });
    client.SearchProductList = async () => ({ code: 0, data: { products: [{ product_id: "shop-all" }] } });
    const fail = await runTikTokLiveApi({ action: "snapshot" });
    assert.ok(!JSON.stringify(fail).includes("shop-all"), "shop-wide product methods must not be used");
    delete client.ListShopProduct; delete client.SearchProductList;
    assert.equal(fail.ok, false);
    assert.equal(readCalled, 1);
    assert.match(fail.error, /Chẩn đoán/);
    assert.match(fail.error, /ListLiveProduct\(\{\}\)=\[.*data\{total,has_more\}/);
    assert.match(fail.error, /GetLiveProductList/);
    assert.doesNotMatch(fail.error, /TOKEN-XYZ|777|shop_abc/);
    // Hành động khác vẫn bị chặn khi không có room_id.
    const pinPre = await runTikTokLiveApi({ action: "pin", productId: "a1" });
    assert.equal(pinPre.ok, false);
    assert.deepEqual(writeCalls, [], "prelive must never call write methods");
    delete client.GetLiveProductList; delete client.UpdateProductList;
    client.GetLiveRoomInfo = saved.room; client.ListLiveProduct = saved.list;
    // 3.11.6: ListLiveProduct báo lỗi (code != 0) => ok:false, KHÔNG "0 sản phẩm" thành công.
    client.ListLiveProduct = async () => ({ code: 4003, message: "too frequent", data: {} });
    const listErr = await runTikTokLiveApi({ action: "snapshot" });
    assert.equal(listErr.ok, false, "ListLiveProduct error must not look like an empty cart");
    assert.match(listErr.error, /too frequent/);
    client.ListLiveProduct = saved.list;
    const live = await runTikTokLiveApi({ action: "snapshot" });
    assert.equal(live.snapshot.room_id, "room-19");
    assert.equal(live.snapshot.phase, undefined, "LIVE snapshot unchanged");
  }

  {
    // 3.11.10: hình dạng đúng log chủ shop 20/10. GetLiveRoomInfo CÓ khoá room_id
    // nhưng giá trị 0/"0"/"" (chưa LIVE), shopping_bag_product_count > 0;
    // ListLiveProduct({}) trả products: [] ; giỏ thật nằm ở ListLiveProductPagination.
    const saved = { room: client.GetLiveRoomInfo, list: client.ListLiveProduct };
    const calls2 = [];
    let pagedProducts = [{ product_id: "g1", title: "Kích Rễ Cực Mạnh TC01", cover: {url_list: ["https://img.example/g1"]} }, { product_id: "g2", title: "BO TRI ONE" }];
    client.ListLiveProduct = async (payload) => { calls2.push(["list", payload]); return { code: 0, message: "success",
      data: { products: [], introduce_id: "", server_time: "1", lucky_bag_info: {}, card_type: 0 } }; };
    client.ListLiveProductPagination = async (payload) => { calls2.push(["paged", payload]);
      return { code: 0, data: { products: pagedProducts, has_more: false } }; };
    client.ListTargetPlanProduct = async (payload) => { calls2.push(["plan", payload]); return { code: 0, data: { products: [] } }; };
    for (const rawRoom of [0, "0", "", "000"]) {
      calls2.length = 0;
      client.GetLiveRoomInfo = async () => ({ code: 0, message: "success", data: { room_id: rawRoom,
        create_timestamp: 0, user_count: 0, live_gmv: "0", shopping_bag_product_count: 2 } });
      const pre = await runTikTokLiveApi({ action: "snapshot", roomId: "0", includeCatalogRaw: true });
      assert.equal(pre.ok, true, `room_id=${JSON.stringify(rawRoom)} must fall back to prelive cart`);
      assert.equal(pre.snapshot.phase, "prelive");
      assert.equal(pre.snapshot.room_id, "");
      assert.deepEqual(pre.snapshot.products.map((p) => p.product_id), ["g1", "g2"]);
      assert.equal(pre.snapshot.raw_api_data.products[0].cover.url_list[0], "https://img.example/g1");
      assert.ok(!calls2.some(([, p]) => p && p.room_id !== undefined), "never call with room_id 0");
      assert.ok(calls2.length <= 5, "one bounded round");
    }
    // Không method nào có giỏ: lỗi nêu rõ room_id 0 và số SP TikTok báo, gọi tối đa 1 vòng.
    pagedProducts = [];
    calls2.length = 0;
    const fail = await runTikTokLiveApi({ action: "snapshot" });
    assert.equal(fail.ok, false);
    assert.match(fail.error, /room_id=0\/rỗng/);
    assert.match(fail.error, /giỏ có 2 SP/);
    assert.match(fail.error, /đã thử=\[ListLiveProduct,ListLiveProductPagination,ListTargetPlanProduct/);
    assert.ok(calls2.filter(([n]) => n !== "list").length <= 4);
    // room_id số lớn mất chính xác => không dùng; có room_id_str thì dùng bản chuỗi.
    pagedProducts = [{ product_id: "g1", title: "X" }];
    client.GetLiveRoomInfo = async () => ({ code: 0, data: { room_id: 7429000000000000123, shopping_bag_product_count: 1 } });
    const unsafe = await runTikTokLiveApi({ action: "snapshot" });
    assert.equal(unsafe.snapshot.phase, "prelive");
    client.GetLiveRoomInfo = async () => ({ code: 0, data: { room_id: 7429000000000000123, room_id_str: "7429000000000000123" } });
    client.ListLiveProduct = async (payload) => ({ code: 0, data: { products: payload.room_id === "7429000000000000123" ? [{ product_id: "L1" }] : [] } });
    const big = await runTikTokLiveApi({ action: "snapshot" });
    assert.equal(big.snapshot.room_id, "7429000000000000123");
    assert.equal(big.snapshot.products[0].product_id, "L1");
    // Đang LIVE nhưng ListLiveProduct({room_id}) trả rỗng: thử phân trang đúng 1 lần.
    client.ListLiveProduct = async () => ({ code: 0, data: { products: [] } });
    pagedProducts = [{ product_id: "P9", title: "Y", cover: {url_list: ["https://img.example/P9"]} }];
    calls2.length = 0;
    const livePaged = await runTikTokLiveApi({ action: "snapshot", includeCatalogRaw: true });
    assert.equal(livePaged.snapshot.products[0].product_id, "P9");
    assert.equal(livePaged.snapshot.raw_api_data.products[0].cover.url_list[0], "https://img.example/P9",
      "LIVE pagination fallback must keep the covers from the actual product response");
    assert.equal(calls2.filter(([n]) => n === "paged").length, 1);
    delete client.ListLiveProductPagination; delete client.ListTargetPlanProduct;
    client.GetLiveRoomInfo = saved.room; client.ListLiveProduct = saved.list;
  }

  {
    // Listener return true phải luôn trả lời, kể cả loại tin lạ.
    const reply = await new Promise((resolve) => {
      assert.equal(messageListener({ type: "LIVE_RE_UNKNOWN_X" }, {}, resolve), true);
    });
    assert.equal(reply.ok, false);
    assert.match(reply.error, /không hỗ trợ/);
  }

  console.log("LIVE_RE_BACKGROUND_API_TEST_OK");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
