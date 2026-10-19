"""Bộ xử lý route HTTP (settings) — tách NGUYÊN VĂN từ Handler.do_GET/do_POST trong server.py.

Mỗi hàm nhận ``self`` là Handler hiện tại (send_json, local_tool_request, engine...).
Tên toàn cục (ROOT, WEB_DIR, clean, report_problem, ...) được tra trong module server:
http_routes.bind() gắn lại __globals__ bằng engine_mixins.rebind — không import server ở đây.
"""
from __future__ import annotations

# ruff: noqa: F821  (tên toàn cục lấy từ server khi bind)


def get_system_prefix(self, route, parsed):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    try:
        self.send_json(200, self.engine.system_get(
            route[len("/api/system/"):], {k: v[0] for k, v in parse_qs(parsed.query).items()}))
    except ValueError as exc:
        self.send_json(409 if str(exc) in {'maintenance_required', 'maintenance_job_busy', 'backup_preview_changed'}
                       else 404 if str(exc).endswith("not_found") else 400,
                       {"ok": False, "error": str(exc)})
    except Exception as exc:
        report_problem("api_error", f"GET {route}", exc)
        self.send_json(500, {"ok": False, "error": "internal_error"})


def get_support_preview(self, route, parsed):
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    self.send_json(200, self.engine.support_report_preview())


def get_settings_gemini_models(self, route, parsed):
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    self.send_json(200, self.engine.gemini_models())


def get_settings(self, route, parsed):
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    self.send_json(200, self.engine.settings_state())


def get_audio_rescan(self, route, parsed):
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    self.send_json(200, {"ok": True, "audio_report": self.engine.audio_report()})


def get_logs_prefix(self, route, parsed):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    from log_viewer import recent_logs
    query = parse_qs(parsed.query)
    self.send_json(200, recent_logs(
        LOG_DIR, component=(query.get("component") or [""])[0],
        severity=(query.get("severity") or [""])[0],
    ))


def post_system_documents_upload(self, route):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.local_tool_request():
        self.reject(403, "local_only")
        return
    try:
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > 30 * 1024 * 1024:
            raise ValueError("File quá lớn (tối đa 30 MB)")
        query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        meta = self.engine.documents.add(unquote(query.get("filename", "")), self.rfile.read(size),
                                         category=query.get("category", "khac"),
                                         title=unquote(query.get("title", "")))
        self.send_json(200, {"ok": True, "document": meta})
    except ValueError as exc:
        self.send_json(400, {"ok": False, "error": str(exc)})
    except Exception as exc:
        report_problem("api_error", f"POST {route}", exc)
        self.send_json(500, {"ok": False, "error": "internal_error"})


def post_system_prefix(self, route):
    if not self.local_tool_request():
        self.reject(403, "local_only")
        return
    try:
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 2_000_000:
            raise ValueError("invalid_body_size")
        payload = request_json_object(self.rfile.read(size)) if size else {}
        result = self.engine.system_post(route[len("/api/system/"):], payload)
        self.send_json(202 if result.get('accepted') is True else 200, result)
    except (ValueError, json.JSONDecodeError) as exc:
        self.send_json(409 if str(exc) in {'maintenance_required', 'maintenance_job_busy', 'backup_preview_changed'}
                       else 404 if str(exc).endswith("not_found") else 400,
                       {"ok": False, "error": str(exc)})
    except Exception as exc:
        report_problem("api_error", f"POST {route}", exc)
        self.send_json(500, {"ok": False, "error": "internal_error"})


def post_diagnostics_fix(self, route, payload, flags):
    result = self.engine.diagnostics_fix()
    return result


def post_support_create(self, route, payload, flags):
    result = self.engine.support_report_create()
    return result


def post_settings(self, route, payload, flags):
    result = self.engine.save_settings(payload)
    return result


def post_settings_check_key(self, route, payload, flags):
    result = self.engine.check_key(payload)
    return result


def post_settings_gemini_models(self, route, payload, flags):
    result = self.engine.gemini_models(payload)
    return result


def post_audio_test(self, route, payload, flags):
    result = self.engine.test_audio_device(payload)
    return result
