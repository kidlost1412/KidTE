"""Bảng định tuyến HTTP của Handler (thay chuỗi if/elif dài trong do_GET/do_POST).

THỨ TỰ các dòng trong mỗi bảng = đúng thứ tự nhánh if/elif cũ; dòng đầu tiên khớp
thắng (khớp chính xác, tiền tố, hậu tố, tập hợp, hoặc biểu thức gốc giữ nguyên văn).
tests/test_http_route_table.py so bảng này với danh sách điều kiện trích tự động từ code cũ.
"""
from __future__ import annotations

import routes_backup
import routes_captcha
import routes_extension
import routes_live
import routes_misc
import routes_settings
import routes_updates
import routes_video
import routes_workspace

RESPONSE_SENT = object()  # bộ xử lý đã tự gửi phản hồi (vd 409 DoseRuleConflict)


def exact(value):
    return lambda self, route, flags: route == value


def raw_path(value):
    # Nhánh cũ so self.path (kể cả query) chứ không phải route.
    return lambda self, route, flags: self.path == value


def one_of(*values):
    return lambda self, route, flags: route in values


def prefix(value):
    return lambda self, route, flags: route.startswith(value)


def suffix(value):
    return lambda self, route, flags: route.endswith(value)


def flag(name):
    return lambda self, route, flags: flags[name]


def expr(func):
    return func


def always():
    return lambda self, route, flags: True


def match(table, handler, route, flags=None):
    """Bộ xử lý của dòng ĐẦU TIÊN khớp (như chuỗi if/elif cũ), hoặc None."""
    for matcher, func in table:
        if matcher(handler, route, flags):
            return func
    return None


# GET: func(self, route, parsed). Không khớp -> 404 not_found (trong do_GET).
GET_ROUTES = [
    (exact('/api/update-ready'), routes_updates.get_update_ready),
    (exact('/api/updates'), routes_updates.get_updates),
    (exact('/health'), routes_misc.get_health),
    (one_of('/', '/app', '/app/'), routes_misc.get_root),
    (prefix('/assets/'), routes_misc.get_assets_prefix),
    (prefix('/api/learning'), routes_workspace.get_learning_prefix),
    (exact('/api/backup/v2/list'), routes_backup.get_backup_v2_list),
    (prefix('/api/system/'), routes_settings.get_system_prefix),
    (exact('/api/trash'), routes_backup.get_trash),
    (exact('/api/workspace/sales-playbook'), routes_workspace.get_workspace_sales_playbook),
    (exact('/api/workspace/dose-rules'), routes_workspace.get_workspace_dose_rules),
    (prefix('/api/workspace/'), routes_workspace.get_workspace_prefix),
    (prefix('/api/privacy/requests/'), routes_backup.get_privacy_requests_prefix),
    (exact('/api/dashboard'), routes_live.get_dashboard),
    (exact('/api/live/readiness'), routes_live.get_live_readiness),
    (exact('/api/captcha'), routes_captcha.get_captcha),
    (raw_path('/api/support/preview'), routes_settings.get_support_preview),
    (exact('/api/settings/gemini-models'), routes_settings.get_settings_gemini_models),
    (raw_path('/api/settings'), routes_settings.get_settings),
    (exact('/api/audio/rescan'), routes_settings.get_audio_rescan),
    (prefix('/api/logs'), routes_settings.get_logs_prefix),
    (expr(lambda self, route, flags: route.startswith("/api/profiles/") and route.endswith(("/enrich", "/enrich-batch"))), routes_workspace.get_profiles_prefix),
    (prefix('/api/ingest/'), routes_workspace.get_ingest_prefix),
    (prefix('/api/product/image/'), routes_workspace.get_product_image_prefix),
    (prefix('/api/video/media/'), routes_video.get_video_media_prefix),
    (prefix('/api/video/job-media/'), routes_video.get_video_job_media_prefix),
    (exact('/api/video/catalog'), routes_video.get_video_catalog),
    (exact('/api/video/assets'), routes_video.get_video_assets),
    (prefix('/api/video/asset/'), routes_video.get_video_asset_prefix),
    (prefix('/api/profile/thumb/'), routes_workspace.get_profile_thumb_prefix),
    (prefix('/api/product/thumb/'), routes_workspace.get_product_thumb_prefix),
    (raw_path('/api/mapping'), routes_workspace.get_mapping),
    (exact('/api/video/position'), routes_video.get_video_position),
    (exact('/api/live/sources'), routes_live.get_live_sources),
    (raw_path('/api/video/timelines'), routes_video.get_video_timelines),
    (raw_path('/api/shopee/mappings'), routes_workspace.get_shopee_mappings),
    (raw_path('/api/shopee/ai/status'), routes_live.get_shopee_ai_status),
    (prefix('/api/video/analysis/status'), routes_video.get_video_analysis_status_prefix),
    (raw_path('/api/plugins'), routes_extension.get_plugins),
]

