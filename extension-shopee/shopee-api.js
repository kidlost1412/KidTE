"use strict";

// Serialized by chrome.scripting.executeScript into the Shopee tab's MAIN world.
// Uses the page's initialized API client for products and LIVE chat.
async function runShopeeApi(request) {
  const sessionId = new URL(location.href).searchParams.get("session") || "";
  if (!/^\d{1,20}$/.test(sessionId) || location.hostname !== "live.shopee.vn") {
    return { ok: false, error: "Không ở trang quản lý một phiên Shopee LIVE" };
  }
  if (request?.expected_session_id && request.expected_session_id !== sessionId) {
    return {ok: false, error: "Phiên Shopee đã đổi; đang tự kết nối lại"};
  }
  if (["show", "hide", "chat_poll", "chat_send"].includes(request?.action) &&
      location.pathname.replace(/\/+$/, "") !== "/pc/live") {
    return {ok: false, error: "Phiên chưa LIVE; chỉ chuẩn bị giỏ"};
  }
  if (!["catalog", "state", "show", "hide", "chat_poll", "chat_send"].includes(request?.action)) {
    return { ok: false, error: "Thao tác Shopee không hợp lệ" };
  }
  const chunks = window.webpackJsonp;
  if (!Array.isArray(chunks) || typeof chunks.push !== "function") {
    return { ok: false, error: "Shopee LIVE bundle chưa sẵn sàng" };
  }
  let requireFn;
  const id = `kidte_shopee_probe_${Date.now()}_${Math.random().toString(36).slice(2)}`;
  // Webpack 4's entry callback receives the page's require function. The
  // extension passes no credentials and uses Shopee's own initialized client.
  chunks.push([[id], { [id]: (_module, _exports, require) => { requireFn = require; } }, [[id, 15]]]);
  if (typeof requireFn !== "function") {
    return { ok: false, error: "Không lấy được Shopee LIVE runtime" };
  }
  // Chỉ đọc module trang đã nạp. require() một module chưa nạp sẽ chạy nó quá sớm
  // (ví dụ ngay sau F5) và có thể để lại bản rỗng trong cache của trang.
  const loaded = moduleId => {
    if (!requireFn.c) return requireFn(moduleId); // môi trường test không có cache
    return requireFn.c[moduleId] ? requireFn.c[moduleId].exports : undefined;
  };
  const api = loaded(86);
  const list = api?.b;
  // Read the current committed UI tree. Shopee updates showingProduct after
  // its SESSION_SHOW_ITEM event and item detail request have completed.
  function displayedProduct() {
    if (typeof document === "undefined") return { available: false };
    for (const element of document.querySelectorAll("div")) {
      const root = element._reactRootContainer?._internalRoot?.current;
      if (!root) continue;
      const queue = [root];
      let visited = 0;
      while (queue.length && visited++ < 20000) {
        const node = queue.pop();
        const props = node?.memoizedProps;
        if (String(props?.sessionId) === sessionId &&
            Object.prototype.hasOwnProperty.call(props, "showingProduct")) {
          const item = props.showingProduct;
          if (item == null) return { available: true, product_id: "", shop_id: "" };
          const productId = String(item.itemId ?? item.item_id ?? "");
          const shopId = String(item.shopId ?? item.shop_id ?? "");
          if (/^\d+$/.test(productId) && /^\d+$/.test(shopId)) {
            return { available: true, product_id: productId, shop_id: shopId,
              title: String(item.name || "").slice(0, 500) };
          }
          return { available: false };
        }
        if (node?.child) queue.push(node.child);
        if (node?.sibling) queue.push(node.sibling);
      }
    }
    return { available: false };
  }
  async function confirmation(productId = "", shopId = "") {
    for (let attempt = 0; attempt < 16; attempt++) {
      if (new URL(location.href).searchParams.get("session") !== sessionId) return { confirmed: false };
      const state = displayedProduct();
      if (!state.available) return { confirmed: false };
      if (state.product_id === productId && state.shop_id === shopId) {
        return { confirmed: true, confirmation_source: "shopee_ui", displayed: state };
      }
      if (attempt < 15) await new Promise(resolve => setTimeout(resolve, 200));
    }
    return { confirmed: false, displayed: displayedProduct() };
  }
  // Shopee đổi số module mỗi lần phát hành bundle (1189 -> số khác), khác nhau giữa
  // các máy/shop. Không dựa vào số: nhận hàm theo NỘI DUNG định nghĩa.
  //  - đọc bình luận một lần: ({chatroomId, uuid, timestamp}) — KHÔNG phải lớp bao
  //    đăng ký liên tục ({chatroomId, uuid, timestampStart}) của module khác;
  //  - gửi bình luận: ({sessionId, uuid, usersig, content, pin}).
  // Hàm xuất ra có thể bị bọc, nên đọc cả mã factory của module: webpack khai báo
  // n.d(exports, "a", function(){return X}) rồi định nghĩa X ở chỗ khác trong factory.
  // Trả về {a: poll, b: send} như module cũ để phần dưới không đổi.
  function classifyChatSource(source) {
    if (/chatroomId/.test(source) && /\btimestamp\b/.test(source) && !/timestampStart/.test(source)) return "poll";
    if (/usersig/.test(source) && /\bcontent\b/.test(source)) return "send";
    return "";
  }
  function factoryChatExports(factorySource) {
    const found = {};
    const exportMap = /\.d\(\s*\w+\s*,\s*"(\w+)"\s*,\s*\(?\s*(?:function\s*\(\)\s*\{\s*return\s+|\(\)\s*=>\s*)([\w$]+)/g;
    for (const [, exportName, local] of factorySource.matchAll(exportMap)) {
      const escaped = local.replace(/\$/g, "\\$");
      const definition = new RegExp(`(?:^|[,;{}\\s(])(?:async\\s+)?(?:function\\s+${escaped}\\s*\\(|${escaped}\\s*=(?!=))`).exec(factorySource);
      if (!definition) continue;
      const kind = classifyChatSource(factorySource.slice(definition.index, definition.index + 500));
      if (kind && !found[kind]) found[kind] = exportName;
    }
    return found;
  }
  function findChatModule(require) {
    // Chỉ dùng module trang đã nạp: require() một module chưa nạp sẽ chạy mã khởi tạo của nó.
    const pick = (key, name) => {
      try {
        const exports = require.c ? require.c[key]?.exports : require(key); // không có cache: môi trường test
        const fn = exports?.[name];
        return typeof fn === "function" ? fn : null;
      } catch { return null; }
    };
    const cached = globalThis.__kidteShopeeChatModule;
    if (cached) {
      const poll = pick(cached.pollKey, cached.pollName), send = pick(cached.sendKey, cached.sendName);
      if (poll && send) return { a: poll, b: send };
    }
    // Không gọi require(1189) khi trang chưa nạp module chat: chạy sớm (ngay sau F5) để lại
    // một module rỗng trong cache và AI kẹt "Phiên thiếu phòng bình luận" tới lần tải lại.
    if (pick(1189, "a") && pick(1189, "b")) return { a: pick(1189, "a"), b: pick(1189, "b") };
    let poll = null, send = null;
    // 1) Mã nguồn hàm xuất ra (khi không bị bọc).
    for (const key of Object.keys(require.c || {})) {
      const exports = require.c[key]?.exports;
      if (!exports || (typeof exports !== "object" && typeof exports !== "function")) continue;
      for (const [name, value] of Object.entries(exports)) {
        if (typeof value !== "function") continue;
        let source = "";
        try { source = Function.prototype.toString.call(value); } catch { continue; }
        const kind = classifyChatSource(source);
        if (kind === "poll" && !poll) poll = { key, name };
        if (kind === "send" && !send) send = { key, name };
      }
      if (poll && send) break;
    }
    // 2) Mã factory của module (đúng cả khi hàm xuất ra bị bọc).
    if (!poll || !send) {
      for (const key of Object.keys(require.m || {})) {
        let source = "";
        try { source = Function.prototype.toString.call(require.m[key]); } catch { continue; }
        if (!/chatroomId|usersig/.test(source)) continue;
        const found = factoryChatExports(source);
        if (found.poll && !poll && pick(key, found.poll)) poll = { key, name: found.poll };
        if (found.send && !send && pick(key, found.send)) send = { key, name: found.send };
        if (poll && send) break;
      }
    }
    if (!poll || !send) return null;
    globalThis.__kidteShopeeChatModule = { pollKey: poll.key, pollName: poll.name, sendKey: send.key, sendName: send.name };
    return { a: pick(poll.key, poll.name), b: pick(send.key, send.name) };
  }
  if (request.action === "state") return { ok: true, session_id: sessionId, ...displayedProduct() };
  if (request.action === "chat_poll" || request.action === "chat_send") {
    const chat = findChatModule(requireFn);
    const uuid = loaded(43)?.a;
    const preview = loaded(26)?.w;
    const types = loaded(3)?.i;
    if (!uuid || typeof preview !== "function" || !chat || !types) {
      // Thường là trang vừa tải, Shopee chưa nạp phần chat: chờ nhịp sau, không phải lỗi API.
      return { ok: false, error: "Shopee chưa nạp xong phần bình luận; đang chờ" };
    }
    const state = globalThis.__kidteShopeeChat || (globalThis.__kidteShopeeChat = {});
    if (state.sessionId !== sessionId) {
      state.sessionId = sessionId;
      state.timestamp = Math.floor(Date.now() / 1000) - 2;
      state.previewData = null;
    }
    if (!state.previewData || Date.now() - state.previewAt > 30000) {
      const previewResult = await preview(sessionId, uuid);
      const [previewError, previewData] = Array.isArray(previewResult)
        ? previewResult : [new Error("preview_invalid")];
      if (previewError || !previewData?.session || !previewData.usersig ||
          String(previewData.session.session_id) !== sessionId) {
        return { ok: false, error: "Shopee không xác nhận phiên LIVE" };
      }
      state.previewData = previewData;
      state.previewAt = Date.now();
    }
    const previewData = state.previewData;
    if (request.action === "chat_send") {
      const body = String(request.text || "").trim();
      if (!body || body.length > 150 || typeof chat.b !== "function") {
        return { ok: false, error: "Câu Shopee không hợp lệ" };
      }
      const content = JSON.stringify({ type: types.NORMAL_HOST, content: body });
      const sent = await chat.b({ sessionId, uuid, usersig: previewData.usersig,
        content, pin: false });
      const [error, data] = Array.isArray(sent) ? sent : [new Error("send_invalid")];
      if (error || !data?.message_id) return { ok: false, error: String(error?.message || "Shopee chưa xác nhận gửi") };
      return { ok: true, message_id: String(data.message_id), session_id: sessionId };
    }
    const chatroomId = previewData.session.chatroom_id;
    if (!chatroomId || typeof chat.a !== "function") return { ok: false, error: "Phiên thiếu phòng bình luận" };
    const received = await chat.a({ chatroomId, uuid, timestamp: state.timestamp });
    const [error, data] = Array.isArray(received) ? received : [new Error("poll_invalid")];
    if (error || !data || !Number.isFinite(Number(data.timestamp)) ||
        (data.message != null && !Array.isArray(data.message))) {
      return { ok: false, error: String(error?.message || "Shopee không trả bình luận") };
    }
    const next = Number(data.timestamp);
    if (Number.isFinite(next) && next >= state.timestamp) state.timestamp = next;
    const comments = [];
    for (const group of data.message || []) for (const row of group?.msgs || []) {
      let content;
      try { content = JSON.parse(row.content || "{}"); } catch { continue; }
      if (![types.NORMAL, types.Moderator].includes(content.type)) continue;
      if (!row.id || !row.uid || !content.content ||
          String(row.uid) === String(previewData.session.uid)) continue;
      comments.push({ comment_id: String(row.id), user_id: String(row.uid),
        nickname: String(row.nickname || "Khách").slice(0, 100),
        comment: String(content.content).slice(0, 600),
        shop_id: String(content.shop_id || ""), product_id: String(content.item_id || "") });
    }
    return { ok: true, session_id: sessionId, comments: comments.slice(-30) };
  }
  if (typeof list !== "function") {
    return { ok: false, error: "Shopee đổi module danh sách sản phẩm" };
  }

  // Hiding also requires the exact active session, but no item payload.
  if (request.action === "hide") {
    if (typeof api.o !== "function") return { ok: false, error: "Shopee đổi API bỏ hiển thị" };
    const response = await api.o(sessionId);
    const [error] = Array.isArray(response) ? response : [new Error("response_invalid")];
    return error ? { ok: false, error: String(error.message || error) }
      : { ok: true, action: "hide", session_id: sessionId, accepted: true, ...await confirmation() };
  }

  const products = [];
  let targetItem = null;
  const seen = new Set();
  let offset = 0;
  let totalCount = 0;
  for (let page = 0; page < 10; page += 1) {
    const response = await list({ sessionId, visible: true, offset, limit: 50, useNew: true });
    const [error, data] = Array.isArray(response) ? response : [new Error("response_invalid"), null];
    if (error || !data || !Array.isArray(data.items)) {
      return { ok: false, error: String(error?.message || "Shopee không trả danh sách sản phẩm") };
    }
    totalCount = Number(data.total_count || totalCount || 0);
    for (const item of data.items) {
      if (!item || typeof item !== "object") continue;
      const productId = String(item.item_id ?? item.itemId ?? item.id ?? "").trim();
      const shopId = String(item.shop_id ?? item.shopId ?? "").trim();
      const key = `${shopId}:${productId}`;
      if (!productId || seen.has(key)) continue;
      seen.add(key);
      if (request.action === "show" && productId === String(request.product_id || "")
          && shopId === String(request.shop_id || "")) targetItem = item;
      products.push({
        platform: "shopee",
        product_id: productId,
        shop_id: shopId,
        title: String(item.title ?? item.name ?? "").trim().slice(0, 500),
        bag_index: Number(item.sequence_no ?? item.item_no ?? products.length + 1) || products.length + 1,
        price: String(item.display_price ?? (typeof item.price === 'object' ? '' : item.price) ?? "").slice(0, 100),
        price_data: Object.fromEntries(Object.entries(item).filter(([key]) => /^(?:price|price_min|price_max|price_min_before_discount|price_max_before_discount|original_price|display_price|currency|price_before_discount)$/.test(key)))
      });
    }
    const next = Number(data.next_offset);
    if (!data.has_more || !data.items.length || !Number.isFinite(next) || next <= offset) break;
    offset = next;
  }
  if (request.action === "show") {
    if (!/^\d+$/.test(String(request.product_id || "")) || !/^\d+$/.test(String(request.shop_id || ""))) {
      return { ok: false, error: "Thiếu ID sản phẩm và ID shop Shopee hợp lệ" };
    }
    if (!targetItem) return { ok: false, error: "Sản phẩm đã ánh xạ không còn trong giỏ Shopee" };
    if (typeof api.s !== "function") return { ok: false, error: "Shopee đổi API hiển thị" };
    const response = await api.s(sessionId, targetItem);
    const [error] = Array.isArray(response) ? response : [new Error("response_invalid")];
    return error ? { ok: false, error: String(error.message || error) }
      : { ok: true, action: "show", session_id: sessionId, product_id: String(request.product_id),
        shop_id: String(request.shop_id), accepted: true,
        ...await confirmation(String(request.product_id), String(request.shop_id)) };
  }
  return { ok: true, platform: "shopee", session_id: sessionId,
    product_count: products.length, total_count: totalCount, products };
}

globalThis.KidTEShopeeApi = runShopeeApi;
if (typeof module !== "undefined" && module.exports) module.exports = { runShopeeApi };
