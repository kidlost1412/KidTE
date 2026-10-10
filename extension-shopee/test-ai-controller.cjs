"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

async function scenario(autoSend, disableDuringReply = false, pin = "20", transientFailure = false, receiptFailure = false, sendFailure = false, slowReply = false, v2 = false, queueApi = false) {
  const queueCalls = []; let queueFail = queueApi;
  const config = { aiEnabled: true, aiAutoSend: autoSend, sessionId: "123",
    videoId: "video1", tabId: 5, backendQueueMirror: queueApi };
  let tick, clock = 100000, inferenceCalls = 0, receiptCalls = 0, sendCalls = 0;
  class TestDate extends Date {static now(){return clock;}}
  const calls = [];
  const v2Messages = [1,2,3].map(n=>({message_id:'internal-'+n,content:'@Mai Tin '+n,content_hash:'hash-'+n,state:'ready'}));
  const saved = {};
  const chrome = { storage: { local: {
    async get() { return { shopeeConfig: config }; },
    async set(value) { Object.assign(saved, value); }
  } }, runtime: { id: "test", async sendMessage(message) {
    calls.push(message);
    if(message.resource==='ai_status') return {ok:true,capabilities:{multi_message_reply:true,...(queueApi?{comment_queue_api:true}:{})}};
    if(message.resource==='queue_enqueue'){
      queueCalls.push(message.payload);
      if(queueFail){queueFail=false;throw Error('backend down');}
      return {ok:true,queue_id:'q-'+message.payload.comment_payload.comment_id,state:'queued'};
    }
    if(message.resource==='heartbeat') return {ok:true};
    if(message.type==='KIDTE_SHOPEE_MESSAGE') {
      const row=v2Messages.find(row=>row.message_id===message.payload.message_id);
      if(message.operation==='status') return {ok:true,turn:{messages:v2Messages}};
      if(message.operation==='claim') return {ok:true,claim_token:'lease',content:row.content,content_hash:row.content_hash};
      if(message.operation==='receipt'){row.state='sent';receiptCalls++;return {ok:true};}
      return {ok:true};
    }
    if (message.type === "KIDTE_SHOPEE_API") {
      if (message.request.action === "chat_poll") return { ok: true, comments: [
        { comment_id: "c1", user_id: "u1", nickname: "Mai", comment: "TC01 dùng sao?" },
        ...(sendFailure ? [{comment_id: "c2", user_id: "u2", nickname: "Lan", comment: "TC01 giá?", attempts: 3}] : [])
      ] };
      if (message.request.action === "state") return {ok:true,available:true,shop_id:pin?"10":"",product_id:pin};
      if (message.request.action === "catalog") return { ok: true, products: [
        { shop_id: "10", product_id: "20", title: "Kích Rễ TC01" }
      ] };
      if (message.request.action === "chat_send") {
        sendCalls++;
        if (sendFailure && sendCalls === 1) throw Error("send connection lost");
        return { ok: true, message_id: "m1" };
      }
    }
    if (message.resource === "position") return { ok: true, available: true, video_id:"video1" };
    if (message.resource === "timelines") return { ok: true, timelines: {} };
    if (message.resource === "mapping") return { ok: true, videos: { video1: { mappings: {} } } };
    if (message.resource === "reply") {
      inferenceCalls++;
      if (transientFailure && inferenceCalls===1) throw Error("model temporarily unavailable");
      assert.equal(message.payload.product_id,pin);
      if (disableDuringReply) config.aiEnabled=false;
      if (slowReply) clock += 121000;
      return { ok: true, should_send: true, turn_id: "turn1", reply: "@Mai Dạ TC01 nha",
        ...(v2?{protocol_version:2,room_id:'123',messages:v2Messages}: {}) };
    }
    if (message.resource === "delivery") {
      receiptCalls++;
      if (receiptFailure && receiptCalls === 1) throw Error("receipt connection lost");
      return { ok: true, sent: Boolean(message.payload.message_id) };
    }
    throw Error(`Unexpected call: ${JSON.stringify(message)}`);
  } } };
  const context = { chrome, URL, location: { href: "https://live.shopee.vn/pc/live?session=123" },
    Date:TestDate, setInterval(fn) {tick=fn;}, KidTEShopeeCore: { liveSession: require("./core.js").liveSession, desired: () => ({ ok: true, action: "show",
      shop_id: "10", product_id: "20" }) } };
  if(v2) vm.runInNewContext(fs.readFileSync(path.join(__dirname,'message-delivery.js'),'utf8'),context);
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "ai-controller.js"), "utf8"), context);
  await new Promise((resolve) => setTimeout(resolve, 30));
  if (transientFailure) {
    assert.equal(calls.some(x=>x.request?.action==='chat_send'),false);
    clock+=11000;await tick();await new Promise(resolve=>setImmediate(resolve));
    assert.equal(calls.filter(x=>x.resource==='reply').at(-1).payload.retry_unanswered,true);
  }
  if (sendFailure) {
    clock+=11000;await tick();await new Promise(resolve=>setImmediate(resolve));
    assert.equal(calls.filter(x=>x.resource==='reply').at(-1).payload.comment_id, 'c2');
  }
  assert.equal(calls.filter(x=>x.request?.action==='chat_send').length,disableDuringReply||slowReply?0:v2?3:sendFailure?2:1);
  if (receiptFailure) assert.equal(receiptCalls, 2);
  assert.equal(calls.some((x) => v2 ? x.type==='KIDTE_SHOPEE_MESSAGE'&&x.operation==='receipt' : x.resource === "delivery"), true);
  if (slowReply) assert.equal(calls.find(x=>x.resource==='delivery').payload.error, 'expired before send');
  else if (!disableDuringReply) assert.match(saved.shopeeAiStatus.label, /Đã gửi/);
  else assert.equal(calls.find(x=>x.resource==='delivery').payload.error, 'AI disabled before send');
  assert.equal(saved.shopeeAiDraft, undefined);
  if (queueApi) {
    // Lần đầu lỗi, nhịp sau gửi lại với đúng received_at_ms gốc (100000).
    assert.ok(queueCalls.length >= 2, 'queue enqueue must be retried after failure');
    assert.equal(queueCalls[0].room_id, '123');
    assert.equal(queueCalls[0].comment_payload.comment_id, 'c1');
    assert.equal(queueCalls[0].comment_payload.received_at_ms, 100000);
    assert.equal(queueCalls[1].comment_payload.received_at_ms, 100000);
    assert.equal(queueCalls.filter(x=>x.comment_payload.comment_id==='c1').length, 2, 'no resend once marked');
  } else {
    assert.equal(queueCalls.length, 0, 'without comment_queue_api nothing is enqueued');
    assert.equal(calls.some(x => x.resource === 'queue_next' || x.resource === 'queue_cancel'), false,
      'OFF: không đụng queue/next, queue/cancel');
  }
}