# POST tự xử lý trọn (tự kiểm local_only, đọc thân, trả lời): func(self, route).
POST_EARLY_ROUTES = [
    (exact('/api/updates/restart'), routes_updates.post_updates_restart),
    (prefix('/api/updates/'), routes_updates.post_updates_prefix),
    (one_of('/api/captcha/settings', '/api/captcha/telegram', '/api/captcha/telegram/test', '/api/captcha/selftest', '/api/captcha/start', '/api/captcha/stop'), routes_captcha.post_captcha_settings),
    (exact('/api/system/documents/upload'), routes_settings.post_system_documents_upload),
    (exact('/api/ingest/upload'), routes_workspace.post_ingest_upload),
    (expr(lambda self, route, flags: route.startswith("/api/profiles/") and route.endswith(("/enrich", "/enrich/apply", "/enrich-batch", "/enrich-batch/cancel"))), routes_workspace.post_profiles_prefix),
    (one_of('/api/ingest/extract', '/api/ingest/draft', '/api/ingest/link', '/api/ingest/combo/propose', '/api/ingest/combo/propose-batch', '/api/ingest/combo/draft', '/api/ingest/product/remove', '/api/ingest/product/restore', '/api/ingest/defaults/save', '/api/ingest/defaults/choose'), routes_workspace.post_ingest_extract),
    (prefix('/api/system/'), routes_settings.post_system_prefix),
    (prefix('/api/learning/'), routes_workspace.post_learning_prefix),
]

