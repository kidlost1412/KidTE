"""Bộ xử lý route HTTP (misc) — tách NGUYÊN VĂN từ Handler.do_GET/do_POST trong server.py.

Mỗi hàm nhận ``self`` là Handler hiện tại (send_json, local_tool_request, engine...).
Tên toàn cục (ROOT, WEB_DIR, clean, report_problem, ...) được tra trong module server:
http_routes.bind() gắn lại __globals__ bằng engine_mixins.rebind — không import server ở đây.
"""
from __future__ import annotations

# ruff: noqa: F821  (tên toàn cục lấy từ server khi bind)


def get_health(self, route, parsed):
    if not self.loopback_read_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    self.send_json(200, self.engine.health())


def get_root(self, route, parsed):
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    page = WEB_DIR / "operator.html"
    try:
        self.send_html(200, versioned_operator_page(page.read_text(encoding="utf-8"), _app_version()))
    except OSError:
        self.send_json(500, {"ok": False, "error": "app_page_missing"})


def get_assets_prefix(self, route, parsed):
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    relative = route[len("/assets/"):].lstrip("/")
    try:
        target = (WEB_DIR / relative).resolve()
        if not target.is_relative_to(WEB_DIR.resolve()) or not target.is_file():
            self.send_json(404, {"ok": False, "error": "not_found"})
            return
        mime = (
            "application/javascript" if target.suffix == ".js"
            else "text/css" if target.suffix == ".css"
            else "text/html" if target.suffix == ".html"
            else "image/png" if target.suffix == ".png"
            else "image/svg+xml" if target.suffix == ".svg"
            else "application/octet-stream"
        )
        self.send_local_file(target, mime)
    except (ValueError, OSError):
        self.send_json(404, {"ok": False, "error": "not_found"})


def post_focus(self, route, payload, flags):
    from windows_integration import focus_current_process_window
    result = {"ok": focus_current_process_window()}
    return result


def post_shutdown(self, route, payload, flags):
    shutdown = getattr(self.engine, "request_shutdown", None)
    result = {"ok": callable(shutdown)}
    if callable(shutdown):
        shutdown()
    return result
