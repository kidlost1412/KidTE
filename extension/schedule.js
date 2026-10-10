(function loadKidTESchedule(root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.KidTESchedule = api;
  root.LiveReSchedule = api; // Keep tabs opened before the extension update working.
})(typeof globalThis !== "undefined" ? globalThis : this, () => {
  "use strict";

  // Lịch ghim KHÔNG còn nằm trong mã nguồn. Nó là dữ liệu của từng video, ở
  // backend/data/videos/<video_id>/timeline.json (qua /api/video/timelines), nạp lúc chạy.
  // Ở đây chỉ còn cỗ máy: đọc timeline, suy ra sản phẩm tại một thời điểm.
  // Bản DEFAULT_TIMELINE dưới đây là bản dự phòng để tiện ích vẫn chạy khi
  // backend chưa bật; nó phải luôn khớp với timeline.json của live1.
  const DEFAULT_TIMELINE = Object.freeze({
    schema_version: 1,
    video_id: "live1",
    title: "Live 1 — video phát lại 19 phút",
    cycle_ms: 1140770,
    repin_every_ms: 20000,
    products: {
      tc01: { bag_index: 1, short_name: "TC01",
        title: "Kích Rễ Cực Mạnh TC01", include_all: ["tc01"], exclude_any: [] },
      fish1: { bag_index: 2, short_name: "Đạm cá 1L",
        title: "Đạm Cá Amino 1L Chất Hữu Cơ 20%",
        include_all: ["dam ca", "1l"], exclude_any: ["5 lit", "5l", "can 5"] },
      topone: { bag_index: 3, short_name: "Top Bo One",
        title: "BO TRI ONE | Chế Phẩm Top Bo One Tri",
        include_all: ["bo tri one"], exclude_any: [] },
      fish5: { bag_index: 4, short_name: "Đạm cá 5L",
        title: "Đạm Cá Hữu Cơ Amino Giúp Cây Bật Mầm (Can 5 Lít)",
        include_all: ["dam ca", "5 lit"], exclude_any: [] },
      soil: { bag_index: 5, short_name: "Sạch Sâu Đất",
        title: "SẠCH SÂU ĐẤT 1KG Công Nghệ",
        include_all: ["sach sau dat", "1kg"], exclude_any: [] }
    },
    events: [
      { at_ms: 0, product: "tc01" },
      { at_ms: 167000, product: "topone" },
      { at_ms: 302000, product: null },
      { at_ms: 390000, product: "tc01" },
      { at_ms: 510000, product: "fish1", alternate_with: "fish5", alternate_every_ms: 60000 },
      { at_ms: 727000, product: "topone" },
      { at_ms: 940000, product: "fish1", alternate_with: "fish5", alternate_every_ms: 60000 },
      { at_ms: 1000000, product: "topone" },
      { at_ms: 1040000, product: "soil" },
      { at_ms: 1080000, product: "topone" }
    ],
    script_contexts: [
      { at_ms: 0, topic: "TC01 và bộ rễ yếu",
        facts: ["Ở đoạn TC01 này, người dẫn hướng dẫn phun qua lá khi bộ rễ đang yếu."] },
      { at_ms: 167000, topic: "Top One xử lý bọ trĩ, rầy rệp và côn trùng chích hút",
        facts: ["Người dẫn đang nói Top One thấm qua lá và lưu dẫn để xử lý bọ trĩ ở vị trí khuất."] },
      { at_ms: 302000, topic: "Đoạn chuyển sản phẩm", facts: [] },
      { at_ms: 390000, topic: "TC01 và chăm bộ rễ",
        facts: ["Ở đoạn TC01 này, người dẫn ưu tiên đường phun qua lá khi bộ rễ yếu."] },
      { at_ms: 510000, topic: "Đạm cá hữu cơ Amino 1L và 5L", facts: [] },
      { at_ms: 727000, topic: "Top One và côn trùng chích hút", facts: [] },
      { at_ms: 940000, topic: "Đạm cá hữu cơ Amino", facts: [] },
      { at_ms: 1000000, topic: "Top One và côn trùng chích hút", facts: [] },
      { at_ms: 1040000, topic: "Sạch Sâu Đất 1KG đang được giới thiệu nhưng chưa có hồ sơ kỹ thuật duyệt", facts: [] },
      { at_ms: 1080000, topic: "Top One và cơ chế tác động lên côn trùng chích hút", facts: [] }
    ]
  });

  const text = (value) => String(value === null || value === undefined ? "" : value);
  const positiveInt = (value) => {
    const number = Number(value);
    return Number.isFinite(number) && number >= 0 ? Math.round(number) : null;
  };

  /**
   * Kiểm timeline trước khi dùng. Một timeline sai không được phép làm hỏng
   * phiên live: hàm này trả về danh sách lỗi, người gọi sẽ giữ bản đang dùng.
   */
  function validate(timeline) {
    const errors = [];
    if (!timeline || typeof timeline !== "object") return ["timeline không phải object"];
    if (!text(timeline.video_id).trim()) errors.push("thiếu video_id");
    const cycle = positiveInt(timeline.cycle_ms);
    if (!cycle || cycle < 1000) errors.push("cycle_ms phải là số ms > 1000");
    const products = timeline.products;
    if (!products || typeof products !== "object" || !Object.keys(products).length) {
      errors.push("thiếu products");
    } else {
      for (const [key, item] of Object.entries(products)) {
        if (positiveInt(item?.bag_index) === null) errors.push(`products.${key}: bag_index sai`);
        if (!Array.isArray(item?.include_all) || !item.include_all.length) {
          errors.push(`products.${key}: include_all phải là mảng không rỗng`);
        }
        if (item?.exclude_any !== undefined && !Array.isArray(item.exclude_any)) {
          errors.push(`products.${key}: exclude_any phải là mảng`);
        }
      }
    }
    if (!Array.isArray(timeline.events) || !timeline.events.length) {
      errors.push("thiếu events");
    } else {
      let previous = -1;
      timeline.events.forEach((event, index) => {
        const at = positiveInt(event?.at_ms);
        if (at === null) errors.push(`events[${index}]: at_ms sai`);
        else if (at <= previous) errors.push(`events[${index}]: at_ms phải tăng dần`);
        else if (cycle && at >= cycle) errors.push(`events[${index}]: at_ms vượt cycle_ms`);
        else previous = at;
        const key = event?.product;
        if (key !== null && key !== undefined && !products?.[key]) {
          errors.push(`events[${index}]: product "${key}" không có trong products`);
        }
        const other = event?.alternate_with;
        if (other !== undefined && other !== null) {
          if (!products?.[other]) errors.push(`events[${index}]: alternate_with "${other}" không có`);
          if (!positiveInt(event?.alternate_every_ms)) {
            errors.push(`events[${index}]: alternate_with cần alternate_every_ms`);
          }
        }
      });
      if (timeline.events[0] && positiveInt(timeline.events[0].at_ms) !== 0) {
        errors.push("events[0].at_ms phải là 0 để luôn có sản phẩm ngay đầu vòng");
      }
    }
    if (timeline.script_contexts !== undefined && !Array.isArray(timeline.script_contexts)) {
      errors.push("script_contexts phải là mảng");
    }
    return errors;
  }

  function normalize(timeline) {
    const products = {};
    for (const [key, item] of Object.entries(timeline.products || {})) {
      products[key] = Object.freeze({
        key,
        bagIndex: positiveInt(item.bag_index),
        productId: text(item.product_id),
        shortName: text(item.short_name) || key,
        title: text(item.title) || text(item.short_name) || key,
        includeAll: Object.freeze((item.include_all || []).map(text)),
        excludeAny: Object.freeze((item.exclude_any || []).map(text))
      });
    }
    const events = (timeline.events || [])
      .map((event) => Object.freeze({
        atMs: positiveInt(event.at_ms),
        productKey: event.product === undefined ? null : event.product,
        alternateWith: event.alternate_with || null,
        alternateEveryMs: positiveInt(event.alternate_every_ms) || 0
      }))
      .sort((left, right) => left.atMs - right.atMs);
    const contexts = (timeline.script_contexts || [])
      .map((item) => Object.freeze({
        atMs: positiveInt(item.at_ms) || 0,
        topic: text(item.topic),
        facts: Object.freeze((item.facts || []).map(text))
      }))
      .sort((left, right) => left.atMs - right.atMs);
    return Object.freeze({
      videoId: text(timeline.video_id),
      title: text(timeline.title) || text(timeline.video_id),
      cycleMs: positiveInt(timeline.cycle_ms),
      repinEveryMs: positiveInt(timeline.repin_every_ms) || 20000,
      products: Object.freeze(products),
      events: Object.freeze(events),
      scriptContexts: Object.freeze(contexts)
    });
  }

  let active = normalize(DEFAULT_TIMELINE);

  function load(timeline) {
    const errors = validate(timeline);
    if (errors.length) return { ok: false, errors };
    const previous = active.videoId;
    active = normalize(timeline);
    return { ok: true, videoId: active.videoId, replaced: previous };
  }

  function positiveModulo(value, modulus) {
    return ((value % modulus) + modulus) % modulus;
  }

  function eventAt(positionMs) {
    let current = active.events[0];
    for (const event of active.events) {
      if (event.atMs > positionMs) break;
      current = event;
    }
    return current;
  }

  function scriptContextAt(positionMs) {
    if (!active.scriptContexts.length) return { atMs: 0, topic: "", facts: [] };
    let current = active.scriptContexts[0];
    for (const item of active.scriptContexts) {
      if (item.atMs > positionMs) break;
      current = item;
    }
    return current;
  }

  function resolve(elapsedMs) {
    const safeElapsed = Math.max(0, Number(elapsedMs) || 0);
    const cycle = active.cycleMs;
    const loopIndex = Math.floor(safeElapsed / cycle);
    const positionMs = positiveModulo(safeElapsed, cycle);
    const event = eventAt(positionMs);
    let productKey = event.productKey;
    let phase = 0;

    if (event.alternateWith && event.alternateEveryMs && productKey) {
      phase = Math.floor((positionMs - event.atMs) / event.alternateEveryMs);
      productKey = phase % 2 === 0 ? productKey : event.alternateWith;
    }

    return {
      elapsedMs: safeElapsed,
      loopIndex,
      positionMs,
      eventAtMs: event.atMs,
      fishPhase: phase,
      productKey,
      product: productKey ? active.products[productKey] || null : null,
      transitionToken: `${loopIndex}:${event.atMs}:${productKey || "none"}:${phase}`,
      pulseToken: `${loopIndex}:${Math.floor(positionMs / active.repinEveryMs)}:${productKey || "none"}`
    };
  }

  function nextBoundary(elapsedMs) {
    const current = resolve(elapsedMs);
    const cycle = active.cycleMs;
    const candidates = active.events
      .filter((event) => event.atMs > current.positionMs)
      .map((event) => event.atMs);

    const event = eventAt(current.positionMs);
    if (event.alternateWith && event.alternateEveryMs) {
      const nextSwap = event.atMs + (current.fishPhase + 1) * event.alternateEveryMs;
      const nextBase = candidates.length ? Math.min(...candidates) : cycle;
      if (nextSwap < nextBase) candidates.push(nextSwap);
    }

    const boundary = candidates.length ? Math.min(...candidates) : cycle;
    const deltaMs = boundary - current.positionMs;
    const next = resolve(elapsedMs + deltaMs + 1);
    return {
      inMs: deltaMs,
      atPositionMs: boundary === cycle ? 0 : boundary,
      productKey: next.productKey,
      product: next.product
    };
  }

  function formatClock(ms) {
    const totalSeconds = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }

  function parseClock(value) {
    const raw = String(value || "").trim();
    if (!raw) return null;
    const parts = raw.split(":");
    if (parts.length < 1 || parts.length > 3 ||
        parts.some((part) => !/^\d+$/.test(part))) return null;

    const numbers = parts.map(Number);
    let totalSeconds = 0;
    if (numbers.length === 1) {
      totalSeconds = numbers[0];
    } else if (numbers.length === 2) {
      const [minutes, seconds] = numbers;
      if (seconds >= 60) return null;
      totalSeconds = minutes * 60 + seconds;
    } else {
      const [hours, minutes, seconds] = numbers;
      if (minutes >= 60 || seconds >= 60) return null;
      totalSeconds = hours * 3600 + minutes * 60 + seconds;
    }
    return totalSeconds * 1000;
  }

  // Giữ nguyên mặt API cũ: content.js, popup.js và test vẫn đọc
  // SCHEDULE.CYCLE_MS, SCHEDULE.PRODUCTS... nhưng giờ chúng lấy từ timeline
  // đang hoạt động thay vì từ hằng số cứng.
  const api = {
    load,
    validate,
    DEFAULT_TIMELINE,
    resolve,
    nextBoundary,
    formatClock,
    parseClock,
    scriptContextAt
  };
  Object.defineProperties(api, {
    CYCLE_MS: { get: () => active.cycleMs, enumerable: true },
    REPIN_EVERY_MS: { get: () => active.repinEveryMs, enumerable: true },
    PRODUCTS: { get: () => active.products, enumerable: true },
    EVENTS: { get: () => active.events, enumerable: true },
    VIDEO_ID: { get: () => active.videoId, enumerable: true },
    VIDEO_TITLE: { get: () => active.title, enumerable: true }
  });
  return Object.freeze(api);
});
