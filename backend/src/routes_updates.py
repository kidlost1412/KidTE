"""Bộ xử lý route HTTP (updates) — tách NGUYÊN VĂN từ Handler.do_GET/do_POST trong server.py.

Mỗi hàm nhận ``self`` là Handler hiện tại (send_json, local_tool_request, engine...).
Tên toàn cục (ROOT, WEB_DIR, clean, report_problem, ...) được tra trong module server:
http_routes.bind() gắn lại __globals__ bằng engine_mixins.rebind — không import server ở đây.
"""
from __future__ import annotations

# ruff: noqa: F821  (tên toàn cục lấy từ server khi bind)


def get_update_ready(self, route, parsed):
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    self.send_json(200, {"ok": True, "pid": os.getpid(),
                         "launcher_nonce": os.environ.get("KIDTE_LAUNCH_NONCE", ""),
                         "launcher_generation": os.environ.get("KIDTE_LAUNCH_GENERATION", ""),
                         "version": _app_version(), "data_root": str(ROOT)})


def get_updates(self, route, parsed):
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    manager = getattr(self.engine, "updates", None)
    self.send_json(200, manager.state() if manager is not None else
                   {"ok": True, "enabled": False, "available": [], "installed": {},
                    "error": "update_source_mode_disabled"})


def post_updates_restart(self, route):
    if not self.local_tool_request():
        self.reject(403, "local_only")
        return
    try:
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 10_000:
            raise ValueError("invalid_body_size")
        payload = request_json_object(self.rfile.read(size)) if size else {}
        manager = getattr(self.engine, "updates", None)
        restart = getattr(self.engine, "request_restart", None)
        if manager is None:
            raise ValueError("update_source_mode_disabled")
        from update_restart import evaluate_restart, launcher_supports_restart
        verdict = evaluate_restart(
            has_pending=bool(manager.state().get("restart_required")),
            live=bool(self.engine._is_live_running()), payload=payload,
            supported=callable(restart) and launcher_supports_restart())
        self.send_json(verdict["status"], verdict["body"])
        if verdict["go"]:
            # Reply first, then let the core exit so the launcher restarts it.
            threading.Timer(0.4, restart).start()
    except (ValueError, json.JSONDecodeError) as exc:
        self.send_json(400, {"ok": False, "error": str(exc)})
    except Exception as exc:
        report_problem("api_error", f"POST {route}", exc)
        self.send_json(500, {"ok": False, "error": "internal_error"})


def post_updates_prefix(self, route):
    if not self.local_tool_request():
        self.reject(403, "local_only")
        return
    try:
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 10_000:
            raise ValueError("invalid_body_size")
        payload = request_json_object(self.rfile.read(size)) if size else {}
        manager = getattr(self.engine, "updates", None)
        if manager is None:
            raise ValueError("update_source_mode_disabled")
        result = manager.action(route[len("/api/updates/"):], payload)
        self.send_json(202 if result.get("accepted") is True else 200, result)
    except (ValueError, json.JSONDecodeError) as exc:
        self.send_json(409 if str(exc) in {"update_job_busy", "update_restart_required"}
                       else 404 if str(exc).endswith("not_found") else 400,
                       {"ok": False, "error": str(exc)})
    except Exception as exc:
        report_problem("api_error", f"POST {route}", exc)
        self.send_json(500, {"ok": False, "error": "internal_error"})
