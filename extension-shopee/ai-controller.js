"use strict";
(() => {
  if (globalThis.KidTEShopeeAiStarted) return;
  globalThis.KidTEShopeeAiStarted = true;
  const queue = [];
  const seen = new Set();
  let busy = false;
  let lastHandledAt = 0;
  let catalog = null;
  let catalogAt = 0;
  let stopped = false;
  let timer;
  let ownerSession = "";
  let protocolV2 = false, protocolCheckedAt = 0;
  let queueApi = false, queueSyncBusy = false, queueSyncAgain = false;
  let pendingWrites = Promise.resolve();
  const deadline = row => Number.isFinite(row.firstSentAtMs) ? row.firstSentAtMs + 60000 : row.receivedAtMs + 120000;
  const persistQueue = () => {
    const snapshot = {sessionId:ownerSession,rows:queue.map(row=>({...row}))};
    const task = pendingWrites.then(()=>save({shopeeAiQueue:snapshot}));
    pendingWrites = task.catch(()=>{});
    return task;
  };
  // F02: backend privacy_purge — drop matching queue rows and cached customer text, then acknowledge.
  globalThis.KidTEShopeeAiPurge = async items => {
    const core = globalThis.KidTEShopeeCore;
    const valid = (Array.isArray(items) ? items : []).filter(item =>
      typeof item?.id === 'string' && Array.isArray(item.identifiers) && item.identifiers.length);
    if (!valid.length) return [];
    const hit = value => valid.some(item => core.purgeMentions(value, item.identifiers));
    try {
      const kept = queue.filter(row => !hit(row));
      if (kept.length !== queue.length) { queue.length = 0; queue.push(...kept); }
      await persistQueue();
      const saved = await call(() => chrome.storage.local.get(['shopeePinLog','shopeeAiStatus','shopeeAiLastActivity','shopeeStatus']));
      const update = {};
      if (Array.isArray(saved.shopeePinLog)) {
        const log = saved.shopeePinLog.filter(entry => !hit(entry));
        if (log.length !== saved.shopeePinLog.length) update.shopeePinLog = log;
      }
      for (const key of ['shopeeAiStatus','shopeeAiLastActivity','shopeeStatus']) {
        if (saved[key] && hit(saved[key])) update[key] = {sessionId: ownerSession, ok: true, label: '', at: Date.now()};
      }
      if (Object.keys(update).length) await save(update);
      const ack = await send({type:'KIDTE_SHOPEE_BACKEND', resource:'privacy_purge_ack', method:'POST',
        payload:{items: valid.map(item => ({id: item.id, identifiers: item.identifiers}))}});
      return ack?.ok ? valid.map(item => item.id) : [];
    } catch { return []; }
  };
  const stop = () => { stopped = true; clearInterval(timer); queue.length = 0; };
  globalThis.KidTEShopeeAiStop = stop;
  const alive = () => {
    try { return !stopped && Boolean(chrome.runtime?.id); }
    catch { return false; }
  };
  const call = async fn => {
    if (!alive()) { stop(); throw new Error('Extension context invalidated.'); }
    return await fn();
  };
  const send = message => call(() => chrome.runtime.sendMessage(message));
  const getConfig = async () => {
    const data = await call(() => chrome.storage.local.get(['shopeeConfig','shopeeAiRequested']));
    const config = {...data.shopeeConfig};
    if (typeof data.shopeeAiRequested === 'boolean') config.aiEnabled = data.shopeeAiRequested;
    return {...data, shopeeConfig: config};
  };
  const save = value => call(() => chrome.storage.local.set(value));
  const status = value => save({ shopeeAiStatus: { ...value, sessionId: ownerSession, at: Date.now() } });
  const activity = async value => {
    await save({shopeeAiLastActivity: {...value, sessionId: ownerSession, at: Date.now()}});
    await status(value);
  };

  async function confirmDelivery(payload) {
    let error;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const result = await send({type: "KIDTE_SHOPEE_BACKEND", resource: "delivery",
          method: "POST", payload});
        if (!result?.ok) throw new Error(result?.error || "Kid TE chưa lưu biên nhận Shopee");
        return result;
      } catch (failure) {
        error = failure;
        if (!alive()) throw failure;
      }
    }
    throw error;
  }

  // Sản phẩm đang ghim trên UI Shopee (ngữ cảnh shop/sản phẩm gửi kèm /reply và bản sao hàng chờ).
  async function displayedProduct() {
    if (!catalog || Date.now() - catalogAt > 30000) {
      const result = await send({ type: "KIDTE_SHOPEE_API", request: { action: "catalog" } });
      if (!result?.ok) throw new Error(result?.error || "Không đọc được giỏ Shopee");
      catalog = result.products || [];
      catalogAt = Date.now();
    }
    const displayed = await send({type: "KIDTE_SHOPEE_API", request: {action: "state"}});
    return displayed?.ok && displayed.available ? catalog.find((item) =>
      item.shop_id === displayed.shop_id && item.product_id === displayed.product_id) : null;
  }

  // Bản sao phụ lên hàng chờ backend: nuốt mọi lỗi, không đổi đường gửi. Hàng chưa
  // có backendQueueId được gửi lại ở nhịp sau với ĐÚNG receivedAtMs gốc.
  async function syncBackendQueue(sessionId, videoId) {
    if (!queueApi) return;
    if (queueSyncBusy) { queueSyncAgain = true; return; }
    queueSyncBusy = true;
    try {
      let changed = false;
      for (const row of queue.slice()) {
        if (row.backendQueueId || Date.now() >= deadline(row) || stopped) continue;
        try {
          const product = await displayedProduct();
          const result = await send({type: "KIDTE_SHOPEE_BACKEND", resource: "queue_enqueue", method: "POST",
            payload: {room_id: sessionId, comment_payload: {
              session_id: sessionId, video_id: videoId, protocol_version: protocolV2 ? 2 : 1,
              received_at_ms: row.receivedAtMs, comment_id: row.comment_id, user_id: row.user_id,
              nickname: row.nickname, comment: row.comment,
              shop_id: product?.shop_id || "", product_id: product?.product_id || "",
              title: product?.title || ""}}});
          if (result?.ok) { row.backendQueueId = result.queue_id || `state:${result.state || "none"}`; changed = true; }
        } catch {}
      }
      if (changed) await persistQueue();
    } catch {} finally {
      queueSyncBusy = false;
      if (queueSyncAgain) { queueSyncAgain = false; syncBackendQueue(sessionId, videoId).catch(() => {}); }
    }
  }

  // Gửi lượt trả lời protocol 2; dùng chung cho đường /reply cục bộ và queue/next.
  async function deliverTurn(generated, row, sessionId) {
    return globalThis.KidTEMessageDelivery.sendTurn(generated, {
      active: async () => {
        const current = (await getConfig()).shopeeConfig || {};
        return alive() && current.aiEnabled && current.sessionId === sessionId &&
          KidTEShopeeCore.liveSession(location.href) === sessionId;
      },
      control:(operation,payload) => send({type:'KIDTE_SHOPEE_MESSAGE',operation,payload}),
      send:message => send({type:'KIDTE_SHOPEE_API',request:{action:'chat_send',turn_id:message.message_id,text:message.content}}),
      ack:payload => send({type:'KIDTE_SHOPEE_MESSAGE',operation:'receipt',payload}),
      unknown:payload => send({type:'KIDTE_SHOPEE_MESSAGE',operation:'failure',payload}),
      onSent:async(_message,receipt)=>{
        if(!row.firstSentAtMs) row.firstSentAtMs=receipt.sent_at_ms||Date.now();
        await persistQueue();
      }
    });
  }

  // BẬT hàng chờ backend: comment bỏ ở tiện ích mà không gửi phải huỷ ở backend, nếu không
  // nó nằm đầu hàng và chặn lượt sau. Lỗi mạng bị nuốt (backend tự hết hạn sau 120s).
  function cancelBackendRow(sessionId, row) {
    if (!queueApi || !row?.comment_id) return;
    send({type: "KIDTE_SHOPEE_BACKEND", resource: "queue_cancel", method: "POST",
      payload: {room_id: sessionId, comment_id: row.comment_id}}).catch(() => {});
  }
  const removeRow = commentId => {
    const index = queue.findIndex(item => item.comment_id === commentId);
    if (index >= 0) queue.splice(index, 1);
  };

  async function tick() {
    if (busy || stopped) return;
    if (!alive()) { stop(); return; }
    busy = true;
    let activeRow;
    try {
      const config = (await getConfig()).shopeeConfig || {};
      const sessionId = KidTEShopeeCore.liveSession(location.href);
      if (!config.aiEnabled || config.sessionId !== sessionId) {
        for (const dropped of queue) cancelBackendRow(ownerSession || sessionId, dropped);
        queue.length = 0; await persistQueue(); return;
      }
      if (ownerSession !== sessionId) {
        ownerSession = sessionId; queue.length = 0; catalog = null; catalogAt = 0;
        const saved = (await call(()=>chrome.storage.local.get('shopeeAiQueue'))).shopeeAiQueue;
        for (const row of saved?.sessionId===sessionId && Array.isArray(saved.rows) ? saved.rows : []) {
          if (!Number.isFinite(row.receivedAtMs) || row.receivedAtMs>Date.now()+5000 || Date.now()>=deadline(row)) continue;
          queue.push(row); seen.add(`${sessionId}:${row.comment_id}`);
        }
        queue.sort((a,b)=>a.receivedAtMs-b.receivedAtMs);
      }
      if (Date.now() - protocolCheckedAt > 60000) {
        const backend = await send({type:'KIDTE_SHOPEE_BACKEND',resource:'ai_status'}).catch(() => null);
        protocolV2 = Boolean(globalThis.KidTEMessageDelivery) && backend?.ok && backend.capabilities?.multi_message_reply === true;
        queueApi = (config.backendQueueMirror === true || backend?.capabilities?.backend_queue_mode === true) &&
          backend?.ok === true && backend.capabilities?.comment_queue_api === true; // tắt mặc định, xem comments.js TikTok
        protocolCheckedAt = Date.now();
      }
      const polled = await send({ type: "KIDTE_SHOPEE_API", request: { action: "chat_poll" } });
      if (!polled?.ok) throw new Error(polled?.error || "Không đọc được bình luận Shopee");
      for (const row of polled.comments || []) {
        const key = `${sessionId}:${row.comment_id}`;
        if (!seen.has(key)) { seen.add(key); queue.push({...row, receivedAtMs: Date.now()}); }
      }
      if (seen.size > 5000) seen.clear();
      syncBackendQueue(sessionId, config.videoId).catch(() => {});
      while (queue.length && Date.now() >= deadline(queue[0])) cancelBackendRow(sessionId, queue.shift());
      await persistQueue();
      if (!queue.length) { await status({ok: true, label: "Đang nghe bình luận thật · chưa có câu mới cần trả lời"}); return; }
      if (Date.now() - lastHandledAt < 10000) return;
      const row = queue[0];
      if (!queueApi) activeRow = row;
      const position = await send({type: "KIDTE_SHOPEE_BACKEND", resource: "position"});
      if (!position?.ok || !position.available || position.video_id !== config.videoId) {
        throw new Error("Kid TE chưa xác nhận nguồn video cho AI Shopee");
      }
      if (queueApi) {
        // Backend giữ FIFO và sinh câu trả lời: tiện ích chỉ gửi lượt trả về. Lỗi mạng hoặc
        // delivery_control_inactive thì KHÔNG gửi cục bộ thay thế (tránh gửi đôi), giữ hàng, nhịp sau thử lại.
        if (protocolV2) await send({type:'KIDTE_SHOPEE_BACKEND',resource:'heartbeat',method:'POST',payload:{
          platform:'shopee',session_id:sessionId,live_running:true,ai_enabled:true,ai_ready:true}});
        const next = await send({type: "KIDTE_SHOPEE_BACKEND", resource: "queue_next", method: "POST",
          payload: {room_id: sessionId}});
        if (!next?.ok) throw new Error(next?.error || "Hàng chờ backend không phản hồi");
        if (!next.available) return;
        lastHandledAt = Date.now();
        const turn = next.result || {};
        const target = next.source_comment || {};
        if (!turn.should_send) {
          removeRow(target.comment_id); await persistQueue();
          await activity({ ok: true, label: `Bỏ qua: ${turn.reason}`, reason: turn.reason }); return;
        }
        if (turn.protocol_version !== 2) throw new Error("Lượt trả lời từ hàng chờ không phải protocol 2");
        const outcome = await deliverTurn(turn, queue.find(item => item.comment_id === target.comment_id) || {}, sessionId);
        removeRow(target.comment_id);
        await persistQueue();
        await activity(outcome?.stopped ? {ok:true,label:'Dữ kiện đã đổi trước khi gửi; backend soạn lại',reason:outcome.stopped}
          : {ok:true,label:'Đã gửi đủ các tin của lượt trả lời'}); return;
      }
      // Pin transitions must not block named-product or conversation follow-ups.
      // The backend worker verifies the source; use the observed UI pin as context.
      const product = await displayedProduct();
      await status({ok: true, label: "Đang xử lý bình luận thật của " + row.nickname});
      row.attempts = Number(row.attempts || 0) + 1;
      lastHandledAt = Date.now();
      if (protocolV2) await send({type:'KIDTE_SHOPEE_BACKEND',resource:'heartbeat',method:'POST',payload:{
        platform:'shopee',session_id:sessionId,live_running:true,ai_enabled:true,ai_ready:true}});
      const generated = await send({ type: "KIDTE_SHOPEE_BACKEND", resource: "reply",
        method: "POST", payload: {
          session_id: sessionId, video_id: config.videoId,
          protocol_version:protocolV2 ? 2 : 1,
          received_at_ms: row.receivedAtMs,
          comment_id: row.comment_id, user_id: row.user_id,
          nickname: row.nickname, comment: row.comment,
          shop_id: product?.shop_id || "", product_id: product?.product_id || "",
          title: product?.title || "", retry_unanswered: row.attempts > 1
        } });
      if (!generated?.ok) throw new Error(generated?.error || "DeepSeek Shopee lỗi");
      if (generated.protocol_version !== 2 || !generated.should_send) { queue.shift(); await persistQueue(); }
      lastHandledAt = Date.now();
      if (!generated.should_send) { await activity({ ok: true, label: `Bỏ qua: ${generated.reason}`, reason: generated.reason }); return; }
      if (generated.protocol_version === 2) {
        const outcome = await deliverTurn(generated, row, sessionId);
        if(queue[0]===row) queue.shift();
        await persistQueue();
        await activity(outcome?.stopped ? {ok:true,label:'Dữ kiện đã đổi trước khi gửi; không gửi tin',reason:outcome.stopped}
          : {ok:true,label:'Đã gửi đủ các tin của lượt trả lời'}); return;
      }
      if (Date.now() - row.receivedAtMs >= 120000) {
        await confirmDelivery({session_id: sessionId, turn_id: generated.turn_id,
          error: "expired before send"});
        return;
      }
      // Recheck the switch after slow model inference; disabling AI must stop delivery.
      if (!alive()) { stop(); return; }
      const current = (await getConfig()).shopeeConfig || {};
      if (KidTEShopeeCore.liveSession(location.href) !== sessionId ||
          !current.aiEnabled || current.sessionId !== sessionId || current.videoId !== config.videoId) {
        await confirmDelivery({session_id: sessionId, turn_id: generated.turn_id,
          error: "AI disabled before send"});
        return;
      }
      const sent = await send({ type: "KIDTE_SHOPEE_API", request: {
        action: "chat_send", turn_id: generated.turn_id, text: generated.reply
      } });
      const delivered = await confirmDelivery({session_id: sessionId, turn_id: generated.turn_id,
        message_id: sent?.ok ? sent.message_id : "", error: sent?.error || ""});
      if (!sent?.ok || !delivered?.sent) throw new Error(sent?.error || "Shopee chưa xác nhận gửi");
      await activity({ ok: true, label: `Đã gửi #${sent.message_id}`, messageId: sent.message_id });
    } catch (error) {
      if (!alive() || /Extension context invalidated/i.test(String(error?.message))) { stop(); return; }
      if (queue[0] === activeRow && Number(activeRow?.attempts || 0) >= 3) queue.shift();
      await persistQueue().catch(()=>{});
      await activity({ ok: false, error: String(error.message || error) }).catch(() => {});
    } finally { busy = false; }
  }
  const runTick = () => tick().catch(() => stop());
  timer = setInterval(runTick, 3000);
  runTick();
})();
