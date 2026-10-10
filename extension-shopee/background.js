"use strict";
importScripts("shopee-api.js", "core.js");
const pendingShopeeReplies = new Map();
let pendingLoaded;
let receiptFlush = Promise.resolve();
async function loadPendingReplies() {
  if (!pendingLoaded) pendingLoaded = (async () => {
    const saved = await chrome.storage.local.get('shopeePendingReplies');
    for (const [id, record] of Object.entries(saved.shopeePendingReplies || {})) {
      if (record && typeof record.sessionId === 'string' && typeof record.text === 'string') {
        pendingShopeeReplies.set(id, record);
      }
    }
  })();
  await pendingLoaded;
}
async function savePendingReplies() {
  await chrome.storage.local.set({shopeePendingReplies: Object.fromEntries(pendingShopeeReplies)});
}
// Lịch thử lại receipt: 1,2,5,10,30s rồi mỗi 60s, tối đa 24h kể từ lúc tạo bản ghi
// biên nhận. chrome.alarms bị kẹp ~30s trong MV3 nên bước <30s dùng setTimeout
// (chỉ trong bộ nhớ); alarm vẫn là lưới an toàn khi service worker bị tắt.
const RECEIPT_MAX_AGE_MS = 86400000;
const receiptTimers = new Map();
function receiptRetryDelay(attempt) {
  return [1000, 2000, 5000, 10000, 30000][attempt - 1] || 60000;
}
function scheduleReceiptRetry(id, record) {
  const delay = Math.max(0, Number(record.nextReceiptAt) - Date.now());
  chrome.alarms?.create('shopee-receipts', {when: record.nextReceiptAt});
  if (delay < 30000) {
    clearTimeout(receiptTimers.get(id));
    receiptTimers.set(id, setTimeout(() => { receiptTimers.delete(id); retryPendingReceipts(); }, delay));
  }
}
// Đặt lịch lần đầu cho biên nhận; receiptCreatedAt chỉ ghi một lần.
function queueReceipt(id, record, delay = 1000) {
  if (!record.receiptCreatedAt) record.receiptCreatedAt = Date.now();
  record.nextReceiptAt = Date.now() + delay;
  scheduleReceiptRetry(id, record);
}
// Quá 24h: ngừng thử, GIỮ bản ghi để đối soát, không bao giờ xoá hay gửi lại nội dung.
function receiptExpired(record) {
  return Date.now() - Number(record.receiptCreatedAt ?? record.at ?? 0) >= RECEIPT_MAX_AGE_MS;
}
async function flushPendingReceipts() {
  await loadPendingReplies();
  for (const [id, record] of pendingShopeeReplies) {
    if (!record.deliveryPayload || record.expired_for_reconcile) continue;
    if (receiptExpired(record)) {
      record.expired_for_reconcile = true;
      await savePendingReplies();
      continue;
    }
    if (Date.now() + 100 < Number(record.nextReceiptAt || 0)) continue;
    try {
      const response = await backendFetch(record.protocol_version === 2
        ? `http://127.0.0.1:8793/api/shopee/reply/${record.deliveryOperation || 'receipt'}`
        : 'http://127.0.0.1:8793/api/shopee/delivery', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(record.deliveryPayload)
      });
      const body = response.ok ? await response.json() : null;
      if (!body?.ok) throw new Error('Receipt not accepted');
      pendingShopeeReplies.delete(id);
    } catch {
      record.receiptAttempts = Number(record.receiptAttempts || 0) + 1;
      queueReceipt(id, record, receiptRetryDelay(record.receiptAttempts));
    }
    await savePendingReplies();
  }
}
function retryPendingReceipts() {
  receiptFlush = receiptFlush.then(flushPendingReceipts).catch(() => {});
  return receiptFlush;
}
chrome.alarms?.onAlarm?.addListener(alarm => {
  if (alarm.name === 'shopee-receipts') retryPendingReceipts();
});
chrome.runtime.onStartup?.addListener(() => retryPendingReceipts());
let connectionQueue = Promise.resolve();
function backendFetch(url, options = {}) {
  return fetch(url, {...options, signal: globalThis.AbortSignal?.timeout?.(url.endsWith('/api/shopee/reply') || url.endsWith('/queue/next') ? 120000 : 10000)});
}
async function readConfig() {
  const stored = await chrome.storage.local.get(['shopeeConfig','shopeeAiRequested']);
  const config = {...stored.shopeeConfig};
  if (typeof stored.shopeeAiRequested === 'boolean') {
    config.aiEnabled = stored.shopeeAiRequested; config.aiAutoSend = stored.shopeeAiRequested;
  }
  return config;
}
chrome.runtime.onInstalled?.addListener(async () => {
  const tabs = await chrome.tabs.query({ url: "https://live.shopee.vn/*" });
  for (const tab of tabs) {
    if (!globalThis.KidTEShopeeCore.liveSession(tab.url, true)) continue;
    await chrome.scripting.executeScript({target: {tabId: tab.id}, func: () => {
      globalThis.KidTEShopeeControllerStop?.();
      globalThis.KidTEShopeeControllerStarted = false;
      globalThis.KidTEShopeeAiStop?.();
      globalThis.KidTEShopeeAiStarted = false;
    }}).catch(() => {});
    await chrome.scripting.executeScript({target: {tabId: tab.id},
      files: ["live-widget.js", "core.js", "message-delivery.js", "controller.js", "ai-controller.js"]}).catch(() => {});
  }
});

