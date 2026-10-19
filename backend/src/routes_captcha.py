"""Bộ xử lý route HTTP (captcha) — tách NGUYÊN VĂN từ Handler.do_GET/do_POST trong server.py.

Mỗi hàm nhận ``self`` là Handler hiện tại (send_json, local_tool_request, engine...).
Tên toàn cục (ROOT, WEB_DIR, clean, report_problem, ...) được tra trong module server:
http_routes.bind() gắn lại __globals__ bằng engine_mixins.rebind — không import server ở đây.
"""
from __future__ import annotations

# ruff: noqa: F821  (tên toàn cục lấy từ server khi bind)


def get_captcha(self, route, parsed):
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    self.send_json(200, self.engine.captcha.status())


def post_captcha_settings(self, route):
    if not self.local_tool_request():
        self.reject(403, "local_only")
        return
    try:
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 2048:
            raise ValueError("invalid_body_size")
        payload = request_json_object(self.rfile.read(size)) if size else {}
        if route == "/api/captcha/settings":
            result = self.engine.captcha.save_key(str(payload.get("api_key", "")))
        elif route == "/api/captcha/telegram":
            result = self.engine.captcha.save_telegram(
                str(payload.get("bot_token", "")), str(payload.get("chat_id", "")))
        elif route == "/api/captcha/telegram/test":
            result = self.engine.captcha.test_telegram()
        elif route == "/api/captcha/selftest":
            result = self.engine.captcha.selftest()
        elif route == "/api/captcha/start":
            result = self.engine.captcha.start()
        else:
            result = self.engine.captcha.stop()
        self.send_json(200 if result.get("ok") else 409, result)
    except (ValueError, json.JSONDecodeError) as exc:
        self.send_json(400, {"ok": False, "error": str(exc)})
    except Exception as exc:
        report_problem("api_error", f"POST {route}", exc)
        self.send_json(500, {"ok": False, "error": "internal_error"})
