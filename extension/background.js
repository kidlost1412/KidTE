"use strict";

// Nhật ký chẩn đoán captcha (diag-log.js): không bao giờ ném lỗi, không chặn luồng chính.
let diag = {record() {}, event() {}, api() {}};
try {
  if (typeof importScripts === "function") importScripts("diag-log.js");
  diag = globalThis.KidTEDiag || (typeof require === "function" ? require("./diag-log.js") : diag);
  diag.initBackground();
} catch (error) { console.warn("Kid TE: không bật được nhật ký chẩn đoán", error); }

let receiptQueue = Promise.resolve();
let chatIntentQueue = Promise.resolve();
function reserveChatIntent(request, tabId) {
  const task = chatIntentQueue.then(async () => {
    const saved = await chrome.storage.local.get(['liveReChatIntents', 'liveReAiEnabled', 'liveReState']);
    const state = saved.liveReState || {};
    if (saved.liveReAiEnabled !== true || state.running !== true || Number(state.controllerTabId) !== tabId) {
      return {ok: false, error: 'AI hoặc quyền điều khiển đã tắt'};
    }
    const intents = saved.liveReChatIntents || {};
    const id = request.delivery.message_id || request.delivery.reply_id;
    if (intents[id]) return intents[id].receipt?.ok
      ? {ok:true,replayed:true,receipt:intents[id].receipt}
      : {ok: false, error: 'Lượt này đã phát lệnh gửi; cần đối soát, không gửi lại'};
    intents[id] = {state: 'sending', at: Date.now(), content: request.content,
      room_id: request.expected_room_id, ...request.delivery};
    await chrome.storage.local.set({liveReChatIntents: intents});
    return {ok: true};
  });
  chatIntentQueue = task.catch(() => {});
  return task;
}
function completeChatIntent(request, result) {
  const task = chatIntentQueue.then(async () => {
    const saved = await chrome.storage.local.get('liveReChatIntents');
    const intents = saved.liveReChatIntents || {};
    const id = request.delivery.message_id || request.delivery.reply_id;
    if (!intents[id]) throw new Error('Thiếu bản ghi ý định gửi');
    intents[id] = {...intents[id], state: result?.ok ? 'sent' : 'unknown', receipt: result, receipt_recorded: false};
    await chrome.storage.local.set({liveReChatIntents: intents});
    return intents[id];
  });
  chatIntentQueue = task.catch(() => {});
  return task;
}
const receiptUrls = {
  LIVE_RE_AI_ACK: 'http://127.0.0.1:8793/api/reply/ack',
  LIVE_RE_AI_DELIVERY: 'http://127.0.0.1:8793/api/reply/delivery',
  LIVE_RE_MESSAGE_ACK: 'http://127.0.0.1:8793/api/reply/receipt',
  LIVE_RE_MESSAGE_UNKNOWN: 'http://127.0.0.1:8793/api/reply/failure'
};
// Lịch thử lại receipt: 1,2,5,10,30s rồi mỗi 60s, tối đa 24h kể từ lúc tạo bản ghi.
// chrome.alarms bị kẹp ~30s trong MV3 nên bước <30s dùng setTimeout (chỉ trong bộ
// nhớ), alarm vẫn là lưới an toàn khi service worker bị tắt.
const RECEIPT_MAX_AGE_MS = 86400000;
const receiptDelay = attempt => [1000, 2000, 5000, 10000, 30000][attempt - 1] || 60000;
const receiptTimers = new Map();
function scheduleReceiptRetry(id, record) {
  const delay = Math.max(0, record.nextAt - Date.now());
  chrome.alarms?.create('tiktok-receipts', {when: record.nextAt});
  if (delay < 30000) {
    clearTimeout(receiptTimers.get(id));
    receiptTimers.set(id, setTimeout(() => { receiptTimers.delete(id); retrySavedReceipts(); }, delay));
  }
}
// Quá 24h: ngừng thử, GIỮ bản ghi để đối soát, không bao giờ xoá hay gửi lại nội dung.
function receiptExpired(record) {
  if (Date.now() - Number(record.createdAt || 0) < RECEIPT_MAX_AGE_MS) return false;
  record.expired_for_reconcile = true;
  return true;
}
async function deliverSavedReceipt(id, record, outbox) {
  if (receiptExpired(record)) {
    outbox[id] = record;
    await chrome.storage.local.set({liveReReceiptOutbox: outbox});
    return {ok: false, error: 'receipt_expired_for_reconcile', receipt_saved: true, expired_for_reconcile: true};
  }
  try {
    const response = await fetch(receiptUrls[record.type], {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(record.payload), signal: globalThis.AbortSignal?.timeout?.(10000)
    });
    const result = await response.json();
    if (!response.ok || !result?.ok) throw new Error(result?.error || 'Receipt not accepted');
    delete outbox[id];
    await chrome.storage.local.set({liveReReceiptOutbox: outbox});
    const marker = chatIntentQueue.then(async () => {
      const saved = await chrome.storage.local.get('liveReChatIntents');
      const intents = saved.liveReChatIntents || {};
      if (intents[id]) {
        intents[id] = {...intents[id], receipt_recorded: true};
        await chrome.storage.local.set({liveReChatIntents: intents});
      }
    });
    chatIntentQueue = marker.catch(() => {});
    await marker;
    return result;
  } catch (error) {
    record.attempts = Number(record.attempts || 0) + 1;
    record.nextAt = Date.now() + receiptDelay(record.attempts);
    outbox[id] = record;
    await chrome.storage.local.set({liveReReceiptOutbox: outbox});
    scheduleReceiptRetry(id, record);
    return {ok: false, error: String(error?.message || error), receipt_saved: true};
  }
}
function persistAndDeliverReceipt(message) {
  const task = receiptQueue.then(async () => {
    const saved = await chrome.storage.local.get('liveReReceiptOutbox');
    const outbox = saved.liveReReceiptOutbox || {};
    const id = String(message.payload?.message_id || message.payload?.reply_id || '');
    if (!id || !message.payload?.interaction_id) return {ok: false, error: 'receipt_identity_required'};
    const old = outbox[id];
    if (['LIVE_RE_AI_ACK','LIVE_RE_MESSAGE_ACK'].includes(old?.type) && !['LIVE_RE_AI_ACK','LIVE_RE_MESSAGE_ACK'].includes(message.type)) {
      return {ok: false, error: 'success_receipt_already_pending'};
    }
    const record = {...old, type: message.type, payload: message.payload,
      createdAt: old?.createdAt || Date.now(), nextAt: Date.now()};
    outbox[id] = record;
    await chrome.storage.local.set({liveReReceiptOutbox: outbox});
    return deliverSavedReceipt(id, record, outbox);
  });
  receiptQueue = task.catch(() => {});
  return task;
}
function retrySavedReceipts() {
  receiptQueue = receiptQueue.then(async () => {
    const saved = await chrome.storage.local.get('liveReReceiptOutbox');
    const outbox = saved.liveReReceiptOutbox || {};
    for (const [id, record] of Object.entries(outbox)) {
      if (!receiptUrls[record?.type] || record.expired_for_reconcile) continue;
      if (!receiptExpired(record) && Date.now() + 100 < record.nextAt) continue;
      await deliverSavedReceipt(id, record, outbox);
    }
  }).catch(() => {});
  return receiptQueue;
}
async function recoverChatIntents() {
  const saved = await chrome.storage.local.get(['liveReChatIntents', 'liveReReceiptOutbox']);
  for (const [id, intent] of Object.entries(saved.liveReChatIntents || {})) {
    if (intent.receipt_recorded || saved.liveReReceiptOutbox?.[id]) continue;
    const identity = {reply_id: intent.reply_id || id, interaction_id: intent.interaction_id,
      ...(intent.message_id ? {message_id:intent.message_id,turn_id:intent.turn_id,
        content_hash:intent.content_hash,room_id:intent.room_id} : {})};
    const result = intent.receipt;
    if (!result) await completeChatIntent({delivery: identity}, {ok: false, error: 'background_restarted_after_send_intent'});
    await persistAndDeliverReceipt(result?.ok ? {type: intent.message_id ? 'LIVE_RE_MESSAGE_ACK' : 'LIVE_RE_AI_ACK', payload: {
      ...identity, receipt: {room_id: result.room_id, response_code: result.response_code, sent_at_ms: result.sent_at_ms}
    }} : {type: intent.message_id ? 'LIVE_RE_MESSAGE_UNKNOWN' : 'LIVE_RE_AI_DELIVERY', payload: {...identity, status: 'unknown',
      detail: 'background_restarted_after_send_intent'}});
  }
}
chrome.alarms?.onAlarm?.addListener(alarm => {
  if (alarm.name === 'tiktok-receipts') retrySavedReceipts();
});
chrome.runtime.onStartup?.addListener(() => { recoverChatIntents().then(retrySavedReceipts).catch(() => {}); });
recoverChatIntents().catch(() => {});