async function connectPage(tab) {
  const sessionId = globalThis.KidTEShopeeCore.liveSession(tab.url, true);
  const stored = await chrome.storage.local.get(["shopeeConfig", "shopeeOwnerLease", "shopeePaused", "shopeeAiRequested"]);
  const old = stored.shopeeConfig || {};
  const lease = stored.shopeeOwnerLease || {};
  if (old.tabId !== tab.id && Number.isInteger(old.tabId) && Date.now() - Number(lease.at || 0) < 12000) {
    let owner = null;
    try { owner = await chrome.tabs.get(old.tabId); } catch {}
    if (globalThis.KidTEShopeeCore.liveSession(owner?.url, true) === old.sessionId) {
      return { ok: true, controller: false, error: "Một tab Shopee khác đang điều khiển" };
    }
  }
  const response = await backendFetch("http://127.0.0.1:8793/api/video/position?platform=shopee", { cache: "no-store" });
  if (!response.ok) throw new Error(`Kid TE trả HTTP ${response.status}`);
  const position = await response.json();
  const controls = await chrome.storage.local.get(["shopeePaused", "shopeeAiRequested"]);
  const sameSession = old.tabId === tab.id && old.sessionId === sessionId;
  const config = { ...old, sessionId, tabId: tab.id,
    videoId: position.available ? String(position.video_id || "") : "",
    enabled: !controls.shopeePaused && Boolean(globalThis.KidTEShopeeCore.liveSession(tab.url)),
    aiEnabled: typeof controls.shopeeAiRequested === "boolean" ? controls.shopeeAiRequested : (sameSession && Boolean(old.aiEnabled)),
    aiAutoSend: typeof controls.shopeeAiRequested === "boolean" ? controls.shopeeAiRequested : (sameSession && Boolean(old.aiEnabled)) };
  const update = { shopeeOwnerLease: { at: Date.now(), tabId: tab.id, sessionId } };
  if (JSON.stringify(config) !== JSON.stringify(old)) update.shopeeConfig = config;
  await chrome.storage.local.set(update);
  return { ok: true, controller: true, config, position, paused: Boolean(controls.shopeePaused) };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!String(message?.type || "").startsWith("KIDTE_SHOPEE_")) return undefined;
  (async () => {
    const isPage = Boolean(sender.tab?.id && globalThis.KidTEShopeeCore.liveSession(sender.tab.url));
    const isManagement = Boolean(sender.tab?.id && globalThis.KidTEShopeeCore.liveSession(sender.tab.url, true));
    const isPopup = !sender.tab && String(sender.url || "").startsWith(chrome.runtime.getURL("popup.html"));
    if (!isManagement && !isPopup) throw new Error("Nguồn lệnh Shopee không hợp lệ");
    await loadPendingReplies();
    if (message.resource !== 'delivery') retryPendingReceipts();
    if (message.type === "KIDTE_SHOPEE_CONNECT") {
      if (!isManagement) throw new Error("Chỉ trang Shopee được tự kết nối");
      const next = connectionQueue.then(() => connectPage(sender.tab));
      connectionQueue = next.catch(() => {});
      sendResponse(await next);
      return;
    }
    if (message.type === 'KIDTE_SHOPEE_MESSAGE') {
      if (!['status','claim','dispatched','receipt','failure'].includes(message.operation)) throw new Error('Tác vụ gửi không hợp lệ');
      const config = await readConfig();
      if (!isPage || config.tabId !== sender.tab.id || config.sessionId !== message.payload?.room_id ||
          config.sessionId !== globalThis.KidTEShopeeCore.liveSession(sender.tab.url, true)) throw new Error('Sai tab hoặc phòng gửi');
      if (['claim','dispatched'].includes(message.operation) && !config.aiEnabled) throw new Error('AI đã tắt');
      const pending = pendingShopeeReplies.get(message.payload?.message_id);
      if (message.operation === 'failure' && pending?.deliveryOperation === 'receipt') {
        sendResponse({ok:false,error:'success_receipt_already_pending'}); return;
      }
      if (['receipt','failure'].includes(message.operation) && pending) {
        pending.deliveryPayload = message.payload;
        pending.deliveryOperation = message.operation;
        queueReceipt(message.payload.message_id, pending);
        await savePendingReplies();
      }
      const response = await backendFetch(`http://127.0.0.1:8793/api/shopee/reply/${message.operation}`, {
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(message.payload)});
      const result = await response.json();
      if (result?.ok && pending && ['receipt','failure'].includes(message.operation)) {
        pendingShopeeReplies.delete(message.payload.message_id); await savePendingReplies();
      }
      sendResponse(result); return;
    }
    if (message.type === "KIDTE_SHOPEE_BACKEND") {
      if (!["position", "timelines", "mapping", "sync", "ai_status", "reply", "delivery", "heartbeat", "queue_enqueue", "queue_next", "queue_cancel", "privacy_purge_ack"].includes(message.resource)) throw new Error("Tài nguyên Kid TE không hợp lệ");
      const method = ["mapping", "sync", "reply", "delivery", "heartbeat", "queue_enqueue", "queue_next", "queue_cancel", "privacy_purge_ack"].includes(message.resource) && message.method === "POST" ? "POST" : "GET";
      if (message.resource === "heartbeat") {
        const config = await readConfig();
        if (!isManagement || method !== "POST" || config.tabId !== sender.tab.id ||
            config.sessionId !== message.payload?.session_id) throw new Error("Tab này không điều khiển Shopee");
      }
      if (message.resource === "privacy_purge_ack") {
        // F02: xoá bản ghi biên nhận/trả lời chờ khớp danh tính, rồi mới báo backend.
        if (!isManagement || method !== "POST") throw new Error("Tab này không điều khiển Shopee");
        const items = Array.isArray(message.payload?.items) ? message.payload.items : [];
        const before = pendingShopeeReplies.size;
        for (const [id, record] of [...pendingShopeeReplies]) {
          if (items.some(item => globalThis.KidTEShopeeCore.purgeMentions(record, item?.identifiers))) pendingShopeeReplies.delete(id);
        }
        if (pendingShopeeReplies.size !== before) await savePendingReplies();
        const ackResponse = await backendFetch("http://127.0.0.1:8793/api/privacy/purge-ack", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ids: items.map(item => item?.id).filter(Boolean), platform: "shopee"})});
        sendResponse(await ackResponse.json()); return;
      }
      if (message.resource === "queue_enqueue") {
        // Chỉ tab đang điều khiển đúng phiên mới được báo comment lên hàng chờ.
        const config = await readConfig();
        if (!isPage || method !== "POST" || !config.aiEnabled || config.tabId !== sender.tab.id ||
            config.sessionId !== message.payload?.room_id) throw new Error("Tab này không điều khiển Shopee");
      }
      if (message.resource === "queue_next" || message.resource === "queue_cancel") {
        // Như queue_enqueue; huỷ vẫn được phép khi AI vừa tắt (dọn hàng bỏ cục bộ).
        const config = await readConfig();
        if (!isPage || method !== "POST" || (message.resource === "queue_next" && !config.aiEnabled) ||
            config.tabId !== sender.tab.id || config.sessionId !== message.payload?.room_id ||
            config.sessionId !== new URL(sender.tab.url).searchParams.get("session")) throw new Error("Tab này không điều khiển Shopee");
      }
      if (message.resource === "sync") {
        const config = await readConfig();
        if (!isManagement || method !== "POST" || config.tabId !== sender.tab.id ||
            config.sessionId !== message.payload?.session_id ||
            config.sessionId !== globalThis.KidTEShopeeCore.liveSession(sender.tab.url, true)) {
          throw new Error("Chỉ tab điều khiển được đồng bộ giỏ");
        }
      }
      if ((message.resource === "mapping" && method === "POST" && !isPopup) ||
          (["reply", "delivery"].includes(message.resource) && (!isPage || method !== "POST"))) {
        throw new Error("Chỉ popup Shopee được lưu ánh xạ");
      }
      const path = message.resource === "heartbeat" ? "/api/heartbeat" : message.resource === "queue_enqueue" ? "/api/shopee/queue/enqueue" : message.resource === "queue_next" ? "/api/shopee/queue/next" : message.resource === "queue_cancel" ? "/api/shopee/queue/cancel" : ["mapping", "sync", "reply", "delivery", "ai_status"].includes(message.resource)
        ? `/api/shopee/${message.resource === "ai_status" ? "ai/status" : message.resource === "mapping" ? "mappings" : message.resource}`
        : `/api/video/${message.resource === "position" ? "position?platform=shopee" : "timelines"}`;
      if (["reply", "delivery"].includes(message.resource)) {
        const config = await readConfig();
        if ((message.resource === "reply" && !config.aiEnabled) || config.tabId !== sender.tab.id ||
            config.sessionId !== new URL(sender.tab.url).searchParams.get("session")) {
          throw new Error("AI Shopee chưa bật cho phiên này");
        }
        if (message.resource === "reply" && (message.payload?.session_id !== config.sessionId ||
            message.payload?.video_id !== config.videoId)) throw new Error("Ngữ cảnh AI sai phiên");
        if (message.resource === "reply") {
          const positionResponse = await backendFetch("http://127.0.0.1:8793/api/video/position?platform=shopee", {cache: "no-store"});
          if (!positionResponse.ok) {
            throw new Error("Kid TE không xác nhận được ngữ cảnh AI");
          }
          const position = await positionResponse.json();
          if (!position?.ok || !position.available || position.video_id !== config.videoId ||
              !Number.isFinite(Number(position.age_s)) || Number(position.age_s) < 0 || Number(position.age_s) > 25 ||
              !Number.isFinite(Number(position.margin)) || Number(position.margin) < Math.max(Number(position.min_margin) || .06, .06)) {
            throw new Error("Nguồn video AI đã đổi hoặc mất cập nhật");
          }
          if (message.payload?.product_id || message.payload?.shop_id) {
            const displayed = await chrome.scripting.executeScript({
              target: {tabId: sender.tab.id}, world: "MAIN", func: globalThis.KidTEShopeeApi,
              args: [{action: "state", expected_session_id: config.sessionId}]
            });
            const pin = displayed?.[0]?.result;
            if (!pin?.ok || !pin.available || pin.product_id !== message.payload.product_id || pin.shop_id !== message.payload.shop_id) {
              throw new Error("Sản phẩm đang hiển thị đã đổi; chờ cập nhật ngữ cảnh");
            }
          }
        }
      }
      if (message.resource === "queue_next") {
        // Cùng kiểm tra nguồn video như /reply, trước khi backend sinh câu trả lời.
        const config = await readConfig();
        const positionResponse = await backendFetch("http://127.0.0.1:8793/api/video/position?platform=shopee", {cache: "no-store"});
        if (!positionResponse.ok) throw new Error("Kid TE không xác nhận được ngữ cảnh AI");
        const position = await positionResponse.json();
        if (!position?.ok || !position.available || position.video_id !== config.videoId ||
            !Number.isFinite(Number(position.age_s)) || Number(position.age_s) < 0 || Number(position.age_s) > 25 ||
            !Number.isFinite(Number(position.margin)) || Number(position.margin) < Math.max(Number(position.min_margin) || .06, .06)) {
          throw new Error("Nguồn video AI đã đổi hoặc mất cập nhật");
        }
      }
      if (message.resource === 'delivery') {
        const pending = pendingShopeeReplies.get(message.payload?.turn_id);
        if (pending && pending.sessionId === message.payload?.session_id) {
          pending.deliveryPayload = message.payload;
          queueReceipt(message.payload?.turn_id, pending);
          await savePendingReplies();
        }
      }
      const response = await backendFetch(`http://127.0.0.1:8793${path}`, method === "POST" ? {
        method, headers: { "Content-Type": "application/json" },
        body: JSON.stringify(message.payload || {})
      } : { cache: "no-store" });
      if (!response.ok) {
        if (message.resource === "mapping" && response.status === 404) {
          throw new Error("Kid TE đang chạy mã cũ; hãy khởi động lại Kid TE");
        }
        throw new Error(`Kid TE trả HTTP ${response.status}`);
      }
      const body = await response.json();
      // queue_next trả lượt trả lời trong body.result: đăng ký như /reply để chat_send/biên nhận hoạt động.
      const turnBody = message.resource === "queue_next" ? (body?.available ? body.result : null) : body;
      if ((message.resource === "reply" || message.resource === "queue_next") && turnBody?.should_send && turnBody.turn_id) {
        for (const [id, pending] of pendingShopeeReplies) {
          if (pending.state === 'ready' && Date.now() - pending.at > 120000) pendingShopeeReplies.delete(id);
        }
        if (turnBody.protocol_version === 2) {
          for (const row of turnBody.messages) {
            if (!pendingShopeeReplies.has(row.message_id)) pendingShopeeReplies.set(row.message_id, {
              protocol_version:2,text:row.content,sessionId:turnBody.room_id,tabId:sender.tab.id,at:Date.now(),state:'ready',
              turn_id:turnBody.turn_id,message_id:row.message_id,content_hash:row.content_hash,
              interaction_id:turnBody.interaction_id,room_id:turnBody.room_id});
          }
        } else if (!pendingShopeeReplies.has(turnBody.turn_id)) {
          pendingShopeeReplies.set(turnBody.turn_id, { text: turnBody.reply, sessionId: message.payload.session_id || message.payload.room_id,
            tabId: sender.tab.id, at: Date.now(), state: 'ready' });
        }
        await savePendingReplies();
      }
      if (message.resource === "delivery" && body?.ok) {
        pendingShopeeReplies.delete(message.payload?.turn_id);
        await savePendingReplies();
      }
      sendResponse(body);
      return;
    }
    if (message.type === "KIDTE_SHOPEE_API") {
      if (!Number.isInteger(sender.tab?.id) && !isPopup) throw new Error("Tab Shopee không hợp lệ");
      const tab = isPopup ? (Number.isInteger(message.tabId)
        ? await chrome.tabs.get(message.tabId)
        : await globalThis.KidTEShopeeCore.findLiveTab(chrome.tabs)) : sender.tab;
      const url = new URL(tab?.url || "about:blank");
      if (!globalThis.KidTEShopeeCore.liveSession(tab?.url, message.request?.action === "catalog")) throw new Error("Hãy chọn tab Shopee LIVE; trang chuẩn bị chỉ được quét giỏ");
      const config = await readConfig();
      const action = message.request?.action;
      if (["chat_poll", "chat_send"].includes(action)) {
        if (!isPage || !config.aiEnabled || config.tabId !== sender.tab.id ||
            config.sessionId !== url.searchParams.get("session")) {
          throw new Error("AI Shopee chưa bật cho phiên này");
        }
        if (action === "chat_send") {
          if (!config.aiAutoSend) throw new Error("AI Shopee chưa được bật gửi thật");
          const pending = pendingShopeeReplies.get(message.request?.turn_id);
          if (!pending || pending.state !== 'ready' || pending.tabId !== sender.tab.id || pending.sessionId !== config.sessionId ||
              pending.text !== message.request?.text || (!pending.protocol_version && Date.now() - pending.at > 60000)) {
            throw new Error("Câu gửi không khớp DeepSeek của phiên này");
          }
        }
      } else if (action === "state") {
        if (!isPage || config.tabId !== sender.tab.id || config.sessionId !== url.searchParams.get("session")) {
          throw new Error("Tab này không điều khiển phiên Shopee");
        }
      } else if (action !== "catalog") {
        if (!isPage || !config.enabled || config.tabId !== sender.tab.id ||
            config.sessionId !== url.searchParams.get("session")) {
          throw new Error("Phiên Shopee chưa được bật ghim");
        }
        const mappingResponse = await backendFetch("http://127.0.0.1:8793/api/shopee/mappings", { cache: "no-store" });
        const mappingData = mappingResponse.ok ? await mappingResponse.json() : null;
        const record = mappingData?.videos?.[config.videoId];
        if (!record) throw new Error("Kid TE chưa lưu ánh xạ cho video này");
        const [positionResponse, timelineResponse] = await Promise.all([
          backendFetch("http://127.0.0.1:8793/api/video/position?platform=shopee", { cache: "no-store" }),
          backendFetch("http://127.0.0.1:8793/api/video/timelines", { cache: "no-store" })
        ]);
        if (!positionResponse.ok || !timelineResponse.ok) throw new Error("Kid TE không xác nhận được timeline");
        const wanted = globalThis.KidTEShopeeCore.desired(
          { ...config, ...record }, await positionResponse.json(),
          (await timelineResponse.json()).timelines);
        const refreshing = action === "hide" && message.request.refresh === true;
        if (!wanted.ok || wanted.action !== (refreshing ? "show" : action) ||
            ((action === "show" || refreshing) && (wanted.product_id !== String(message.request.product_id) ||
              wanted.shop_id !== String(message.request.shop_id)))) {
          throw new Error("Lệnh ghim không còn khớp vị trí video hiện tại");
        }
      }
      if (["show", "hide", "chat_send"].includes(action)) {
        const latest = (await chrome.storage.local.get(["shopeeConfig", "shopeePaused", "shopeeAiRequested"]));
        const current = {...latest.shopeeConfig};
        if (typeof latest.shopeeAiRequested === 'boolean') { current.aiEnabled = latest.shopeeAiRequested; current.aiAutoSend = latest.shopeeAiRequested; }
        if (current.tabId !== tab.id || current.sessionId !== config.sessionId ||
            current.videoId !== config.videoId ||
            (action === "chat_send" ? !current.aiEnabled || !current.aiAutoSend : !current.enabled || latest.shopeePaused)) {
          throw new Error("Trạng thái đã đổi; hủy lệnh Shopee đang chờ");
        }
      }
      if (action === 'chat_send') {
        const pending = pendingShopeeReplies.get(message.request.turn_id);
        if (!pending || pending.state !== 'ready') throw new Error('Lượt này đã phát lệnh gửi');
        if (pending.protocol_version===2) {
          const response = await backendFetch('http://127.0.0.1:8793/api/shopee/reply/status',{
            method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({turn_id:pending.turn_id,room_id:pending.room_id})});
          const status = await response.json();
          const row = status.turn?.messages?.find(m=>m.message_id===pending.message_id);
          if(!status.ok || row?.state!=='sending' || row.dispatched_at==null || row.content!==pending.text ||
              row.content_hash!==pending.content_hash) throw new Error('Tin chưa được backend cấp quyền phát lệnh');
        }
        pending.state = 'sending';
        await savePendingReplies();
      }
      let results;
      try {
        results = await chrome.scripting.executeScript({
          target: { tabId: tab.id }, world: "MAIN", func: globalThis.KidTEShopeeApi,
          args: [{ ...message.request, expected_session_id: url.searchParams.get("session") }]
        });
      } catch (error) {
        if (action === 'chat_send') {
          const pending = pendingShopeeReplies.get(message.request.turn_id);
          if (pending) {
            pending.state = 'unknown';
            pending.deliveryOperation = pending.protocol_version === 2 ? 'failure' : undefined;
            pending.deliveryPayload = pending.protocol_version === 2 ? {
              turn_id:pending.turn_id,message_id:pending.message_id,room_id:pending.room_id,content_hash:pending.content_hash
            } : {session_id: pending.sessionId, turn_id: message.request.turn_id,
                                      status: 'unknown', error: 'send result unknown'};
            queueReceipt(message.request.turn_id, pending);
            await savePendingReplies();
          }
        }
        throw error;
      }
      if (action === 'chat_send') {
        const result = results?.[0]?.result;
        const pending = pendingShopeeReplies.get(message.request.turn_id);
        if (pending && result?.ok && result.message_id) {
          result.sent_at_ms = Date.now();
          pending.state = 'sent';
          pending.deliveryOperation = pending.protocol_version === 2 ? 'receipt' : undefined;
          pending.deliveryPayload = pending.protocol_version === 2 ? {
            turn_id:pending.turn_id,message_id:pending.message_id,room_id:pending.room_id,content_hash:pending.content_hash,
            receipt:{room_id:pending.room_id,shopee_message_id:String(result.message_id),sent_at_ms:result.sent_at_ms}
          } : {session_id: pending.sessionId, turn_id: message.request.turn_id,
                                    message_id: String(result.message_id)};
          queueReceipt(message.request.turn_id, pending);
          await savePendingReplies();
        }
      }
      sendResponse(results?.[0]?.result || { ok: false, error: "Shopee API không phản hồi" });
      return;
    }
    throw new Error("Lệnh Shopee không hợp lệ");
  })().catch((error) => sendResponse({ ok: false, error: String(error.message || error) }));
  return true;
});
