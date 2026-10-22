"""Bộ xử lý route HTTP (live) — tách NGUYÊN VĂN từ Handler.do_GET/do_POST trong server.py.

Mỗi hàm nhận ``self`` là Handler hiện tại (send_json, local_tool_request, engine...).
Tên toàn cục (ROOT, WEB_DIR, clean, report_problem, ...) được tra trong module server:
http_routes.bind() gắn lại __globals__ bằng engine_mixins.rebind — không import server ở đây.
"""
from __future__ import annotations

# ruff: noqa: F821  (tên toàn cục lấy từ server khi bind)


def get_dashboard(self, route, parsed):
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    self.send_json(200, self.engine.dashboard_state())


def get_live_readiness(self, route, parsed):
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    self.send_json(200, self.engine.dashboard_state().get("readiness") or {"ok": False})


def get_live_sources(self, route, parsed):
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    self.send_json(200, self.engine.live_sources_state())


def get_shopee_ai_status(self, route, parsed):
    if not self.local_tool_request() and not self.headers.get("Origin", "").startswith("chrome-extension://"):
        self.send_json(403, {"ok": False, "error": "extension_origin_required"})
        return
    if not self.extension_paired("shopee"):
        self.send_json(403, {"ok": False, "error": "extension_not_paired"})
        return
    checker = getattr(self.engine.responder, "check_balance", None)
    status = checker() if callable(checker) else {"ok": False, "error": "deepseek_unavailable"}
    status = {**status, 'capabilities': ReplyEngine.delivery_capabilities()}
    self.send_json(200, status)


def post_route_in_delivery_routes(self, route, payload, flags):
    delivery_routes = flags['delivery_routes']
    result = self.engine.delivery_operation(*delivery_routes[route], payload)
    return result


def post_route_in_queue_routes(self, route, payload, flags):
    queue_routes = flags['queue_routes']
    result = self.engine.queue_operation(*queue_routes[route], payload)
    return result


def post_heartbeat(self, route, payload, flags):
    result = self.engine.heartbeat(payload)
    return result


def post_tiktok_lease(self, route, payload, flags):
    # Lease "người gọi TikTok" giữa các trình duyệt/profile (chỉ trong bộ nhớ).
    import tiktok_lease
    return tiktok_lease.LEASE.handle(payload)


def post_tiktok_browser_diag(self, route, payload, flags):
    # Nhật ký chẩn đoán captcha trình duyệt: ghi <DATA>/logs/tiktok-browser.jsonl (xoay vòng).
    import browser_diag_log
    return browser_diag_log.append(LOG_DIR, payload)


def post_shopee_sync(self, route, payload, flags):
    result = self.engine.sync_shopee_catalog(payload)
    return result


def post_live_sources(self, route, payload, flags):
    result = self.engine.save_live_sources(payload)
    return result


def post_live_control(self, route, payload, flags):
    result = self.engine.set_live_control(payload)
    return result


def post_live_queue_mode(self, route, payload, flags):
    result = self.engine.set_queue_mode(payload)
    return result


def post_live_reidentify(self, route, payload, flags):
    result = self.engine.live_sources.request_reidentify(payload.get('platform'))
    self.engine.set_video_position({'platform': payload.get('platform'), 'available': False})
    return result


def post_shopee_reply(self, route, payload, flags):
    result = (self.engine.queued_reply('shopee', payload) if payload.get('protocol_version') == 2
              else self.engine.shopee_ai().reply(payload))
    return result


def post_shopee_delivery(self, route, payload, flags):
    result = self.engine.shopee_ai().delivery(payload)
    return result


def post_observe(self, route, payload, flags):
    result = self.engine.observe(payload)
    return result


def post_catalog_carts(self, route, payload, flags):
    """Tiện ích (đã ghép đôi) đọc danh sách giỏ cho popup quét tay.

    Ghi duy nhất: use_live=true (chủ shop bấm "Dùng giỏ của tài khoản LIVE") = bỏ chọn giỏ,
    AI quay về giỏ của tài khoản đang LIVE.
    """
    if payload.get("use_live") is True:
        self.engine.catalog.select("")
        self.engine.workspace._touch("products", "catalog", "dashboard")
    state = self.engine.catalog.carts_state()
    return {"ok": True, "active": state.get("active", ""), "active_name": state.get("active_name", ""),
            "live": state.get("live", ""), "live_name": state.get("live_name", ""),
            "live_mismatch": bool(state.get("live_mismatch")),
            "active_source": state.get("active_source", ""),
            "carts": [{key: row.get(key) for key in ("key", "name", "product_count", "live", "active", "synced_at")}
                      for row in state.get("carts") or []]}


def post_route_endswith_catalog_sync(self, route, payload, flags):
    context = payload.get("live_context", payload)
    if payload.get("user_initiated") is True:
        # Tiện ích cũ không gửi cart_target -> giỏ mới (không bao giờ ghi đè ngầm).
        return self.engine.sync_catalog(context, user_initiated=True,
                                        cart_target=clean(payload.get("cart_target"), 200) or "new",
                                        cart_name=clean(payload.get("cart_name"), 80),
                                        confirmed=payload.get("confirmed") is True)
    result = self.engine.sync_catalog(context)
    return result


def post_route_endswith_reply_ack(self, route, payload, flags):
    result = self.engine.acknowledge_reply(payload)
    return result


def post_route_endswith_reply_delivery(self, route, payload, flags):
    result = self.engine.update_reply_delivery(payload)
    return result


def post_reply(self, route, payload, flags):
    result = (self.engine.queued_reply('tiktok', payload) if payload.get('protocol_version') == 2
              else self.engine.handle(payload))
    return result
