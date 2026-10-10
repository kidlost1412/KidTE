(function (root, factory) {
  const api = factory();
  root.KidTEShopeeCore = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, () => {
  "use strict";
  function desired(config, position, timelines) {
    if (!config?.enabled) return { ok: false, error: "Chưa bật ghim Shopee" };
    if (!position?.ok || !position.available) return { ok: false, error: "Chưa có vị trí video còn hiệu lực" };
    const margin = Number(position.margin);
    if (!Number.isFinite(margin) || margin < Math.max(Number(position.min_margin) || 0.06, 0.06)) {
      return { ok: false, error: "Tín hiệu nhận diện video yếu" };
    }
    if (!Number.isFinite(Number(position.age_s)) || Number(position.age_s) < 0 || Number(position.age_s) > 25 ||
        !Number.isFinite(Number(position.position_s)) || Number(position.position_s) < 0) {
      return { ok: false, error: "Vị trí video đã cũ" };
    }
    const videoId = String(position.video_id || "");
    if (!videoId || videoId !== config.videoId) return { ok: false, error: "Video đang phát khác video đã ánh xạ" };
    const timeline = timelines?.[videoId];
    if (!timeline || !Number.isFinite(Number(timeline.cycle_ms)) || Number(timeline.cycle_ms) < 1000) {
      return { ok: false, error: "Kid TE chưa có timeline hợp lệ cho video này" };
    }
    const events = timeline.events;
    if (!Array.isArray(events) || !events.length) return { ok: false, error: "Timeline không có mốc ghim" };
    const cycle = Number(timeline.cycle_ms);
    const positionMs = ((Math.floor(Number(position.position_s) * 1000) % cycle) + cycle) % cycle;
    let event = null;
    for (const row of events) {
      if (Number(row.at_ms) <= positionMs) event = row;
      else break;
    }
    if (!event) return { ok: false, error: "Timeline không bắt đầu ở mốc 00:00" };
    let productKey = event.product ?? null;
    let phase = 0;
    if (productKey && event.alternate_with && Number(event.alternate_every_ms) > 0) {
      phase = Math.floor((positionMs - Number(event.at_ms)) / Number(event.alternate_every_ms));
      if (phase % 2) productKey = event.alternate_with;
    }
    const loop = Math.floor(Number(position.position_s) * 1000 / cycle);
    if (!productKey) return { ok: true, action: "hide", token: `${videoId}:${loop}:hide`, label: "Không hiển thị sản phẩm" };
    if (config.unresolved?.includes(productKey) ||
        (phase % 2 && config.unresolved?.includes(`${event.product}:luan_phien_1l`))) {
      return {ok: false, error: `Giỏ Shopee thiếu hoặc trùng sản phẩm cho ${productKey}; chưa ghim`};
    }
    let mapping = config.mappings?.[productKey];
    if (phase % 2 && event.product && event.alternate_with) {
      const base = config.mappings?.[event.product];
      if (base && mapping && String(base.shop_id) === String(mapping.shop_id) &&
          String(base.product_id) === String(mapping.product_id)) {
        mapping = config.alternates?.[event.product] || mapping;
      }
    }
    if (!/^\d+$/.test(String(mapping?.product_id || "")) || !/^\d+$/.test(String(mapping?.shop_id || ""))) {
      return { ok: false, error: `Chưa ánh xạ Shopee cho ${productKey}` };
    }
    const token = `${videoId}:${loop}:show:${mapping.shop_id}:${mapping.product_id}`;
    return { ok: true, action: "show", token, product_id: String(mapping.product_id),
      shop_id: String(mapping.shop_id),
      label: `${timeline.products?.[productKey]?.short_name || productKey} → Shopee ${mapping.product_id}` };
  }
  function liveSession(url, allowPreview = false) {
    try {
      const parsed = new URL(url);
      const session = parsed.searchParams.get("session") || "";
      return parsed.protocol === "https:" && parsed.hostname === "live.shopee.vn" &&
        (parsed.pathname.replace(/\/+$/, "") === "/pc/live" ||
          (allowPreview && parsed.pathname.replace(/\/+$/, "") === "/pc/preview")) && /^\d{1,20}$/.test(session)
        ? session : "";
    } catch { return ""; }
  }
  async function findLiveTab(tabs, preferredId, allowPreview = false) {
    const active = (await tabs.query({ active: true, currentWindow: true })).filter(t => liveSession(t.url, allowPreview));
    if (active.length) return active[0];
    const candidates = (await tabs.query({ url: "https://live.shopee.vn/*" })).filter(t => liveSession(t.url, allowPreview));
    const preferred = candidates.find(t => t.id === preferredId);
    if (preferred) return preferred;
    if (candidates.length === 1) return candidates[0];
    throw new Error(candidates.length ? "Có nhiều phiên Shopee; hãy chọn tab phiên cần dùng rồi quét lại."
      : "Không tìm thấy tab quản lý Shopee LIVE có session trong Chrome này.");
  }
  // F02: so khớp danh tính backend yêu cầu xoá (đã gấp dấu/chữ hoa) với bản ghi cục bộ.
  function purgeFold(value) {
    return String(value ?? "").toLowerCase().replace(/\u0111/g, "d").normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "").replace(/\s+/g, " ").trim();
  }
  function purgeMentions(row, identifiers) {
    const idents = (Array.isArray(identifiers) ? identifiers : []).map(purgeFold).filter(Boolean);
    if (!idents.length) return false;
    let blob;
    try { blob = purgeFold(JSON.stringify(row)); } catch (_) { return false; }
    return idents.some(ident => {
      let from = 0;
      while ((from = blob.indexOf(ident, from)) >= 0) {
        const before = blob[from - 1], after = blob[from + ident.length];
        if (!(before && /[0-9a-z]/.test(before)) && !(after && /[0-9a-z]/.test(after))) return true;
        from += 1;
      }
      return false;
    });
  }
  return { desired, liveSession, findLiveTab, purgeFold, purgeMentions };
});