# POST JSON chung (sau post_guard + đọc thân + kiểm ghép đôi): func(self, route, payload, flags) -> result.
POST_JSON_ROUTES = [
    (expr(lambda self, route, flags: route in flags['delivery_routes']), routes_live.post_route_in_delivery_routes),
    (expr(lambda self, route, flags: route in flags['queue_routes']), routes_live.post_route_in_queue_routes),
    (exact('/api/workspace/corrections'), routes_workspace.post_workspace_corrections),
    (flag('correction_undo'), routes_workspace.post_correction_undo),
    (exact('/api/trash/empty-expired'), routes_backup.post_trash_empty_expired),
    (exact('/api/trash/purge'), routes_backup.post_trash_purge),
    (flag('video_draft_route'), routes_video.post_video_draft_route),
    (flag('dose_test_route'), routes_workspace.post_dose_test_route),
    (flag('dose_rules_route'), routes_workspace.post_dose_rules_route),
    (flag('duplicate_route'), routes_video.post_duplicate_route),
    (flag('conversation_example_route'), routes_workspace.post_conversation_example_route),
    (flag('video_job_draft_route'), routes_video.post_video_job_draft_route),
    (flag('video_job_publish_route'), routes_video.post_video_job_publish_route),
    (flag('product_image_route'), routes_workspace.post_product_image_route),
    (flag('workspace_route'), routes_workspace.post_workspace_route),
    (exact('/api/video/upload/start'), routes_video.post_video_upload_start),
    (exact('/api/video/upload/resume'), routes_video.post_video_upload_resume),
    (exact('/api/video/upload/finish'), routes_video.post_video_upload_finish),
    (exact('/api/video/analysis/resume'), routes_video.post_video_analysis_resume),
    (exact('/api/video/assets/retry'), routes_video.post_video_assets_retry),
    (exact('/api/mapping'), routes_workspace.post_mapping),
    (exact('/api/mapping/undo'), routes_workspace.post_mapping_undo),
    (exact('/api/mapping/auto-match'), routes_workspace.post_mapping_auto_match),
    (exact('/api/profiles/bulk-draft'), routes_workspace.post_profiles_bulk_draft),
    (exact('/api/profiles/approve'), routes_workspace.post_profiles_approve),
    (exact('/api/backup/restore'), routes_backup.post_backup_restore),
    (exact('/api/backup'), routes_backup.post_backup),
    (exact('/api/backup/v2'), routes_backup.post_backup_v2),
    (exact('/api/backup/restore/reenable-ai'), routes_backup.post_backup_restore_reenable_ai),
    (exact('/api/backup/v2/delete/preview'), routes_backup.post_backup_v2_delete_preview),
    (exact('/api/backup/v2/delete'), routes_backup.post_backup_v2_delete),
    (exact('/api/backup/indexeddb'), routes_backup.post_backup_indexeddb),
    (exact('/api/privacy/forget'), routes_backup.post_privacy_forget),
    (exact('/api/privacy/purge-ack'), routes_backup.post_privacy_purge_ack),
    (exact('/api/heartbeat'), routes_live.post_heartbeat),
    (exact('/api/tiktok/lease'), routes_live.post_tiktok_lease),
    (exact('/api/diagnostics/fix'), routes_settings.post_diagnostics_fix),
    (exact('/api/support/create'), routes_settings.post_support_create),
    (exact('/api/settings'), routes_settings.post_settings),
    (exact('/api/settings/check-key'), routes_settings.post_settings_check_key),
    (exact('/api/settings/gemini-models'), routes_settings.post_settings_gemini_models),
    (exact('/api/audio/test'), routes_settings.post_audio_test),
    (exact('/api/extension/pairing/reset'), routes_extension.post_extension_pairing_reset),
    (exact('/api/extension/setup'), routes_extension.post_extension_setup),
    (exact('/api/focus'), routes_misc.post_focus),
    (exact('/api/shutdown'), routes_misc.post_shutdown),
    (exact('/api/knowledge/update'), routes_workspace.post_knowledge_update),
    (exact('/api/knowledge/undo'), routes_workspace.post_knowledge_undo),
    (exact('/api/knowledge/export'), routes_workspace.post_knowledge_export),
    (exact('/api/plugins/action'), routes_extension.post_plugins_action),
    (exact('/api/video/position'), routes_video.post_video_position),
    (exact('/api/shopee/mappings'), routes_workspace.post_shopee_mappings),
    (exact('/api/shopee/sync'), routes_live.post_shopee_sync),
    (exact('/api/live/sources'), routes_live.post_live_sources),
    (exact('/api/live/control'), routes_live.post_live_control),
    (exact('/api/live/queue-mode'), routes_live.post_live_queue_mode),
    (exact('/api/live/reidentify'), routes_live.post_live_reidentify),
    (exact('/api/shopee/reply'), routes_live.post_shopee_reply),
    (exact('/api/shopee/delivery'), routes_live.post_shopee_delivery),
    (suffix('/observe'), routes_live.post_observe),
    (suffix('catalog/sync'), routes_live.post_route_endswith_catalog_sync),
    (suffix('reply/ack'), routes_live.post_route_endswith_reply_ack),
    (suffix('reply/delivery'), routes_live.post_route_endswith_reply_delivery),
    (always(), routes_live.post_reply),
]


