"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

let listener;
const executions = [];
const config = { enabled: true, tabId: 5, sessionId: "41590640", videoId: "live1",
  mappings: { tc01: { shop_id: "10", product_id: "111" } } };
let runtimeState = {shopeeConfig: config};
let detectedVideo = "live1";
const tabsById = new Map([[5,{id:5,url:"https://live.shopee.vn/pc/preview?session=41590640"}]]);
const chrome = {
  runtime: { onMessage: { addListener(fn) { listener = fn; } },
    getURL(name) { return `chrome-extension://test/${name}`; } },
  tabs: { async get(id) { if (!tabsById.has(id)) throw Error("closed"); return tabsById.get(id); }, async query() { return [{ id: 5, url: "https://live.shopee.vn/pc/live?session=41590640" }]; } },
  storage: { local: { async get() { return runtimeState; }, async set(value) { Object.assign(runtimeState,value); } } },
  scripting: { async executeScript(args) {
    executions.push(args);
    return [{ result: { ok: true, accepted: true, available: true, shop_id:"10", product_id:"111" } }];
  } }
};
const fakeTimers = [];
const context = { chrome, URL, setTimeout: (fn, ms) => { fakeTimers.push({fn, ms}); return fakeTimers.length; }, clearTimeout() {}, importScripts(...names) {
  assert.deepEqual(names, ["shopee-api.js", "core.js"]);
  context.KidTEShopeeApi = async () => {};
  context.KidTEShopeeCore = require("./core.js");
}, fetch: async (url, options) => { assert.ok(!url.endsWith("/api/shopee/mapping"), "mapping route must match backend"); return ({ ok: true, json: async () => url.endsWith("/api/shopee/mappings")
  ? { ok: true, videos: { live1: { mappings: config.mappings, alternates: {} } } }
  : url.endsWith("/api/shopee/reply")
    ? { ok: true, should_send: true, turn_id: "turn1", reply: "@Mai Dạ TC01 nha" }
  : url.endsWith("/api/video/timelines")
    ? { ok: true, timelines: { live1: { cycle_ms: 100000, events: [{ at_ms: 0, product: "tc01" }],
      products: { tc01: { short_name: "TC01" } } } } }
    : { ok: true, available: true, video_id: detectedVideo, position_s: 1,
      margin: 0.2, min_margin: 0.06, age_s: 1, method: options?.method || "GET" } }); } };