// Hàm này được serialize và chạy trong MAIN world của chính trang TikTok.
// Nó dùng client API đã được bundle TikTok khởi tạo, nên cookie, CSRF và các
// header bảo mật đều đi qua đúng request wrapper của TikTok.
async function runTikTokLiveApi(request) {
  const chunk = window.webpackChunkecom_streamer;
  if (!Array.isArray(chunk)) {
    return { ok: false, error: "TikTok API bundle chưa sẵn sàng" };
  }

  let webpackRequire = null;
  const runtimeId = `live-re-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  chunk.push([[runtimeId], {}, (requireFn) => {
    webpackRequire = requireFn;
  }]);
  if (!webpackRequire) {
    return { ok: false, error: "Không lấy được TikTok webpack runtime" };
  }

  function findApiModule() {
    try {
      const known = webpackRequire(591595);
      if (known?.gO?.PinLiveProduct && known?.gO?.ListLiveProduct) return known;
    } catch {}

    // Module ID có thể đổi sau khi TikTok phát hành bundle mới. Tìm theo
    // endpoint là phương án dự phòng, vẫn hoàn toàn không phụ thuộc DOM.
    for (const [moduleId, factory] of Object.entries(webpackRequire.m || {})) {
      let source = "";
      try { source = Function.prototype.toString.call(factory); } catch { continue; }
      if (!source.includes("/streamer_desktop/live_product/pin")) continue;
      try {
        const candidate = webpackRequire(moduleId);
        for (const value of Object.values(candidate || {})) {
          if (value?.PinLiveProduct && value?.ListLiveProduct) {
            return { gO: value, hW: { Pin: 1, Unpin: 2 } };
          }
        }
      } catch {}
    }
    return null;
  }

  const apiModule = findApiModule();
  const rawClient = apiModule?.gO;
  if (!rawClient) return { ok: false, error: "Không tìm thấy TikTok LIVE API client" };
  // Đếm số request thật tới TikTok trong lượt này (để background tính req/phút).
  let apiCalls = 0;
  const apiMethods = [];
  const client = new Proxy(rawClient, {
    get(target, prop) {
      const value = target[prop];
      if (typeof value !== "function") return value;
      return (...args) => { apiCalls += 1; apiMethods.push(String(prop)); return value.apply(target, args); };
    }
  });

  const cleanText = (value) => String(value || "").trim();
  const asNumber = (value) => {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
  };
  const moneyText = (value) => {
    if (value === null || value === undefined) return "";
    if (typeof value === "string" || typeof value === "number") return cleanText(value);
    if (typeof value !== "object") return "";
    for (const key of [
      "format_price", "price_text", "display_price", "formatted_price",
      "format_available_price", "formatted_available_price",
      "format_origin_price", "format_product_origin_price",
      "formatted_original_price", "amount_text", "min_price", "max_price", "price"
    ]) {
      const found = moneyText(value[key]);
      if (found) return found;
    }
    return "";
  };
  const findField = (roots, keys, depth = 3) => {
    const queue = (Array.isArray(roots) ? roots : [roots]).map((value) => ({ value, depth: 0 }));
    const visited = new Set();
    while (queue.length) {
      const current = queue.shift();
      const value = current.value;
      if (!value || typeof value !== "object" || visited.has(value)) continue;
      visited.add(value);
      for (const key of keys) {
        if (value[key] !== undefined && value[key] !== null) return value[key];
      }
      if (current.depth >= depth) continue;
      for (const child of Object.values(value)) {
        if (!child || typeof child !== "object") continue;
        if (Array.isArray(child)) {
          for (const item of child.slice(0, 100)) {
            queue.push({ value: item, depth: current.depth + 1 });
          }
        } else {
          queue.push({ value: child, depth: current.depth + 1 });
        }
      }
    }
    return undefined;
  };
  const compactSku = (sku) => {
    const skuBase = sku?.base || {};
    const roots = [sku, skuBase, sku?.price, skuBase?.price];
    const original = moneyText(findField(roots, [
      "original_price", "origin_price", "list_price", "market_price", "retail_price",
      "format_origin_price", "format_product_origin_price", "formatted_original_price"
    ]));
    const sale = moneyText(findField(roots, [
      "promotion_price", "promo_price", "sale_price", "discount_price", "current_price",
      "format_available_price", "formatted_available_price"
    ]));
    const generic = moneyText(findField(roots, [
      "format_price", "display_price", "price_text", "price",
      "format_available_price", "formatted_available_price"
    ]));
    return {
      sku_id: cleanText(sku?.sku_id || sku?.id || skuBase?.sku_id || sku?.seller_sku),
      seller_sku: cleanText(sku?.seller_sku || sku?.seller_sku_id || skuBase?.seller_sku),
      title: cleanText(sku?.title || sku?.sku_name || sku?.name || skuBase?.sku_name),
      original_price: original || (!sale ? generic : ""),
      sale_price: sale,
      stock_num: asNumber(
        sku?.stock_num || sku?.stock || sku?.available_stock ||
        skuBase?.stock_num || skuBase?.stock
      )
    };
  };
  const compactProduct = (product, fallbackIndex) => {
    const base = product?.base || product?.product || {};
    const title = cleanText(
      product?.title || product?.product_name || product?.name ||
      base?.title || base?.product_name || base?.name
    );
    const productId = cleanText(
      product?.product_id || product?.productId ||
      base?.product_id || base?.productId
    );
    const index = asNumber(
      product?.index || product?.bag_index || product?.position ||
      product?.order || fallbackIndex
    );
    const priceValue = (
      product?.format_price || product?.price_text || product?.display_price ||
      product?.sale_price || product?.price || base?.format_price ||
      base?.price_text || base?.display_price || base?.sale_price || base?.price
    );
    const price = moneyText(priceValue);
    const roots = [product, base, product?.price, base?.price];
    const explicitOriginal = moneyText(findField(roots, [
      "original_price", "origin_price", "list_price", "market_price", "retail_price",
      "format_origin_price", "format_product_origin_price", "formatted_original_price"
    ]));
    const salePrice = moneyText(findField(roots, [
      "promotion_price", "promo_price", "sale_price", "discount_price", "current_price",
      "format_available_price", "formatted_available_price"
    ]));
    const skuSource = findField([product, base], ["skus", "sku_list", "sku_infos"], 2);
    const skus = Array.isArray(skuSource) ? skuSource.map(compactSku) : [];
    const promotionValue = findField([product, base], [
      "promotion_info", "promotion", "campaign_info", "campaign", "activity_info",
      "discount_info", "live_promotion", "flash_sale",
      "exclusive_stock_with_promotion_info"
    ], 3);
    let promotion = "";
    if (typeof promotionValue === "string") promotion = cleanText(promotionValue);
    else if (promotionValue && typeof promotionValue === "object") {
      try { promotion = JSON.stringify(promotionValue).slice(0, 3000); } catch {}
    }
    return {
      product_id: productId,
      sku_id: cleanText(product?.sku_id || base?.sku_id),
      title,
      bag_index: index,
      is_pinned: Boolean(product?.isPined || product?.is_pined || product?.pinned),
      stock_num: asNumber(product?.stock_num || base?.stock_num),
      price: salePrice || price || explicitOriginal,
      original_price: explicitOriginal || (!salePrice ? price : ""),
      sale_price: salePrice,
      promotion,
      skus
    };
  };

  // Chủ phòng (host) lấy từ chính phản hồi GetLiveRoomInfo: không cần cấu hình tay.
  const OWNER_KEYS = ["owner", "owner_info", "anchor", "anchor_info", "room_owner", "streamer", "host", "host_info", "creator"];
  const pickId = (obj, keys) => {
    for (const key of keys) {
      const value = obj?.[key];
      if (value !== undefined && value !== null && typeof value !== "object" && cleanText(value)) return cleanText(value);
    }
    return "";
  };
  const extractRoomOwner = (data) => {
    if (!data || typeof data !== "object") return null;
    const build = (obj, flat) => {
      const owner = {
        user_id: pickId(obj, flat ? ["owner_id", "owner_user_id", "anchor_id", "anchor_user_id"] : ["id", "user_id", "uid", "id_str"]),
        sec_uid: pickId(obj, flat ? ["owner_sec_uid", "anchor_sec_uid"] : ["sec_uid", "sec_user_id"]),
        unique_id: pickId(obj, flat ? ["owner_unique_id", "owner_display_id", "anchor_unique_id"] : ["unique_id", "display_id", "handle"]),
        nickname: pickId(obj, flat ? ["owner_nickname", "anchor_nickname", "owner_name"] : ["nickname", "nick_name", "name"])
      };
      return owner.user_id || owner.sec_uid || owner.unique_id ? owner : null;
    };
    const queue = [{ value: data, depth: 0 }];
    const visited = new Set();
    while (queue.length) {
      const { value, depth } = queue.shift();
      if (!value || typeof value !== "object" || visited.has(value)) continue;
      visited.add(value);
      for (const key of OWNER_KEYS) {
        const child = value[key];
        if (child && typeof child === "object" && !Array.isArray(child)) {
          const owner = build(child, false) || build(child.user || child.user_info || {}, false);
          if (owner) return owner;
        }
      }
      const flat = build(value, true);
      if (flat) return flat;
      if (depth >= 3) continue;
      for (const child of Object.values(value)) {
        if (child && typeof child === "object" && !Array.isArray(child)) queue.push({ value: child, depth: depth + 1 });
      }
    }
    return null;
  };
  let roomOwner;
  let roomInfoResponse = null;
  let emptyListResponse = null;
  let emptyListError = "";

  // Chỉ những method ĐỌC mới được thử khi quét giỏ trước LIVE.
  const READ_METHOD_RE = /^(Get|List|Query|Search)/;
  const WRITE_METHOD_RE = /(Pin|Add|Delete|Update|Set|Remove|Chat|Start|End)/;
  const PRODUCT_ARRAY_KEYS = [
    "products", "product_list", "productList", "live_products", "liveProducts",
    "product_infos", "items", "list", "data"
  ];
  const looksLikeProduct = (item) => !!item && typeof item === "object" && !Array.isArray(item) && !!(
    item.product_id || item.productId || item.base?.product_id || item.product?.product_id ||
    item.base?.productId || item.product?.productId
  );
  // Tìm mảng sản phẩm trong nhiều dạng response: data.products, data.product_list,
  // data.live_products, mảng lồng (data.list[0].products), hoặc data là mảng.
  const extractProductArray = (root) => {
    const queue = [{ value: root, depth: 0 }];
    const visited = new Set();
    while (queue.length) {
      const { value, depth } = queue.shift();
      if (!value || typeof value !== "object" || visited.has(value)) continue;
      visited.add(value);
      if (Array.isArray(value)) {
        if (value.some(looksLikeProduct)) return value.filter(looksLikeProduct);
        if (depth < 4) for (const item of value.slice(0, 50)) queue.push({ value: item, depth: depth + 1 });
        continue;
      }
      for (const key of PRODUCT_ARRAY_KEYS) {
        const child = value[key];
        if (Array.isArray(child) && child.some(looksLikeProduct)) return child.filter(looksLikeProduct);
      }
      if (depth >= 4) continue;
      for (const child of Object.values(value)) {
        if (child && typeof child === "object") queue.push({ value: child, depth: depth + 1 });
      }
    }
    return [];
  };
  // Chẩn đoán an toàn: chỉ tên khoá cấp 1-2, không giá trị.
  const keyShape = (response) => {
    const out = {};
    if (!response || typeof response !== "object") return out;
    for (const key of Object.keys(response).slice(0, 20)) {
      const child = response[key];
      out[key] = child && typeof child === "object" && !Array.isArray(child)
        ? Object.keys(child).slice(0, 30)
        : (Array.isArray(child) ? `array(${child.length})` : typeof child);
    }
    return out;
  };
  const clientMethodNames = () => {
    const names = new Set();
    let obj = client;
    for (let i = 0; obj && obj !== Object.prototype && i < 4; i += 1) {
      for (const name of Object.getOwnPropertyNames(obj)) {
        try { if (typeof client[name] === "function") names.add(name); } catch {}
      }
      obj = Object.getPrototypeOf(obj);
    }
    return [...names].filter((name) => name !== "constructor");
  };

  async function preliveSnapshot() {
    let response = emptyListResponse;
    let source = "ListLiveProduct";
    let items = extractProductArray(response?.data ?? response);
    const tried = [];
    if (!items.length) {
      const candidates = clientMethodNames().filter((name) =>
        /product/i.test(name) && /list/i.test(name) && /live/i.test(name) && READ_METHOD_RE.test(name) &&
        !WRITE_METHOD_RE.test(name.replace(READ_METHOD_RE, "")) && name !== "ListLiveProduct"
      );
      for (const name of candidates.slice(0, 6)) {
        tried.push(name);
        let candidate = null;
        try { candidate = await client[name]({}); } catch { continue; }
        const found = extractProductArray(candidate?.data ?? candidate);
        if (found.length) { response = candidate; items = found; source = name; break; }
      }
    }
    if (!items.length) {
      const diagnostic = {
        list_live_product_keys: keyShape(emptyListResponse),
        list_live_product_error: emptyListError,
        room_info_keys: keyShape(roomInfoResponse),
        product_methods: clientMethodNames().filter((name) => /product/i.test(name)).slice(0, 40),
        tried_methods: tried
      };
      const shapeText = (shape) => Object.entries(shape)
        .map(([key, value]) => Array.isArray(value) ? `${key}{${value.join(",")}}` : `${key}:${value}`)
        .join(" ");
      const diagnosticText = [
        `ListLiveProduct({})=[${shapeText(diagnostic.list_live_product_keys)}${emptyListError ? ` lỗi:${emptyListError}` : ""}]`,
        `GetLiveRoomInfo({})=[${shapeText(diagnostic.room_info_keys)}]`,
        `method sản phẩm=[${diagnostic.product_methods.join(",")}]`
      ].join(" · ");
      return {
        ok: false,
        error: `Chưa quét được giỏ trước LIVE: API không trả room_id và không có danh sách sản phẩm. Chẩn đoán: ${diagnosticText}`,
        diagnostic
      };
    }
    if (roomOwner === undefined) roomOwner = extractRoomOwner(roomInfoResponse?.data) || null;
    const products = items.map((product, index) => compactProduct(product, index + 1));
    return {
      ok: true,
      snapshot: {
        room_id: "",
        phase: "prelive",
        source,
        host: roomOwner ? { ...roomOwner, source: "room_info" } : null,
        products,
        pin_id: "",
        response_code: asNumber(response?.code),
        raw_api_data: {}
      }
    };
  }

  async function getRoomId() {
    if (request?.roomId && !['send_chat','pin','unpin'].includes(request.action)) return cleanText(request.roomId);
    // pin/unpin/chat: dung room da xac nhan trong 60 giay gan nhat (MAIN world
    // cua tab), khong goi GetLiveRoomInfo truoc moi lenh. Loi thi xoa cache.
    const roomCache = window.__kidteRoomCache;
    if (["send_chat", "pin", "unpin"].includes(request?.action) && roomCache?.roomId &&
        Date.now() - Number(roomCache.at || 0) < 60000) return roomCache.roomId;
    const roomResponse = await client.GetLiveRoomInfo({});
    roomInfoResponse = roomResponse;
    roomOwner = extractRoomOwner(roomResponse?.data);
    const nestedRoomId = findField(
      [roomResponse?.data, roomResponse],
      ["room_id", "roomId", "live_room_id", "liveRoomId"],
      5
    );
    if (cleanText(nestedRoomId)) {
      window.__kidteRoomCache = { roomId: cleanText(nestedRoomId), at: Date.now() };
      return { roomId: cleanText(nestedRoomId), diagnostic: {} };
    }
    // TikTok bao chua co phong (LIVE da ket thuc): bo room da nho.
    window.__kidteRoomCache = null;

    // Một số bản TikTok không còn trả room_id ở GetLiveRoomInfo nhưng lại
    // đính kèm nó trong response danh sách sản phẩm. Thử đường này trước khi
    // kết luận dashboard chưa gắn với phòng LIVE.
    let listResponse = null;
    try { listResponse = await client.ListLiveProduct({}); } catch (error) { emptyListError = cleanText(error?.name || "error"); }
    emptyListResponse = listResponse;
    const listRoomId = findField(
      [listResponse?.data, listResponse],
      ["room_id", "roomId", "live_room_id", "liveRoomId"],
      5
    );
    return {
      roomId: cleanText(listRoomId),
      diagnostic: {
        response_code: roomResponse?.code ?? "",
        message: cleanText(roomResponse?.message || roomResponse?.msg),
        data_keys: roomResponse?.data && typeof roomResponse.data === "object"
          ? Object.keys(roomResponse.data).slice(0, 20)
          : [],
        list_response_code: listResponse?.code ?? "",
        list_message: cleanText(listResponse?.message || listResponse?.msg),
        list_data_keys: listResponse?.data && typeof listResponse.data === "object"
          ? Object.keys(listResponse.data).slice(0, 20)
          : []
      }
    };
  }

  async function snapshot(roomId) {
    const response = await client.ListLiveProduct({ room_id: roomId });
    const data = response?.data || {};
    const listCode = response?.code;
    if (listCode === 0 || listCode === "0" || listCode === undefined) {
      window.__kidteRoomCache = { roomId, at: Date.now() };
    } else {
      window.__kidteRoomCache = null;
      // TikTok bao loi (qua tai/xac minh/phong het): KHONG tra "0 san pham"
      // thanh cong, de phia goi khong ghi de catalog/cache bang danh sach rong.
      const error = new Error(cleanText(response?.message || response?.msg) ||
        `ListLiveProduct trả mã lỗi ${cleanText(listCode)}`);
      error.code = cleanText(listCode);
      throw error;
    }
    const products = Array.isArray(data.products)
      ? data.products.map((product, index) => compactProduct(product, index + 1))
      : [];
    const pinId = cleanText(
      data.introduce_id || data.pin_id || data.pinned_product_id ||
      products.find((product) => product.is_pinned)?.product_id
    );
    if (roomOwner === undefined) {
      // Chủ phòng không đổi trong một phòng LIVE: nhớ 10 phút ở MAIN world,
      // tránh mỗi snapshot tốn thêm một GetLiveRoomInfo.
      const cachedOwner = window.__kidteRoomOwnerCache;
      if (cachedOwner && cachedOwner.roomId === roomId && Date.now() - cachedOwner.at < 600000) {
        roomOwner = cachedOwner.owner;
      } else {
        try { roomOwner = extractRoomOwner((await client.GetLiveRoomInfo({}))?.data); } catch { roomOwner = null; }
        if (roomOwner) window.__kidteRoomOwnerCache = { roomId, owner: roomOwner, at: Date.now() };
      }
    }
    let rawApiData = {};
    if (request?.includeCatalogRaw === true) {
      try { rawApiData = JSON.parse(JSON.stringify(data)); } catch {}
    }
    return {
      room_id: roomId,
      host: roomOwner ? { ...roomOwner, source: "room_info" } : null,
      products,
      pin_id: pinId,
      card_type: asNumber(data.card_type),
      server_time: cleanText(data.server_time),
      response_code: asNumber(response?.code),
      raw_api_data: rawApiData
    };
  }

  const result = await (async () => { try {
    if (request?.action==='send_chat' && (typeof request.content!=='string' || !request.content.trim() || request.content.length>150)) {
      return {ok:false,error:'Nội dung trả lời trống hoặc vượt 150 ký tự'};
    }
    const roomLookup = await getRoomId();
    const roomId = typeof roomLookup === "string" ? roomLookup : roomLookup.roomId;
    if (!roomId && request?.action === "snapshot") {
      // Chưa LIVE: chỉ quét giỏ, không có room_id nên mọi thao tác khác vẫn bị chặn.
      const pre = await preliveSnapshot();
      if (pre.ok) return pre;
      return { ...pre, room_lookup: roomLookup?.diagnostic || {} };
    }
    if (!roomId) return {
      ok: false,
      error: "API chưa trả về room_id của LIVE hiện tại",
      room_lookup: roomLookup?.diagnostic || {}
    };

    if (request?.action === "snapshot") {
      return { ok: true, snapshot: await snapshot(roomId) };
    }

    if (request?.action === "pin" || request?.action === "unpin") {
      const productId = cleanText(request.productId);
      if (!productId) return { ok: false, error: "Thiếu product_id" };
      const op = request.action === "pin"
        ? Number(apiModule?.hW?.Pin || 1)
        : Number(apiModule?.hW?.Unpin || 2);
      const response = await client.PinLiveProduct({
        room_id: roomId,
        product_id: productId,
        op
      });
      const rawCode = response?.code;
      const code = rawCode === 0 || rawCode === "0" ? 0 :
        (rawCode !== null && rawCode !== undefined && rawCode !== "" &&
         rawCode !== false && Number.isFinite(Number(rawCode)) ? Number(rawCode) : -1);
      if (code !== 0) window.__kidteRoomCache = null;
      return {
        ok: code === 0,
        room_id: roomId,
        product_id: productId,
        op,
        response_code: code,
        error: code === 0 ? "" : (cleanText(response?.message) || "TikTok API không trả mã thành công")
      };
    }

    if (request?.action === "send_chat") {
      if (request.expected_room_id && request.expected_room_id !== roomId) {
        return {ok: false, error: 'Phòng LIVE đã thay đổi trước khi gửi'};
      }
      const content = typeof request.content === 'string' ? request.content : '';
      const atUid = cleanText(request.at_uid);
      if (!content) return { ok: false, error: "Nội dung trả lời trống" };
      if (content.length > 150) return { ok: false, error: "Nội dung trả lời vượt 150 ký tự" };
      if (typeof client.Chat !== "function") {
        return { ok: false, error: "TikTok Chat API chưa sẵn sàng" };
      }

      // TikTok tách app_id sang một module hằng số. Module ID thay đổi theo
      // bản phát hành, nên tìm export B8 từ chính factory gọi Chat thay vì
      // đóng đinh một con số đoán mò.
      let appId = request.app_id;
      if (appId === undefined || appId === null || appId === "") {
        try { appId = webpackRequire(690426)?.B8; } catch {}
      }
      if (appId === undefined || appId === null || appId === "") {
        for (const [moduleId, factory] of Object.entries(webpackRequire.m || {})) {
          let source = "";
          try { source = Function.prototype.toString.call(factory); } catch { continue; }
          if (!source.includes("ec_streamer_key") || !source.includes("app_id")) continue;
          const ids = [...source.matchAll(/\b[enrt]\((\d+)\)/g)].map((match) => match[1]);
          for (const id of ids) {
            try {
              const candidate = webpackRequire(id);
              if (candidate?.B8 !== undefined && candidate?.B8 !== null) {
                appId = candidate.B8;
                break;
              }
            } catch {}
          }
          if (appId !== undefined && appId !== null && appId !== "") break;
        }
      }
      const meta = {
        source: apiModule?.Yv?.EC_PC,
        app_id: appId,
        room_id: roomId,
        ec_streamer_key: globalThis.crypto?.randomUUID?.() ||
          `live-re-chat-${Date.now()}-${Math.random().toString(36).slice(2)}`
      };
      for (const key of Object.keys(meta)) {
        if (meta[key] === undefined || meta[key] === null || meta[key] === "") {
          delete meta[key];
        }
      }
      const payload = {
        content,
        meta,
        client_start_time_stamp_millisecond: Date.now().toString()
      };
      if (atUid) payload.at_uid = atUid;
      const response = await client.Chat(payload);
      const rawCode = response?.code;
      const code = rawCode === 0 || rawCode === "0" ? 0 :
        (rawCode !== null && rawCode !== undefined && rawCode !== "" &&
         rawCode !== false && Number.isFinite(Number(rawCode)) ? Number(rawCode) : -1);
      if (code !== 0) window.__kidteRoomCache = null;
      return {
        ok: code === 0,
        room_id: roomId,
        response_code: code,
        error: code === 0 ? "" : cleanText(response?.message)
      };
    }

    return { ok: false, error: "Tác vụ TikTok API không hợp lệ" };
  } catch (error) {
    window.__kidteRoomCache = null;
    return {
      ok: false,
      error: cleanText(error?.message || error),
      error_code: cleanText(error?.code)
    };
  } })();
  if (result && typeof result === "object") { result.api_calls = apiCalls; result.api_methods = apiMethods.slice(0, 12); }
  return result;
}

// ---------------------------------------------------------------------
// Bộ chặn chung cho mọi lượt gọi TikTok: đếm request/phút và TẠM DỪNG gọi
// tự động trên tab khi TikTok đòi xác minh (captcha) / báo quá tải.
// ---------------------------------------------------------------------
const API_PAUSE_STEPS_MS = [120000, 240000, 480000, 600000];
const API_VERIFY_HINTS = [
  "captcha", "verify", "verification", "xac minh", "xác minh",
  "risk", "too frequent", "frequent", "too many", "slow down", "rate limit",
  "try again later", "forbidden", "blocked", "限制", "验证", "频繁"
];
const apiGuards = new Map();
chrome.tabs?.onRemoved?.addListener?.((tabId) => { apiGuards.delete(tabId); });
let apiGuardWriteAt = 0;

function looksLikeTikTokVerification(result) {
  const text = [result?.error, result?.error_code, result?.room_lookup?.message,
    result?.room_lookup?.list_message].map((v) => String(v || "")).join(" ").toLowerCase();
  if (API_VERIFY_HINTS.some((hint) => text.includes(hint))) return true;
  // HTTP 429 / 403 từ TikTok.
  return /(429|403)/.test(String(result?.error_code || "")) ||
    [429, 403].includes(Number(result?.response_code));
}

function apiGuardFor(tabId) {
  let guard = apiGuards.get(tabId);
  if (!guard) {
    guard = { pausedUntil: 0, step: 0, reason: "", calls: [] };
    apiGuards.set(tabId, guard);
  }
  return guard;
}

function apiGuardSummary(now = Date.now()) {
  let perMinute = 0, pausedUntil = 0, reason = "";
  for (const guard of apiGuards.values()) {
    guard.calls = guard.calls.filter((at) => now - at < 60000);
    perMinute += guard.calls.length;
    if (guard.pausedUntil > pausedUntil) { pausedUntil = guard.pausedUntil; reason = guard.reason; }
  }
  const leaseDenied = browserLease.granted === false && now - browserLease.checkedAt < 15000;
  return { perMinute, pausedUntil: pausedUntil > now ? pausedUntil : 0, reason, updatedAtMs: now,
    otherBrowser: leaseDenied ? BROWSER_LEASE_DENIED_MESSAGE : "",
    leaseFailOpen: browserLease.failOpen === true };
}

async function writeApiGuard(force = false) {
  const now = Date.now();
  if (!force && now - apiGuardWriteAt < 2000) return;
  apiGuardWriteAt = now;
  try { await chrome.storage.local.set({ liveReApiGuard: apiGuardSummary(now) }); }
  catch (error) { console.warn("Không ghi được bộ đếm TikTok API", error); }
}

/** Trả về lỗi tạm dừng nếu tab đang bị chặn gọi tự động, ngược lại null. */
function apiGuardBlocked(tabId, request) {
  const guard = apiGuardFor(tabId);
  const remaining = guard.pausedUntil - Date.now();
  if (remaining <= 0 || request?.userInitiated === true) return null;
  return {
    ok: false, paused: true, paused_until_ms: guard.pausedUntil,
    error: `TikTok đang yêu cầu xác minh – tạm dừng gọi tự động (còn ${Math.ceil(remaining / 1000)} giây)`
  };
}

function apiGuardRecord(tabId, result) {
  const guard = apiGuardFor(tabId);
  const now = Date.now();
  const count = Math.max(1, Number(result?.api_calls) || 1);
  for (let i = 0; i < count; i += 1) guard.calls.push(now);
  let changed = false;
  if (!result?.ok && looksLikeTikTokVerification(result)) {
    const step = API_PAUSE_STEPS_MS[Math.min(guard.step, API_PAUSE_STEPS_MS.length - 1)];
    guard.step += 1;
    guard.pausedUntil = now + step;
    guard.reason = String(result?.error || "TikTok đòi xác minh").slice(0, 160);
    changed = true;
    diag.event("guard_pause", {tab: tabId, ms: step, step: guard.step, reason: guard.reason});
    console.warn(`TikTok đòi xác minh trên tab ${tabId}; tạm dừng gọi tự động ${step / 1000}s`, guard.reason);
  } else if (result?.ok && guard.step && now >= guard.pausedUntil) {
    guard.step = 0;
    guard.reason = "";
    changed = true;
  }
  writeApiGuard(changed).catch(() => {});
  if (result && changed && guard.pausedUntil > now) {
    result.paused = true;
    result.paused_until_ms = guard.pausedUntil;
  }
}

// Đăng ký emitter IM của TikTok ở MAIN world rồi chuyển một bản dữ liệu tối
// thiểu sang content script. Không đọc danh sách comment từ DOM.
function installTikTokCommentBridge(nonce) {
  try {
    const chunk = window.webpackChunkecom_streamer;
    if (!Array.isArray(chunk)) return { ok: false, error: "TikTok IM bundle chưa sẵn sàng" };
    let webpackRequire = null;
    chunk.push([[`live-re-chat-${Date.now()}`], {}, (requireFn) => {
      webpackRequire = requireFn;
    }]);
    if (!webpackRequire) return { ok: false, error: "Không lấy được TikTok runtime" };

    const emitterModule = webpackRequire(380757);
    const enumModule = webpackRequire(779233);
    const emitter = emitterModule?.S;
    const chatEvent = enumModule?.tbY?.ChatMessage || "ChatMessage";
    if (!emitter?.on) return { ok: false, error: "Không tìm thấy TikTok ChatMessage emitter" };

    if (typeof window.__LIVE_RE_CHAT_CLEANUP__ === "function") {
      window.__LIVE_RE_CHAT_CLEANUP__();
    }
    const handler = (event) => {
      const user = event?.user || {};
      const common = event?.common || {};
      window.postMessage({
        source: "LIVE_RE_TIKTOK_CHAT",
        nonce,
        payload: {
          comment: String(event?.content || "").trim(),
          comment_id: String(common?.msg_id || event?.msg_id || ""),
          created_at: String(common?.create_time || ""),
          user_id: String(user?.id || ""),
          sec_uid: String(user?.sec_uid || ""),
          unique_id: String(user?.display_id || user?.unique_id || ""),
          nickname: String(user?.nickname || user?.display_id || "Khách"),
          is_host: Boolean(
            event?.is_self || event?.is_host || user?.is_host ||
            user?.user_attr?.is_host
          )
        }
      }, "*");
    };
    emitter.on(chatEvent, handler);
    window.__LIVE_RE_CHAT_CLEANUP__ = () => {
      try { emitter.off?.(chatEvent, handler); } catch {}
      window.__LIVE_RE_CHAT_CLEANUP__ = null;
    };
    return { ok: true, event: String(chatEvent) };
  } catch (error) {
    return { ok: false, error: String(error?.message || error) };
  }
}

const DEFAULT_STATE = Object.freeze({
  running: false,
  startedAtMs: 0,
  pausedElapsedMs: 0,
  revision: 0,
  sessionId: "",
  controllerTabId: 0
});

function makeSessionId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
}

async function readState() {
  const stored = await chrome.storage.local.get("liveReState");
  return { ...DEFAULT_STATE, ...(stored.liveReState || {}) };
}

async function writeState(next) {
  const state = { ...DEFAULT_STATE, ...next };
  await chrome.storage.local.set({ liveReState: state });
  await broadcastState(state);
  return state;
}

function elapsedFor(state, now = Date.now()) {
  if (state.running && state.startedAtMs) {
    return Math.max(0, now - Number(state.startedAtMs));
  }
  return Math.max(0, Number(state.pausedElapsedMs) || 0);
}

async function broadcastState(state) {
  const tabs = await chrome.tabs.query({ url: ["https://shop.tiktok.com/*"] });
  await Promise.allSettled(tabs
    .filter((tab) => Number.isInteger(tab.id))
    .map((tab) => chrome.tabs.sendMessage(tab.id, {
      type: "LIVE_RE_STATE",
      state,
      isController: Number(tab.id) === Number(state.controllerTabId)
    })));
}

async function chooseControllerTab() {
  const activeTabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const active = activeTabs.find((tab) =>
    String(tab.url || "").startsWith("https://shop.tiktok.com/") &&
    String(tab.url || "").includes("/streamer/live/product/dashboard")
  );
  if (active?.id) return active.id;
  const candidates = await chrome.tabs.query({ url: ["https://shop.tiktok.com/*"] });
  const dashboard = candidates.find((tab) =>
    String(tab.url || "").includes("/streamer/live/product/dashboard")
  );
  if (dashboard?.id) return dashboard.id;
  if (candidates.length === 1 && candidates[0]?.id) return candidates[0].id;
  throw new Error("Hãy mở hoặc chọn đúng tab TikTok LIVE Manager trước khi bắt đầu");
}

// Quyền GHI DỮ LIỆU tách khỏi quyền ĐIỀU KHIỂN. controllerTabId chỉ được gán
// khi bấm "Bắt đầu"; nếu buộc việc thu thập phụ thuộc vào nó thì một phiên
// người vận hành chỉ ngồi rep tay sẽ không ghi được dòng nào — đúng thứ cần
// thu nhất. Vì vậy một tab LIVE Manager bất kỳ được nhận vai quan sát, giữ
// bằng nhịp tim để chỉ một tab ghi tại một thời điểm.
const OBSERVER_LEASE_MS = 20000;

async function claimObserver(tabId) {
  if (!Number.isInteger(tabId)) return false;
  const stored = await chrome.storage.local.get(
    ["liveReObserverTabId", "liveReObserverAt", "liveReState"]
  );
  const controllerTabId = Number(stored.liveReState?.controllerTabId || 0);
  // Tab đang điều khiển còn sống luôn được ưu tiên làm tab quan sát. Nếu tab
  // đó đã đóng hoặc rời LIVE Manager, không để controllerTabId cũ khóa bộ thu
  // của mọi phiên sau.
  if (controllerTabId && controllerTabId !== tabId) {
    const controller = await chrome.tabs.get(controllerTabId).catch(() => null);
    const controllerAlive = controller &&
      String(controller.url || "").startsWith("https://shop.tiktok.com/") &&
      String(controller.url || "").includes("/streamer/live/product/dashboard");
    if (controllerAlive) return false;
    await chrome.storage.local.set({
      liveReState: { ...(stored.liveReState || DEFAULT_STATE), controllerTabId: 0 }
    });
  }
  const holder = Number(stored.liveReObserverTabId || 0);
  const heldAt = Number(stored.liveReObserverAt || 0);
  const expired = Date.now() - heldAt > OBSERVER_LEASE_MS;
  if (holder && holder !== tabId && !expired) {
    // Tab giữ vai có thể đã bị đóng; kiểm tra thật trước khi nhường.
    const alive = await chrome.tabs.get(holder).catch(() => null);
    // 3.11.8: tab LIVE dashboard giành vai từ tab TikTok Shop KHÁC trang (vd. trang
    // sản phẩm). Trước đây tab khác trang giữ vai quan sát -> chỉ nó nhận lệnh app,
    // còn cổng một tab (resolveApiOwner) lại chặn nó gọi TikTok -> bật AI thất bại.
    if (alive) {
      const self = isLiveDashboardTab(alive) ? null : await chrome.tabs.get(tabId).catch(() => null);
      if (!self || !isLiveDashboardTab(self)) return false;
    }
  }
  await chrome.storage.local.set({
    liveReObserverTabId: tabId,
    liveReObserverAt: Date.now()
  });
  return true;
}

// Tự nhận điều khiển như tiện ích Shopee: tab LIVE Manager thấy phòng live thì
// được nhận vai controller và bắt đầu đúng một lần. liveReAutoRoom ghi phòng đã
// được xử lý (bắt đầu / nối lại / người vận hành đã dừng) nên bấm Dừng thì không
// tự bắt đầu lại cho tới khi phòng đổi. Bật AI vẫn là quyền của người vận hành.
let autoStartQueue = Promise.resolve();

function isLiveDashboardTab(tab) {
  const url = String(tab?.url || "");
  return url.startsWith("https://shop.tiktok.com/") &&
    url.includes("/streamer/live/product/dashboard");
}

// ---------------------------------------------------------------------
// Lease liên trình duyệt/profile (Chrome + Cốc Cốc cùng shop): mỗi profile có
// chrome.storage riêng nên cổng một-tab không thấy nhau; backend Kid TE giữ
// lease "người gọi TikTok" (30s, chỉ trong bộ nhớ). Định danh = id ngẫu nhiên
// của profile (liveReProfileId) + tabId. Kết quả cache vài giây.
// Backend tắt/lỗi: KHÔNG làm tê liệt LIVE — fail-open, trừ khi profile này vừa
// bị từ chối gần đây (trình duyệt khác đang giữ), có ghi cảnh báo.
// ---------------------------------------------------------------------
const BROWSER_LEASE_URL = "http://127.0.0.1:8793/api/tiktok/lease";
const BROWSER_LEASE_CACHE_MS = 5000;
const BROWSER_LEASE_TTL_MS = 30000;
const BROWSER_LEASE_DENY_HOLD_MS = 120000;
const BROWSER_LEASE_DENIED_MESSAGE = "Trình duyệt khác đang LIVE – trình duyệt này tạm không gọi TikTok";
const browserLease = { tabId: 0, granted: null, checkedAt: 0, grantedAt: 0, deniedAt: 0, warnedAt: 0, failOpen: false };
let browserProfileIdPromise = null;

function browserProfileId() {
  if (!browserProfileIdPromise) {
    browserProfileIdPromise = chrome.storage.local.get("liveReProfileId").then(async (stored) => {
      let id = String(stored?.liveReProfileId || "");
      if (!/^[A-Za-z0-9_-]{8,80}$/.test(id)) {
        id = globalThis.crypto?.randomUUID?.() ||
          `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
        await chrome.storage.local.set({ liveReProfileId: id });
      }
      return id;
    });
    browserProfileIdPromise.catch(() => { browserProfileIdPromise = null; });
  }
  return browserProfileIdPromise;
}