// WP5: đường gửi qua backend queue/next (cùng cờ backendQueueMirror).
async function backendNext() {
  const config = { aiEnabled: true, aiAutoSend: true, sessionId: "123", videoId: "video1", tabId: 5, backendQueueMirror: true };
  let tick, clock = 100000, nextScript = [];
  const polls = [[{comment_id: 'c1', user_id: 'u1', nickname: 'Mai', comment: 'TC01 dùng sao?'}]];
  class TestDate extends Date {static now(){return clock;}}
  const calls = [], saved = {}, enqueued = [];
  let claimVerdict = '';
  const msg = {message_id: 'internal-1', content: '@Mai Tin 1', content_hash: 'hash-1', state: 'ready'};
  const turn = id => ({ok: true, available: true, queue_id: 'q-' + id,
    result: {ok: true, should_send: true, protocol_version: 2, turn_id: 'turn-' + id, room_id: '123', messages: [msg]},
    source_comment: {comment_id: id}});
  const chrome = { storage: { local: {
    async get() { return { shopeeConfig: config }; },
    async set(value) { Object.assign(saved, value); }
  } }, runtime: { id: "test", async sendMessage(message) {
    calls.push(message);
    if (message.resource === 'ai_status') return {ok: true, capabilities: {multi_message_reply: true, comment_queue_api: true}};
    if (message.resource === 'queue_enqueue') { enqueued.push(message.payload.comment_payload); return {ok: true, queue_id: 'q', state: 'queued'}; }
    if (message.resource === 'queue_cancel') return {ok: true};
    if (message.type === 'KIDTE_SHOPEE_API' && message.request.action === 'catalog') {
      return {ok: true, products: [{shop_id: '10', product_id: '20', title: 'Kích Rễ TC01'}]};
    }
    if (message.type === 'KIDTE_SHOPEE_API' && message.request.action === 'state') {
      return {ok: true, available: true, shop_id: '10', product_id: '20'};
    }
    if (message.resource === 'queue_next') {
      const step = nextScript.shift();
      if (step instanceof Error) throw step;
      return step || {ok: true, available: false};
    }
    if (message.resource === 'heartbeat') return {ok: true};
    if (message.resource === 'position') return {ok: true, available: true, video_id: 'video1'};
    if (message.type === 'KIDTE_SHOPEE_MESSAGE') {
      if (message.operation === 'status') return {ok: true, turn: {messages: [msg]}};
      if (message.operation === 'claim' && claimVerdict) return {ok: false, error: claimVerdict, recompose: true};
      if (message.operation === 'claim') return {ok: true, claim_token: 'lease', content: msg.content, content_hash: msg.content_hash};
      if (message.operation === 'receipt') { msg.state = 'sent'; return {ok: true}; }
      return {ok: true};
    }
    if (message.type === 'KIDTE_SHOPEE_API') {
      if (message.request.action === 'chat_poll') return {ok: true, comments: polls.shift() || []};
      if (message.request.action === 'chat_send') return {ok: true, message_id: 'm1'};
    }
    throw Error(`Unexpected call: ${JSON.stringify(message)}`);
  } } };
  const context = { chrome, URL, location: { href: "https://live.shopee.vn/pc/live?session=123" },
    Date: TestDate, setInterval(fn) {tick = fn;}, KidTEShopeeCore: { liveSession: require("./core.js").liveSession } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'message-delivery.js'), 'utf8'), context);
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "ai-controller.js"), "utf8"), context);
  const settle = () => new Promise(resolve => setTimeout(resolve, 30));
  const step = async () => { clock += 11000; await tick(); await settle(); };
  const sends = () => calls.filter(x => x.request?.action === 'chat_send').length;
  const rows = () => [...saved.shopeeAiQueue.rows.map(row => row.comment_id)];
  await settle();
  assert.equal(calls.some(x => x.resource === 'reply'), false, 'BẬT: không gọi /reply cục bộ');
  // available:false -> không làm gì, giữ hàng.
  assert.equal(sends(), 0);
  assert.deepEqual(rows(), ['c1']);
  const first = calls.findIndex(x => x.resource === 'queue_next');
  assert.ok(first > 0 && calls[first - 1].resource === 'heartbeat', 'heartbeat ngay trước next');
  assert.equal(calls[first].payload.room_id, '123');

  // Lỗi mạng và delivery_control_inactive: không gửi cục bộ, giữ hàng.
  nextScript = [new Error('net down')];
  await step();
  nextScript = [{ok: false, error: 'delivery_control_inactive'}];
  await step();
  assert.equal(sends(), 0);
  assert.equal(calls.some(x => x.resource === 'reply'), false);
  assert.deepEqual(rows(), ['c1']);
  assert.match(saved.shopeeAiLastActivity.error, /delivery_control_inactive/);

  // Mirror carries the same shop/product context as the local /reply call.
  const mirror = enqueued.find(item => item.comment_id === 'c1');
  assert.deepEqual({shop_id: mirror.shop_id, product_id: mirror.product_id, title: mirror.title},
    {shop_id: '10', product_id: '20', title: 'Kích Rễ TC01'});
  assert.equal(mirror.video_id, 'video1');

  // Lượt phục hồi gửi đúng một lần rồi xoá hàng cục bộ khớp comment_id.
  nextScript = [{...turn('c1'), recovered: true}];
  await step();
  assert.equal(sends(), 1);
  assert.deepEqual(rows(), []);
  await step();
  assert.equal(sends(), 1, 'không gửi lại');

  // B06: claim says revision_changed -> stop, nothing sent, no unknown receipt, no error.
  polls.push([{comment_id: 'rv1', user_id: 'u9', nickname: 'Vân', comment: 'TC01 giá?'}]);
  await step();
  claimVerdict = 'revision_changed';
  const mark = calls.length;
  msg.state = 'ready';
  nextScript = [turn('rv1')];
  await step();
  claimVerdict = '';
  assert.equal(sends(), 1, 'nothing sent for the stopped turn');
  assert.equal(calls.slice(mark).some(x => x.type === 'KIDTE_SHOPEE_MESSAGE' && ['dispatched', 'failure'].includes(x.operation)), false);
  assert.match(saved.shopeeAiLastActivity.label, /Dữ kiện đã đổi/);
  assert.deepEqual(rows(), []);

  // Hết hạn / huỷ từ backend: không gửi, xoá hàng cục bộ.
  polls.push([{comment_id: 'c2', user_id: 'u2', nickname: 'Lan', comment: 'TC01 giá?'}]);
  nextScript = [{ok: true, available: true, queue_id: 'q-c2',
    result: {ok: true, should_send: false, reason: 'expired'}, source_comment: {comment_id: 'c2'}}];
  await step();
  assert.equal(sends(), 1);
  assert.deepEqual(rows(), []);

  // Hàng bỏ cục bộ (hết hạn) khi BẬT phải huỷ ở backend.
  assert.equal(calls.some(x => x.resource === 'queue_cancel'), false);
  polls.push([{comment_id: 'c3', user_id: 'u3', nickname: 'Hoa', comment: 'TC01 mua sao?'}]);
  await step();
  assert.deepEqual(rows(), ['c3']);
  clock += 130000;
  await tick(); await settle();
  assert.deepEqual(rows(), []);
  const cancel = calls.filter(x => x.resource === 'queue_cancel');
  assert.equal(cancel.length, 1);
  assert.deepEqual({...cancel[0].payload}, {room_id: '123', comment_id: 'c3'});
  console.log("Shopee backend queue/next path passed");
}

