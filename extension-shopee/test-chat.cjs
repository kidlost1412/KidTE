"use strict";
const assert = require("node:assert/strict");
const { runShopeeApi } = require("./shopee-api.js");

global.location = { href: "https://live.shopee.vn/pc/live?session=41590640", hostname: "live.shopee.vn", pathname: "/pc/live" };
const sends = [];
const modules = {
  86: { b: async () => [null, { items: [], has_more: false }] },
  43: { a: "device-test" },
  26: { w: async (session) => [null, { usersig: "internal-test-only",
    session: { session_id: Number(session), chatroom_id: "room1", uid: "host1" } }] },
  3: { i: { NORMAL: 100, Moderator: 102, NORMAL_HOST: 101 } },
  1189: { a: async () => [null, { timestamp: Math.floor(Date.now() / 1000), message: [
    { msgs: [
      { id: "m1", uid: "buyer1", nickname: "Mai", content: JSON.stringify({ type: 100, content: "TC01 dùng sao?" }) },
      { id: "m2", uid: "host1", nickname: "Shop", content: JSON.stringify({ type: 101, content: "Xin chào" }) }
    ] }
  ] }], b: async (request) => { sends.push(request); return [null, { message_id: "sent1" }]; } }
};
global.window = { webpackJsonp: [] };
window.webpackJsonp.push = (payload) => {
  const id = Object.keys(payload[1])[0];
  payload[1][id]({}, {}, (moduleId) => modules[moduleId]);
};

(async () => {
  const read = await runShopeeApi({ action: "chat_poll" });
  assert.equal(read.ok, true);
  assert.deepEqual(read.comments.map((row) => row.comment_id), ["m1"]);
  const originalPoll = modules[1189].a;
  modules[1189].a = async () => [null, {system: {}, timestamp: Math.floor(Date.now()/1000), poll_interval: 2}];
  const empty = await runShopeeApi({action: "chat_poll"});
  assert.equal(empty.ok, true, "Shopee omits message when there are no new comments");
  assert.deepEqual(empty.comments, []);
  modules[1189].a = originalPoll;
  const sent = await runShopeeApi({ action: "chat_send", text: "@Mai Dạ em xem TC01 nha" });
  assert.equal(sent.message_id, "sent1");
  assert.deepEqual(JSON.parse(sends[0].content), { type: 101, content: "@Mai Dạ em xem TC01 nha" });
  assert.equal(sends[0].pin, false);
  assert.equal((await runShopeeApi({ action: "chat_send", text: "x".repeat(151) })).ok, false);

  // Shopee phát hành bundle mới (như 04/10/2026): module 1189 rỗng; module 1720 là lớp
  // bao đăng ký liên tục ({chatroomId, uuid, timestampStart}) — KHÔNG được chọn; hàm
  // thật nằm ở module khác và bị bọc nên mã nguồn hàm xuất ra không có tên trường.
  const realPoll = async (options) => [null, { timestamp: options.timestamp, message: [{ msgs: [
    { id: "m9", uid: "buyer2", nickname: "Lan", content: JSON.stringify({ type: 100, content: "giá sao?" }) }] }] }];
  const realSend = async (options) => { sends.push({ usersig: options.usersig, content: options.content, pin: options.pin });
    return [null, { message_id: "sent2" }]; };
  const wrapped = fn => function () { return fn.apply(this, arguments); };
  const decoyCalls = [];
  const cache = {
    3: { exports: modules[3] }, 26: { exports: modules[26] }, 43: { exports: modules[43] }, 86: { exports: modules[86] },
    6: { exports: { S: () => 0 } },
    1720: { exports: { a: ({chatroomId, uuid, timestampStart = 0} = {}) => { decoyCalls.push(chatroomId); return [() => {}, () => {}]; } } },
    2044: { exports: { k: wrapped(realPoll), q: wrapped(realSend), z: () => 1 } }
  };
  const factories = {
    1720: function (t, e, n) { n.d(e, "a", (function () { return x; })); const x = ({chatroomId: t, uuid: e, timestampStart: n = 0} = {}) => n; },
    2044: function (t, e, n) { n.d(e, "k", (function () { return o; })); n.d(e, "q", (function () { return u; })); n.d(e, "z", (function () { return w; }));
      const w = () => 1, o = async ({chatroomId: t, uuid: e, timestamp: n}) => [t, e, n],
        u = async ({sessionId: t, uuid: e, usersig: n, content: r, pin: i}) => [t, e, n, r, i]; },
    9999: function () { throw new Error("module chưa nạp không được chạy"); }
  };
  const legacy = modules[1189];
  modules[1189] = {};
  window.webpackJsonp.push = (payload) => {
    const id = Object.keys(payload[1])[0];
    const require = (moduleId) => modules[moduleId];
    require.c = cache;
    require.m = factories;
    payload[1][id]({}, {}, require);
  };
  globalThis.__kidteShopeeChat = null;
  const movedRead = await runShopeeApi({ action: "chat_poll" });
  assert.equal(movedRead.ok, true, movedRead.error);
  assert.deepEqual(movedRead.comments.map((row) => row.comment_id), ["m9"]);
  const movedSend = await runShopeeApi({ action: "chat_send", text: "@Lan Dạ em gửi giá nha" });
  assert.equal(movedSend.message_id, "sent2");
  assert.deepEqual(decoyCalls, [], "lớp bao timestampStart không được gọi");
  assert.equal(globalThis.__kidteShopeeChatModule.pollKey, "2044");
  // Ngay sau F5: phần chat chưa nạp. Không được tự chạy module (không gọi require),
  // chỉ báo chờ; khi trang nạp xong thì chạy bình thường.
  const required = [];
  const pageModules = { 3: cache[3], 26: cache[26], 43: cache[43], 86: cache[86] };
  window.webpackJsonp.push = (payload) => {
    const id = Object.keys(payload[1])[0];
    const require = (moduleId) => { required.push(moduleId); return modules[moduleId]; };
    require.c = pageModules;
    require.m = {};
    payload[1][id]({}, {}, require);
  };
  globalThis.__kidteShopeeChatModule = null;
  globalThis.__kidteShopeeChat = null;
  const early = await runShopeeApi({ action: "chat_poll" });
  assert.equal(early.ok, false);
  assert.match(early.error, /chưa nạp xong/);
  assert.deepEqual(required, [], "không require module trang chưa nạp");
  pageModules[1189] = { exports: legacy };
  const later = await runShopeeApi({ action: "chat_poll" });
  assert.equal(later.ok, true, later.error);
  modules[1189] = legacy;
  console.log("Shopee chat API tests passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