async function postBrowserLease(action, tabId) {
  if (typeof fetch !== "function") throw new Error("fetch không khả dụng");
  const profileId = await browserProfileId();
  const response = await fetch(BROWSER_LEASE_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, profile_id: profileId, tab_id: String(tabId) }),
    signal: globalThis.AbortSignal?.timeout?.(3000)
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok || result?.ok !== true) {
    throw new Error(result?.error || `HTTP ${response.status}`);
  }
  if (action !== "release" && typeof result.granted !== "boolean") {
    throw new Error("phản hồi lease không hợp lệ");
  }
  return result;
}

async function browserLeaseAllows(tabId, force = false) {
  const now = Date.now();
  if (!force && browserLease.tabId === tabId && browserLease.granted !== null &&
      now - browserLease.checkedAt < BROWSER_LEASE_CACHE_MS) {
    return browserLease.granted;
  }
  const before = browserLease.granted;
  let granted;
  try {
    granted = (await postBrowserLease("claim", tabId)).granted === true;
    if (granted) browserLease.grantedAt = now; else browserLease.deniedAt = now;
    browserLease.failOpen = false;
  } catch (error) {
    // Lần cuối backend trả lời là "trình duyệt khác giữ" và chưa quá 2 phút:
    // vẫn chặn. Ngược lại (đang giữ / chưa từng biết / quá lâu): cho phép.
    const recentlyDenied = browserLease.deniedAt > browserLease.grantedAt &&
      now - browserLease.deniedAt < BROWSER_LEASE_DENY_HOLD_MS;
    granted = !recentlyDenied;
    browserLease.failOpen = granted;
    if (now - browserLease.warnedAt > 60000) {
      browserLease.warnedAt = now;
      console.warn(`Kid TE: không kiểm được lease liên trình duyệt (${error?.message || error}); ` +
        (granted ? "fail-open: vẫn cho tab này gọi TikTok" : "giữ chặn vì trình duyệt khác vừa giữ quyền"));
    }
  }
  browserLease.tabId = tabId;
  browserLease.granted = granted;
  browserLease.checkedAt = now;
  if (before !== granted) {
    diag.event("lease_change", {tab: tabId, on: granted, fail_open: browserLease.failOpen === true});
    writeApiGuard(true).catch(() => {});
  }
  return granted;
}