def post_route_flags(route):
    correction_undo = (route.startswith("/api/workspace/corrections/") and
                       route.endswith("/undo"))
    dose_rules_route = (route in ("/api/workspace/dose-rules/import",
                                  "/api/workspace/dose-rules/import-label") or (
        route.startswith("/api/workspace/dose-rules/") and route.endswith("/review")))
    workspace_route = route == "/api/workspace/corrections" or correction_undo or dose_rules_route or (
        route.startswith("/api/workspace/") and
        (route.endswith("/rollback") or route.endswith("/restore"))
    )
    trash_route = route in {"/api/trash/empty-expired", "/api/trash/purge"}
    video_draft_route = (route.startswith("/api/workspace/videos/") and
                         route.endswith("/draft"))
    dose_test_route = (route.startswith("/api/workspace/profiles/") and
                       route.endswith("/dose-test"))
    duplicate_route = (route.startswith("/api/workspace/videos/") and
                       route.endswith("/duplicates"))
    conversation_example_route = (route.startswith("/api/workspace/conversations/") and
                                  (route.endswith("/approve-example") or route.endswith("/disable-example")))
    video_job_draft_route = (route.startswith("/api/workspace/video-jobs/") and
                             route.endswith("/draft"))
    video_job_publish_route = (route.startswith("/api/workspace/video-jobs/") and
                               route.endswith("/publish"))
    product_image_route = (route.startswith("/api/workspace/products/") and
                           route.endswith("/image"))
    local_video_routes = {
        "/api/video/upload/start", "/api/video/upload/chunk",
        "/api/video/upload/resume",
        "/api/video/upload/finish",
        "/api/video/analysis/resume",
        "/api/video/assets/retry",
    }
    delivery_routes = {f'/api/{prefix}/{operation}': (platform, operation)
                       for platform, prefix in (('tiktok', 'reply'), ('shopee', 'shopee/reply'))
                       for operation in ('claim', 'status', 'dispatched', 'receipt', 'failure')}
    queue_routes = {f'/api/{prefix}/queue/{operation}': (platform, operation)
                    for platform, prefix in (('tiktok', 'reply'), ('shopee', 'shopee/reply'),
                                             ('tiktok', 'tiktok'), ('shopee', 'shopee'))
                    for operation in ('enqueue', 'next', 'status', 'cancel')}
    return {'correction_undo': correction_undo, 'dose_rules_route': dose_rules_route, 'workspace_route': workspace_route, 'trash_route': trash_route, 'video_draft_route': video_draft_route, 'dose_test_route': dose_test_route, 'duplicate_route': duplicate_route, 'conversation_example_route': conversation_example_route, 'video_job_draft_route': video_job_draft_route, 'video_job_publish_route': video_job_publish_route, 'product_image_route': product_image_route, 'local_video_routes': local_video_routes, 'delivery_routes': delivery_routes, 'queue_routes': queue_routes}