vm.runInNewContext(fs.readFileSync(require("node:path").join(__dirname, "background.js"), "utf8"), context);
function request(message, sender) {
  return new Promise((resolve) => {
    assert.equal(listener(message, sender, resolve), true);
  });
}
(async () => {
  const page = { tab: { id: 5, url: "https://live.shopee.vn/pc/live?session=41590640" } };
  const badSession = { tab: { id: 5, url: "https://live.shopee.vn/pc/live?session=999" } };
  const show = { type: "KIDTE_SHOPEE_API", request: { action: "show", product_id: "111", shop_id: "10" } };
  assert.equal((await request(show, badSession)).ok, false);
  assert.equal((await request({ ...show, request: { action: "show", product_id: "222", shop_id: "10" } }, page)).ok, false);
  assert.equal(executions.length, 0);
  assert.equal((await request(show, page)).ok, true);
  assert.equal(executions.length, 1);
  assert.equal((await request({type:'KIDTE_SHOPEE_API',request:{action:'hide',refresh:true,shop_id:'10',product_id:'222'}},page)).ok,false);
  assert.equal((await request({type:'KIDTE_SHOPEE_API',request:{action:'hide',refresh:true,shop_id:'10',product_id:'111'}},page)).ok,true);
  executions.pop();
  const originalFetch = context.fetch;
  context.fetch = async (...args) => {
    const response = await originalFetch(...args);
    if (args[0].endsWith("/api/video/timelines")) runtimeState.shopeePaused = true;
    return response;
  };
  assert.equal((await request(show, page)).ok, false, "pause during backend checks cancels the pending pin");
  assert.equal(executions.length, 1);
  runtimeState.shopeePaused = false;
  context.fetch = originalFetch;
  assert.equal((await request({ type: "KIDTE_SHOPEE_BACKEND", resource: "mapping", method: "POST",
    payload: { video_id: "live1", mappings: config.mappings } }, page)).ok, false);
  assert.equal((await request({ type: "KIDTE_SHOPEE_BACKEND", resource: "mapping", method: "POST",
    payload: { video_id: "live1", mappings: config.mappings } },
  { url: "chrome-extension://test/popup.html" })).ok, true);
  assert.equal((await request({ type: "KIDTE_SHOPEE_API", request: { action: "catalog" } },
    { url: "chrome-extension://test/popup.html" })).ok, true);
  assert.equal(executions.length, 2);
  config.aiEnabled = true;
  config.aiAutoSend = false;
  assert.equal((await request({ type: "KIDTE_SHOPEE_API", request: { action: "chat_send",
    turn_id: "turn1", text: "@Mai Dạ TC01 nha" } }, page)).ok, false);
  const generated = await request({ type: "KIDTE_SHOPEE_BACKEND", resource: "reply",
    method: "POST", payload: { session_id: "41590640", video_id: "live1",
      shop_id: "10", product_id: "111" } }, page);
  assert.equal(generated.should_send, true);
  // WP4: queue_enqueue chỉ cho tab điều khiển đúng phiên, đi tới /api/shopee/queue/enqueue.
  const enqueueUrls = [];
  const fetchBeforeQueue = context.fetch;
  context.fetch = async (url, options) => {
    if (url.endsWith('/api/shopee/queue/enqueue')) { enqueueUrls.push([url, JSON.parse(options.body)]);
      return {ok: true, json: async () => ({ok: true, queue_id: 'q1', state: 'queued'})}; }
    return fetchBeforeQueue(url, options);
  };
  const queuePayload = {room_id: '41590640', comment_payload: {comment_id: 'c1', received_at_ms: 777}};
  assert.equal((await request({type: 'KIDTE_SHOPEE_BACKEND', resource: 'queue_enqueue', method: 'POST',
    payload: queuePayload}, page)).queue_id, 'q1');
  assert.equal(enqueueUrls[0][1].comment_payload.received_at_ms, 777);
  assert.equal((await request({type: 'KIDTE_SHOPEE_BACKEND', resource: 'queue_enqueue', method: 'POST',
    payload: {...queuePayload, room_id: 'other'}}, page)).ok, false);
  assert.equal((await request({type: 'KIDTE_SHOPEE_BACKEND', resource: 'queue_enqueue', method: 'POST',
    payload: queuePayload}, {url: 'chrome-extension://test/popup.html'})).ok, false);
  assert.equal(enqueueUrls.length, 1);
  // WP5: queue_next/queue_cancel cùng quyền như queue_enqueue; next kiểm nguồn video, timeout 120s,
  // và đăng ký lượt trả về để chat_send/biên nhận hoạt động như /reply.
  const queueFetches = [], timeouts = [];
  context.AbortSignal = {timeout: ms => { timeouts.push(ms); return undefined; }};
  context.fetch = async (url, options) => {
    if (/\/api\/shopee\/queue\/(next|cancel)$/.test(url)) {
      queueFetches.push([url, JSON.parse(options.body)]);
      return {ok: true, json: async () => url.endsWith('/next')
        ? {ok: true, available: true, queue_id: 'qn', source_comment: {comment_id: 'c1'},
           result: {ok: true, should_send: true, protocol_version: 2, turn_id: 'turn-qn', room_id: '41590640',
             interaction_id: 'i1', messages: [{message_id: 'm-qn', content: '@Mai Dạ', content_hash: 'h'}]}}
        : {ok: true, cancelled: true}};
    }
    return fetchBeforeQueue(url, options);
  };
  const nextPayload = {room_id: '41590640'};
  const nextResult = await request({type: 'KIDTE_SHOPEE_BACKEND', resource: 'queue_next', method: 'POST', payload: nextPayload}, page);
  assert.equal(nextResult.available, true);
  assert.ok(timeouts.includes(120000), 'queue/next phải có timeout 120s');
  assert.equal(runtimeState.shopeePendingReplies['m-qn'].text, '@Mai Dạ', 'lượt từ queue/next được đăng ký');
  assert.equal((await request({type: 'KIDTE_SHOPEE_BACKEND', resource: 'queue_next', method: 'POST',
    payload: {room_id: 'other'}}, page)).ok, false);
  assert.equal((await request({type: 'KIDTE_SHOPEE_BACKEND', resource: 'queue_next', method: 'POST',
    payload: nextPayload}, {url: 'chrome-extension://test/popup.html'})).ok, false);
  assert.equal((await request({type: 'KIDTE_SHOPEE_BACKEND', resource: 'queue_cancel', method: 'POST',
    payload: {room_id: '41590640', comment_id: 'c1'}}, page)).cancelled, true);
  assert.deepEqual(queueFetches.map(item => item[0].split('/api/shopee/')[1]), ['queue/next', 'queue/cancel']);
  detectedVideo = 'other-video';
  assert.equal((await request({type: 'KIDTE_SHOPEE_BACKEND', resource: 'queue_next', method: 'POST', payload: nextPayload}, page)).ok, false,
    'nguồn video đổi thì không gọi queue/next');
  detectedVideo = 'live1';
  assert.equal(queueFetches.length, 2);
  delete context.AbortSignal;
  context.fetch = fetchBeforeQueue;
  config.aiAutoSend = true;
  assert.equal((await request({ type: "KIDTE_SHOPEE_API", request: { action: "chat_send",
    turn_id: "turn1", text: "wrong" } }, page)).ok, false);
  assert.equal((await request({ type: "KIDTE_SHOPEE_API", request: { action: "chat_send",
    turn_id: "turn1", text: "@Mai Dạ TC01 nha" } }, page)).ok, true);
  assert.equal((await request({ type: "KIDTE_SHOPEE_API", request: { action: "chat_send",
    turn_id: "turn1", text: "@Mai Dạ TC01 nha" } }, page)).ok, false);
  vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname, 'background.js'), 'utf8'), {...context});
  assert.equal((await request({type:'KIDTE_SHOPEE_API', request:{action:'chat_send',
    turn_id:'turn1', text:'@Mai Dạ TC01 nha'}}, page)).ok, false,
    'worker restart must not repeat a dispatched chat');
  context.fetch = async (url, options) => url.endsWith('/api/shopee/delivery')
    ? {ok:true, json:async()=>({ok:false,error:'db_busy'})} : originalFetch(url, options);
  vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname, 'background.js'), 'utf8'), {...context});
  const failedAck = await request({type:'KIDTE_SHOPEE_BACKEND',resource:'delivery',method:'POST',
    payload:{session_id:config.sessionId,turn_id:'turn1',message_id:'m1'}}, page);
  assert.equal(failedAck.ok, false);
  assert.equal(runtimeState.shopeePendingReplies.turn1.deliveryPayload.message_id, 'm1',
    'failed receipt persistence must retain the receipt, without sending chat again');
  runtimeState.shopeePendingReplies.turn1.nextReceiptAt = 0;
  context.fetch = originalFetch;
  vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname, 'background.js'), 'utf8'), {...context});
  await request({type:'KIDTE_SHOPEE_BACKEND',resource:'position'}, page);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(runtimeState.shopeePendingReplies.turn1, undefined,
    'worker restart should retry only the saved receipt');
  const popup = {url: "chrome-extension://test/popup.html"};
  assert.equal((await request({type: "KIDTE_SHOPEE_BACKEND", resource: "mapping"}, popup)).videos.live1.mappings.tc01.product_id, "111");
  assert.equal((await request({type: "KIDTE_SHOPEE_API", tabId: 5, request: {action: "catalog"}}, popup)).ok, true);
  assert.equal((await request({...show, tabId: 5}, popup)).ok, false);
  tabsById.set(5,page.tab);
  const connected = await request({type:"KIDTE_SHOPEE_CONNECT"},page);
  assert.equal(connected.controller,true);
  assert.equal(connected.config.videoId,"live1");
  const secondPage={tab:{id:6,url:"https://live.shopee.vn/pc/live?session=999"}};
  tabsById.set(6,secondPage.tab);
  assert.equal((await request({type:"KIDTE_SHOPEE_CONNECT"},secondPage)).controller,false);
  tabsById.delete(5);
  const takeover=await request({type:"KIDTE_SHOPEE_CONNECT"},secondPage);
  assert.equal(takeover.controller,true);
  assert.equal(takeover.config.aiEnabled,false);
  assert.equal(takeover.config.sessionId,"999");
  detectedVideo="live2";
  assert.equal((await request({type:"KIDTE_SHOPEE_CONNECT"},secondPage)).config.videoId,"live2");
  runtimeState.shopeePaused=true;
  assert.equal((await request({type:"KIDTE_SHOPEE_CONNECT"},secondPage)).config.enabled,false);
  assert.equal((await request({type:"KIDTE_SHOPEE_BACKEND",resource:"sync",method:"POST",payload:{session_id:"wrong",products:[]}},secondPage)).ok,false);
  assert.equal((await request({type:"KIDTE_SHOPEE_BACKEND",resource:"sync",method:"POST",payload:{session_id:"999",products:[]}},popup)).ok,false);
  // WP6: lịch thử lại biên nhận với đồng hồ giả: 1,2,5,10,30s rồi 60s, dừng sau 24h nhưng giữ bản ghi.
  {
    let now = 1_700_000_000_000;
    class FakeDate extends Date { static now() { return now; } }
    const alarms = [], alarmListeners = [];
    let deliveryUp = false, deliveryAttempts = 0;
    const receiptContext = {...context, Date: FakeDate, fakeTimers: undefined,
      chrome: {...chrome, alarms: {create: (name, info) => alarms.push({name, ...info}),
        onAlarm: {addListener: fn => alarmListeners.push(fn)}}},
      fetch: async url => {
        if (url.endsWith('/api/shopee/delivery')) {
          deliveryAttempts++;
          if (!deliveryUp) throw new Error('backend unavailable');
        }
        return {ok: true, json: async () => ({ok: true})};
      }};
    const timers = [];
    receiptContext.setTimeout = (fn, ms) => { timers.push({fn, ms}); return timers.length; };
    const record = (extra = {}) => ({sessionId: '41590640', text: 'x', at: now, state: 'sent',
      deliveryPayload: {session_id: '41590640', turn_id: 'x', message_id: 'm'}, nextReceiptAt: now, ...extra});
    const created = now;
    runtimeState.shopeePendingReplies = {
      fresh: record({receiptCreatedAt: created}),
      legacy: record({at: created - 86400000 - 1})   // bản ghi cũ không có receiptCreatedAt: dùng at
    };
    vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname, 'background.js'), 'utf8'), receiptContext);
    const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };
    const alarm = async () => { alarmListeners.at(-1)({name: 'shopee-receipts'}); await settle(); };
    await alarm();
    assert.equal(runtimeState.shopeePendingReplies.legacy.expired_for_reconcile, true, 'bản ghi cũ >24h theo at');
    assert.equal(deliveryAttempts, 1, 'bản ghi hết hạn không được thử');
    const delays = [];
    for (let i = 0; i < 4; i++) {
      const timer = timers.at(-1);
      delays.push(timer.ms);
      now += timer.ms; timer.fn(); await settle();
    }
    assert.deepEqual(delays, [1000, 2000, 5000, 10000], 'bước <30s dùng setTimeout');
    const timerCount = timers.length;
    assert.equal(alarms.at(-1).when, now + 30000);
    now += 30000; await alarm();
    assert.equal(alarms.at(-1).when, now + 60000);
    assert.equal(timers.length, timerCount, 'từ 30s chỉ dùng alarm');
    now += 60000; await alarm();
    assert.equal(alarms.at(-1).when, now + 60000, 'sau đó mỗi 60s');
    now = created + 86400000 - 1000;
    const before = deliveryAttempts; await alarm();
    assert.equal(deliveryAttempts, before + 1);
    now = created + 86400000 + 1;
    const limit = deliveryAttempts; await alarm(); await alarm();
    assert.equal(deliveryAttempts, limit, 'quá 24h không thử nữa');
    assert.equal(runtimeState.shopeePendingReplies.fresh.expired_for_reconcile, true);
    assert.equal(runtimeState.shopeePendingReplies.fresh.deliveryPayload.message_id, 'm', 'giữ bản ghi');
    // Thành công thì xoá.
    runtimeState.shopeePendingReplies.ok = record({receiptCreatedAt: now});
    deliveryUp = true;
    vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname, 'background.js'), 'utf8'), {...receiptContext});
    await alarm();
    assert.equal(runtimeState.shopeePendingReplies.ok, undefined);
  }
  console.log("Shopee background guards passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