// Gia hạn theo nhịp heartbeat 10s của tab đang giữ lease.
function renewBrowserLeaseOnHeartbeat(tabId) {
  if (!tabId || browserLease.tabId !== tabId || browserLease.granted !== true) return;
  if (Date.now() - browserLease.checkedAt < BROWSER_LEASE_CACHE_MS) return;
  browserLeaseAllows(tabId, true).catch(() => {});
}

// Tab giữ lease đóng/rời LIVE dashboard: trả lease để profile khác nhận ngay.
function releaseBrowserLease(tabId) {
  if (!tabId || browserLease.tabId !== tabId || browserLease.granted !== true) return;
  browserLease.tabId = 0;
  browserLease.granted = null;
  browserLease.checkedAt = 0;
  postBrowserLease("release", tabId).catch((error) => {
    console.warn(`Kid TE: không trả được lease liên trình duyệt (${error?.message || error}); lease tự hết hạn sau 30s`);
  });
}
chrome.tabs?.onRemoved?.addListener?.((tabId) => releaseBrowserLease(tabId));
chrome.tabs?.onUpdated?.addListener?.((tabId, info) => {
  if (info?.url && !isLiveDashboardTab({ url: info.url })) releaseBrowserLease(tabId);
});

// MOT tab duy nhat duoc goi TikTok tai mot thoi diem: tab dieu khien con song
// tren LIVE dashboard, neu khong thi tab dang giu vai quan sat (lease 20s, cung
// khoa voi claimObserver). Tab khac / trang khac bi tu choi, khong goi TikTok.
// Thao tac tay (userInitiated, vd quet gio tu popup) duoc phep tu tab dashboard.
let apiOwnerQueue = Promise.resolve();
async function tabOnDashboard(tabId) {
  if (!tabId) return false;
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  return Boolean(tab && isLiveDashboardTab(tab));
}
async function resolveApiOwner(tab) {
  const tabId = Number(tab?.id || 0);
  if (!tabId || !isLiveDashboardTab(tab)) return false;
  const stored = await chrome.storage.local.get(
    ["liveReObserverTabId", "liveReObserverAt", "liveReState"]);
  const controllerTabId = Number(stored.liveReState?.controllerTabId || 0);
  if (controllerTabId && controllerTabId !== tabId && await tabOnDashboard(controllerTabId)) return false;
  const holder = Number(stored.liveReObserverTabId || 0);
  const fresh = Date.now() - Number(stored.liveReObserverAt || 0) <= OBSERVER_LEASE_MS;
  if (controllerTabId !== tabId && holder && holder !== tabId && fresh && await tabOnDashboard(holder)) return false;
  if (holder !== tabId) diag.event("owner_change", {from: holder, to: tabId});
  await chrome.storage.local.set({ liveReObserverTabId: tabId, liveReObserverAt: Date.now() });
  // Đã thắng cổng nội bộ (trong profile này); còn phải giữ lease liên trình duyệt.
  return (await browserLeaseAllows(tabId)) ? true : "other_browser";
}
function apiOwnerAllows(tab, request) {
  // Thao tac tay (quet gio tu popup): cho moi tab TikTok Shop (trang chuan bi
  // LIVE, quan ly san pham...), khong chi LIVE dashboard. 3.11.5 chi nhan
  // dashboard nen quet tay o trang khac bi tu choi -> "khong lay duoc san pham".
  if (request?.userInitiated === true) {
    return Promise.resolve(String(tab?.url || "").startsWith("https://shop.tiktok.com/"));
  }
  const run = apiOwnerQueue.then(() => resolveApiOwner(tab));
  apiOwnerQueue = run.catch(() => {});
  return run;
}

