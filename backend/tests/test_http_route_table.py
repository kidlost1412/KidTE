"""Bảng route mới (http_routes) phải chọn ĐÚNG bộ xử lý mà chuỗi if/elif cũ chọn.

Các danh sách OLD_* dưới đây là điều kiện của từng nhánh if/elif trong Handler.do_GET /
do_POST TRƯỚC khi tách (trích tự động bằng ast từ server.py ở commit 55b5c84, giữ thứ tự).
Với mọi đường dẫn ứng viên (sinh từ các chuỗi trong điều kiện cũ + ghép tiền tố/hậu tố),
nhánh cũ khớp đầu tiên phải trùng dòng bảng mới khớp đầu tiên.
"""
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import server  # noqa: E402,F401  (gắn __globals__ cho bộ xử lý)
import http_routes  # noqa: E402

OLD_GET = [
    'route == "/api/update-ready"',
    'route == "/api/updates"',
    'route == "/health"',
    'route in ("/", "/app", "/app/")',
    'route.startswith("/assets/")',
    'route.startswith("/api/learning")',
    'route == "/api/backup/v2/list"',
    'route.startswith("/api/system/")',
    'route == "/api/trash"',
    'route == "/api/workspace/sales-playbook"',
    'route == "/api/workspace/dose-rules"',
    'route.startswith("/api/workspace/")',
    'route.startswith("/api/privacy/requests/")',
    'route == "/api/dashboard"',
    'route == "/api/live/readiness"',
    'route == "/api/captcha"',
    'self.path == "/api/support/preview"',
    'route == "/api/settings/gemini-models"',
    'self.path == "/api/settings"',
    'route == "/api/audio/rescan"',
    'route.startswith("/api/logs")',
    'route.startswith("/api/profiles/") and route.endswith(("/enrich", "/enrich-batch"))',
    'route.startswith("/api/ingest/")',
    'route.startswith("/api/product/image/")',
    'route.startswith("/api/video/media/")',
    'route.startswith("/api/video/job-media/")',
    'route == "/api/video/catalog"',
    'route == "/api/video/assets"',
    'route.startswith("/api/video/asset/")',
    'route.startswith("/api/profile/thumb/")',
    'route.startswith("/api/product/thumb/")',
    'self.path == "/api/mapping"',
    'route == "/api/video/position"',
    'route == "/api/live/sources"',
    'self.path == "/api/video/timelines"',
    'self.path == "/api/shopee/mappings"',
    'self.path == "/api/shopee/ai/status"',
    'route.startswith("/api/video/analysis/status")',
    'self.path == "/api/plugins"',
]

OLD_POST_EARLY = [
    'route == "/api/updates/restart"',
    'route.startswith("/api/updates/")',
    'route in {"/api/captcha/settings", "/api/captcha/telegram", "/api/captcha/telegram/test", "/api/captcha/selftest", "/api/captcha/start", "/api/captcha/stop"}',
    'route == "/api/system/documents/upload"',
    'route == "/api/ingest/upload"',
    'route.startswith("/api/profiles/") and route.endswith(("/enrich", "/enrich/apply", "/enrich-batch", "/enrich-batch/cancel"))',
    'route in ("/api/ingest/extract", "/api/ingest/draft", "/api/ingest/link", "/api/ingest/combo/propose", "/api/ingest/combo/propose-batch", "/api/ingest/combo/draft", "/api/ingest/product/remove", "/api/ingest/product/restore", "/api/ingest/product/trash-batch", "/api/ingest/product/restore-batch", "/api/ingest/product/purge-batch", "/api/ingest/defaults/save", "/api/ingest/defaults/choose")',
    'route.startswith("/api/system/")',
    'route.startswith("/api/learning/")',
]

# Nhánh cuối "else" (trả lời TikTok) = luôn khớp.
OLD_POST_JSON = [
    'route in delivery_routes',
    'route in queue_routes',
    'route == "/api/workspace/corrections"',
    'correction_undo',
    'route == "/api/trash/empty-expired"',
    'route == "/api/trash/purge"',
    'video_draft_route',
    'dose_test_route',
    'dose_rules_route',
    'duplicate_route',
    'conversation_example_route',
    'video_job_draft_route',
    'video_job_publish_route',
    'product_image_route',
    'workspace_route',
    'route == "/api/video/upload/start"',
    'route == "/api/video/upload/resume"',
    'route == "/api/video/upload/finish"',
    'route == "/api/video/analysis/resume"',
    'route == "/api/video/assets/retry"',
    'route == "/api/mapping"',
    'route == "/api/mapping/undo"',
    'route == "/api/mapping/auto-match"',
    'route == "/api/profiles/bulk-draft"',
    'route == "/api/profiles/approve"',
    'route == "/api/backup/restore"',
    'route == "/api/backup"',
    'route == "/api/backup/v2"',
    'route == "/api/backup/restore/reenable-ai"',
    'route == "/api/backup/v2/delete/preview"',
    'route == "/api/backup/v2/delete"',
    'route == "/api/backup/indexeddb"',
    'route == "/api/privacy/forget"',
    'route == "/api/privacy/purge-ack"',
    'route == "/api/heartbeat"',
    'route == "/api/tiktok/lease"',
    'route == "/api/diag/tiktok-browser"',
    'route == "/api/diagnostics/fix"',
    'route == "/api/support/create"',
    'route == "/api/settings"',
    'route == "/api/settings/check-key"',
    'route == "/api/settings/gemini-models"',
    'route == "/api/audio/test"',
    'route == "/api/extension/pairing/reset"',
    'route == "/api/extension/setup"',
    'route == "/api/focus"',
    'route == "/api/shutdown"',
    'route == "/api/knowledge/update"',
    'route == "/api/knowledge/undo"',
    'route == "/api/knowledge/export"',
    'route == "/api/plugins/action"',
    'route == "/api/video/position"',
    'route == "/api/shopee/mappings"',
    'route == "/api/shopee/sync"',
    'route == "/api/live/sources"',
    'route == "/api/live/control"',
    'route == "/api/live/queue-mode"',
    "route == '/api/live/reidentify'",
    'route == "/api/shopee/reply"',
    'route == "/api/shopee/delivery"',
    'route.endswith("/observe")',
    'route.endswith("catalog/sync")',
    'route.endswith("reply/ack")',
    'route.endswith("reply/delivery")',
]


