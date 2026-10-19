"""Bộ xử lý route HTTP (extension) — tách NGUYÊN VĂN từ Handler.do_GET/do_POST trong server.py.

Mỗi hàm nhận ``self`` là Handler hiện tại (send_json, local_tool_request, engine...).
Tên toàn cục (ROOT, WEB_DIR, clean, report_problem, ...) được tra trong module server:
http_routes.bind() gắn lại __globals__ bằng engine_mixins.rebind — không import server ở đây.
"""
from __future__ import annotations

# ruff: noqa: F821  (tên toàn cục lấy từ server khi bind)


def get_plugins(self, route, parsed):
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    self.send_json(200, self.engine.plugin_state())


def post_extension_pairing_reset(self, route, payload, flags):
    result = self.engine.reset_extension_pairing(payload.get("platform"))
    return result


def post_extension_setup(self, route, payload, flags):
    result = self.engine.open_extension_setup(payload.get("platform", "tiktok"))
    return result


def post_plugins_action(self, route, payload, flags):
    result = self.engine.plugin_action(payload)
    return result
