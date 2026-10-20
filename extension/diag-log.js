"use strict";
/* Nhật ký chẩn đoán captcha TikTok (không chặn luồng chính, mọi lỗi chỉ console.warn).
   Dùng ở 2 nơi: service worker (initBackground: gom lô, gửi backend, buffer) và content
   script (initContent: phát hiện captcha bằng MutationObserver, chuyển sự kiện về background).
   KHÔNG ghi cookie/token/header/nội dung chat/ID khách: chỉ danh sách trường cố định,
   chat chỉ có độ dài + hash. */
(function (root) {
  const DIAG_URL = "http://127.0.0.1:8793/api/diag/tiktok-browser";
  const KEY_ENABLED = "liveReDiagEnabled";   // mặc định BẬT (undefined = bật)
  const KEY_BUFFER = "liveReDiagBuffer";
  const MAX_BUFFER = 500, BATCH = 100, FLUSH_MS = 30000, WINDOW_MS = 5 * 60 * 1000;
  const WRITE_METHODS = new Set(["PinLiveProduct", "Chat", "pin", "unpin", "send_chat", "Pin", "Unpin"]);
  const STR_LIMITS = { kind: 16, name: 32, phase: 12, op: 80, blocked: 24, sig: 24, code: 24, hash: 16, reason: 60, url: 200, msg: 120 };
  const NUM_KEYS = ["tab", "calls", "ms", "len", "from", "to", "step", "shown_ms"];
  const BOOL_KEYS = ["user", "ok", "visible", "on", "fail_open"];
  const SECRET = /(ms_?token|token|cookie|authorization|bearer|session_?id|sid_\w+|csrf|secret|password|x-bogus|a_bogus|signature)\s*[=:]\s*\S+/gi;
  const BLOB = /[A-Za-z0-9_\-+=]{32,}/g;
  const warn = (what, error) => { try { console.warn(`Kid TE diag: ${what}`, error?.message || error || ""); } catch {} };

  function text(value, limit) {
    return String(value ?? "").replace(/\s+/g, " ").slice(0, limit * 2).replace(SECRET, "[x]").replace(BLOB, "[x]").slice(0, limit);
  }
  function cleanUrl(value) {
    try { const u = new URL(String(value)); return text(u.origin + u.pathname, STR_LIMITS.url); }
    catch { return text(String(value).split(/[?#]/)[0], STR_LIMITS.url); }
  }
  // FNV-1a 32 bit: đủ để đối chiếu hai lượt gửi cùng nội dung, không lưu nội dung.
  function hashText(value) {
    let h = 0x811c9dc5;
    for (const ch of String(value)) { h ^= ch.codePointAt(0); h = Math.imul(h, 0x01000193) >>> 0; }
    return h.toString(16).padStart(8, "0");
  }
  function sanitize(raw) {
    const out = {};
    for (const [key, limit] of Object.entries(STR_LIMITS)) {
      if (raw[key] === undefined || raw[key] === null || typeof raw[key] === "object" || typeof raw[key] === "boolean") continue;
      out[key] = key === "url" ? cleanUrl(raw[key]) : text(raw[key], limit);
    }
    for (const key of NUM_KEYS) if (Number.isFinite(raw[key])) out[key] = Math.round(raw[key]);
    for (const key of BOOL_KEYS) if (typeof raw[key] === "boolean") out[key] = raw[key];
    out.t = Number.isFinite(raw.t) ? Math.round(raw.t) : Date.now();
    if (raw.summary && typeof raw.summary === "object") out.summary = raw.summary;
    return out;
  }

  function createDiag(deps = {}) {
    const getChrome = () => deps.chrome || root.chrome;
    const getFetch = () => deps.fetch || root.fetch;
    const now = () => (deps.now ? deps.now() : Date.now());
    let mode = "";
    let enabled = true;
    let pending = [], recent = [], timer = null, flushing = null, rerun = false, lastWarn = 0;

    const storageGet = async (key) => (await getChrome().storage.local.get(key)) || {};
    const storageSet = (value) => getChrome().storage.local.set(value);

    function summarize(at) {
      recent = recent.filter((item) => at - item.t <= WINDOW_MS);
      const byMethod = {}, tabs = new Set();
      let writes = 0, calls = 0;
      for (const item of recent) {
        byMethod[item.m] = (byMethod[item.m] || 0) + 1;
        calls += 1;
        if (item.write) writes += 1;
        if (item.tab) tabs.add(item.tab);
      }
      return { window_s: WINDOW_MS / 1000, calls, writes, tabs: tabs.size, by_method: byMethod };
    }

    function schedule() {
      if (timer || mode !== "background") return;
      timer = setTimeout(() => { timer = null; flush().catch((error) => warn("flush", error)); }, FLUSH_MS);
      timer?.unref?.();
    }

    function push(entry) {
      const isCaptchaShow = entry.kind === "captcha" && entry.phase === "show";
      if (isCaptchaShow) entry.summary = summarize(entry.t);
      pending.push(entry);
      if (pending.length > MAX_BUFFER) pending = pending.slice(-MAX_BUFFER);
      if (isCaptchaShow) flush().catch((error) => warn("flush", error));
      else schedule();
    }

    /** Ghi một dòng; đồng bộ, rẻ, không bao giờ ném lỗi. */
    function record(raw) {
      try {
        if (!enabled || !raw) return;
        const entry = sanitize(raw);
        if (!Number.isFinite(raw.t)) entry.t = now();
        if (mode === "content") {
          const send = getChrome()?.runtime?.sendMessage?.({ type: "KIDTE_DIAG", entry });
          send?.catch?.(() => {});
        } else if (mode === "background") {
          push(entry);
        }
      } catch (error) { warn("record", error); }
    }

    function event(name, fields = {}) { record({ kind: "event", name, ...fields }); }

    /** Một lượt gọi TikTok (hoặc bị chặn). Không await: tab.active tra cứu bất đồng bộ. */
    function api({ tabId, request, result, startedAt, blocked }) {
      try {
        if (!enabled) return;
        const t = now();
        const action = String(request?.action || "");
        const methods = Array.isArray(result?.api_methods) && result.api_methods.length
          ? result.api_methods.map(String) : [action || "unknown"];
        const user = request?.userInitiated === true;
        const entry = { kind: "api", t, tab: tabId, op: methods.join("+"), user, blocked: blocked || "" };
        if (!blocked) {
          entry.calls = Math.max(1, Number(result?.api_calls) || 1);
          entry.ms = startedAt ? Math.max(0, t - startedAt) : 0;
          entry.ok = result?.ok === true;
          const code = result?.response_code ?? result?.error_code;
          if (code !== undefined && code !== null && code !== "") entry.code = code;
          if (!entry.ok) entry.msg = result?.error || "";
          for (const m of methods) recent.push({ t, m, tab: tabId, write: WRITE_METHODS.has(m) || WRITE_METHODS.has(action) });
          if (recent.length > 3000) recent = recent.slice(-3000);
        }
        if (action === "send_chat" && typeof request?.content === "string") {
          entry.len = request.content.length;
          entry.hash = hashText(request.content);
        }
        const tabInfo = Number.isInteger(tabId) ? getChrome()?.tabs?.get?.(tabId) : null;
        Promise.resolve(tabInfo).then((tab) => tab && tab.active === true && tab.discarded !== true, () => null)
          .then((visible) => { if (typeof visible === "boolean") entry.visible = visible; record(entry); })
          .catch((error) => warn("api", error));
      } catch (error) { warn("api", error); }
    }

    function pulseStart() {
      const started = now();
      event("pulse", { phase: "start" });
      return () => event("pulse", { phase: "end", ms: now() - started });
    }

    async function post(batch) {
      const fetchFn = getFetch();
      if (typeof fetchFn !== "function") return false;
      const response = await fetchFn(DIAG_URL, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ v: 1, entries: batch }),
        signal: root.AbortSignal?.timeout?.(5000)
      });
      if (!response?.ok) return false;
      const body = await response.json().catch(() => ({}));
      return body?.ok === true;
    }

    /** Gửi buffer (storage) + dòng mới; thất bại thì giữ lại tối đa 500 dòng để gửi bù. */
    function flush() {
      if (flushing) { rerun = true; return flushing; }
      flushing = (async () => {
        do { rerun = false; await flushOnce(); } while (rerun);
      })().finally(() => { flushing = null; if (pending.length) schedule(); });
      return flushing;
    }
    async function flushOnce() {
      try {
        const taken = pending.length;
        let buffer = ((await storageGet(KEY_BUFFER))[KEY_BUFFER] || []).concat(pending.slice(0, taken));
        if (!Array.isArray(buffer) || !buffer.length) { pending = pending.slice(taken); return; }
        buffer = buffer.slice(-MAX_BUFFER);
        let failure = null;
        while (buffer.length) {
          const batch = buffer.slice(0, BATCH);
          let sent = false;
          try { sent = await post(batch); } catch (error) { failure = error; }
          if (!sent) { failure = failure || new Error("backend không nhận"); break; }
          buffer = buffer.slice(batch.length);
        }
        pending = pending.slice(taken);
        await storageSet({ [KEY_BUFFER]: buffer });
        if (failure && now() - lastWarn > 60000) { lastWarn = now(); warn("gửi nhật ký lỗi, giữ buffer", failure); }
      } catch (error) { warn("flush", error); }
    }

    function watchEnabled() {
      getChrome().storage.local.get(KEY_ENABLED).then((stored) => { enabled = stored?.[KEY_ENABLED] !== false; }, () => {});
      getChrome().storage.onChanged?.addListener?.((changes, area) => {
        if (area === "local" && changes[KEY_ENABLED]) enabled = changes[KEY_ENABLED].newValue !== false;
      });
    }

    function initBackground() {
      mode = "background";
      try {
        const c = getChrome();
        watchEnabled();
        c.alarms?.create?.("kidte-diag-flush", { periodInMinutes: 0.5 });
        c.alarms?.onAlarm?.addListener?.((alarm) => {
          if (alarm?.name === "kidte-diag-flush") flush().catch((error) => warn("flush", error));
        });
        c.runtime?.onMessage?.addListener?.((message, sender) => {
          if (message?.type !== "KIDTE_DIAG" || !message.entry) return undefined;
          if (!String(sender?.tab?.url || "").startsWith("https://shop.tiktok.com/")) return undefined;
          record({ ...message.entry, tab: sender.tab.id });
          return undefined;
        });
        c.storage?.onChanged?.addListener?.((changes, area) => {
          if (area !== "local") return;
          if (changes.liveReAiEnabled) event("ai_toggle", { on: changes.liveReAiEnabled.newValue === true });
          if (changes.liveReState) {
            const before = changes.liveReState.oldValue || {}, after = changes.liveReState.newValue || {};
            if (Boolean(before.running) !== Boolean(after.running)) event("autopin_toggle", { on: Boolean(after.running) });
            if (Number(before.controllerTabId || 0) !== Number(after.controllerTabId || 0)) {
              event("controller_change", { from: Number(before.controllerTabId || 0), to: Number(after.controllerTabId || 0) });
            }
          }
        });
        // Dòng còn trong buffer từ lần trước (service worker ngủ/backend tắt): gửi bù.
        flush().catch((error) => warn("flush", error));
      } catch (error) { warn("initBackground", error); }
    }

    // ----- content script: phát hiện captcha -----
    const TOKENS = ["captcha", "secsdk", "verify"];
    const SELECTOR = TOKENS.flatMap((t) => [`[id*="${t}" i]`, `[class*="${t}" i]`]).concat(
      ['iframe[src*="captcha" i]', 'iframe[src*="verify" i]']).join(",");
    function visibleBox(el, doc) {
      try {
        const rect = el.getBoundingClientRect?.();
        if (!rect || rect.width < 100 || rect.height < 100) return false;
        const style = doc.defaultView?.getComputedStyle?.(el);
        return !(style && (style.display === "none" || style.visibility === "hidden"));
      } catch { return false; }
    }
    function signature(el) {
      const hay = `${el.id || ""} ${typeof el.className === "string" ? el.className : ""} ${el.src || ""}`.toLowerCase();
      if (String(el.tagName).toUpperCase() === "IFRAME") return "iframe";
      return TOKENS.find((t) => hay.includes(t)) || "";
    }
    function initContent(env = {}) {
      mode = "content";
      try {
        const doc = env.document || root.document;
        const Observer = env.MutationObserver || root.MutationObserver;
        const loc = env.location || root.location;
        watchEnabled();
        let shownAt = 0, scheduled = null, recheck = null;
        const check = () => {
          scheduled = null;
          if (!enabled) return;
          let sig = "";
          try {
            for (const el of doc.querySelectorAll(SELECTOR)) {
              if (visibleBox(el, doc)) { sig = signature(el); break; }
            }
          } catch (error) { warn("detect", error); }
          if (sig && !shownAt) {
            shownAt = now();
            record({ kind: "captcha", phase: "show", sig, url: loc?.href });
            // Ẩn bằng CSS (không gỡ node) thì observer childList không thấy: kiểm thưa 3s khi đang hiện.
            recheck = setInterval(check, 3000); recheck?.unref?.();
          } else if (!sig && shownAt) {
            record({ kind: "captcha", phase: "hide", shown_ms: now() - shownAt, url: loc?.href });
            shownAt = 0; clearInterval(recheck); recheck = null;
          }
        };
        const trigger = () => { if (!scheduled) { scheduled = setTimeout(check, 400); scheduled?.unref?.(); } };
        const observer = new Observer(trigger);
        observer.observe(doc.documentElement || doc, { childList: true, subtree: true });
        trigger();
        return { observer, check };
      } catch (error) { warn("initContent", error); return null; }
    }

    return { record, event, api, pulseStart, flush, sanitize, summarize, initBackground, initContent,
      hashText, get pending() { return pending; }, get enabled() { return enabled; }, createDiag };
  }

  const diag = createDiag();
  root.KidTEDiag = diag;
  if (typeof module !== "undefined" && module.exports) module.exports = diag;
  // Content script trên TikTok Shop: tự khởi động (không cần móc vào content.js).
  try {
    if (typeof document !== "undefined" && root.location?.hostname === "shop.tiktok.com" && root.chrome?.runtime?.id) {
      diag.initContent();
    }
  } catch (error) { warn("autostart", error); }
})(typeof globalThis !== "undefined" ? globalThis : this);