def _literals():
    text = " ".join(OLD_GET + OLD_POST_EARLY + OLD_POST_JSON)
    text += " " + Path(http_routes.__file__).read_text(encoding="utf-8")
    found = set(re.findall(r"[\"']((?:/|[a-z])[A-Za-z0-9_/.-]*)[\"']", text))
    flags = http_routes.post_route_flags("/")
    found |= set(flags["delivery_routes"]) | set(flags["queue_routes"]) | set(flags["local_video_routes"])
    return sorted(x for x in found if "/" in x)


def _candidates():
    lits = _literals()
    heads = [x for x in lits if x.startswith("/")]
    tails = [x for x in lits if x.startswith("/") or x[0].isalpha()]
    out = {"/", "", "/x", "/api", "/api/", "/api/unknown"}
    for lit in lits:
        out.update({lit, lit + "x", lit + "/x", lit + "/", "/api/x/" + lit.lstrip("/")})
    for head in heads:
        if head.endswith("/"):
            for tail in tails:
                out.add(head + "id1" + ("" if tail.startswith("/") else "/") + tail)
                out.add(head + "a/b" + ("" if tail.startswith("/") else "/") + tail)
    return sorted(out)


def _first_old(conditions, route, path, flags=None):
    scope = dict(flags or {}, route=route, self=SimpleNamespace(path=path))
    for index, condition in enumerate(conditions):
        if eval(condition, {}, scope):  # noqa: S307 - điều kiện cố định ở trên
            return index
    return None


def _first_new(table, route, path, flags=None):
    fake = SimpleNamespace(path=path)
    for index, (matcher, _func) in enumerate(table):
        if matcher(fake, route, flags):
            return index
    return None


def test_table_sizes_match_old_chains():
    assert len(http_routes.GET_ROUTES) == len(OLD_GET)
    assert len(http_routes.POST_EARLY_ROUTES) == len(OLD_POST_EARLY)
    assert len(http_routes.POST_JSON_ROUTES) == len(OLD_POST_JSON) + 1


def test_new_tables_pick_the_same_branch_as_old_if_elif():
    candidates = _candidates()
    assert len(candidates) > 1000
    checked = 0
    for route in candidates:
        for path in (route, route + "?a=1"):
            assert _first_new(http_routes.GET_ROUTES, route, path) == _first_old(OLD_GET, route, path), route
            assert (_first_new(http_routes.POST_EARLY_ROUTES, route, path)
                    == _first_old(OLD_POST_EARLY, route, path)), route
            flags = http_routes.post_route_flags(route)
            old = _first_old(OLD_POST_JSON, route, path, flags)
            new = _first_new(http_routes.POST_JSON_ROUTES, route, path, flags)
            assert new == (len(OLD_POST_JSON) if old is None else old), route
            checked += 1
    assert checked == 2 * len(candidates)


def test_every_old_branch_is_reachable_by_some_candidate():
    candidates = _candidates()
    for table, old in ((http_routes.GET_ROUTES, OLD_GET), (http_routes.POST_EARLY_ROUTES, OLD_POST_EARLY)):
        hit = {_first_new(table, r, r) for r in candidates}
        assert set(range(len(old))) <= hit
    hit = {_first_new(http_routes.POST_JSON_ROUTES, r, r, http_routes.post_route_flags(r)) for r in candidates}
    assert set(range(len(OLD_POST_JSON) + 1)) <= hit


def test_handlers_are_bound_to_server_globals():
    for table in (http_routes.GET_ROUTES, http_routes.POST_EARLY_ROUTES, http_routes.POST_JSON_ROUTES):
        for _matcher, func in table:
            assert func.__globals__ is vars(server), func.__name__
    assert server.ReplyEngine.handle.__globals__ is vars(server)
    assert server.ReplyEngine._price_values.__func__.__globals__ is vars(server)
