"use strict";
(() => {
  if (globalThis.KidTEShopeeControllerStarted) return;
  globalThis.KidTEShopeeControllerStarted = true;
  const core = globalThis.KidTEShopeeCore;
  let busy = false, stopped = false, timer;
  const alive = () => {
    try { return !stopped && Boolean(chrome.runtime?.id); }
    catch { return false; }
  };
  const call = async fn => {
    if (!alive()) { stop(); throw new Error('Extension context invalidated.'); }
    return await fn();
  };
  const send = message => call(() => chrome.runtime.sendMessage(message));
  const save = value => call(() => chrome.storage.local.set(value));
  let scope = "", lastToken = "", catalogAt = 0, timelinesAt = 0;
  let nextRepinAt = 0;
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const pinLog = [];
  async function act(request) {
    const result = await send({type: "KIDTE_SHOPEE_API", request});
    pinLog.push({at: new Date().toISOString(), sessionId: scope.split(":")[0],
      action: request.action, refresh: Boolean(request.refresh), productId: request.product_id || "",
      ok: Boolean(result?.ok), error: result?.error || "", confirmed: Boolean(result?.confirmed),
      confirmationSource: result?.confirmation_source || "", displayed: result?.displayed || null});
    if (pinLog.length > 100) pinLog.shift();
    await save({shopeePinLog: pinLog.slice()}).catch(() => {});
    if (!result?.ok) throw new Error(result?.error || "Shopee từ chối ghim");
    return result;
  }
  let timelines = null, mappings = null, productCount = 0, products = [];
  let widget;
  const stop = () => { stopped = true; clearInterval(timer); widget?.remove?.(); };
  globalThis.KidTEShopeeControllerStop = stop;

  async function status(value, persist = true) {
    if (!widget && document?.documentElement) {
      for (const old of document.querySelectorAll?.('[id="kidte-shopee-status"]') || []) old.remove();
      widget = globalThis.KidTELiveWidget.create('shopee', 'kidte-shopee-status');
    }
    if (widget) globalThis.KidTELiveWidget.update(widget, value);
    if (persist) await save({shopeeStatus: {...value, at: Date.now()}}).catch(() => {});
  }

  // Lệnh bật/tắt AI từ app Kid TE (Trung tâm LIVE) đi kèm heartbeat. Áp đúng MỘT lần
  // theo seq bằng cùng khóa popup dùng (shopeeAiRequested); background/ai-controller vẫn
  // giữ mọi chặn (tab điều khiển, phiên, video). Bấm popup sau đó không bị ép lại.
  async function applyAppControl(control) {
    const seq = Number(control?.seq);
    if (!Number.isInteger(seq) || seq <= 0) return false;
    const saved = await call(() => chrome.storage.local.get("shopeeControlSeq"));
    if (seq <= Number(saved?.shopeeControlSeq || 0)) return false;
    if (typeof control.ai_enabled === "boolean") await save({shopeeAiRequested: control.ai_enabled});
    await save({shopeeControlSeq: seq});
    return true;
  }
  globalThis.KidTEShopeeApplyControl = applyAppControl;

  async function tick() {
    if (busy || stopped) return;
    if (!alive()) { stop(); return; }
    const sessionId = core.liveSession(location.href, true);
    if (!sessionId) return;
    busy = true;
    try {
      const connection = await send({type: "KIDTE_SHOPEE_CONNECT"});
      if (!connection?.ok) throw new Error(connection?.error || "Chưa nối được Kid TE; tự thử lại");
      if (!connection.controller) { await status({ok: true, label: "Tab theo dõi — tab khác đang điều khiển"}, false); return; }
      const {config, position} = connection;
      const stored = await call(() => chrome.storage.local.get(["shopeeAiStatus", "shopeeControlSeq"]));
      const aiState = stored.shopeeAiStatus;
      const appliedSeq = Number(stored.shopeeControlSeq || 0);
      const heartbeat = await send({type: "KIDTE_SHOPEE_BACKEND", resource: "heartbeat", method: "POST",
        payload: {platform: "shopee", session_id: config.sessionId,
          live_running: Boolean(core.liveSession(location.href)), ai_enabled: Boolean(config.aiEnabled),
          ai_ready: Boolean(config.aiEnabled && aiState?.sessionId === config.sessionId && aiState?.ok === true &&
            Date.now() - Number(aiState.at || 0) >= 0 && Date.now() - Number(aiState.at || 0) < 20000),
          observer: !connection.paused,
          ...(appliedSeq > 0 ? {control_applied_seq: appliedSeq} : {})}});
      if (heartbeat?.control) await applyAppControl(heartbeat.control).catch(() => false);
      if (Array.isArray(heartbeat?.privacy_purge) && heartbeat.privacy_purge.length &&
          typeof globalThis.KidTEShopeeAiPurge === "function") {
        await globalThis.KidTEShopeeAiPurge(heartbeat.privacy_purge).catch(() => []);
      }
      const nextScope = `${sessionId}:${config.videoId}`;
      if (scope !== nextScope) { scope = nextScope; lastToken = ""; nextRepinAt = 0; catalogAt = 0; }
      if (!timelines || Date.now() - timelinesAt >= 30000) {
        const schedule = await send({type: "KIDTE_SHOPEE_BACKEND", resource: "timelines"});
        if (!schedule?.ok) throw new Error(schedule?.error || "Chưa đọc được kịch bản Kid TE");
        timelines = schedule.timelines;
        timelinesAt = Date.now();
      }
      if (!mappings || Date.now() - catalogAt >= 30000) {
        const bag = await send({type: "KIDTE_SHOPEE_API", request: {action: "catalog"}});
        if (!bag?.ok) throw new Error(bag?.error || "Chưa đọc được giỏ Shopee; tự thử lại");
        const synced = await send({type: "KIDTE_SHOPEE_BACKEND", resource: "sync", method: "POST",
          payload: {session_id: sessionId, products: bag.products}});
        if (!synced?.ok) throw new Error(synced?.error || "Chưa lưu được giỏ vào Kid TE");
        mappings = synced.videos;
        productCount = bag.product_count; products = bag.products || [];
        catalogAt = Date.now();
        await save({shopeeCatalog: {sessionId, products: bag.products}, shopeeTimelines: timelines});
      }
      const video = timelines?.[config.videoId];
      const label = `${video?.title || config.videoId || "Đang chờ nhận diện âm thanh; kiểm tra Đồng bộ video trong Kid TE nếu chờ lâu"} · Giỏ ${productCount} sản phẩm`;
      if (!core.liveSession(location.href)) {
        await status({ok: true, sessionId, videoId: config.videoId, label: "Chuẩn bị LIVE · " + label + "\nĐã tự đọc giỏ; bắt đầu LIVE sẽ ghim theo kịch bản."});
        return;
      }
      if (connection.paused) { lastToken = ""; nextRepinAt = 0; await status({ok: true, label: "Tự ghim đang tạm dừng · " + label}); return; }
      const record = mappings?.[config.videoId];
      let wanted = core.desired({...config, ...record}, position, timelines);
      if (!wanted.ok) { lastToken = ""; nextRepinAt = 0; throw new Error(wanted.error); }
      const clock = `${Math.floor(Number(position.position_s) / 60)}:${String(Math.floor(Number(position.position_s) % 60)).padStart(2, "0")}`;
      const repinEveryMs = Math.max(10000, Number(video.repin_every_ms) || 20000);
      const refresh = wanted.action === "show" && wanted.token === lastToken && Date.now() >= nextRepinAt;
      if (wanted.token !== lastToken || refresh) {
        if (refresh) {
          const sourceLock = position.lock_id, sourceRevision = position.revision;
          const expectedSession = config.sessionId, expectedVideo = config.videoId;
          await act({...wanted, action: "hide", refresh: true});
          lastToken = "";
          await sleep(800);
          // Re-read after the gap: pause, ownership, video and timeline may have changed.
          const latest = await send({type: "KIDTE_SHOPEE_CONNECT"});
          if (!latest?.ok || !latest.controller || latest.paused ||
              latest.config?.sessionId !== expectedSession || latest.config?.videoId !== expectedVideo ||
              latest.position?.lock_id !== sourceLock || latest.position?.revision !== sourceRevision)
            throw new Error("Đã đổi trạng thái; hủy ghim lại");
          wanted = core.desired({...latest.config, ...mappings?.[latest.config.videoId]}, latest.position, timelines);
          if (!wanted.ok) throw new Error(wanted.error);
        }
        try { await act(wanted); }
        catch (error) { catalogAt = 0; throw error; }
        lastToken = wanted.token;
        nextRepinAt = Date.now() + repinEveryMs;
      }
      const displayed = await send({type: "KIDTE_SHOPEE_API", request: {action: "state"}});
      const confirmed = displayed?.ok && displayed.available && (wanted.action === "hide"
        ? !displayed.product_id : displayed.product_id === wanted.product_id && displayed.shop_id === wanted.shop_id);
      const confirmationLabel = confirmed
        ? (wanted.action === "hide" ? "UI Shopee xác nhận đã bỏ ghim" : "UI Shopee xác nhận đang hiển thị đúng sản phẩm")
        : (displayed?.available ? "UI Shopee chưa khớp; sẽ ghim lại theo kịch bản" : "Đã gửi lệnh; đang chờ xác nhận từ UI Shopee");
      await status({ok: true, sessionId, videoId: config.videoId, action: wanted.action, confirmed: Boolean(confirmed),
        videoTitle: video?.title || config.videoId, positionS: position.position_s,
        productTitle: products.find(item => item.product_id === wanted.product_id && item.shop_id === wanted.shop_id)?.title || wanted.label, productId: wanted.product_id, productCount,
        nextAtMs: (video.events || []).find(event => Number(event.at_ms) > Number(position.position_s) * 1000)?.at_ms,
        label: `${label} · ${clock}\n${wanted.label} · Làm mới ghim ${Math.round(repinEveryMs / 1000)} giây\n${confirmationLabel}`});
    } catch (error) {
      if (!alive() || /Extension context invalidated/i.test(String(error?.message))) { stop(); return; }
      await status({ok: false, error: String(error?.message || error)});
    } finally { busy = false; }
  }
  const runTick = () => tick().catch(() => stop());
  timer = setInterval(runTick, 3000);
  call(() => chrome.storage.local.get("shopeePinLog")).then(value => {
    if (Array.isArray(value.shopeePinLog)) pinLog.push(...value.shopeePinLog.slice(-100));
  }).catch(() => {}).then(runTick).catch(() => stop());
})();
