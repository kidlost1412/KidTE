"""Bộ xử lý route HTTP (backup) — tách NGUYÊN VĂN từ Handler.do_GET/do_POST trong server.py.

Mỗi hàm nhận ``self`` là Handler hiện tại (send_json, local_tool_request, engine...).
Tên toàn cục (ROOT, WEB_DIR, clean, report_problem, ...) được tra trong module server:
http_routes.bind() gắn lại __globals__ bằng engine_mixins.rebind — không import server ở đây.
"""
from __future__ import annotations

# ruff: noqa: F821  (tên toàn cục lấy từ server khi bind)


def get_backup_v2_list(self, route, parsed):
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    try:
        self.send_json(200, self.engine.backup_v2_list())
    except Exception as exc:
        report_problem("api_error", f"GET {route}", exc)
        self.send_json(500, {"ok": False, "error": "internal_error"})


def get_trash(self, route, parsed):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    self.send_json(200, self.engine.workspace_trash({
        key: values[0] for key, values in parse_qs(parsed.query).items()
    }))


def get_privacy_requests_prefix(self, route, parsed):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    try:
        self.send_json(200, self.engine.privacy_request(unquote(route.rsplit("/", 1)[1])))
    except ValueError as exc:
        self.send_json(404, {"ok": False, "error": str(exc)})


def post_trash_empty_expired(self, route, payload, flags):
    result = self.engine.workspace_purge_expired()
    return result


def post_trash_purge(self, route, payload, flags):
    result = self.engine.workspace.purge_now(payload.get("trash_ids") or [],
                                             everything=payload.get("all") is True)
    return result


def post_backup_restore(self, route, payload, flags):
    result = self.engine.restore_backup(payload)
    return result


def post_backup(self, route, payload, flags):
    result = self.engine.backup_now_and_refresh(payload)
    return result


def post_backup_v2(self, route, payload, flags):
    result = self.engine.backup_v2_start(payload)
    return result


def post_backup_restore_reenable_ai(self, route, payload, flags):
    result = self.engine.reenable_ai_after_restore(payload)
    return result


def post_backup_v2_delete_preview(self, route, payload, flags):
    result = self.engine.backup_v2_delete_preview(payload)
    return result


def post_backup_v2_delete(self, route, payload, flags):
    result = self.engine.backup_v2_delete(payload)
    return result


def post_backup_indexeddb(self, route, payload, flags):
    result = self.engine.backup_indexeddb_save(payload)
    return result


def post_privacy_forget(self, route, payload, flags):
    result = self.engine.privacy_forget(payload)
    return result


def post_privacy_purge_ack(self, route, payload, flags):
    result = self.engine.privacy_purge_ack(payload)
    return result