async function autoStartFromRoom(tabId, tabUrl, roomId) {
  roomId = String(roomId || "").trim();
  if (!Number.isInteger(tabId) || !roomId ||
      !isLiveDashboardTab({ url: tabUrl })) {
    return { ok: true, started: false, reason: "no_room" };
  }
  const stored = await chrome.storage.local.get(["liveReState", "liveReAutoRoom"]);
  const current = { ...DEFAULT_STATE, ...(stored.liveReState || {}) };
  const handledRoom = String(stored.liveReAutoRoom || "");
  const heldBy = Number(current.controllerTabId || 0);
  if (heldBy && heldBy !== tabId) {
    const holder = await chrome.tabs.get(heldBy).catch(() => null);
    if (isLiveDashboardTab(holder)) {
      return { ok: true, started: false, reason: "other_controller" };
    }
  }
  const sessionActive = Boolean(current.running || current.startedAtMs);
  if (current.running || (sessionActive && handledRoom === roomId)) {
    // Phiên đang chạy (hoặc đang tạm dừng) của phòng này: nối lại, không đặt lại đồng hồ.
    if (heldBy === tabId && handledRoom === roomId) {
      return { ok: true, started: false, reason: "already_controller" };
    }
    await chrome.storage.local.set({ liveReAutoRoom: roomId });
    if (heldBy !== tabId) {
      await writeState({ ...current, controllerTabId: tabId });
    }
    return { ok: true, started: false, resumed: true };
  }
  if (handledRoom === roomId) {
    // Người vận hành đã dừng phòng này: chỉ nhận vai để popup thấy tab, không bắt đầu.
    if (heldBy !== tabId) await writeState({ ...current, controllerTabId: tabId });
    return { ok: true, started: false, reason: "stopped_by_operator" };
  }
  await chrome.storage.local.set({ liveReAutoRoom: roomId });
  await writeState({
    running: true,
    startedAtMs: Date.now(),
    pausedElapsedMs: 0,
    revision: Number(current.revision || 0) + 1,
    sessionId: makeSessionId(),
    controllerTabId: tabId
  });
  return { ok: true, started: true };
}