def post_guard(self, route, flags):
    """Kiểm Origin/local_only và route hợp lệ, đúng thứ tự cũ. False = đã reject()."""
    workspace_route = flags['workspace_route']
    trash_route = flags['trash_route']
    video_draft_route = flags['video_draft_route']
    dose_test_route = flags['dose_test_route']
    duplicate_route = flags['duplicate_route']
    conversation_example_route = flags['conversation_example_route']
    video_job_draft_route = flags['video_job_draft_route']
    video_job_publish_route = flags['video_job_publish_route']
    product_image_route = flags['product_image_route']
    local_video_routes = flags['local_video_routes']
    delivery_routes = flags['delivery_routes']
    queue_routes = flags['queue_routes']
    if route in (("/api/mapping", "/api/video/position", "/api/plugins/action", "/api/backup",
                     "/api/diagnostics/fix", "/api/support/create", "/api/extension/setup",
                     "/api/mapping/undo", "/api/focus", "/api/shutdown", "/api/live/sources", "/api/live/reidentify",
                     "/api/live/control", "/api/live/queue-mode",
                     "/api/mapping/auto-match", "/api/profiles/bulk-draft", "/api/profiles/approve",
                     "/api/knowledge/update", "/api/knowledge/undo",
                     "/api/knowledge/export",
                     "/api/audio/test", "/api/backup/restore", "/api/privacy/forget",
                     "/api/backup/v2", "/api/backup/v2/delete/preview", "/api/backup/v2/delete", "/api/backup/restore/reenable-ai", "/api/backup/indexeddb") + tuple(local_video_routes)) or workspace_route or trash_route or video_draft_route or dose_test_route or duplicate_route or conversation_example_route or video_job_draft_route or video_job_publish_route or product_image_route:
        # Các route cấu hình/UI chỉ nhận same-origin local.
        if not self.local_tool_request():
            self.reject(403, "local_only")
            return False
    elif route in ("/api/settings", "/api/settings/check-key", "/api/settings/gemini-models",
                   "/api/extension/pairing/reset"):
        if not self.local_tool_request():
            self.reject(403, "local_only")
            return False
    elif not self.headers.get("Origin", "").startswith("chrome-extension://"):
        self.reject(403, "extension_origin_required")
        return False
    if route not in {"/api/reply", "/api/reply/ack", "/api/reply/delivery", "/api/catalog/sync",
                         *delivery_routes,
                         *queue_routes,
                         "/api/shopee/mappings", "/api/shopee/sync", "/api/shopee/reply", "/api/shopee/delivery",
                         "/api/observe", "/api/mapping", "/api/mapping/undo",
                         "/api/mapping/auto-match", "/api/profiles/bulk-draft", "/api/profiles/approve",
                         "/api/video/position", "/api/plugins/action", "/api/backup",
                     "/api/heartbeat", "/api/tiktok/lease", "/api/diagnostics/fix", "/api/support/create",
                     "/api/extension/setup", "/api/knowledge/update",
                     "/api/knowledge/undo", "/api/knowledge/export",
                     "/api/focus", "/api/shutdown", "/api/live/sources", "/api/live/reidentify",
                     "/api/live/control", "/api/live/queue-mode",
                     "/api/audio/test", "/api/backup/restore", "/api/privacy/forget", "/api/privacy/purge-ack",
                     "/api/backup/v2", "/api/backup/v2/delete/preview", "/api/backup/v2/delete", "/api/backup/restore/reenable-ai", "/api/backup/indexeddb",
                     *local_video_routes} and not workspace_route and not trash_route and not video_draft_route and not dose_test_route and not duplicate_route and not conversation_example_route and not video_job_draft_route and not video_job_publish_route and not product_image_route:
        if route not in ("/api/settings", "/api/settings/check-key", "/api/settings/gemini-models",
                         "/api/extension/pairing/reset"):
            self.reject(404, "not_found")
            return False
    return True


def bind(namespace):
    """Gắn __globals__ của mọi bộ xử lý về module server (xem engine_mixins.rebind)."""
    from engine_mixins import rebind
    bound = {}
    for module in (routes_backup, routes_captcha, routes_extension, routes_live, routes_misc, routes_settings, routes_updates, routes_video, routes_workspace,):
        for key, value in list(vars(module).items()):
            if key.startswith(("get_", "post_")) and getattr(value, "__module__", None) == module.__name__:
                bound[value] = rebind(value, namespace)
                setattr(module, key, bound[value])
    for table in (GET_ROUTES, POST_EARLY_ROUTES, POST_JSON_ROUTES):
        for index, (matcher, func) in enumerate(table):
            table[index] = (matcher, bound.get(func, func))
    globals()["post_guard"] = rebind(post_guard, namespace)