async function privacyPurge() {
  const config = { aiEnabled: true, aiAutoSend: false, sessionId: "123", videoId: "video1", tabId: 5 };
  const saved = {
    shopeePinLog: [{ action: "show", note: "Nguyen Van Nong Dan" }, { action: "show", note: "khach khac" }],
    shopeeAiStatus: { ok: true, label: "Đang xử lý bình luận thật của Nguyễn Văn Nông Dân", at: 1 }
  };
  const calls = [];
  const chrome = { storage: { local: {
    async get() { return { shopeeConfig: config, ...saved }; },
    async set(value) { Object.assign(saved, value); }
  } }, runtime: { id: "test", async sendMessage(message) {
    calls.push(message);
    if (message.resource === "privacy_purge_ack") return { ok: true };
    if (message.resource === "ai_status") return { ok: true, capabilities: {} };
    if (message.type === "KIDTE_SHOPEE_API" && message.request.action === "chat_poll") return { ok: true, comments: [
      { comment_id: "p1", user_id: "nongdan9999", nickname: "Nguyễn Văn Nông Dân", comment: "giá sao" },
      { comment_id: "p2", user_id: "u2", nickname: "Hoa Binh", comment: "ship nào" }] };
    return { ok: true, available: false };
  } } };
  let tick;
  const context = { chrome, URL, location: { href: "https://live.shopee.vn/pc/live?session=123" },
    setInterval(fn) { tick = fn; }, KidTEShopeeCore: require("./core.js") };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "ai-controller.js"), "utf8"), context);
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(typeof context.KidTEShopeeAiPurge, "function");
  const done = await context.KidTEShopeeAiPurge([
    { id: "purge-1", platform: "shopee", identifiers: ["nguyen van nong dan", "nongdan9999"] },
    { id: "bad", platform: "shopee", identifiers: [] }]);
  assert.deepEqual([...done], ["purge-1"]);
  assert.equal(saved.shopeePinLog.length, 1);
  assert.equal(saved.shopeePinLog[0].note, "khach khac");
  assert.equal(JSON.stringify(saved.shopeeAiStatus).includes("Nông Dân"), false);
  assert.equal(JSON.stringify(saved.shopeeAiQueue || {}).includes("nongdan9999"), false);
  for (const row of saved.shopeeAiQueue?.rows || []) assert.notEqual(row.comment_id, "p1");
  const ack = calls.filter(x => x.resource === "privacy_purge_ack");
  assert.equal(ack.length, 1);
  assert.equal(ack[0].payload.items[0].id, "purge-1");
  console.log("Shopee privacy purge passed");
}

(async () => {
  await scenario(false);
  await scenario(true);
  await scenario(true, true);
  await scenario(true, false, "");
  await scenario(true, false, "20", true);
  await scenario(true, false, "20", false, true);
  await scenario(true, false, "20", false, false, true);
  await scenario(true, false, "20", false, false, false, true);
  await scenario(true, false, '20', false, false, false, false, true);
  await backendNext();
  await privacyPurge();
  console.log("Shopee AI real delivery and legacy setting migration passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
