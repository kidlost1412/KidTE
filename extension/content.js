(() => {
  "use strict";
  if (window.__LIVE_RE_PIN_LOADED__) return;
  window.__LIVE_RE_PIN_LOADED__ = true;

  const SCHEDULE = globalThis.KidTESchedule;
  const VERSION = "3.10.1";
  const UNPIN_GAP_MS = 500;
  const CONFIRM_TIMEOUT_MS = 6000;
  const CONFIRM_POLL_MS = 300;
  const OBSERVE_EVERY_MS = 3000;
  const MANUAL_OVERRIDE_MS = 60 * 1000;
  const repinEveryMs = () => Number(SCHEDULE.REPIN_EVERY_MS) || 20 * 1000;
  // Dong bo tu dong theo tieng video. Nguong 3s vi ranh gioi ghim cach nhau
  // hang chuc giay; chinh vi mot chut lech chi lam reset cache vo ich.
  const VIDEO_POLL_MS = 5000;
  const VIDEO_DRIFT_TOLERANCE_MS = 3000;
  const VIDEO_RESYNC_COOLDOWN_MS = 20000;
  const VIDEO_MIN_MARGIN = 0.06;

  let liveState = {
    running: false,
    startedAtMs: 0,
    pausedElapsedMs: 0,
    revision: 0,
    sessionId: ""
  };
  let roomId = "";
  let productCache = new Map();
  let liveProductsById = new Map();
  let lastTransitionToken = "";
  let lastObserveAt = 0;
  let nextRepinAt = 0;
  let manualOverride = null;
  let externalCandidate = null;
  let operationBusy = false;
  let pendingOperation = null;
  let lastStatusWriteAt = 0;
  let currentStatusMessage = "Đang nối TikTok LIVE API…";
  let currentStatusOk = true;
  let aiEnabled = false;
  let aiStatusMessage = "AI đang tắt";
  let aiStatusOk = true;
  let collector = null;
  let videoSync = null;
  let lastVideoPollAt = 0;
  let lastResyncAt = 0;
  let videoPollBusy = false;
  let timelines = null;
  let timelineFetchAt = 0;
  let timelineNote = "";
  let widget = null;
  let observedPin = null;
  let tickTimer = null;
  let isController = false;
  if (globalThis.KidTELiveWidget) document.querySelector('#live-re-pin-host')?.remove();

  document.documentElement.setAttribute("data-live-re-pin", VERSION);
  document.documentElement.setAttribute("data-live-re-mode", "tiktok-api");

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  function hasExtensionContext() {
    try {
      return Boolean(chrome?.runtime?.id);
    } catch {
      return false;
    }
  }

  function stopInvalidatedContext() {
    if (tickTimer !== null) {
      clearInterval(tickTimer);
      tickTimer = null;
    }
    pendingOperation = null;
    if (widget?.shadowRoot) {
      const status = widget.shadowRoot.querySelector("#status");
      status.textContent = "Extension vừa được tải lại — hãy tải lại tab TikTok để nối bản mới.";
      status.classList.add("bad", "open");
    }
  }

  const normalize = (text) => String(text || "")
    .replace(/\u00a0/g, " ")
    .replace(/[ \t]+/g, " ")
    .trim();
  const searchable = (text) => normalize(text)
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/đ/g, "d")
    .replace(/Đ/g, "D")
    .toLowerCase();

  function elapsedMs(now = Date.now()) {
    if (liveState.running && liveState.startedAtMs) {
      return Math.max(0, now - Number(liveState.startedAtMs));
    }
    return Math.max(0, Number(liveState.pausedElapsedMs) || 0);
  }

  function makeWidget() {
    return globalThis.KidTELiveWidget.create('tiktok', 'live-re-pin-host');
  }

  function updateWidget(message = "", ok = true) {
    if (message) {
      currentStatusMessage = message;
      currentStatusOk = ok;
    }
    widget ||= document.querySelector("#live-re-pin-host") || makeWidget();
    const root = widget.shadowRoot;
    const resolved = SCHEDULE.resolve(elapsedMs());
    const manualProduct = manualOverride?.productId
      ? (manualOverride.title || "Sản phẩm ghim thủ công")
      : "Đang giữ không ghim theo lệnh thủ công";
    root.querySelector("#clock").textContent = SCHEDULE.formatClock(resolved.positionMs);
    root.querySelector("#dot").classList.toggle("running", Boolean(liveState.running));
    root.querySelector("#product").textContent = !isController
      ? "Tab theo dõi — không có quyền điều khiển"
      : liveState.running
      ? (observedPin && Date.now() - observedPin.at < 15000 ? observedPin.title || 'Không có sản phẩm đang ghim' : 'Chờ xác nhận sản phẩm trên LIVE')
      : (liveState.startedAtMs ? "Đang tạm dừng" : "Đang dừng");
    const aiLine = root.querySelector("#ai");
    aiLine.textContent = aiEnabled
      ? `AI TỰ TRẢ LỜI: ${aiStatusMessage || "đang bật"}`
      : "AI TỰ TRẢ LỜI: đang tắt";
    aiLine.classList.toggle("on", aiEnabled && aiStatusOk);
    aiLine.classList.toggle("bad", aiEnabled && !aiStatusOk);
    const collectLine = root.querySelector("#collect");
    if (collector) {
      const stale = Date.now() - Number(collector.updatedAtMs || 0) > 20000;
      collectLine.textContent = collector.ok && !stale
        ? `Bộ thu dữ liệu: ${collector.rows} bình luận` +
          (collector.queued ? ` · chờ gửi ${collector.queued}` : "") +
          (collector.retried ? ` · gửi lại ${collector.retried}` : "") +
          (collector.aiDropped ? ` · bỏ ${collector.aiDropped} lượt AI` : "")
        : `Bộ thu dữ liệu LỖI: ${collector.error || "mất kết nối backend"}`;
      collectLine.classList.toggle("bad", !collector.ok || stale);
    } else {
      collectLine.textContent = "Bộ thu dữ liệu: chưa nhận tín hiệu";
      collectLine.classList.add("bad");
    }
    const vsyncLine = root.querySelector("#vsync");
    vsyncLine.className = "";
    if (!videoSync || videoSync.state === "offline") {
      vsyncLine.textContent = "Đồng bộ video: chưa bật — mở Kid TE → Trợ lý tự động → Bật “Đồng bộ video”";
      vsyncLine.classList.add("off");
    } else if (videoSync.state === "waiting" || videoSync.state === "stale") {
      vsyncLine.textContent = "Đồng bộ video: chưa nghe được tiếng video";
      vsyncLine.classList.add("bad");
    } else if (videoSync.state === "weak") {
      vsyncLine.textContent =
        `Đồng bộ video: ${SCHEDULE.formatClock(videoSync.positionMs)}`
        + ` · tin cậy thấp (${videoSync.margin.toFixed(2)}), chưa chỉnh`;
      vsyncLine.classList.add("bad");
    } else {
      vsyncLine.textContent =
        `TikTok Studio · ${SCHEDULE.VIDEO_TITLE || videoSync.videoId || SCHEDULE.VIDEO_ID}:`
        + ` ${SCHEDULE.formatClock(videoSync.positionMs)}`
        + ` · lệch ${(videoSync.driftMs / 1000).toFixed(1)}s`
        + ` · tin cậy ${videoSync.margin.toFixed(2)}`;
    }
    root.querySelector("#status").textContent = currentStatusMessage;
    root.querySelector("#status").classList.toggle("bad", !currentStatusOk);

    const now = Date.now();
    if (isController && now - lastStatusWriteAt > 1800 && hasExtensionContext()) {
      lastStatusWriteAt = now;
      try {
        const writing = chrome.storage.local.set({
          liveRePageStatus: {
            ok: currentStatusOk,
            message: currentStatusMessage,
            positionMs: resolved.positionMs,
            productKey: resolved.productKey,
            pinnedProductTitle: observedPin?.title || '',
            pinObservedAtMs: observedPin?.at || 0,
            roomId,
            controlMode: "tiktok_api",
            videoSync,
            updatedAtMs: now
          }
        });
        writing?.catch?.(() => {});
      } catch {
        stopInvalidatedContext();
      }
    }
  }

  // -------------------------------------------------------------------
  // Video phat lai khong bao gio bat dau tu 00:00. Tool
  // dịch vụ Đồng bộ video (bật trong Kid TE → Trợ lý tự động)
  // nghe tieng video, so voi dau van tay cua live1.mp4, roi bao vi tri that
  // vao backend. O day chi doc lai va chinh dong ho khi lech qua nguong.
  // -------------------------------------------------------------------
  // Lich ghim la du lieu cua tung video, nam o backend/data/videos/*/
  // timeline.json. Tien ich nap ve roi ap dung theo video_id ma bo nghe nhan
  // ra, nen them video moi khong phai sua ma nguon nua.
  async function fetchTimelines(force = false) {
    if (!force && timelines && Date.now() - timelineFetchAt < 300000) return timelines;
    try {
      const result = await chrome.runtime.sendMessage({
        type: "LIVE_RE_VIDEO_TIMELINES"
      });
      if (result?.ok && result.timelines) {
        timelines = result.timelines;
        timelineFetchAt = Date.now();
        try {
          await chrome.storage.local.set({ liveReTimelines: timelines });
        } catch {}
        if (Array.isArray(result.broken) && result.broken.length) {
          timelineNote = "timeline lỗi: " + result.broken
            .map((item) => `${item.video_id} (${item.errors.join("; ")})`)
            .join(" · ");
          updateWidget(timelineNote, false);
        }
      }
    } catch {
      // Backend chua bat: dung ban da luu, cuoi cung la ban mac dinh trong
      // schedule.js. Khong bao gio de tien ich mat lich.
      try {
        const stored = await chrome.storage.local.get("liveReTimelines");
        if (stored.liveReTimelines) timelines = stored.liveReTimelines;
      } catch {}
    }
    return timelines;
  }

  async function applyTimeline(videoId) {
    if (!videoId || videoId === SCHEDULE.VIDEO_ID) return true;
    let library = await fetchTimelines();
    if (!library?.[videoId]) library = await fetchTimelines(true);
    const timeline = library?.[videoId];
    if (!timeline) {
      timelineNote = `Bộ nghe nhận ra video "${videoId}" nhưng chưa có lịch ghim`
        + " — đang giữ lịch " + SCHEDULE.VIDEO_ID;
      updateWidget(timelineNote, false);
      return false;
    }
    const applied = SCHEDULE.load(timeline);
    if (!applied.ok) {
      timelineNote = `Lịch của "${videoId}" không hợp lệ: ${applied.errors.join("; ")}`;
      updateWidget(timelineNote, false);
      return false;
    }
    // Doi video la doi ca bo san pham: phai bo cache va tinh lai tu dau.
    productCache = new Map();
    lastTransitionToken = "";
    nextRepinAt = 0;
    manualOverride = null;
    externalCandidate = null;
    timelineNote = "";
    chrome.storage.local.set({ liveReActiveVideoId: videoId }).catch(() => {});
    updateWidget(
      `Đã chuyển sang lịch video "${videoId}" (${SCHEDULE.VIDEO_TITLE}).`, true
    );
    return true;
  }

  function driftMs(detectedPositionMs, currentPositionMs) {
    const cycle = SCHEDULE.CYCLE_MS;
    let drift = detectedPositionMs - currentPositionMs;
    if (drift > cycle / 2) drift -= cycle;
    else if (drift < -cycle / 2) drift += cycle;
    return drift;
  }

  async function pollVideoPosition() {
    if (videoPollBusy || !hasExtensionContext()) return;
    videoPollBusy = true;
    try {
      const result = await chrome.runtime.sendMessage({
        type: "LIVE_RE_VIDEO_POSITION"
      });
      if (!result?.ok) {
        videoSync = { state: "offline", error: result?.error || "" };
        return;
      }
      if (!result.available) {
        videoSync = { state: result.reason === "stale" ? "stale" : "waiting" };
        return;
      }
      const margin = Number(result.margin || 0);
      if (margin >= VIDEO_MIN_MARGIN && result.video_id) {
        if (!(await applyTimeline(result.video_id))) return;
      }
      const detected = Number(result.position_s) * 1000;
      const resolved = SCHEDULE.resolve(elapsedMs());
      const drift = driftMs(detected, resolved.positionMs);
      videoSync = {
        state: "locked", positionMs: detected, margin, driftMs: drift,
        lockId: result.lock_id, revision: result.revision,
        videoId: result.video_id || SCHEDULE.VIDEO_ID
      };
      if (margin < VIDEO_MIN_MARGIN) {
        videoSync.state = "weak";
        return;
      }
      if (!isController || !liveState.running || manualOverride) return;
      if (Math.abs(drift) <= VIDEO_DRIFT_TOLERANCE_MS) return;
      if (Date.now() - lastResyncAt < VIDEO_RESYNC_COOLDOWN_MS) return;
      lastResyncAt = Date.now();
      const target = resolved.loopIndex * SCHEDULE.CYCLE_MS + detected;
      const applied = await chrome.runtime.sendMessage({
        type: "LIVE_RE_RESYNC", elapsedMs: Math.max(0, Math.round(target))
      });
      if (applied?.ok) {
        updateWidget(
          `Da dong bo theo tieng video: ${SCHEDULE.formatClock(detected)}` +
          ` (lech ${(drift / 1000).toFixed(1)}s).`,
          true
        );
      }
    } catch (error) {
      videoSync = { state: "offline", error: String(error?.message || error) };
    } finally {
      videoPollBusy = false;
    }
  }

  async function apiRequest(action, payload = {}) {
    if (!hasExtensionContext()) {
      stopInvalidatedContext();
      throw new Error("Extension context đã hết hiệu lực; hãy tải lại tab TikTok");
    }
    const response = await chrome.runtime.sendMessage({
      type: "LIVE_RE_TIKTOK_API",
      request: { action, roomId, ...payload }
    });
    if (!response?.ok) {
      const lookup = response?.room_lookup || {};
      const detail = [
        lookup.response_code !== "" && lookup.response_code !== undefined
          ? `code=${lookup.response_code}` : "",
        lookup.message || "",
        Array.isArray(lookup.data_keys) && lookup.data_keys.length
          ? `data=[${lookup.data_keys.join(",")}]` : "",
        lookup.list_response_code !== "" && lookup.list_response_code !== undefined
          ? `list_code=${lookup.list_response_code}` : "",
        lookup.list_message || "",
        Array.isArray(lookup.list_data_keys) && lookup.list_data_keys.length
          ? `list_data=[${lookup.list_data_keys.join(",")}]` : ""
      ].filter(Boolean).join(" · ");
      throw new Error(
        (response?.error || "TikTok API không phản hồi") + (detail ? ` (${detail})` : "")
      );
    }
    if (response.room_id) roomId = String(response.room_id);
    return response;
  }

  function matchesProduct(item, product) {
    if (!item || !product) return false;
    // Timeline duyet tu giao dien luu product_id that. Uu tien khoa on dinh
    // nay; matcher ten chi la duong tuong thich cho timeline cu.
    if (product.productId && item.product_id) {
      return String(item.product_id) === String(product.productId);
    }
    const title = searchable(item.title);
    const included = product.includeAll.every((part) => title.includes(searchable(part)));
    const excluded = product.excludeAny.some((part) => title.includes(searchable(part)));
    if (!included || excluded) return false;
    const bagIndex = Number(item.bag_index || 0);
    return !bagIndex || bagIndex === product.bagIndex;
  }

  function validateAndCacheProducts(products) {
    const next = new Map();
    const unresolved = [];
    liveProductsById = new Map(
      products
        .filter((item) => item?.product_id)
        .map((item) => [String(item.product_id), item])
    );
    for (const product of Object.values(SCHEDULE.PRODUCTS)) {
      const matches = products.filter((item) => matchesProduct(item, product));
      if (matches.length === 1) next.set(product.key, matches[0]);
      else unresolved.push(`#${product.bagIndex} ${product.shortName} (${matches.length})`);
    }
    // Trước đây thiếu một SP là ném lỗi, xoá sạch cache và roomId rồi rơi vào
    // vòng thử lại — giữa LIVE thì đó là mất luôn việc ghim. Giờ chạy tiếp với
    // những SP nhận được và chỉ báo cái chưa nhận ra.
    if (unresolved.length === Object.keys(SCHEDULE.PRODUCTS).length) {
      throw new Error(`API chưa nhận diện được sản phẩm nào: ${unresolved.join(", ")}`);
    }
    productCache = next;
    if (unresolved.length) {
      updateWidget(
        `Chưa nhận diện: ${unresolved.join(", ")}. Kiểm tra thứ tự giỏ hàng; ` +
        `các sản phẩm còn lại vẫn chạy bình thường.`,
        false
      );
    }
  }

  async function getSnapshot(refreshProducts = false) {
    const response = await apiRequest("snapshot");
    const snapshot = response.snapshot || {};
    roomId = String(snapshot.room_id || roomId || "");
    const products = Array.isArray(snapshot.products) ? snapshot.products : [];
    const pinned = products.find(item => String(item.product_id) === String(snapshot.pin_id || ''));
    observedPin = {at: Date.now(), title: pinned ? String(pinned.title || pinned.name || '') : ''};
    if (refreshProducts || productCache.size === 0) {
      validateAndCacheProducts(products);
    }
    return snapshot;
  }

  function cachedProduct(product, optional = false) {
    const item = productCache.get(product.key);
    if (!item?.product_id) {
      if (optional) return null;
      throw new Error(`Chưa có product_id API cho ${product.shortName}`);
    }
    return item;
  }

  async function waitForPin(productId, shouldBePinned) {
    const deadline = Date.now() + CONFIRM_TIMEOUT_MS;
    while (Date.now() < deadline) {
      const snapshot = await getSnapshot(false);
      const currentPinId = String(snapshot.pin_id || "");
      if (shouldBePinned ? currentPinId === productId : currentPinId !== productId) {
        return snapshot;
      }
      await sleep(CONFIRM_POLL_MS);
    }
    return null;
  }

  async function unpinCurrent() {
    const snapshot = await getSnapshot(false);
    const currentPinId = String(snapshot.pin_id || "");
    if (!currentPinId) return true;
    await apiRequest("unpin", { productId: currentPinId });
    const ok = Boolean(await waitForPin(currentPinId, false));
    if (ok) {
      nextRepinAt = 0;
      externalCandidate = null;
    }
    return ok;
  }

  async function canRepin(expectedRoom, expectedSession, expectedRevision, expectedSource) {
    if (!hasExtensionContext() || !isController || !liveState.running) return false;
    const control = await chrome.runtime.sendMessage({type:'LIVE_RE_GET_STATE'});
    if (!control?.isController || !control.state?.running || control.state.sessionId !== expectedSession ||
        control.state.revision !== expectedRevision) return false;
    const fresh = await getSnapshot(false);
    if (String(fresh.room_id || '') !== expectedRoom) return false;
    if (expectedSource?.lockId) {
      const source = await chrome.runtime.sendMessage({type:'LIVE_RE_VIDEO_POSITION'});
      if (!source?.ok || !source.available || source.lock_id !== expectedSource.lockId ||
          source.revision !== expectedSource.revision || source.video_id !== expectedSource.videoId) return false;
    }
    return isController && liveState.running && liveState.sessionId === expectedSession &&
      liveState.revision === expectedRevision;
  }

  async function pinProduct(product, forceRefresh) {
    await getSnapshot(productCache.size === 0);
    const cached = cachedProduct(product, true);
    if (!cached) {
      updateWidget(
        `Bỏ qua nhịp này: chưa nhận diện được ${product.shortName} trong giỏ hàng.`,
        false
      );
      return false;
    }
    let target = cached;
    let snapshot = await getSnapshot(false);
    const startingRoom = String(snapshot.room_id || '');
    const startingSession = liveState.sessionId;
    const startingRevision = liveState.revision;
    const startingSource = videoSync;
    let currentPinId = String(snapshot.pin_id || "");

    if (currentPinId === target.product_id && !forceRefresh) {
      nextRepinAt ||= Date.now() + repinEveryMs();
      updateWidget(`API xác nhận đúng SP #${product.bagIndex} đang ghim.`, true);
      return true;
    }

    if (currentPinId === target.product_id && forceRefresh) {
      updateWidget(`API làm mới ghim mỗi ${Math.round(repinEveryMs() / 1000)} giây: ${product.shortName}…`, true);
      await apiRequest("unpin", { productId: target.product_id });
      if (!(await waitForPin(target.product_id, false))) {
        throw new Error(`API chưa xác nhận bỏ ghim ${product.shortName}`);
      }
      await sleep(UNPIN_GAP_MS);
      if (!liveState.running) return true;
      if (!await canRepin(startingRoom, startingSession, startingRevision, startingSource)) return false;

      const latest = SCHEDULE.resolve(elapsedMs()).product;
      if (!latest) return unpinCurrent();
      product = latest;
      target = cachedProduct(product);
    }

    updateWidget(`API đang ghim SP #${product.bagIndex}: ${product.shortName}…`, true);
    await apiRequest("pin", { productId: target.product_id });
    snapshot = await waitForPin(target.product_id, true);
    if (!snapshot) throw new Error(`API chưa xác nhận ghim ${product.shortName}`);
    nextRepinAt = Date.now() + repinEveryMs();
    externalCandidate = null;
    updateWidget(`API đã ghim đúng SP #${product.bagIndex}: ${product.shortName}.`, true);
    return true;
  }

  function activateManualOverride(productId, snapshot) {
    const id = String(productId || "");
    const product = (snapshot?.products || []).find(
      (item) => String(item?.product_id || "") === id
    ) || liveProductsById.get(id) || {};
    manualOverride = {
      productId: id,
      title: id ? String(product.title || "Sản phẩm ghim thủ công") : "",
      expiresAtMs: Date.now() + MANUAL_OVERRIDE_MS
    };
    nextRepinAt = id ? Date.now() + repinEveryMs() : 0;
    externalCandidate = null;
    updateWidget(
      id
        ? `Đã nhận lệnh ghim thủ công: ${manualOverride.title}. Giữ 60 giây.`
        : "Đã nhận lệnh bỏ ghim thủ công. Giữ trống 60 giây.",
      true
    );
  }

  async function observeExternalPin() {
    const snapshot = await getSnapshot(false);
    const actual = String(snapshot.pin_id || "");
    const resolved = SCHEDULE.resolve(elapsedMs());
    const scripted = resolved.product
      ? (cachedProduct(resolved.product, true)?.product_id || "") : "";
    const expected = manualOverride ? manualOverride.productId : String(scripted || "");
    if (actual === expected) {
      externalCandidate = null;
      return;
    }
    if (externalCandidate?.productId === actual) externalCandidate.count += 1;
    else externalCandidate = { productId: actual, count: 1 };
    // Hai snapshot liên tiếp tránh coi độ trễ nhất thời của API là thao tác tay.
    if (externalCandidate.count >= 2) activateManualOverride(actual, snapshot);
  }

  async function repinManualProduct() {
    if (!manualOverride?.productId) return;
    const productId = manualOverride.productId;
    const snapshot = await getSnapshot(false);
    const startingRoom = String(snapshot.room_id || '');
    const startingSession = liveState.sessionId;
    const startingRevision = liveState.revision;
    const startingSource = videoSync;
    const currentPinId = String(snapshot.pin_id || "");
    if (currentPinId !== productId) return;
    await apiRequest("unpin", { productId });
    if (!(await waitForPin(productId, false))) {
      throw new Error("API chưa xác nhận bỏ ghim sản phẩm thủ công");
    }
    await sleep(UNPIN_GAP_MS);
    if (!liveState.running || manualOverride?.productId !== productId) return;
    if (!await canRepin(startingRoom, startingSession, startingRevision, startingSource)) return;
    await apiRequest("pin", { productId });
    if (!(await waitForPin(productId, true))) {
      throw new Error("API chưa xác nhận ghim lại sản phẩm thủ công");
    }
    nextRepinAt = Date.now() + repinEveryMs();
    updateWidget(`Đã làm mới ghim thủ công: ${manualOverride.title}.`, true);
  }

  async function performOperation(reason) {
    if (reason === "stop") {
      manualOverride = null;
      const ok = await unpinCurrent();
      updateWidget(ok ? "Đã dừng và API đã bỏ ghim." : "Đã dừng; API chưa xác nhận bỏ ghim.", ok);
      return;
    }
    if (!liveState.running) return;

    if (reason === "observe") {
      await observeExternalPin();
      return;
    }

    if (manualOverride && Date.now() < manualOverride.expiresAtMs) {
      if (reason === "pulse") await repinManualProduct();
      return;
    }
    if (manualOverride) {
      manualOverride = null;
      externalCandidate = null;
      nextRepinAt = 0;
    }

    const resolved = SCHEDULE.resolve(elapsedMs());
    if (!resolved.product) {
      const ok = await unpinCurrent();
      updateWidget(ok ? "Đoạn nghỉ: API xác nhận không ghim sản phẩm." : "API chưa xác nhận bỏ ghim.", ok);
      return;
    }
    await pinProduct(resolved.product, reason === "pulse");
  }

  const PRIORITY = Object.freeze({ observe: 1, pulse: 2, transition: 3, stop: 4 });
  function queueOperation(reason) {
    if (!isController) return;
    if (!pendingOperation || PRIORITY[reason] > PRIORITY[pendingOperation]) {
      pendingOperation = reason;
    }
    if (operationBusy) return;
    operationBusy = true;
    (async () => {
      while (pendingOperation) {
        const next = pendingOperation;
        pendingOperation = null;
        try {
          await performOperation(next);
        } catch (error) {
          productCache = new Map();
          liveProductsById = new Map();
          roomId = "";
          updateWidget(`Lỗi TikTok API: ${String(error?.message || error)}. Sẽ tự thử lại.`, false);
        }
      }
      operationBusy = false;
    })();
  }

  // Tự nhận điều khiển: báo phòng live đang thấy cho background, nơi quyết định
  // (một lần, không có tab điều khiển khác, tôn trọng lệnh Dừng của người vận hành).
  // Tab controller dùng roomId đã có; tab khác hỏi snapshot thưa hơn.
  let autoStartAt = 0, autoStartBusy = false, autoStartPause = 0, autoReportedRoom = "";
  async function autoStartProbe() {
    if (autoStartBusy || !hasExtensionContext()) return;
    const now = Date.now();
    if (now - autoStartAt < (isController ? 3000 : 15000) || now < autoStartPause) return;
    autoStartAt = now;
    autoStartBusy = true;
    try {
      let room = String(roomId || "");
      if (!isController && !room) {
        const response = await apiRequest("snapshot");
        room = String(response?.snapshot?.room_id || "");
      }
      if (!room || (isController && room === autoReportedRoom)) return;
      const result = await chrome.runtime.sendMessage({ type: "LIVE_RE_AUTO_START", roomId: room });
      if (isController || result?.started || result?.resumed) autoReportedRoom = room;
      if (result?.reason === "other_controller") autoStartPause = Date.now() + 60000;
    } catch {
      // Chưa có phòng live hoặc API chưa sẵn sàng; thử lại ở nhịp sau.
    } finally {
      autoStartBusy = false;
    }
  }

  function tick() {
    if (!hasExtensionContext()) {
      stopInvalidatedContext();
      return;
    }
    updateWidget();

    // Van doc vi tri video ngay ca khi dong ho chua chay, de nguoi van hanh
    // thay video dang o doan nao TRUOC khi bam bat dau.
    if (Date.now() - lastVideoPollAt >= VIDEO_POLL_MS) {
      lastVideoPollAt = Date.now();
      pollVideoPosition();
    }

    autoStartProbe();

    if (!isController || !liveState.running) return;
    const now = Date.now();
    const resolved = SCHEDULE.resolve(elapsedMs(now));

    if (manualOverride && now >= manualOverride.expiresAtMs) {
      manualOverride = null;
      externalCandidate = null;
      nextRepinAt = 0;
      lastTransitionToken = "";
      queueOperation("transition");
      return;
    }

    if (resolved.transitionToken !== lastTransitionToken) {
      lastTransitionToken = resolved.transitionToken;
      if (!manualOverride) queueOperation("transition");
      return;
    }
    const activeProductId = manualOverride?.productId || (manualOverride ? "" : resolved.productKey);
    if (activeProductId && nextRepinAt && now >= nextRepinAt) {
      // Đặt mốc tạm để tick 250ms không xếp lặp trong lúc API đang chạy.
      nextRepinAt = now + repinEveryMs();
      queueOperation("pulse");
      return;
    }
    if (!operationBusy && now - lastObserveAt >= OBSERVE_EVERY_MS) {
      lastObserveAt = now;
      queueOperation("observe");
    }
  }

  function applyState(nextState, controllerFlag = isController) {
    const incoming = { ...liveState, ...(nextState || {}) };
    const nextIsController = Boolean(controllerFlag);
    const sameState =
      Boolean(incoming.running) === Boolean(liveState.running) &&
      Number(incoming.startedAtMs || 0) === Number(liveState.startedAtMs || 0) &&
      Number(incoming.pausedElapsedMs || 0) === Number(liveState.pausedElapsedMs || 0) &&
      Number(incoming.revision || 0) === Number(liveState.revision || 0) &&
      String(incoming.sessionId || "") === String(liveState.sessionId || "") &&
      nextIsController === isController;
    if (sameState) {
      updateWidget();
      return;
    }

    const hadActiveSession = Boolean(liveState.running || liveState.startedAtMs);
    liveState = incoming;
    isController = nextIsController;
    lastTransitionToken = "";
    lastObserveAt = 0;
    nextRepinAt = 0;
    manualOverride = null;
    externalCandidate = null;
    productCache = new Map();
    liveProductsById = new Map();

    if (!isController) {
      pendingOperation = null;
      updateWidget("Tab này chỉ theo dõi; một tab LIVE Manager khác đang điều khiển.", true);
      return;
    }
    roomId = "";

    const fullyStopped = !liveState.running && !liveState.startedAtMs;
    if (fullyStopped && hadActiveSession) queueOperation("stop");
    else if (liveState.running) tick();
    else updateWidget("Đã tạm dừng; sản phẩm hiện tại vẫn giữ nguyên.", true);
  }

  chrome.runtime.onMessage.addListener((message) => {
    if (message?.type === "LIVE_RE_STATE") {
      applyState(message.state, message.isController === true);
    }
  });
  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName === "local" && changes.liveReState?.newValue) {
      chrome.runtime.sendMessage({ type: "LIVE_RE_GET_STATE" })
        .then((response) => applyState(response?.state, response?.isController === true))
        .catch(() => {});
    }
    if (areaName === "local" && changes.liveReAiEnabled) {
      aiEnabled = changes.liveReAiEnabled.newValue === true;
      updateWidget();
    }
    if (areaName === "local" && changes.liveReCollector?.newValue) {
      collector = changes.liveReCollector.newValue;
      updateWidget();
    }
    if (areaName === "local" && changes.liveReAiStatus?.newValue) {
      const next = changes.liveReAiStatus.newValue || {};
      aiStatusMessage = String(next.message || (aiEnabled ? "đang kết nối…" : "đang tắt"));
      aiStatusOk = next.ok !== false;
      updateWidget();
    }
  });

  makeWidget();
  fetchTimelines().catch(() => {});
  chrome.storage.local.get(["liveReAiEnabled", "liveReAiStatus", "liveReCollector"])
    .then((stored) => {
      collector = stored.liveReCollector || null;
      aiEnabled = stored.liveReAiEnabled === true;
      aiStatusMessage = String(stored.liveReAiStatus?.message || (aiEnabled ? "đang kết nối…" : "đang tắt"));
      aiStatusOk = stored.liveReAiStatus?.ok !== false;
      updateWidget();
    })
    .catch(() => {});
  chrome.runtime.sendMessage({ type: "LIVE_RE_GET_STATE" })
    .then((response) => applyState(response?.state || liveState, response?.isController === true))
    .catch((error) => updateWidget(`Không đọc được trạng thái: ${error.message}`, false));

  tickTimer = setInterval(tick, 250);
  document.addEventListener("visibilitychange", tick);
  window.addEventListener("focus", tick);

  if (globalThis.__LIVE_RE_TEST_MODE__) {
    globalThis.__LIVE_RE_DEBUG__ = {
      applyState,
      tick,
      queueOperation,
      observeExternalPin,
      getState: () => ({ ...liveState }),
      getProductCache: () => [...productCache.entries()],
      getManualOverride: () => manualOverride ? { ...manualOverride } : null,
      getNextRepinAt: () => nextRepinAt,
      pollVideoPosition,
      driftMs,
      applyTimeline,
      fetchTimelines,
      getVideoSync: () => videoSync,
      expireManualOverride: () => {
        if (manualOverride) manualOverride.expiresAtMs = Date.now() - 1;
        tick();
      }
    };
  }
})();
