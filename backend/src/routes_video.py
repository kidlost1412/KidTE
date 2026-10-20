"""Bộ xử lý route HTTP (video) — tách NGUYÊN VĂN từ Handler.do_GET/do_POST trong server.py.

Mỗi hàm nhận ``self`` là Handler hiện tại (send_json, local_tool_request, engine...).
Tên toàn cục (ROOT, WEB_DIR, clean, report_problem, ...) được tra trong module server:
http_routes.bind() gắn lại __globals__ bằng engine_mixins.rebind — không import server ở đây.
"""
from __future__ import annotations

# ruff: noqa: F821  (tên toàn cục lấy từ server khi bind)


def get_video_media_prefix(self, route, parsed):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    try:
        self.send_video(self.engine.video_media_path(unquote(route.rsplit("/", 1)[-1])))
    except ValueError as exc:
        self.send_json(404, {"ok": False, "error": str(exc)})


def get_video_job_media_prefix(self, route, parsed):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    try:
        self.send_video(self.engine.video_job_media_path(unquote(route.rsplit("/", 1)[-1])))
    except ValueError as exc:
        self.send_json(404, {"ok": False, "error": str(exc)})


def get_video_catalog(self, route, parsed):
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    from urllib.parse import parse_qs
    cart = (parse_qs(parsed.query).get("cart") or [""])[0]
    try:
        self.send_json(200, self.engine.video_catalog(cart))
    except ValueError as exc:
        self.send_json(404, {"ok": False, "error": str(exc)})


def get_video_assets(self, route, parsed):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    query = parse_qs(parsed.query)
    self.send_json(200, self.engine.video_assets_state(
        (query.get("video_id") or [""])[0], (query.get("job_id") or [""])[0]))


def get_video_asset_prefix(self, route, parsed):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    parts = [unquote(part) for part in route.split("/")[4:] if part]
    try:
        if len(parts) != 2:
            raise ValueError("asset_not_found")
        path = self.engine.video_assets.file(parts[0], parts[1])
        mime = "application/json" if path.suffix == ".json" else "image/jpeg"
        self.send_cacheable_file(path, mime)
    except ValueError as exc:
        self.send_json(404, {"ok": False, "error": str(exc)})


def get_video_position(self, route, parsed):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.loopback_read_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    platform = (parse_qs(parsed.query).get("platform") or ["tiktok"])[0]
    if platform not in PLATFORMS:
        self.send_json(400, {"ok": False, "error": "invalid_platform"})
    else:
        self.send_json(200, self.engine.current_video_position(platform))


def get_video_timelines(self, route, parsed):
    if not self.loopback_read_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    self.send_json(200, self.engine.video_timelines())


def get_video_analysis_status_prefix(self, route, parsed):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    query = parse_qs(parsed.query)
    job_id = (query.get("job_id") or [""])[0]
    include = (query.get("include_draft") or [""])[0] == "1"
    try:
        status = self.engine.video_analysis_status(job_id, include)
    except (ValueError, KeyError, FileNotFoundError):
        # O5: job id sai/rỗng/cũ -> 404 JSON, không traceback ra console.
        self.send_json(404, {"ok": False, "error": "invalid_job_id"})
        return
    self.send_json(200, status)


def post_video_draft_route(self, route, payload, flags):
    from urllib.parse import parse_qs, unquote, urlsplit
    parts = [unquote(part) for part in route.split("/")[3:] if part]
    if len(parts) != 3 or parts[2] != "draft":
        raise ValueError("workspace_route_invalid")
    result = self.engine.workspace_save_video_draft(parts[1], payload)
    return result


def post_duplicate_route(self, route, payload, flags):
    from urllib.parse import parse_qs, unquote, urlsplit
    parts = [unquote(part) for part in route.split("/")[3:] if part]
    if len(parts) != 3 or parts[2] != "duplicates":
        raise ValueError("workspace_route_invalid")
    result = self.engine.workspace_resolve_video_duplicate(parts[1], payload)
    return result


def post_video_job_draft_route(self, route, payload, flags):
    from urllib.parse import parse_qs, unquote, urlsplit
    parts = [unquote(part) for part in route.split("/")[3:] if part]
    if len(parts) != 3:
        raise ValueError("workspace_route_invalid")
    result = self.engine.workspace_save_video_job_draft(parts[1], payload)
    return result


def post_video_job_publish_route(self, route, payload, flags):
    from urllib.parse import parse_qs, unquote, urlsplit
    parts = [unquote(part) for part in route.split("/")[3:] if part]
    if len(parts) != 3:
        raise ValueError("workspace_route_invalid")
    result = self.engine.video_analysis_publish({**payload, "job_id": parts[1]})
    return result


def post_video_upload_start(self, route, payload, flags):
    result = self.engine.video_upload_start(payload)
    return result


def post_video_upload_resume(self, route, payload, flags):
    result = self.engine.video_upload_resume(payload)
    return result


def post_video_upload_finish(self, route, payload, flags):
    result = self.engine.video_upload_finish(payload)
    return result


def post_video_analysis_resume(self, route, payload, flags):
    result = self.engine.video_analysis_resume(payload)
    return result


def post_video_assets_retry(self, route, payload, flags):
    result = self.engine.video_assets_state(
        payload.get("video_id", ""), payload.get("job_id", ""), retry=True)
    return result


def post_video_position(self, route, payload, flags):
    result = self.engine.set_video_position(payload)
    return result