chrome.runtime.onInstalled.addListener(async (details = {}) => {
  const stored = await chrome.storage.local.get(["liveReState", "liveReAiEnabled"]);
  if (!stored.liveReState) await chrome.storage.local.set({ liveReState: DEFAULT_STATE });
  // Fresh install starts off. Updating preserves the operator's existing choice.
  if (details.reason === 'install' || typeof stored.liveReAiEnabled !== 'boolean') {
    await chrome.storage.local.set({ liveReAiEnabled: false });
  }
  await chrome.action.setBadgeText({ text: "RE" });
  await chrome.action.setBadgeBackgroundColor({ color: "#15803d" });
  const tabs = await chrome.tabs.query({url:'https://shop.tiktok.com/*'});
  for (const tab of tabs) {
    if (!Number.isInteger(tab.id)) continue;
    await chrome.scripting.executeScript({target:{tabId:tab.id},files:['live-widget.js','schedule.js','message-delivery.js','content.js','comments.js']}).catch(() => {});
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!String(message?.type || "").startsWith("LIVE_RE_")) return undefined;

  (async () => {
    if (message.type === "LIVE_RE_SCAN_CATALOG") {
      const tabId = await chooseControllerTab();
      const result = await chrome.tabs.sendMessage(tabId, {
        type: "LIVE_RE_SCAN_CATALOG_PAGE"
      });
      sendResponse(result || { ok: false, error: "Tab TikTok chưa trả dữ liệu giỏ" });
      return;
    }

    if (message.type === "LIVE_RE_AUTO_START") {
      const tabId = Number(sender?.tab?.id);
      const run = autoStartQueue.then(
        () => autoStartFromRoom(tabId, sender?.tab?.url, message.roomId));
      autoStartQueue = run.catch(() => {});
      sendResponse(await run);
      return;
    }

    if (message.type === "LIVE_RE_TIKTOK_API") {
      const tabId = sender?.tab?.id;
      const tabUrl = String(sender?.tab?.url || "");
      if (!Number.isInteger(tabId) || !tabUrl.startsWith("https://shop.tiktok.com/")) {
        sendResponse({ ok: false, error: "Yêu cầu API không đến từ TikTok Shop" });
        return;
      }
      const request = message.request || {};
      const diagStartedAt = Date.now();
      const owner = await apiOwnerAllows(sender.tab, request);
      if (owner !== true) {
        const otherBrowser = owner === "other_browser";
        diag.api({tabId, request, blocked: otherBrowser ? "other_browser" : "not_owner"});
        sendResponse({ ok: false, not_owner: true, other_browser: otherBrowser,
          error: otherBrowser ? BROWSER_LEASE_DENIED_MESSAGE
            : "Tab khác đang giữ quyền gọi TikTok (chỉ một tab LIVE được gọi)" });
        return;
      }
      const blocked = apiGuardBlocked(tabId, request);
      if (blocked) {
        diag.api({tabId, request, blocked: "guard_pause"});
        writeApiGuard().catch(() => {});
        sendResponse(blocked);
        return;
      }
      const durableChat = request.action === 'send_chat' && request.delivery;
      if (durableChat) {
        if (!request.expected_room_id || !request.delivery.reply_id || !request.delivery.interaction_id) {
          sendResponse({ok: false, error: 'Thiếu định danh lượt gửi'});
          return;
        }
        const reserved = await reserveChatIntent(request, tabId);
        if (!reserved.ok) { sendResponse(reserved); return; }
        if (reserved.replayed) { sendResponse({...reserved.receipt,replayed:true}); return; }
        if (request.delivery.message_id) {
          const response = await fetch('http://127.0.0.1:8793/api/reply/status', {
            method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request.delivery),
            signal:globalThis.AbortSignal?.timeout?.(10000)});
          const status = await response.json();
          const messageState = status.turn?.messages?.find(m=>m.message_id===request.delivery.message_id);
          if (!response.ok || !status.ok || messageState?.state!=='sending' ||
              messageState.dispatched_at==null || messageState.content!==request.content ||
              messageState.content_hash!==request.delivery.content_hash) {
            await completeChatIntent(request,{ok:false,error:'Tin chưa được backend cấp quyền phát lệnh'});
            sendResponse({ok:false,error:'Tin chưa được backend cấp quyền phát lệnh'}); return;
          }
        }
      }
      let results;
      try { results = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        func: runTikTokLiveApi,
        args: [request]
      }); } catch (error) {
        if (durableChat) await completeChatIntent(request, {ok: false, error: String(error?.message || error)});
        throw error;
      }
      const result = results?.[0]?.result || {
        ok: false,
        error: "TikTok API không trả kết quả"
      };
      apiGuardRecord(tabId, result);
      diag.api({tabId, request, result, startedAt: diagStartedAt});
      if (durableChat) {
        if (result.ok) result.sent_at_ms = Date.now();
        await completeChatIntent(request, result);
        const receiptMessage = result.ok ? {
          type: request.delivery.message_id ? 'LIVE_RE_MESSAGE_ACK' : 'LIVE_RE_AI_ACK', payload: {...request.delivery, receipt: {
            room_id: result.room_id, response_code: result.response_code, sent_at_ms: result.sent_at_ms
          }}
        } : {type: request.delivery.message_id ? 'LIVE_RE_MESSAGE_UNKNOWN' : 'LIVE_RE_AI_DELIVERY', payload: {...request.delivery, status: 'unknown', detail: result.error}};
        await persistAndDeliverReceipt(receiptMessage);
      }
      sendResponse(result);
      return;
    }

    if (message.type === 'LIVE_RE_MESSAGE_CONTROL') {
      if (!String(sender?.tab?.url || '').startsWith('https://shop.tiktok.com/')) throw new Error('Yêu cầu không đến từ TikTok Shop');
      if (!['status','claim','dispatched'].includes(message.operation)) throw new Error('Tác vụ gửi không hợp lệ');
      const response = await fetch(`http://127.0.0.1:8793/api/reply/${message.operation}`, {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(message.payload),
        signal:globalThis.AbortSignal?.timeout?.(10000)});
      sendResponse(await response.json());
      return;
    }
    if (['LIVE_RE_MESSAGE_ACK','LIVE_RE_MESSAGE_UNKNOWN'].includes(message.type)) {
      if (!String(sender?.tab?.url || '').startsWith('https://shop.tiktok.com/')) throw new Error('Yêu cầu không đến từ TikTok Shop');
      sendResponse(await persistAndDeliverReceipt(message)); return;
    }

    if (message.type === "LIVE_RE_INSTALL_COMMENT_BRIDGE") {
      const tabId = sender?.tab?.id;
      const tabUrl = String(sender?.tab?.url || "");
      if (!Number.isInteger(tabId) || !tabUrl.startsWith("https://shop.tiktok.com/")) {
        sendResponse({ ok: false, error: "Yêu cầu không đến từ TikTok Shop" });
        return;
      }
      const results = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        func: installTikTokCommentBridge,
        args: [String(message.nonce || "")]
      });
      sendResponse(results?.[0]?.result || { ok: false, error: "Không cài được cầu bình luận" });
      return;
    }

    if (
      message.type === "LIVE_RE_AI_HEALTH" ||
      message.type === "LIVE_RE_AI_REPLY" ||
      message.type === "LIVE_RE_AI_ACK" ||
      message.type === "LIVE_RE_AI_DELIVERY" ||
      message.type === "LIVE_RE_QUEUE_ENQUEUE" ||
      message.type === "LIVE_RE_QUEUE_NEXT" ||
      message.type === "LIVE_RE_QUEUE_CANCEL" ||
      message.type === "LIVE_RE_CATALOG_SYNC" ||
      message.type === "LIVE_RE_OBSERVE" ||
      message.type === "LIVE_RE_HEARTBEAT" ||
      message.type === "LIVE_RE_PRIVACY_PURGE_ACK" ||
      message.type === "LIVE_RE_VIDEO_POSITION" ||
      message.type === "LIVE_RE_VIDEO_TIMELINES"
    ) {
      const tabUrl = String(sender?.tab?.url || "");
      // Popup của chính tiện ích được hỏi /health (không có tab).
      const fromPopup = !sender?.tab && sender?.id === chrome.runtime.id && message.type === "LIVE_RE_AI_HEALTH";
      if (!fromPopup && !tabUrl.startsWith("https://shop.tiktok.com/")) {
        sendResponse({ ok: false, error: "Yêu cầu AI không đến từ TikTok Shop" });
        return;
      }
      if (receiptUrls[message.type]) {
        sendResponse(await persistAndDeliverReceipt(message));
        return;
      }
      retrySavedReceipts();
      if (message.type === "LIVE_RE_HEARTBEAT") renewBrowserLeaseOnHeartbeat(Number(sender?.tab?.id || 0));
      const ENDPOINTS = {
        LIVE_RE_AI_REPLY: "http://127.0.0.1:8793/api/reply",
        LIVE_RE_AI_ACK: "http://127.0.0.1:8793/api/reply/ack",
        LIVE_RE_AI_DELIVERY: "http://127.0.0.1:8793/api/reply/delivery",
        LIVE_RE_QUEUE_ENQUEUE: "http://127.0.0.1:8793/api/tiktok/queue/enqueue",
        LIVE_RE_QUEUE_NEXT: "http://127.0.0.1:8793/api/tiktok/queue/next",
        LIVE_RE_QUEUE_CANCEL: "http://127.0.0.1:8793/api/tiktok/queue/cancel",
        LIVE_RE_CATALOG_SYNC: "http://127.0.0.1:8793/api/catalog/sync",
        LIVE_RE_OBSERVE: "http://127.0.0.1:8793/api/observe",
        LIVE_RE_HEARTBEAT: "http://127.0.0.1:8793/api/heartbeat",
        LIVE_RE_PRIVACY_PURGE_ACK: "http://127.0.0.1:8793/api/privacy/purge-ack",
        LIVE_RE_AI_HEALTH: "http://127.0.0.1:8793/health",
        LIVE_RE_VIDEO_POSITION: "http://127.0.0.1:8793/api/video/position?platform=tiktok",
        LIVE_RE_VIDEO_TIMELINES: "http://127.0.0.1:8793/api/video/timelines"
      };
      const isHealth = message.type === "LIVE_RE_AI_HEALTH" ||
        message.type === "LIVE_RE_VIDEO_POSITION" ||
        message.type === "LIVE_RE_VIDEO_TIMELINES";
      // Như extension-shopee: trả lời AI / queue next chờ tới 120s, còn lại 10s.
      const longWait = message.type === "LIVE_RE_AI_REPLY" || message.type === "LIVE_RE_QUEUE_NEXT";
      const signal = globalThis.AbortSignal?.timeout?.(longWait ? 120000 : 10000);
      const response = await fetch(
        ENDPOINTS[message.type],
        !isHealth ? {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(message.payload || {}),
          signal
        } : { signal }
      );
      const result = await response.json().catch(() => ({}));
      sendResponse(response.ok ? result : {
        ok: false,
        error: result?.error || `AI local HTTP ${response.status}`
      });
      return;
    }

    const current = await readState();
    const now = Date.now();
    let next = current;

    if (message.type === "LIVE_RE_GET_STATE") {
      const tabId = Number(sender?.tab?.id || 0);
      sendResponse({
        ok: true,
        state: current,
        isController: tabId === Number(current.controllerTabId || 0),
        isObserver: message.claimObserver === true
          ? await claimObserver(tabId)
          : undefined
      });
      return;
    }

    if (message.type === "LIVE_RE_START") {
      const controllerTabId = await chooseControllerTab();
      next = {
        running: true,
        startedAtMs: now,
        pausedElapsedMs: 0,
        revision: Number(current.revision || 0) + 1,
        sessionId: makeSessionId(),
        controllerTabId
      };
    } else if (message.type === "LIVE_RE_START_AT") {
      const requestedElapsedMs = Number(message.elapsedMs);
      if (!Number.isFinite(requestedElapsedMs) || requestedElapsedMs < 0) {
        throw new Error("Mốc thời gian không hợp lệ");
      }
      // Giới hạn đủ rộng để nhập tổng thời gian của một phiên LIVE dài.
      const offsetMs = Math.min(requestedElapsedMs, 7 * 24 * 60 * 60 * 1000);
      const controllerTabId = await chooseControllerTab();
      next = {
        running: true,
        startedAtMs: now - offsetMs,
        pausedElapsedMs: offsetMs,
        revision: Number(current.revision || 0) + 1,
        sessionId: makeSessionId(),
        controllerTabId
      };
    } else if (message.type === "LIVE_RE_RESYNC") {
      // Chinh dong ho theo vi tri video nghe duoc. Khac START_AT o cho no giu
      // nguyen sessionId va tab dieu khien, nen mot lan chinh khong lam mat
      // ca phien thu thap lan trang thai ghim.
      const elapsed = Number(message.elapsedMs);
      if (!current.running || !Number.isFinite(elapsed) || elapsed < 0) {
        sendResponse({ ok: false, error: "Khong the dong bo khi dong ho chua chay" });
        return;
      }
      next = {
        ...current,
        startedAtMs: now - Math.min(elapsed, 7 * 24 * 60 * 60 * 1000),
        pausedElapsedMs: Math.min(elapsed, 7 * 24 * 60 * 60 * 1000),
        revision: Number(current.revision || 0) + 1
      };
    } else if (message.type === "LIVE_RE_PAUSE") {
      next = {
        ...current,
        running: false,
        pausedElapsedMs: elapsedFor(current, now),
        revision: Number(current.revision || 0) + 1
      };
    } else if (message.type === "LIVE_RE_RESUME") {
      const paused = elapsedFor(current, now);
      next = {
        ...current,
        running: true,
        startedAtMs: now - paused,
        pausedElapsedMs: paused,
        revision: Number(current.revision || 0) + 1
      };
    } else if (message.type === "LIVE_RE_ADJUST") {
      const deltaMs = Math.max(-10000, Math.min(10000, Number(message.deltaMs) || 0));
      const elapsed = Math.max(0, elapsedFor(current, now) + deltaMs);
      next = {
        ...current,
        startedAtMs: current.running ? now - elapsed : current.startedAtMs,
        pausedElapsedMs: elapsed,
        revision: Number(current.revision || 0) + 1
      };
    } else if (message.type === "LIVE_RE_STOP") {
      next = {
        ...DEFAULT_STATE,
        revision: Number(current.revision || 0) + 1,
        sessionId: current.sessionId,
        // Giữ controller đủ lâu để đúng tab thực hiện lệnh bỏ ghim.
        controllerTabId: current.controllerTabId
      };
      await chrome.storage.local.set({ liveReAiEnabled: false });
    } else {
      return;
    }

    const saved = await writeState(next);
    sendResponse({ ok: true, state: saved });
  })().catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));

  return true;
});

// Chỉ phục vụ kiểm thử Node; `module` không tồn tại trong service worker Chrome.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    runTikTokLiveApi, installTikTokCommentBridge, chooseControllerTab, claimObserver,
    autoStartFromRoom
  };
}
