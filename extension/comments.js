"use strict";

(() => {
  if (globalThis.__LIVE_RE_COMMENTS_LOADED__) return;
  globalThis.__LIVE_RE_COMMENTS_LOADED__ = true;

  const nonce = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
  const pending = [];
  let aiPersistChain = Promise.resolve();
  let aiRestorePromise = Promise.resolve();
  let aiPersistenceOk = true;
  let aiQueueRevision = 0, aiWriteTask = null;
  const replyDeadline = row => Number.isFinite(row.first_sent_at_ms) ? row.first_sent_at_ms + 60000 : row.received_at_ms + 120000;
  const aiQueueKey = row => `${row.room_id}|${row.comment_id || `${row.user_id}|${row.created_at}|${row.comment}|${row.received_at_ms}`}`;
  function persistAiQueue() {
    aiQueueRevision++;
    if (!aiWriteTask) {
      aiWriteTask = (async () => {
        let written = -1;
        while (written !== aiQueueRevision) {
          const revision = aiQueueRevision;
          await chrome.storage.local.set({liveReAiPending:pending.map(row=>({...row}))});
          written = revision;
        }
        aiPersistenceOk = true;
      })().catch(error=>{aiPersistenceOk=false;throw error;}).finally(()=>{aiWriteTask=null;});
      aiPersistChain = aiWriteTask.catch(()=>{});
    }
    return aiWriteTask;
  }
  async function restoreAiQueue() {
    const saved = await chrome.storage.local.get('liveReAiPending');
    const present = new Set(pending.map(aiQueueKey));
    for (const row of Array.isArray(saved.liveReAiPending) ? saved.liveReAiPending : []) {
      if (!row || typeof row.room_id !== 'string' || !Number.isFinite(row.received_at_ms) ||
          Date.now() >= replyDeadline(row) || row.received_at_ms > Date.now() + 5000) continue;
      const key = aiQueueKey(row);
      if (!present.has(key)) { pending.push(row); present.add(key); }
    }
    pending.sort((a,b) => a.received_at_ms - b.received_at_ms);
    await persistAiQueue();
  }
  const seen = new Map();
  const sentEchoes = new Map();
  // Tài khoản chủ shop: tự học khi TikTok đánh dấu host (không gắn cứng shop nào).
  const hostIdentities = new Set();
  // Chủ phòng theo TỪNG phòng, tự lấy từ GetLiveRoomInfo (snapshot.host) hoặc
  // học từ tiếng vọng câu AI. Phòng mới/tài khoản khác tự được nhận diện.
  const roomHosts = new Map();
  let enabled = false;
  let busy = false;
  let installed = false;      // cầu ChatMessage đã cài (độc lập với AI)
  let lastCommentAtMs = 0;    // lần cuối cầu đưa lên một bình luận (O4)
  let aiReady = false;        // đã kiểm tra backend + đồng bộ danh mục
  let isController = false;
  let protocolV2 = false;
  let queueApi = false;       // backend có comment_queue_api
  let queueSyncBusy = false, queueSyncAgain = false;
  let isObserver = false;
  let liveStateCache = {};
  let observeQueue = [];
  let observeBusy = false;
  let observeRetried = 0;
  let observePersistErrors = 0;
  let observeDropped = 0;
  let aiDropped = 0;
  let lastPinId = "";
  let lastRoomId = "";
  let lastPinAt = 0;
  let collectedRows = 0;
  let collectorOk = true;
  let bridgeFailures = 0;
  let nextBridgeAttemptAt = 0;
  let lastSendAt = 0;
  let catalogSyncInFlight = null;
// TikTok chan gui khi nghi ngo spam: hien captcha/xac minh, hoac tra ve loi
// cho MOI lan Chat. Bo cu khong biet chuyen do: no bao mot dong loi roi lap
// tuc xu ly binh luan tiep theo, gui tiep, hong tiep — vua khong ai tra loi
// duoc, vua dam them vao he thong kiem soat rui ro cua TikTok.
let sendFailStreak = 0;
let sendPausedUntil = 0;
let sendPauseReason = "";
const SEND_PAUSE_STEPS_MS = [30000, 60000, 120000, 300000];
// Cac tu xuat hien khi TikTok doi xac minh. Khong the biet het moi cach
// TikTok dat ten, nen day chi la duong TAT de dung ngay tu lan hong dau;
// con lai da co luat "hong 3 lan lien tiep thi dung" o duoi lo.
const VERIFY_HINTS = [
  "captcha", "verify", "verification", "xac minh", "xác minh",
  "risk", "rui ro", "rủi ro", "too frequent", "qua nhanh", "quá nhanh",
  "frequent", "slow down", "try again later", "forbidden", "blocked",
  "限制", "验证", "频繁"
];

function looksLikeVerification(text) {
  const folded = String(text || "").toLowerCase();
  return VERIFY_HINTS.some((hint) => folded.includes(hint));
}

function sendPauseRemainingMs() {
  return Math.max(0, sendPausedUntil - Date.now());
}

function noteSendOk() {
  sendFailStreak = 0;
  sendPausedUntil = 0;
  sendPauseReason = "";
}

/** Tra ve true neu vua bat dau mot lan tam dung. */
function noteSendFailure(detail) {
  sendFailStreak += 1;
  const verification = looksLikeVerification(detail);
  if (!verification && sendFailStreak < 3) return false;
  const step = SEND_PAUSE_STEPS_MS[
    Math.min(sendFailStreak - 1, SEND_PAUSE_STEPS_MS.length - 1)
  ];
  sendPausedUntil = Date.now() + step;
  sendPauseReason = verification
    ? "TikTok đang đòi xác minh (captcha)"
    : `Gửi hỏng ${sendFailStreak} lần liên tiếp`;
  return true;
}
  let liveRunning = false;

  const OBSERVE_DB_NAME = "live-re-observer-v1";
  const OBSERVE_STORE = "events";
  let observeDbPromise = null;
  let observePersistChain = Promise.resolve();
  let observeRestorePromise = Promise.resolve();

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const clean = (value, limit = 500) => String(value || "").replace(/\s+/g, " ").trim().slice(0, limit);

  function identityKeys(raw) {
    return [
      raw?.user_id ? `id:${clean(raw.user_id, 160)}` : "",
      raw?.sec_uid ? `sec:${clean(raw.sec_uid, 240)}` : "",
      raw?.unique_id ? `unique:${clean(raw.unique_id, 160).toLowerCase()}` : ""
    ].filter(Boolean);
  }

  function isKnownHost(raw) {
    const keys = identityKeys(raw);
    const roomHost = roomHosts.get(String(lastRoomId || ""));
    return keys.some((key) => hostIdentities.has(key) || (roomHost && roomHost.keys.includes(key)));
  }

  function currentHost() {
    const entry = roomHosts.get(String(lastRoomId || ""));
    return entry ? entry.host : null;
  }

  async function setRoomHost(roomId, rawHost, source) {
    const room = clean(roomId, 160);
    const keys = identityKeys(rawHost);
    if (!room || !keys.length) return;
    const host = {
      user_id: clean(rawHost.user_id, 160), sec_uid: clean(rawHost.sec_uid, 240),
      unique_id: clean(rawHost.unique_id, 160), nickname: clean(rawHost.nickname, 160),
      source: source || clean(rawHost.source, 30) || "room_info"
    };
    const old = roomHosts.get(room);
    // Nguồn room_info đáng tin hơn tiếng vọng: không để echo ghi đè.
    if (old && old.host.source === "room_info" && host.source !== "room_info") return;
    if (old && JSON.stringify(old.keys) === JSON.stringify(keys) && old.host.source === host.source) return;
    roomHosts.set(room, { keys, host });
    while (roomHosts.size > 20) roomHosts.delete(roomHosts.keys().next().value);
    try {
      await chrome.storage.local.set({
        liveReRoomHosts: Object.fromEntries([...roomHosts].map(([id, v]) => [id, v.host]))
      });
    } catch {}
  }

  function learnHostFromSnapshot(response) {
    const snap = response?.snapshot;
    if (snap?.host && snap.room_id) setRoomHost(snap.room_id, snap.host, "room_info");
  }

  async function rememberHost(raw) {
    for (const key of identityKeys(raw)) hostIdentities.add(key);
    try {
      await chrome.storage.local.set({
        liveReHostIdentities: [...hostIdentities].slice(-20)
      });
    } catch {}
  }

  // TikTok hiển thị @mention theo NICKNAME ("@Vũ style 🇻🇳") trong khi ta soạn bằng
  // "@unique_id". Khoá tiếng vọng vì vậy chỉ dùng phần thân: bỏ @mention đầu câu,
  // chuẩn hoá Unicode, bỏ emoji/dấu câu/khoảng trắng.
  function echoBody(value) {
    return String(value ?? "").normalize("NFKC").toLocaleLowerCase("vi")
      .replace(/[^\p{L}\p{N}]+/gu, "");
  }
  function echoKey(value) {
    return echoBody(clean(value).replace(/^\s*@[^\s]+\s+/, ""));
  }
  // Tiếng vọng = thân câu đã gửi nằm ở CUỐI văn bản nhận về (đứng sau @mention theo
  // nickname, có thể có khoảng trắng), hoặc TikTok cắt bớt đuôi bằng "...".
  function matchSentEcho(rawText) {
    const text = clean(rawText);
    if (!text) return "";
    const ellipsis = /(?:\.{2,}|…)\s*$/.test(text);
    const full = echoBody(text.replace(/(?:\.{2,}|…)\s*$/, ""));
    if (!full) return "";
    for (const key of sentEchoes.keys()) {
      if (!key) continue;
      if (full === key || (key.length >= 6 && full.endsWith(key))) return key;
      if (ellipsis && key.length >= 12) {
        const at = full.indexOf(key.slice(0, 12));
        if (at >= 0 && key.startsWith(full.slice(at)) && full.length - at >= 12) return key;
      }
    }
    return "";
  }

  function pruneSentEchoes() {
    const cutoff = Date.now() - 10 * 60 * 1000;
    for (const [text, at] of sentEchoes) {
      if (at < cutoff) sentEchoes.delete(text);
    }
  }

  // ---------------------------------------------------------------------
  // THU THẬP DỮ LIỆU. Chạy độc lập hoàn toàn với việc trả lời: ghi cả bình
  // luận khi AI đang tắt, cả câu chủ phòng gõ tay, cả câu quá ngắn bị lọc.
  // Đây là kho dữ liệu để sau này dựng lớp phản xạ và hồ sơ giọng.
  // ---------------------------------------------------------------------
  function queueId(raw = {}) {
    const msgId = clean(raw?.comment_id || raw?.msg_id, 160);
    if (msgId) return `msg:${msgId}`;
    return globalThis.crypto?.randomUUID?.() ||
      `local:${Date.now()}:${Math.random().toString(36).slice(2)}`;
  }

  function openObserveDb() {
    if (!globalThis.indexedDB) return Promise.resolve(null);
    if (observeDbPromise) return observeDbPromise;
    observeDbPromise = new Promise((resolve, reject) => {
      const request = globalThis.indexedDB.open(OBSERVE_DB_NAME, 1);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(OBSERVE_STORE)) {
          db.createObjectStore(OBSERVE_STORE, { keyPath: "_queue_id" });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error("Không mở được kho bộ thu"));
    });
    return observeDbPromise;
  }

  async function persistObserveRow(row) {
    const db = await openObserveDb();
    if (!db) return;
    await new Promise((resolve, reject) => {
      const tx = db.transaction(OBSERVE_STORE, "readwrite");
      tx.objectStore(OBSERVE_STORE).put(row);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error || new Error("Không lưu được hàng đợi bộ thu"));
      tx.onabort = () => reject(tx.error || new Error("Kho bộ thu đã hủy giao dịch"));
    });
  }

  async function deletePersistedRows(rows) {
    const ids = rows.map((row) => row?._queue_id).filter(Boolean);
    if (!ids.length) return;
    const db = await openObserveDb();
    if (!db) return;
    await new Promise((resolve, reject) => {
      const tx = db.transaction(OBSERVE_STORE, "readwrite");
      const store = tx.objectStore(OBSERVE_STORE);
      for (const id of ids) store.delete(id);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error || new Error("Không dọn được hàng đợi đã gửi"));
      tx.onabort = () => reject(tx.error || new Error("Kho bộ thu đã hủy giao dịch"));
    });
  }

  async function restoreObserveQueue() {
    const db = await openObserveDb();
    if (!db) return;
    const rows = await new Promise((resolve, reject) => {
      const tx = db.transaction(OBSERVE_STORE, "readonly");
      const request = tx.objectStore(OBSERVE_STORE).getAll();
      request.onsuccess = () => resolve(request.result || []);
      request.onerror = () => reject(request.error || new Error("Không đọc được hàng đợi bộ thu"));
    });
    const present = new Set(observeQueue.map((row) => row._queue_id));
    for (const row of rows.sort((a, b) => Number(a._queued_at_ms) - Number(b._queued_at_ms))) {
      if (!present.has(row._queue_id)) observeQueue.push(row);
    }
  }

  // ---- F02: backend-requested purge of local customer data (privacy_purge in heartbeat) ----
  const PURGE_PLATFORM = "tiktok";
  function purgeFold(value) {
    return String(value ?? "").toLowerCase().replace(/\u0111/g, "d").normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "").replace(/\s+/g, " ").trim();
  }
  function purgeMentions(row, idents) {
    let blob;
    try { blob = purgeFold(JSON.stringify(row)); } catch (_) { return false; }
    return idents.some((ident) => {
      const at = blob.indexOf(ident);
      if (at < 0) return false;
      let from = 0;
      while ((from = blob.indexOf(ident, from)) >= 0) {
        const before = blob[from - 1], after = blob[from + ident.length];
        if (!(before && /[0-9a-z]/.test(before)) && !(after && /[0-9a-z]/.test(after))) return true;
        from += 1;
      }
      return false;
    });
  }
  async function purgeObserveStore(idents) {
    const db = await openObserveDb().catch(() => null);
    if (!db) return;
    const rows = await new Promise((resolve, reject) => {
      const request = db.transaction(OBSERVE_STORE, "readonly").objectStore(OBSERVE_STORE).getAll();
      request.onsuccess = () => resolve(request.result || []);
      request.onerror = () => reject(request.error);
    });
    await deletePersistedRows(rows.filter((row) => purgeMentions(row, idents)));
  }
  async function purgeMapKey(key, idents) {
    const saved = await chrome.storage.local.get(key);
    const map = saved?.[key];
    if (!map || typeof map !== "object") return;
    let changed = false;
    for (const id of Object.keys(map)) {
      if (purgeMentions(map[id], idents)) { delete map[id]; changed = true; }
    }
    if (changed) await chrome.storage.local.set({ [key]: map });
  }
  async function applyPrivacyPurge(items) {
    const ids = [];
    for (const item of Array.isArray(items) ? items : []) {
      const idents = (Array.isArray(item?.identifiers) ? item.identifiers : [])
        .map(purgeFold).filter(Boolean);
      if (typeof item?.id !== "string" || !idents.length) continue;
      try {
        const kept = pending.filter((row) => !purgeMentions(row, idents));
        if (kept.length !== pending.length) {
          pending.length = 0;
          for (const row of kept) pending.push(row);
        }
        await persistAiQueue();
        observeQueue = observeQueue.filter((row) => !purgeMentions(row, idents));
        await purgeObserveStore(idents);
        await purgeMapKey("liveReReceiptOutbox", idents);
        await purgeMapKey("liveReChatIntents", idents);
        ids.push(item.id);
      } catch (_) { /* not acknowledged: backend re-sends on the next heartbeat */ }
    }
    if (ids.length) {
      await chrome.runtime.sendMessage({ type: "LIVE_RE_PRIVACY_PURGE_ACK",
        payload: { ids, platform: PURGE_PLATFORM } }).catch(() => null);
    }
    return ids;
  }

  function queueObserveRow(row) {
    observeQueue.push(row);
    observePersistChain = observePersistChain.then(() => persistObserveRow(row)).catch(async (error) => {
      observePersistErrors += 1;
      collectorOk = false;
      await writeCollectorStatus(clean(error?.message || error, 140));
    });
  }

  function enqueueObserve(raw, action) {
    queueObserveRow({
      _queue_id: queueId(raw),
      _queued_at_ms: Date.now(),
      data_source: "live_extension",
      ts: new Date().toISOString(),
      room_id: lastRoomId,
      session_id: clean(liveStateCache?.sessionId, 80),
      msg_id: clean(raw?.comment_id, 160),
      user_id: clean(raw?.user_id, 160),
      unique_id: clean(raw?.unique_id, 160),
      nickname: clean(raw?.nickname, 160),
      text: clean(raw?.comment, 600),
      is_host: Boolean(raw?.is_host) || isKnownHost(raw),
      pin_id: lastPinId,
      video_position_s: videoPositionS(),
      ai_enabled: enabled,
      ai_action: action
    });
  }

  // Ghi lại chính câu AI vừa gửi, để một file raw_stream dựng lại được trọn
  // phiên: khách hỏi gì, ai trả lời (AI hay chủ phòng), trả lời ra sao.
  function enqueueAiReply(target, content) {
    queueObserveRow({
      _queue_id: queueId(),
      _queued_at_ms: Date.now(),
      data_source: "live_extension",
      ts: new Date().toISOString(),
      room_id: lastRoomId,
      session_id: clean(liveStateCache?.sessionId, 80),
      msg_id: "",
      user_id: "",
      unique_id: "",
      nickname: "Kid TE (AI)",
      text: content,
      is_host: true,
      reply_to_username: clean(target?.unique_id, 160),
      pin_id: lastPinId,
      video_position_s: videoPositionS(),
      ai_enabled: true,
      ai_action: "ai_replied"
    });
  }

  function videoPositionS() {
    const state = liveStateCache || {};
    if (!state.running || !state.startedAtMs) return 0;
    const elapsed = Math.max(0, Date.now() - Number(state.startedAtMs));
    const resolved = globalThis.KidTESchedule.resolve(elapsed);
    return Math.round(resolved.positionMs / 100) / 10;
  }

  async function refreshPinId() {
    // Tối đa 30 giây một lần, và chỉ khi thật sự có dữ liệu cần gắn nhãn.
    if (!isObserver || Date.now() - lastPinAt < 30000) return;
    lastPinAt = Date.now();
    try {
      // Nhan ghim lay tu cache (chu ky ghim cap nhat moi <=20s); chi khi
      // khong co cache trong 5 phut moi hoi TikTok mot lan.
      const snapshot = await liveSnapshot(PRODUCT_MAX_AGE_MS);
      lastPinId = clean(snapshot?.snapshot?.pin_id, 160);
      lastRoomId = clean(snapshot?.snapshot?.room_id, 160) || lastRoomId;
    } catch (error) {
      console.debug("[Kid TE] Chưa lấy được nhãn ghim:", clean(error?.message || error, 160));
    }
  }

  async function flushObserve() {
    if (observeBusy) return;
    observeBusy = true;
    let batch = [];
    try {
      await observeRestorePromise;
      await observePersistChain;
      if (!observeQueue.length) return;
      batch = observeQueue.splice(0, 100);
      await refreshPinId();
      for (const row of batch) {
        // Lô đầu tiên của phiên được xếp hàng trước khi có snapshot nào, nên
        // phải điền lại room/pin lúc gửi, nếu không cả lô đầu mất nhãn phòng.
        if (!row.pin_id) row.pin_id = lastPinId;
        if (!row.room_id) row.room_id = lastRoomId;
        if (!row.session_id) {
          row.session_id = clean(liveStateCache?.sessionId, 80);
        }
      }
      const result = await chrome.runtime.sendMessage({
        type: "LIVE_RE_OBSERVE",
        payload: { events: batch }
      });
      if (!result?.ok) throw new Error(result?.error || "backend từ chối");
      collectedRows = Number(result.total || collectedRows + batch.length);
      await deletePersistedRows(batch);
      collectorOk = true;
      await writeCollectorStatus("");
    } catch (error) {
      // Không mất dữ liệu khi backend chưa chạy: bản bền vẫn nằm trong
      // IndexedDB và bản RAM được trả lại đầu hàng đợi.
      observeRetried += batch.length;
      if (batch.length) observeQueue = batch.concat(observeQueue);
      collectorOk = false;
      await writeCollectorStatus(clean(error?.message || error, 140));
    } finally {
      observeBusy = false;
    }
  }

  async function writeCollectorStatus(error) {
    try {
      const statusError = error || (observePersistErrors
        ? `Có ${observePersistErrors} lỗi lưu hàng đợi bền`
        : "");
      await chrome.storage.local.set({
        liveReCollector: {
          rows: collectedRows,
          queued: observeQueue.length,
          retried: observeRetried,
          persistErrors: observePersistErrors,
          dropped: observeDropped,
          aiDropped,
          bridge: installed,
          observer: isObserver,
          ok: collectorOk && installed && observePersistErrors === 0,
          error: statusError,
          updatedAtMs: Date.now()
        }
      });
    } catch {}
  }

  // E2: định danh tab (sessionStorage, mỗi tab một giá trị) để backend phân biệt
  // heartbeat của tab điều khiển với tab TikTok Shop khác.
  const heartbeatTabId = (() => {
    const fresh = () => Math.random().toString(36).slice(2) + Date.now().toString(36);
    try {
      let id = sessionStorage.getItem("kidteTabId");
      if (!id) { id = fresh(); sessionStorage.setItem("kidteTabId", id); }
      return id;
    } catch { return fresh(); }
  })();
  let heartbeatEverSent = false;

  // Lệnh bật/tắt từ app Kid TE (Trung tâm LIVE) đi kèm heartbeat. Áp đúng MỘT lần
  // theo seq, và áp bằng đúng đường của popup: đặt liveReAiEnabled (onChanged ->
  // applyEnabled giữ mọi chặn an toàn: room_id, tab điều khiển, tạm dừng gửi) và
  // LIVE_RE_PAUSE/RESUME như nút "Tạm dừng" ghim. Người dùng đổi trong popup sau
  // đó không bị ép lại vì seq đã áp được nhớ trong liveReControlSeq.
  // Lệnh quá 10 phút (vd. cài lại tiện ích còn lệnh cũ trên backend) chỉ ghi nhận seq.
  const CONTROL_MAX_AGE_MS = 10 * 60 * 1000;
  async function applyAppControl(control) {
    const seq = Number(control?.seq);
    if (!Number.isInteger(seq) || seq <= 0) return false;
    // 3.11.8: như Shopee, chỉ tab giữ vai (điều khiển/quan sát) áp và xác nhận lệnh.
    // Trước đây tab TikTok Shop bất kỳ đều "nuốt" seq rồi báo đã áp dù không áp được.
    if (!(isController || isObserver)) return false;
    const saved = await chrome.storage.local.get("liveReControlSeq");
    if (seq <= Number(saved?.liveReControlSeq || 0)) return false;
    const issuedMs = Number(control.issued_at) * 1000;
    if (!Number.isFinite(issuedMs) || issuedMs <= 0 || Date.now() - issuedMs > CONTROL_MAX_AGE_MS) {
      await chrome.storage.local.set({ liveReControlSeq: seq,
        liveReControlError: "Lệnh từ app đã quá hạn (hơn 10 phút); không áp" });
      return false;
    }
    // Lựa chọn MỚI NHẤT thắng: người dùng đã bấm trong popup SAU lúc app phát lệnh
    // thì giữ lựa chọn của popup (vẫn ghi nhận seq + báo lý do để app hiện trạng thái thật).
    const local = await chrome.storage.local.get(["liveReAiChangedAtMs", "liveRePinChangedAtMs"]);
    const skipped = [];
    const failed = [];
    if (typeof control.ai_enabled === "boolean") {
      if (Number(local?.liveReAiChangedAtMs || 0) > issuedMs) skipped.push("AI");
      else {
        await chrome.storage.local.set({ liveReAiEnabled: control.ai_enabled, liveReAiChangedAtMs: issuedMs });
        // Chờ áp thật (install) rồi mới xác nhận: bật thất bại thì báo lý do về app,
        // không để app hiện "đã áp" trong khi AI vẫn tắt.
        await applyEnabledShared(control.ai_enabled);
        if (control.ai_enabled && !enabled) failed.push(`AI: ${lastAiError || "không bật được"}`);
      }
    }
    if (typeof control.auto_pin === "boolean" && Number(local?.liveRePinChangedAtMs || 0) > issuedMs) {
      skipped.push("Tự ghim");
    } else if (typeof control.auto_pin === "boolean") {
      const current = await chrome.runtime.sendMessage({ type: "LIVE_RE_GET_STATE" }).catch(() => null);
      const pinState = current?.state || {};
      let pinResult = null;
      if (control.auto_pin === false && pinState.running === true) {
        pinResult = await chrome.runtime.sendMessage({ type: "LIVE_RE_PAUSE" }).catch(e => ({ ok: false, error: String(e?.message || e) }));
      } else if (control.auto_pin === true && pinState.running !== true && Number(pinState.startedAtMs || 0) > 0) {
        // Chỉ tiếp tục phiên ghim đã có; chưa có phiên thì để tự bắt đầu theo phòng LIVE.
        pinResult = await chrome.runtime.sendMessage({ type: "LIVE_RE_RESUME" }).catch(e => ({ ok: false, error: String(e?.message || e) }));
      } else if (control.auto_pin === true && pinState.running !== true) {
        failed.push("Tự ghim: chưa có phiên ghim — mở tab LIVE dashboard để tự bắt đầu");
      }
      if (pinResult && pinResult.ok === false) failed.push(`Tự ghim: ${pinResult.error || "tiện ích từ chối"}`);
      else if (pinResult?.state && typeof pinResult.state.running === "boolean") {
        liveStateCache = pinResult.state;
        liveRunning = pinResult.state.running === true;
      }
    }
    const notes = [...failed];
    if (skipped.length) notes.push(`Giữ lựa chọn mới hơn trong tiện ích (${skipped.join(", ")})`);
    await chrome.storage.local.set({ liveReControlSeq: seq, liveReControlError: notes.join("; ").slice(0, 200) });
    // Báo app ngay (không đợi 10 giây) để công tắc ở app hết "Đang gửi…".
    sendHeartbeat().catch(() => {});
    return !notes.length;
  }

  async function sendHeartbeat() {
    // Chỉ tab điều khiển/quan sát (hoặc đang bật AI có phòng) mới gửi; tab đã từng
    // gửi thì gửi tiếp để việc tắt AI tới backend ngay.
    const shouldSend = isController || isObserver || (enabled && !!lastRoomId);
    if (!shouldSend && !heartbeatEverSent) return;
    heartbeatEverSent = true;
    try {
      const ctlSaved = await chrome.storage.local.get(["liveReControlSeq", "liveReControlError"]);
      const appliedSeq = Number(ctlSaved?.liveReControlSeq || 0);
      const result = await chrome.runtime.sendMessage({
        type: "LIVE_RE_HEARTBEAT",
        payload: {
          live_running: liveRunning,
          ai_enabled: enabled,
          ai_ready: aiReady,
          session_id: lastRoomId,
          tab_id: heartbeatTabId,
          host: currentHost(),
          observer: isObserver,
          reply_queue: pending.length,
          observe_queue: observeQueue.length,
          send_paused_until_ms: sendPausedUntil,
          send_pause_reason: sendPauseReason,
          // Đồng hồ tự ghim (liveReState.running) — trợ lý LIVE đọc trạng thái Tự ghim thật.
          pin_running: liveStateCache?.running === true,
          // O4: trạng thái thu bình luận để app cảnh báo khi cầu/bộ thu hỏng.
          collector: { ok: collectorOk && observePersistErrors === 0, bridge: installed,
            last_comment_at_ms: lastCommentAtMs },
          ...(appliedSeq > 0 ? { control_applied_seq: appliedSeq,
            control_error: String(ctlSaved?.liveReControlError || "") } : {})
        }
      });
      if (!result?.ok) throw new Error(result?.error || "backend từ chối heartbeat");
      if (Array.isArray(result.privacy_purge) && result.privacy_purge.length) {
        await applyPrivacyPurge(result.privacy_purge);
      }
      if (result.control) await applyAppControl(result.control).catch(() => false);
      // Phong im khong co batch observe de flush van la trang thai binh thuong.
      // Lam moi dong bo thu theo heartbeat, neu khong content.js se coi ban
      // ghi cu hon 20 giay la "mat ket noi backend" du backend van song.
      collectorOk = true;
      if (isObserver) await writeCollectorStatus("");
    } catch (error) {
      collectorOk = false;
      if (isObserver) await writeCollectorStatus(clean(error?.message || error, 140));
    }
  }

  async function setStatus(message, ok = true, extra = {}) {
    try {
      await chrome.storage.local.set({
        liveReAiStatus: { message, ok, enabled, updatedAtMs: Date.now(), ...extra }
      });
    } catch {}
  }

  // Doc tu cache truoc. content.js (cung tab) ghi globalThis.__KidTELiveSnapshot
  // sau moi lan hoi TikTok (chu ky ghim 20s / observe 10s). AI, nhan ghim, dong
  // bo gio dung chung ban nay; chi goi TikTok khi cache qua tuoi cho phep.
  const ROOM_CHECK_MAX_AGE_MS = 60 * 1000;
  // Gia va san pham it doi (chu shop quyet 19/10/2026): AI + dong bo gio dung
  // cache toi 15 phut. Doi room / het LIVE / sau ghim / quet tay van lam moi ngay;
  // khi dong ho ghim chay, cache van duoc cap nhat moi 20s.
  const PRODUCT_CACHE_MAX_AGE_MS = 15 * 60 * 1000;
  const PRODUCT_MAX_AGE_MS = PRODUCT_CACHE_MAX_AGE_MS;
  let snapshotInFlight = null;
  // Snapshot "day du" = co phong LIVE va co it nhat 1 san pham. Snapshot rong
  // (TikTok tra loi/tam thoi rong) hoac gio truoc LIVE KHONG duoc vao cache,
  // khong duoc giu 15 phut, khong duoc day len catalog.
  function completeLiveSnapshot(snapshot) {
    return Boolean(snapshot && String(snapshot.room_id || "") &&
      Array.isArray(snapshot.products) && snapshot.products.length > 0);
  }
  function rememberLiveSnapshot(snapshot) {
    if (completeLiveSnapshot(snapshot)) globalThis.__KidTELiveSnapshot = { snapshot, at: Date.now() };
  }
  function cachedLiveSnapshot() {
    const cached = globalThis.__KidTELiveSnapshot;
    return cached?.snapshot && Number(cached.at) > 0 && completeLiveSnapshot(cached.snapshot) ? cached : null;
  }
  async function liveSnapshot(maxAgeMs = PRODUCT_MAX_AGE_MS, expectedRoom = "") {
    const cached = cachedLiveSnapshot();
    if (cached && Date.now() - cached.at < maxAgeMs &&
        (!expectedRoom || String(cached.snapshot.room_id || "") === String(expectedRoom))) {
      return { ok: true, snapshot: cached.snapshot, cached: true };
    }
    if (!snapshotInFlight) {
      snapshotInFlight = apiRequest("snapshot").then((response) => {
        rememberLiveSnapshot(response.snapshot);
        return response;
      }).finally(() => { snapshotInFlight = null; });
    }
    return snapshotInFlight;
  }
  function clearLiveSnapshot() {
    globalThis.__KidTELiveSnapshot = null;
  }

  async function apiRequest(action, extra = {}) {
    const response = await chrome.runtime.sendMessage({
      type: "LIVE_RE_TIKTOK_API",
      request: { action, ...extra }
    });
    if (!response?.ok) {
      const lookup = response?.room_lookup || {};
      const detail = [
        lookup.response_code !== "" && lookup.response_code !== undefined
          ? `code=${lookup.response_code}` : "",
        lookup.message || ""
      ].filter(Boolean).join(" · ");
      throw new Error(
        (response?.error || `TikTok API ${action} thất bại`) + (detail ? ` (${detail})` : "")
      );
    }
    if (action === "snapshot") learnHostFromSnapshot(response);
    return response;
  }

  function pruneSeen() {
    const cutoff = Date.now() - 10 * 60 * 1000;
    for (const [key, at] of seen) {
      if (at < cutoff) seen.delete(key);
    }
    while (seen.size > 3000) seen.delete(seen.keys().next().value);
  }

  // TikTok cho bình luận tối đa 150 ký tự; server có thể gửi giới hạn khác qua max_chars.
  function taggedReply(comment, body, maxChars = 150) {
    const limit = Math.max(40, Math.min(150, Number(maxChars) || 150));
    const username = clean(comment.unique_id || comment.nickname, 80).replace(/^@+/, "");
    const prefix = `@${username} `;
    const available = Math.max(1, limit - prefix.length);
    let reply = clean(body, 300).replace(/^\s*@[^\s]+\s+/, "");
    // Server đếm UTF-16 (utf16_length) y như String.length ở đây; quá giới hạn thì cắt thân câu
    // theo từ/dấu câu gần nhất thay vì ném lỗi làm mất trả lời.
    if (reply.length > available) {
      let cut = reply.slice(0, available);
      if (/[\uD800-\uDBFF]$/.test(cut)) cut = cut.slice(0, -1);
      const stop = Math.max(cut.lastIndexOf(". "), cut.lastIndexOf("! "), cut.lastIndexOf("? "),
        cut.lastIndexOf(", "), cut.lastIndexOf(" "));
      if (stop >= available * 0.5) cut = cut.slice(0, stop + 1);
      reply = cut.replace(/[\s,;]+$/, "");
      if (!reply) throw new Error("Câu trả lời vượt giới hạn ký tự; cần soạn lại hoặc chia tin");
    }
    return prefix + reply;
  }

  // Gửi một lượt trả lời protocol 2 tới `target` (comment gốc). `row` là hàng cục bộ
  // để ghi first_sent_at_ms. Dùng chung cho đường gửi cục bộ và đường queue/next.
  async function sendProtocol2Turn(result, target, row) {
    // E1: chỉ lỗi của lệnh GỬI lên TikTok (send_chat) mới tính vào tạm dừng; lỗi
    // backend (control/claim/delivery_control_inactive/mạng 8793) thì không.
    let sendError = null;
    let outcome;
    try {
      outcome = await deliverProtocol2Turn(result, target, row, (error) => { sendError = error; });
    } catch (error) {
      if (sendError) {
        const detail = clean(sendError?.message || sendError, 300);
        if (noteSendFailure(detail)) {
          await setStatus(
            `⚠ TẠM DỪNG GỬI — ${sendPauseReason}. Thử lại sau ${Math.round(sendPauseRemainingMs() / 1000)}s. ` +
            `Mở tab TikTok giải captcha nếu có. (${detail})`,
            false,
            { sendPaused: true, sendPauseReason, sendPausedUntilMs: sendPausedUntil, sendFailStreak }
          );
        }
      }
      throw error;
    }
    if (outcome?.sent) noteSendOk();
    if (outcome?.stopped) {
      await setStatus('Dữ kiện đã đổi trước khi gửi; backend soạn lại lượt trả lời',true);
      return false;
    }
    await setStatus('Đã gửi đủ các tin của lượt trả lời',true);
    return true;
  }

  async function deliverProtocol2Turn(result, target, row, onSendError) {
    return globalThis.KidTEMessageDelivery.sendTurn(result, {
      active: async () => {
        const snapshot = await liveSnapshot(ROOM_CHECK_MAX_AGE_MS);
        const role = await chrome.runtime.sendMessage({type:'LIVE_RE_GET_STATE'});
        return enabled && role?.isController && role.state?.running && snapshot.snapshot?.room_id === result.room_id;
      },
      control: (operation,payload) => chrome.runtime.sendMessage({type:'LIVE_RE_MESSAGE_CONTROL',operation,payload}),
      send: (message,identity) => {
        // Đăng ký tiếng vọng TRƯỚC khi gọi API và giữ lại cả khi gọi lỗi/timeout:
        // TikTok có thể đã đăng câu dù receipt về trễ (backend đối soát qua echo).
        sentEchoes.set(echoKey(message.content), Date.now());
        return apiRequest('send_chat',{content:message.content, at_uid:target.user_id,
        expected_room_id:result.room_id,delivery:{...identity,interaction_id:result.interaction_id,reply_id:result.turn_id}})
          .then((receipt) => {
            if (!receipt?.ok) onSendError(new Error(receipt?.error || 'Kết quả gửi chưa rõ'));
            return receipt;
          }, (error) => { onSendError(error); throw error; });
      },
      ack: payload => chrome.runtime.sendMessage({type:'LIVE_RE_MESSAGE_ACK',payload:{...payload,
        interaction_id:result.interaction_id,reply_id:result.turn_id}}),
      unknown: payload => chrome.runtime.sendMessage({type:'LIVE_RE_MESSAGE_UNKNOWN',payload:{...payload,
        interaction_id:result.interaction_id,reply_id:result.turn_id}}),
      onSent: async (message,receipt) => {
        if (!row.first_sent_at_ms) row.first_sent_at_ms = receipt.sent_at_ms || Date.now();
        sentEchoes.set(echoKey(message.content),Date.now());
        enqueueAiReply(target,message.content);
        await persistAiQueue();
      }
    });
  }

  // Bản sao hàng chờ backend BẬT: comment bỏ ở tiện ích mà không gửi phải huỷ ở backend,
  // nếu không nó nằm đầu hàng và chặn lượt sau. Lỗi mạng bị nuốt (backend tự hết hạn sau 120s).
  function cancelBackendRow(row) {
    if (!queueApi || !row?.room_id || !row.comment_id) return;
    chrome.runtime.sendMessage({type: "LIVE_RE_QUEUE_CANCEL",
      payload: {room_id: row.room_id, comment_id: row.comment_id}}).catch(() => {});
  }

  function removeLocalRow(commentId) {
    const index = pending.findIndex(row => row.comment_id === commentId);
    if (index >= 0) pending.splice(index, 1);
  }

  // Một nhịp lấy việc từ backend. Trả 'handled' (đã xử lý xong một comment), 'idle'
  // (không có việc). Lỗi mạng / delivery_control_inactive ném lỗi: KHÔNG gửi cục bộ
  // thay thế (tránh gửi đôi), giữ hàng và thử lại ở nhịp sau.
  async function processBackendNext() {
    // Đang tạm dừng vì TikTok chặn: không xin việc, để backend khỏi soạn câu phí.
    if (sendPauseRemainingMs() > 0) return 'idle';
    const snapshotResult = await liveSnapshot(ROOM_CHECK_MAX_AGE_MS);
    const roomId = snapshotResult.snapshot?.room_id;
    if (!roomId) return 'idle';
    lastRoomId = roomId;
    if (protocolV2) await sendHeartbeat();
    const next = await chrome.runtime.sendMessage({type: "LIVE_RE_QUEUE_NEXT", payload: {room_id: roomId}});
    if (!next?.ok) throw new Error(next?.error || "Hàng chờ backend không phản hồi");
    if (!next.available) return 'idle';
    const result = next.result || {};
    const target = next.source_comment || {};
    if (!result.should_send) {
      removeLocalRow(target.comment_id);
      return 'handled';
    }
    if (result.protocol_version !== 2 || !globalThis.KidTEMessageDelivery) {
      throw new Error("Lượt trả lời từ hàng chờ không phải protocol 2");
    }
    const row = pending.find(item => item.comment_id === target.comment_id) || {};
    // R13: backend soạn lại (stopped) thì chưa gửi — không đánh dấu đã trả lời.
    if (await sendProtocol2Turn(result, target, row)) markAnswered(target);
    removeLocalRow(target.comment_id);
    return 'handled';
  }

  // Cùng một ngữ cảnh video cho đường /api/reply và bản sao lên hàng chờ backend.
  function videoContextFor(state) {
    const running = state.running === true;
    const elapsedMs = running && state.startedAtMs
      ? Math.max(0, Date.now() - Number(state.startedAtMs))
      : Math.max(0, Number(state.pausedElapsedMs) || 0);
    const resolved = globalThis.KidTESchedule.resolve(elapsedMs);
    const script = globalThis.KidTESchedule.scriptContextAt(resolved.positionMs);
    return {
      position_s: Math.round(resolved.positionMs / 100) / 10,
      current_topic: script?.topic || "",
      scripted_product_id: resolved.productKey || "",
      sync_confidence: "manual_clock",
      reviewed_script_facts: Array.isArray(script?.facts) ? [...script.facts] : []
    };
  }

  async function processOne(comment) {
    if (Date.now() >= replyDeadline(comment)) return;
    // Gia/san pham lay tu ban snapshot gan nhat (<=5 phut, dung phong cua
    // comment); qua cu hoac doi phong moi hoi TikTok mot lan.
    const snapshotResult = await liveSnapshot(PRODUCT_MAX_AGE_MS, comment.room_id);
    if (comment.room_id && snapshotResult.snapshot?.room_id !== comment.room_id) return;
    lastRoomId = snapshotResult.snapshot?.room_id || lastRoomId;
    if (!comment.room_id) comment.room_id = lastRoomId;
    const runtime = await chrome.runtime.sendMessage({ type: "LIVE_RE_GET_STATE" });
    const state = runtime?.state || {};
    liveRunning = state.running === true;
    if (protocolV2) await sendHeartbeat();
    const result = await chrome.runtime.sendMessage({
      type: "LIVE_RE_AI_REPLY",
      payload: {
        ...comment,
        protocol_version: protocolV2 ? 2 : 1,
        data_source: "live_extension",
        live_context: snapshotResult.snapshot || {},
        video_context: videoContextFor(state)
      }
    });
    if (!result?.ok) {
      throw new Error(result?.error || result?.reason || "AI local không phản hồi");
    }
    if (!result.should_send) {
      if (result.reason === "missing_data" || result.reason === "model_skipped") {
        const fields = Array.isArray(result.missing_fields)
          ? result.missing_fields.join(", ")
          : "dữ liệu";
        const count = Number(result.missing_count || 0);
        await setStatus(
          `Không trả lời ${comment.nickname}: thiếu ${fields}. Báo cáo hiện có ${count} câu.`,
          false,
          {
            lastMissingFields: result.missing_fields || [],
            missingCount: count,
            reportPath: result.report_path || ""
          }
        );
      }
      return;
    }
    if (result.protocol_version === 2) {
      if (await sendProtocol2Turn(result, result.source_comment || comment, comment)) markAnswered(comment);
      return;
    }
    const content = taggedReply(comment, result.reply, result.max_chars);
    const waitMs = Math.max(0, 2500 - (Date.now() - lastSendAt));
    if (waitMs) await sleep(waitMs);
    if (Date.now() - comment.received_at_ms >= 120000) return;
    const currentSnapshot = await liveSnapshot(ROOM_CHECK_MAX_AGE_MS, comment.room_id);
    if (comment.room_id && currentSnapshot.snapshot?.room_id !== comment.room_id) return;
    if (!enabled || !liveRunning) {
      if (result.interaction_id && result.reply_id) {
        await chrome.runtime.sendMessage({
          type: "LIVE_RE_AI_DELIVERY",
          payload: {
            interaction_id: result.interaction_id,
            reply_id: result.reply_id,
            status: "cancelled",
            detail: !enabled ? "ai_disabled_before_send" : "clock_stopped_before_send"
          }
        });
      }
      return;
    }
    // Đăng ký trước khi gửi vì TikTok có thể phát ChatMessage echo về gần như
    // đồng thời với response của API Chat.
    sentEchoes.set(echoKey(content), Date.now());
    let receipt;
    try {
      receipt = await apiRequest("send_chat", { content, at_uid: comment.user_id,
        expected_room_id: comment.room_id || currentSnapshot.snapshot?.room_id,
        delivery: result.interaction_id && result.reply_id ? {
          interaction_id: result.interaction_id, reply_id: result.reply_id
        } : null });
    } catch (error) {
      sentEchoes.delete(echoKey(content));
      const detail = clean(error?.message || error, 300);
      const paused = noteSendFailure(detail);
      if (result.interaction_id && result.reply_id) {
        await chrome.runtime.sendMessage({
          type: "LIVE_RE_AI_DELIVERY",
          payload: {
            interaction_id: result.interaction_id,
            reply_id: result.reply_id,
            status: paused ? "failed" : "unknown",
            detail: paused ? `${sendPauseReason} · ${detail}` : detail
          }
        }).catch(() => {});
      }
      if (paused) {
        // Gui tiep chi lam TikTok siet chat hon. Dung han, bao that to, roi
        // tu thu lai MOT lan sau khi het gio cho.
        const giay = Math.round(sendPauseRemainingMs() / 1000);
        await setStatus(
          `⚠ TẠM DỪNG GỬI — ${sendPauseReason}. Thử lại sau ${giay}s. ` +
          `Mở tab TikTok giải captcha nếu có. (${detail})`,
          false,
          {
            sendPaused: true,
            sendPauseReason,
            sendPausedUntilMs: sendPausedUntil,
            sendFailStreak
          }
        );
      }
      throw error;
    }
    noteSendOk();
    lastSendAt = Number(receipt.sent_at_ms) || Date.now();
    if (result.requires_ack !== false) {
      let acknowledgement = null;
      for (const delay of [0, 250, 750]) {
        if (delay) await sleep(delay);
        acknowledgement = await chrome.runtime.sendMessage({
          type: "LIVE_RE_AI_ACK",
          payload: {
            interaction_id: result.interaction_id,
            reply_id: result.reply_id,
            receipt: {
              room_id: receipt.room_id || "",
              response_code: receipt.response_code,
              sent_at_ms: lastSendAt
            }
          }
        }).catch(() => null);
        if (acknowledgement?.ok) break;
      }
      if (!acknowledgement?.ok) {
        throw new Error("TikTok đã gửi nhưng nhật ký AI chưa ghi nhận được receipt");
      }
    }
    enqueueAiReply(comment, content);
    await setStatus(`Đã trả lời ${content}`, true, { lastReply: content });
  }

  // Báo comment lên hàng chờ backend (FIFO bền vững). Chỉ là bản sao phụ: lỗi nào
  // cũng nuốt, không bao giờ chặn hay đổi đường gửi. Hàng chưa có backend_queue_id
  // được gửi lại ở nhịp 2s với ĐÚNG received_at_ms gốc (backend giữ mốc đầu tiên).
  async function syncBackendQueue() {
    if (!queueApi) return;
    if (queueSyncBusy) { queueSyncAgain = true; return; }
    queueSyncBusy = true;
    try {
      let changed = false;
      let context = null; // live_context + video_context, same as processOne sends to /api/reply
      for (const row of pending.slice()) {
        // Bình luận nhận lúc chưa biết phòng: gán phòng hiện tại thay vì bỏ rơi mãi.
        if (!row.room_id && lastRoomId) { row.room_id = lastRoomId; changed = true; }
        if (row.backend_queue_id || Date.now() >= replyDeadline(row) || !row.room_id) continue;
        try {
          if (!context) {
            const snapshot = (await liveSnapshot(PRODUCT_MAX_AGE_MS, row.room_id)).snapshot || {};
            const runtime = await chrome.runtime.sendMessage({ type: "LIVE_RE_GET_STATE" });
            context = {live_context: snapshot, video_context: videoContextFor(runtime?.state || {})};
          }
          const {backend_queue_id, ...comment} = row;
          const result = await chrome.runtime.sendMessage({
            type: "LIVE_RE_QUEUE_ENQUEUE",
            payload: {room_id: row.room_id, comment_payload: {
              ...comment, protocol_version: protocolV2 ? 2 : 1,
              data_source: "live_extension", ...context,
              live_context: {...context.live_context, room_id: row.room_id}}}
          });
          if (result?.ok) { row.backend_queue_id = result.queue_id || `state:${result.state || "none"}`; changed = true; }
          else setStatus(`Không đưa được bình luận vào hàng chờ: ${clean(result?.error || "lỗi không rõ", 120)}`, false).catch(() => {});
        } catch (error) {
          setStatus(`Không đưa được bình luận vào hàng chờ: ${clean(error?.message || error, 120)}`, false).catch(() => {});
        }
      }
      if (changed) await persistAiQueue();
    } catch {} finally {
      queueSyncBusy = false;
      if (queueSyncAgain) { queueSyncAgain = false; syncBackendQueue().catch(() => {}); }
    }
  }

  async function drain() {
    if (busy) return;
    busy = true;
    try {
      await aiRestorePromise;
      await aiPersistChain;
      const retained = pending.filter(row=>Date.now()<replyDeadline(row));
      if (retained.length!==pending.length) {
        for (const row of pending) if (!retained.includes(row)) cancelBackendRow(row);
        pending.length = 0;
        for (const row of retained) pending.push(row);
        await persistAiQueue();
      }
      if (!aiPersistenceOk) throw new Error('Không lưu được hàng chờ AI; đã dừng gửi để bảo vệ dữ liệu');
      while (enabled && liveRunning && pending.length) {
        // Dang bi TikTok chan gui thi DUNG HAN, khong duyet tiep hang cho.
        // Bo cu chay tiep nen mot lan bi chan thanh hang chuc lan gui hong.
        const pauseLeftMs = sendPauseRemainingMs();
        if (pauseLeftMs > 0) {
          await setStatus(
            `⚠ TẠM DỪNG GỬI — ${sendPauseReason}. Còn ${Math.round(pauseLeftMs / 1000)}s ` +
            `mới thử lại. ${pending.length} bình luận đang đợi.`,
            false,
            { sendPaused: true, sendPauseReason, sendPausedUntilMs: sendPausedUntil }
          );
          // Phong đơ hẳn khi phòng im: tự đánh thức lại đúng lúc hết giờ chờ.
          setTimeout(() => { drain().catch(() => {}); }, pauseLeftMs + 250);
          break;
        }
        if (queueApi) {
          let outcome = 'idle';
          try {
            await setStatus("AI đang lấy việc từ hàng chờ backend", true);
            outcome = await processBackendNext();
          } catch (error) {
            // Vừa bị chặn khi gửi: quay lại đầu vòng để hiện trạng thái và hẹn giờ thử lại.
            if (sendPauseRemainingMs() > 0) continue;
            await setStatus(`Lỗi AI: ${clean(error?.message || error, 180)}`, false);
            break;
          } finally {
            await persistAiQueue();
          }
          if (outcome !== 'handled') {
            // Hàng chờ backend trống mà máy còn bình luận chưa gửi lên: đẩy lại (trước đây kẹt mãi).
            if (pending.some(row => !row.backend_queue_id && Date.now() < replyDeadline(row))) {
              await syncBackendQueue();
            }
            break;
          }
          continue;
        }
        const comment = pending[0];
        let handled = false;
        try {
          await setStatus(`AI đang xử lý: ${comment.nickname}`, true);
          await processOne(comment);
          handled = true;
        } catch (error) {
          await setStatus(`Lỗi AI: ${clean(error?.message || error, 180)}`, false);
          break;
        } finally {
          if ((handled || Date.now() >= replyDeadline(comment)) && pending[0] === comment) pending.shift();
          await persistAiQueue();
        }
      }
    } finally {
      busy = false;
    }
  }

  const recentQuestions = new Map();
  // Bình luận đã được trả lời (theo comment_id) và khách vừa được shop trả lời
  // (để nhận "2", "5" là câu trả lời cho câu hỏi lại). Chỉ sống 3 phút.
  const answeredComments = new Map();
  const recentlyAnsweredUsers = new Map();
  function whoOf(row) {
    return clean(row?.user_id || row?.sec_uid || row?.unique_id, 240);
  }
  function markAnswered(row) {
    const now = Date.now();
    if (row?.comment_id) answeredComments.set(String(row.comment_id), now);
    const who = whoOf(row);
    if (who) recentlyAnsweredUsers.set(who, now);
    for (const map of [answeredComments, recentlyAnsweredUsers]) {
      if (map.size > 500) for (const [k, at] of map) if (now - at >= 180000) map.delete(k);
    }
  }
  function justAnswered(row) {
    const at = recentlyAnsweredUsers.get(whoOf(row));
    return Boolean(at && Date.now() - at < 180000);
  }
  function isShortAnswer(text) {
    return /^[\p{L}\p{N}]$/u.test(clean(text));
  }

  function acceptComment(raw) {
    // GHI TRƯỚC, LỌC SAU. Mọi nhánh return bên dưới đều đã được ghi lại ở
    // onRawComment, nên bộ dữ liệu không bao giờ mất một dòng nào.
    if (raw?.is_host) return;
    if (isKnownHost(raw)) {
      rememberHost(raw);
      return;
    }
    if (!enabled || !aiReady || !liveRunning) return;
    const rawText = clean(raw?.comment);
    pruneSentEchoes();
    if (matchSentEcho(rawText)) {
      rememberHost(raw);
      setRoomHost(lastRoomId, raw, "echo");
      setStatus("Đã chặn tiếng vọng câu trả lời của chính chủ phòng.", true, {
        selfEchoBlocked: true
      });
      return;
    }
    const comment = {
      comment: rawText,
      comment_id: clean(raw?.comment_id, 160),
      created_at: clean(raw?.created_at, 80),
      user_id: clean(raw?.user_id, 160),
      sec_uid: clean(raw?.sec_uid, 240),
      unique_id: clean(raw?.unique_id, 160),
      nickname: clean(raw?.nickname || raw?.unique_id || "Khách", 160),
      room_id: lastRoomId,
      received_at_ms: Date.now()
    };
    if (!(comment.user_id || comment.sec_uid || comment.unique_id)) return;
    // "2", "5": chỉ nhận khi khách này vừa được shop trả lời/hỏi lại (backend xét tiếp).
    if (comment.comment.length < 2 && !(isShortAnswer(comment.comment) && justAnswered(comment))) return;
    // Chống trùng phải có PHẠM VI phòng, không chỉ nội dung.
    //
    // Khoá cũ là `comment_id || user_id|created_at|comment`, hoàn toàn không
    // biết bình luận thuộc phòng nào. Hai hệ quả:
    //  - `seen` sống qua các lần đổi phòng, nên một bình luận của phòng mới
    //    trùng khoá với phòng cũ sẽ bị bỏ im lặng.
    //  - Khi thiếu `created_at`, khoá rút về `user|comment`, nghĩa là khách
    //    gửi lại đúng câu đó lần nữa sẽ không bao giờ được nhận. Dữ liệu thật
    //    có sẵn những cặp như vậy ("ey", "Alo" từ cùng một khách).
    //
    // Sự kiện mạng phát lại mang đúng `created_at` của lần đầu, còn khách gửi
    // lại thì `created_at` khác — nên mốc thời gian của CHÍNH sự kiện mới là
    // thứ phân biệt hai trường hợp. Thiếu nó thì dùng mốc nhận theo giây để
    // gộp bản phát lại tức thời, thay vì chặn vĩnh viễn.
    const phamVi = String(lastRoomId || "?");
    const moc = comment.created_at || `nhan:${Math.floor(Date.now() / 1000)}`;
    const key = comment.comment_id
      ? `${phamVi}|msg:${comment.comment_id}`
      : `${phamVi}|${comment.user_id}|${moc}|${comment.comment}`;
    if (seen.has(key)) return;
    seen.set(key, Date.now());
    // Cùng khách hỏi lại đúng câu đó trong 3 phút: trả lời một lần.
    const who = comment.user_id || comment.sec_uid || comment.unique_id;
    const dupKey = `${phamVi}|dup:${who}|${comment.comment.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim()}`;
    // Chỉ bỏ khi câu trước còn đang chờ trong hàng hoặc ĐÃ được trả lời; câu
    // trước bị bỏ qua/hết hạn/lỗi thì khách hỏi lại phải được xét lại.
    const prev = recentQuestions.get(dupKey);
    if (prev && Date.now() - prev.at < 180000) {
      const stillWaiting = pending.includes(prev.row)
        || Boolean(prev.id && pending.some(row => row.comment_id === prev.id));
      const answered = Boolean(prev.id && answeredComments.has(prev.id));
      if (stillWaiting || answered) return;
    }
    recentQuestions.set(dupKey, {at: Date.now(), id: comment.comment_id ? String(comment.comment_id) : "", row: comment});
    if (recentQuestions.size > 500) {
      for (const [k, v] of recentQuestions) if (Date.now() - v.at >= 180000) recentQuestions.delete(k);
    }
    pruneSeen();
    while (pending.length && Date.now() >= replyDeadline(pending[0])) { cancelBackendRow(pending.shift()); aiDropped += 1; }
    pending.push(comment);
    syncBackendQueue().catch(() => {});
    persistAiQueue().then(() => drain()).catch(error => setStatus(
      `Không lưu được hàng chờ AI: ${clean(error?.message || error, 180)}`, false));
  }

  function observeAction(raw) {
    // TIẾNG VỌNG PHẢI ĐƯỢC KIỂM TRƯỚC "chủ phòng gõ tay".
    // TikTok phát lại chính câu AI vừa gửi dưới danh nghĩa chủ phòng, nên bản
    // cũ gán nó là host_manual: mỗi câu AI bị ghi HAI dòng (ai_replied +
    // host_manual). Ngày 12/09 có 15 dòng host_manual mà phần lớn là câu máy.
    // Đây chính là bộ dữ liệu dùng để học giọng chủ phòng sau này — dạy máy
    // bằng chính câu của máy thì càng học càng lệch.
    if (matchSentEcho(raw?.comment)) return "self_echo";
    if (Boolean(raw?.is_host) || isKnownHost(raw)) return "host_manual";
    if (!enabled || !aiReady) return "ai_off";
    if (!liveRunning) return "clock_stopped";
    if (clean(raw?.comment).length < 2 && !(isShortAnswer(raw?.comment) && justAnswered(raw))) return "too_short";
    return "queued_for_ai";
  }

  function onRawComment(raw) {
    // Cầu ChatMessage sống trong trang nên vẫn bắn sự kiện sau khi tab này
    // mất vai quan sát. Không chặn ở đây thì hai tab cùng ghi một bình luận.
    if (!isObserver) return;
    try {
      enqueueObserve(raw, observeAction(raw));
    } catch {}
    try {
      acceptComment(raw);
    } catch {}
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    if (event.data?.source !== "LIVE_RE_TIKTOK_CHAT" || event.data?.nonce !== nonce) return;
    lastCommentAtMs = Date.now();   // O4: app cảnh báo khi LIVE mà lâu không có bình luận
    onRawComment(event.data.payload);
  });

  // Cầu ChatMessage được cài ngay khi mở trang, KHÔNG phụ thuộc nút BẬT AI và
  // không phụ thuộc đồng hồ Kid TE. Có như vậy mới thu được phiên anh rep tay.
  async function ensureBridge() {
    const role = await chrome.runtime.sendMessage({
      type: "LIVE_RE_GET_STATE", claimObserver: true
    });
    liveStateCache = role?.state || {};
    liveRunning = liveStateCache.running === true;
    isController = role?.isController === true;
    // Vai quan sát KHÔNG phụ thuộc việc đã bấm "Bắt đầu". Trước đây chỉ tab
    // điều khiển mới cài cầu, mà tab điều khiển chỉ tồn tại sau khi bấm Bắt
    // đầu — nghĩa là phiên chỉ ngồi rep tay sẽ không thu được dòng nào.
    isObserver = role?.isObserver === true || isController;
    if (!isObserver) {
      installed = false;
      return false;
    }
    if (installed) return true;
    if (Date.now() < nextBridgeAttemptAt) return false;
    const result = await chrome.runtime.sendMessage({
      type: "LIVE_RE_INSTALL_COMMENT_BRIDGE",
      nonce
    });
    installed = Boolean(result?.ok);
    if (!installed) {
      // Bundle TikTok chưa sẵn sàng là chuyện thường lúc mới mở trang. Giãn
      // dần để không đẩy hàng nghìn entry vào webpack runtime suốt phiên dài.
      bridgeFailures += 1;
      nextBridgeAttemptAt = Date.now() +
        Math.min(60000, 10000 * Math.min(6, bridgeFailures));
      await writeCollectorStatus(clean(result?.error || "chưa cài được cầu bình luận", 140));
      throw new Error(result?.error || "Không cài được cầu ChatMessage");
    }
    bridgeFailures = 0;
    nextBridgeAttemptAt = 0;
    await writeCollectorStatus("");
    return true;
  }

  // Cờ này bật CẢ bản sao hàng chờ lẫn đường gửi qua backend queue/next (tắt mặc định).
  // Bật bằng công tắc backend (capabilities.backend_queue_mode) hoặc cờ cục bộ cũ.
  let queueModeCheckedAt = 0;
  async function applyQueueMode(health) {
    const mirror = await chrome.storage.local.get('liveReBackendQueueMirror').catch(() => ({}));
    queueApi = health?.capabilities?.comment_queue_api === true &&
      (health.capabilities.backend_queue_mode === true || mirror?.liveReBackendQueueMirror === true);
    queueModeCheckedAt = Date.now();
  }
  async function refreshQueueMode() {
    if (Date.now() - queueModeCheckedAt < 60000) return;
    queueModeCheckedAt = Date.now();
    const health = await chrome.runtime.sendMessage({ type: "LIVE_RE_AI_HEALTH" }).catch(() => null);
    if (health?.ok) await applyQueueMode(health);
  }

  async function install() {
    if (!enabled) return;
    if (!(await ensureBridge())) {
      aiReady = false;
      return;
    }
    const health = await chrome.runtime.sendMessage({ type: "LIVE_RE_AI_HEALTH" });
    if (!health?.ok) throw new Error(health?.error || "Cầu AI local chưa sẵn sàng");
    protocolV2 = health.capabilities?.multi_message_reply === true && Boolean(globalThis.KidTEMessageDelivery);
    await applyQueueMode(health);
    const catalog = await syncCatalog(true);
    if (Number(catalog.product_count || 0) <= 0) {
      throw new Error("API TikTok chưa trả về danh sách sản phẩm; AI vẫn tắt");
    }
    if (!catalog.live_room_id) {
      // Giỏ trước LIVE đã lưu nhưng chưa có room_id: không bật AI trả lời bình luận.
      throw new Error(`Chưa LIVE: đã lưu giỏ ${Number(catalog.product_count || 0)} sản phẩm, AI chờ có phòng LIVE`);
    }
    aiReady = true;
    await setStatus(
      `AI đã sẵn sàng · đã lưu ${Number(catalog.product_count || 0)} sản phẩm từ API TikTok`,
      true,
      { catalogCount: Number(catalog.product_count || 0), catalogSyncedAt: catalog.synced_at }
    );
  }

  // Đang quét thì CHỜ lượt đang chạy, không trả "0 sản phẩm" (bản cũ làm
  // popup báo "Đã lưu 0 SKU" và bật AI thất bại khi bấm trùng lúc).
  // Quét tay (userInitiated) KHÔNG ghép vào lượt tự động đang chạy (lượt đó có thể
  // bị cổng/guard chặn hoặc không xoá tombstone): chờ nó xong rồi tự gọi TikTok.
  let manualSyncInFlight = null;
  function syncCatalog(includeRaw = false, userInitiated = false, target = null) {
    if (userInitiated) {
      if (!manualSyncInFlight) {
        const previous = catalogSyncInFlight ? catalogSyncInFlight.catch(() => {}) : Promise.resolve();
        const run = previous.then(() => runCatalogSync(includeRaw, true, target));
        const tracked = run.finally(() => {
          manualSyncInFlight = null;
          if (catalogSyncInFlight === tracked) catalogSyncInFlight = null;
        });
        catalogSyncInFlight = manualSyncInFlight = tracked;
      }
      return manualSyncInFlight;
    }
    if (!catalogSyncInFlight) {
      catalogSyncInFlight = runCatalogSync(includeRaw, userInitiated).finally(() => { catalogSyncInFlight = null; });
    }
    return catalogSyncInFlight;
  }

  let lastCatalogSignature = "";
  let lastCatalogSyncAt = 0;
  function catalogSignature(snapshot) {
    const products = Array.isArray(snapshot?.products) ? snapshot.products : [];
    return String(snapshot?.room_id || "") + "|" + products.map((item) =>
      [item.product_id, item.bag_index, item.price, item.min_price, item.max_price,
        item.sale_price, item.stock, item.title].join("~")).join("|");
  }

  async function pushCatalog(context, userInitiated = false, target = null) {
    const payload = { live_context: context };
    if (userInitiated) {
      payload.user_initiated = true;
      // Quét tay: "lấy ở đâu lưu ở đó" — giỏ mới (mặc định) hoặc ghi đè đúng giỏ đã chọn.
      payload.cart_target = String(target?.cartTarget || "new");
      payload.cart_name = String(target?.cartName || "");
      if (target?.confirmed === true) payload.confirmed = true;
    }
    const catalog = await chrome.runtime.sendMessage({ type: "LIVE_RE_CATALOG_SYNC", payload });
    if (userInitiated && catalog?.needs_confirm) {
      // Giữ đúng dữ liệu vừa quét: bấm xác nhận thì ghi chính bản này, không gọi TikTok lại.
      pendingOverwrite = { context, cartTarget: payload.cart_target, at: Date.now() };
      return { ...catalog, tiktok_count: context.products?.length || 0 };
    }
    if (!catalog?.ok) throw new Error(catalog?.error || "Không đồng bộ được danh mục LIVE");
    if (userInitiated && catalog.skipped) {
      throw new Error(`Kid TE chưa ghi giỏ mới (${catalog.skipped}); danh mục cũ giữ nguyên`);
    }
    lastCatalogSignature = catalogSignature(context);
    lastCatalogSyncAt = Date.now();
    return {
      ...catalog,
      live_room_id: String(context.room_id || ""),
      live_phase: context.room_id ? "live" : String(context.phase || "prelive")
    };
  }

  // Gio doi (tu snapshot cua chu ky ghim): day len Kid TE ngay, khong goi TikTok.
  async function syncCatalogFromCache() {
    const cached = cachedLiveSnapshot();
    if (!cached || catalogSyncInFlight) return null;
    if (catalogSignature(cached.snapshot) === lastCatalogSignature) return null;
    return pushCatalog(cached.snapshot);
  }

  let pendingOverwrite = null;
  async function runCatalogSync(includeRaw, userInitiated = false, target = null) {
    const pending = pendingOverwrite;
    pendingOverwrite = null;
    if (userInitiated && target?.confirmed === true && pending && pending.cartTarget === target.cartTarget &&
        Date.now() - pending.at < 120000) {
      const saved = await pushCatalog(pending.context, true, target);
      return { ...saved, tiktok_count: pending.context.products.length };
    }
    // Quet tay / cai AI / bat dau LIVE: LUON hoi TikTok (khong dung cache).
    const snapshot = await apiRequest("snapshot", { includeCatalogRaw: includeRaw, userInitiated });
    const context = snapshot.snapshot || {};
    if (!Array.isArray(context.products) || context.products.length === 0) {
      throw new Error("TikTok trả 0 sản phẩm (có thể đang quá tải/xác minh); giữ nguyên danh mục cũ, hãy thử quét lại");
    }
    rememberLiveSnapshot(context);
    const saved = await pushCatalog(context, userInitiated, target);
    return { ...saved, tiktok_count: context.products.length };
  }

  // Gộp các lần áp cùng giá trị đang chạy (lệnh app + onChanged) để không cài AI hai lần.
  let aiApplyInFlight = null;
  let lastAiError = "";
  function applyEnabledShared(value) {
    value = Boolean(value);
    if (aiApplyInFlight && aiApplyInFlight.value === value) return aiApplyInFlight.promise;
    const promise = Promise.resolve(applyEnabled(value)).finally(() => {
      if (aiApplyInFlight?.promise === promise) aiApplyInFlight = null;
    });
    aiApplyInFlight = { value, promise };
    return promise;
  }

  async function applyEnabled(value) {
    enabled = Boolean(value);
    lastAiError = "";
    if (!enabled) {
      await aiRestorePromise;
      pending.length = 0;
      await persistAiQueue();
      aiReady = false;
      await setStatus("AI tự trả lời đang tắt · vẫn đang thu dữ liệu bình luận", true);
      return;
    }
    try {
      await install();
    } catch (error) {
      enabled = false;
      aiReady = false;
      lastAiError = clean(error?.message || error, 160);
      await chrome.storage.local.set({ liveReAiEnabled: false });
      await setStatus(`Chưa bật được AI: ${clean(error?.message || error, 180)}`, false);
    }
  }

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.liveReAiEnabled) {
      // Đổi trong popup/widget/app: gửi heartbeat ngay sau khi áp để app thấy trong vài giây.
      const want = Boolean(changes.liveReAiEnabled.newValue);
      // Đã áp xong đúng giá trị (vd. lệnh app vừa áp trong tab này) thì không cài lại.
      const already = want === enabled && (!want || aiReady) && !aiApplyInFlight;
      Promise.resolve(already ? null : applyEnabledShared(want))
        .finally(() => { sendHeartbeat().catch(() => {}); });
    }
    if (area === "local" && changes.liveReState) {
      liveStateCache = changes.liveReState.newValue || {};
      const pinChanged = (changes.liveReState.oldValue || {}).running !== liveStateCache.running;
      liveRunning = liveStateCache.running === true;
      ensureBridge().catch(() => {});
      if (pinChanged) sendHeartbeat().catch(() => {});
      // Doi phong/ket thuc: bo cache de khong tra loi theo gio cu.
      if (pinChanged && !liveRunning) clearLiveSnapshot();
      if (enabled && (!aiReady || pinChanged)) install().catch(() => { aiReady = false; });
    }
  });
  aiRestorePromise = restoreAiQueue();
  aiRestorePromise.catch(error => setStatus(`Không khôi phục được hàng chờ AI: ${clean(error?.message || error, 180)}`, false));
  chrome.storage.local.get(["liveReAiEnabled", "liveReHostIdentities", "liveReRoomHosts"])
    .then((stored) => {
      for (const key of stored.liveReHostIdentities || []) hostIdentities.add(String(key));
      for (const [room, host] of Object.entries(stored.liveReRoomHosts || {})) {
        const keys = identityKeys(host);
        if (keys.length && !roomHosts.has(room)) roomHosts.set(room, { keys, host });
      }
      return applyEnabled(stored.liveReAiEnabled === true);
    })
    .catch((error) => setStatus(`Không đọc được cấu hình AI: ${error.message}`, false));
  setInterval(() => {
    if (!(enabled && aiReady && liveRunning)) return;
    refreshQueueMode().catch(() => {}).finally(() => { syncBackendQueue().catch(() => {}); drain().catch(() => {}); });
  }, 2000);

  // Cầu thu thập tự nối lại kể cả khi AI tắt.
  observeRestorePromise = restoreObserveQueue().then(() => writeCollectorStatus("")).catch(async (error) => {
    observePersistErrors += 1;
    collectorOk = false;
    await writeCollectorStatus(clean(error?.message || error, 140));
  });
  ensureBridge().catch(() => {});
  sendHeartbeat();
  setInterval(() => { ensureBridge().catch(() => {}); }, 10000);
  setInterval(() => { sendHeartbeat(); }, 10000);
  setInterval(() => { flushObserve(); }, 2000);

  // Thu noi lai AI: gian dan 10s -> 20s -> 40s ... toi da 5 phut; dat lai khi
  // thanh cong hoac khi trang chuyen sang LIVE. Chi tab o LIVE dashboard moi thu
  // (background con chan them: chi tab giu quyen moi duoc goi TikTok).
  const INSTALL_RETRY_MIN_MS = 10000;
  const INSTALL_RETRY_MAX_MS = 5 * 60 * 1000;
  let installRetryMs = INSTALL_RETRY_MIN_MS;
  let installRetryTimer = null;
  function onLiveDashboardPage() {
    return String(globalThis.location?.pathname || "").startsWith("/streamer/live/product/dashboard");
  }
  function scheduleInstallRetry(resetDelay = false) {
    if (resetDelay) installRetryMs = INSTALL_RETRY_MIN_MS;
    if (installRetryTimer !== null) clearTimeout(installRetryTimer);
    installRetryTimer = setTimeout(async () => {
      installRetryTimer = null;
      if (enabled && !aiReady && onLiveDashboardPage()) {
        try {
          await install();
        } catch (error) {
          await setStatus(`Đang thử nối lại AI (lần sau ~${Math.round(installRetryMs * 2 / 1000)}s): ${clean(error?.message || error, 160)}`, false);
        }
        installRetryMs = aiReady ? INSTALL_RETRY_MIN_MS : Math.min(INSTALL_RETRY_MAX_MS, installRetryMs * 2);
      } else if (aiReady) {
        installRetryMs = INSTALL_RETRY_MIN_MS;
      }
      scheduleInstallRetry();
    }, installRetryMs);
  }
  scheduleInstallRetry(true);

  // Làm mới giỏ định kỳ cả khi AI tắt hoặc chưa LIVE (giỏ prelive).
  // Khi AI tắt chỉ tab dashboard giỏ LIVE đang hiển thị mới tự làm mới,
  // để nhiều tab shop.tiktok.com không cùng gọi API và ghi đè giỏ lẫn nhau.
  // Chi tab LIVE (dieu khien/quan sat) va chi khi da co phong LIVE moi dong bo
  // dinh ky. Chua LIVE: khong goi dinh ky; quet tay tu popup van chay.
  const CATALOG_SYNC_EVERY_MS = PRODUCT_CACHE_MAX_AGE_MS;
  function shouldPeriodicSync() {
    if (!(isObserver || isController) || !lastRoomId) return false;
    if (enabled && aiReady) return true;
    const path = String(globalThis.location?.pathname || "");
    const visible = globalThis.document?.visibilityState === "visible";
    return path.startsWith("/streamer/live/product/dashboard") && visible;
  }

  setInterval(() => {
    if (!shouldPeriodicSync()) return;
    const task = Date.now() - lastCatalogSyncAt >= CATALOG_SYNC_EVERY_MS
      // Qua 15 phut: lam moi; neu cache cua chu ky ghim con moi thi dung luon.
      ? liveSnapshot(CATALOG_SYNC_EVERY_MS, lastRoomId).then((response) =>
        completeLiveSnapshot(response.snapshot) ? pushCatalog(response.snapshot) : null)
      : syncCatalogFromCache();
    task.catch((error) => {
      if (enabled) setStatus(`Lỗi làm mới danh mục: ${clean(error?.message || error, 180)}`, false);
    });
  }, 20000);

  // Quet chu dong truoc LIVE, doc lap voi nut Bật AI va dong ho ghim.
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "LIVE_RE_SCAN_CATALOG_PAGE") return undefined;
    syncCatalog(true, true, {
      cartTarget: String(message.cartTarget || "new"),
      cartName: String(message.cartName || ""),
      confirmed: message.confirmed === true
    })
      .then((catalog) => sendResponse(catalog))
      .catch((error) => sendResponse({
        ok: false, error: clean(error?.message || error, 180)
      }));
    return true;
  });

  if (globalThis.__LIVE_RE_TEST_MODE__) {
    globalThis.__LIVE_RE_COMMENTS_DEBUG__ = {
      acceptComment,
      taggedReply,
      applyEnabled,
      getQueue: () => pending.slice(),
      waitForAiPersistence: () => aiPersistChain,
      onRawComment,
      observeAction,
      flushObserve,
      processOne,
      drain,
      getObserveQueue: () => observeQueue.slice(),
      applyPrivacyPurge,
      sendHeartbeat,
      applyAppControl,
      shouldPeriodicSync,
      liveSnapshot,
      syncCatalog,
      syncCatalogFromCache,
      PRODUCT_CACHE_MAX_AGE_MS,
      CATALOG_SYNC_EVERY_MS,
      getInstallRetryMs: () => installRetryMs,
      setRuntimeForTest: (patch = {}) => {
        if (patch.enabled !== undefined) enabled = patch.enabled;
        if (patch.aiReady !== undefined) aiReady = patch.aiReady;
        if (patch.installed !== undefined) installed = patch.installed;
        if (patch.liveRunning !== undefined) liveRunning = patch.liveRunning;
        if (patch.isController !== undefined) isController = patch.isController;
        if (patch.isObserver !== undefined) isObserver = patch.isObserver;
        if (patch.queueApi !== undefined) queueApi = patch.queueApi;
        if (patch.protocolV2 !== undefined) protocolV2 = patch.protocolV2;
        if (patch.lastSendAt !== undefined) lastSendAt = patch.lastSendAt;
        if (patch.roomId !== undefined) lastRoomId = patch.roomId;
        if (patch.heartbeatEverSent !== undefined) heartbeatEverSent = patch.heartbeatEverSent;
        if (patch.sendPausedUntil !== undefined) sendPausedUntil = patch.sendPausedUntil;
        if (patch.sendFailStreak !== undefined) sendFailStreak = patch.sendFailStreak;
      },
      // Cho phép test kiểm chống trùng theo phạm vi phòng.
      getSeenSize: () => seen.size,
      clearSeenForTest: () => seen.clear(),
      markAnsweredForTest: row => markAnswered(row),
      dropQueuedForTest: commentId => removeLocalRow(commentId),
      enqueueAiReply,
      syncBackendQueue,
      learnHostFromSnapshot,
      getState: () => ({
        enabled, busy, installed, aiReady, liveRunning, collectedRows, isObserver,
        hostIdentities: [...hostIdentities], roomHost: currentHost(), sentEchoCount: sentEchoes.size,
        observeQueued: observeQueue.length, observeRetried, observePersistErrors,
        observeDropped, aiDropped, sendPausedUntil, sendFailStreak, heartbeatTabId
      })
    };
  }
})();
