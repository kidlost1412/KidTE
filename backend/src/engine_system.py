"""/api/system/*, sao lưu v2, sổ tay bán hàng, thông tin shop, mở liên kết ngoài.

Tách nguyên văn từ lớp ReplyEngine trong server.py (không đổi logic).
LƯU Ý: các phương thức ở đây được server.py "nhận nuôi" bằng
engine_mixins.adopt(..., globals()) nên tên toàn cục (ROOT, clean, report_problem,
LIVE_CATALOG_PATH...) được tra trong không gian tên của module server — giữ
nguyên hành vi cũ, kể cả monkeypatch.setattr(server, ...). Không import server ở đây.
"""
from __future__ import annotations


# ruff: noqa: F821  (tên toàn cục lấy từ server qua adopt)


class EngineSystemMixin:
    # ================================================================ /api/system/*
    def _system_jobs(self):
        with self._lock:
            store = getattr(self, '_job_store', None)
            if store is None:
                from maintenance import JobStore
                store = self._job_store = JobStore(DATA_DIR / 'system_jobs.sqlite3')
            return store

    def _backup_v2_service(self):
        from backup_service import BackupService
        from captcha_secret import _protect
        from config import SYSTEM_PROMPT_OVERRIDE_PATH
        def references():
            rows = []
            for path in sorted((ROOT / 'data' / 'videos').glob('*/timeline.json')):
                from backup import _strict_json_loads, _reject_links
                _reject_links(path, Path(path.anchor))
                timeline = _strict_json_loads(path.read_text(encoding='utf-8'))
                if not isinstance(timeline, dict):
                    raise ValueError('backup_video_timeline_invalid')
                if not timeline.get('source_file'):
                    continue
                video_id = path.parent.name
                try:
                    source = self.video_media_path(video_id)
                except ValueError:
                    # Preserve the expected missing source explicitly; do not
                    # silently call a backup complete after losing a video.
                    source = Path.home() / 'Downloads' / Path(str(timeline['source_file'])).name
                rows.append({'path': source, 'video_id': video_id,
                             'sha256': timeline.get('source_sha256')})
            return rows
        # Installed solver hash resolution remains a release-manifest task.
        # An empty hash is reported missing instead of claiming it was checked.
        additional = {}
        for relative, source in (('external-settings/processor-settings.json', PROCESSOR_SETTINGS_PATH),
                                 ('external-settings/system-prompt.txt', SYSTEM_PROMPT_OVERRIDE_PATH)):
            source = Path(source).absolute()
            if not source.is_relative_to(Path(ROOT).absolute()) and source.is_file():
                additional[relative] = source
        return BackupService(ROOT, app_version=_app_version(), schema_version=2,
            solver_hash='', reference_provider=references, privacy_db=getattr(self.memory, 'db_path', None),
            dpapi_decrypt=lambda data: _protect(data, decrypt=True), additional_sources=additional)

    def backup_v2_list(self):
        from backup_v2 import BackupV2
        store = BackupV2(ROOT / 'backups' / 'v2')
        items = []
        for path in store.manifests.glob('*.json'):
            try:
                # Listing a backup must never read its videos/models. Checksum
                # verification runs on explicit integrity/delete/restore paths.
                manifest = store._load(path.stem)
                verified = False
            except Exception:
                try:
                    manifest, verified = store._load(path.stem), False
                except Exception:
                    _note_swallowed("backup_manifest_unreadable", once=True, key=path.stem)
                    continue
            if not isinstance(manifest, dict) or manifest.get('scope') not in ('full', 'partial'):
                continue
            files = manifest.get('files') if isinstance(manifest.get('files'), list) else []
            items.append({'id': path.stem, 'kind': manifest['scope'],
                          'created_at': manifest.get('created_at', ''),
                          'size': sum(f.get('bytes', 0) for f in files if isinstance(f, dict)
                                      and isinstance(f.get('bytes'), int)),
                          'verified': verified})
        items.sort(key=lambda item: item['created_at'], reverse=True)
        return {'ok': True, 'items': items}

    def backup_v2_start(self, payload):
        from backup_crypto import _password_bytes
        password = payload.get('password')
        _password_bytes(password)
        full = payload.get('full', True)
        if not isinstance(full, bool):
            raise ValueError('backup_full_invalid')
        result = ReplyEngine._guarded_v2_create(self, password, full)
        return {'ok': True, 'job_id': result['job_id']}

    def _guarded_v2_create(self, password, full=True):
        _V2_WRITERS.begin_create()
        def run(progress):
            try:
                return self._backup_v2_service().create(password, progress=progress, full=full)
            finally:
                _V2_WRITERS.end_create()
        try:
            return self._system_jobs().start('backup_v2', run)
        except BaseException:
            _V2_WRITERS.end_create()
            raise

    @staticmethod
    def _v2_delete_ids(store, payload):
        if payload.get('all') is True:
            if payload.get('ids') is not None:
                raise ValueError('backup_ids_invalid')
            return sorted(path.stem for path in store.manifests.glob('*.json'))
        return payload.get('ids')

    def backup_v2_delete_preview(self, payload):
        from backup import BackupError
        from backup_v2 import BackupV2
        store = BackupV2(ROOT / 'backups' / 'v2')
        _V2_WRITERS.begin_delete()
        try:
            ids = self._v2_delete_ids(store, payload)
            preview = store.preview_delete(ids)
        except BackupError as exc:
            raise ValueError(str(exc)) from None
        finally:
            _V2_WRITERS.end_delete()
        return {'ok': True, 'token': preview['preview_token'],
                'reclaim_bytes': preview['reclaimable_bytes'], 'ids': preview['ids']}

    def backup_v2_delete(self, payload):
        from backup import BackupError
        from backup_v2 import BackupV2
        token = payload.get('token')
        if not isinstance(token, str) or not token:
            raise ValueError('backup_preview_changed')
        store = BackupV2(ROOT / 'backups' / 'v2')
        _V2_WRITERS.begin_delete()
        try:
            ids = self._v2_delete_ids(store, payload)
            return store.delete(ids, preview_token=token)
        except BackupError as exc:
            raise ValueError(str(exc)) from None
        finally:
            _V2_WRITERS.end_delete()

    def backup_indexeddb_save(self, payload):
        import json as _json
        from datetime import datetime, timezone
        data = payload.get('export', payload)
        if not isinstance(data, (dict, list)):
            raise ValueError('indexeddb_export_invalid')
        text = _json.dumps(data, ensure_ascii=False, allow_nan=False)
        if len(text.encode('utf-8')) > 6_000_000:
            raise ValueError('indexeddb_export_too_large')
        folder = ROOT / 'backups' / 'indexeddb'
        folder.mkdir(parents=True, exist_ok=True)
        name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json'
        temporary = folder / (name + '.tmp')
        temporary.write_text(text, encoding='utf-8')
        os.replace(temporary, folder / name)
        for old in sorted(folder.glob('*.json'))[:-5]:
            old.unlink(missing_ok=True)
        return {'ok': True, 'saved': name, 'bytes': len(text.encode('utf-8'))}

    def system_get(self, path, query):
        """Trang Trợ lý tự động và Cài đặt: chi tiết bộ xử lý, model chép lời, sao lưu."""
        parts = [part for part in path.split("/") if part]
        if len(parts) == 2 and parts[0] == 'jobs':
            return {'ok': True, **self._system_jobs().get(parts[1])}
        if len(parts) == 2 and parts[0] == "processors":
            return self.processors.detail(parts[1])
        if parts == ["whisper"]:
            import whisper_models
            return whisper_models.state(self._whisper_downloader())
        if parts == ["backup-parts"]:
            from backup_policy import parts_catalog
            return {"ok": True, "parts": parts_catalog()}
        if len(parts) == 2 and parts[0] == "backups":
            from backup_policy import backup_detail
            return backup_detail(parts[1])
        if parts == ["shop-info"]:
            return {"ok": True, "fields": SHOP_INFO_FIELDS, "values": load_shop_info()}
        if parts == ["documents"]:
            from documents import CATEGORIES
            return {"ok": True, "documents": self.documents.list(), "stats": self.documents.stats(),
                    "categories": CATEGORIES, "ai_ready": getattr(self.responder, "client", None) is not None}
        if len(parts) == 2 and parts[0] == "documents":
            return {"ok": True, "document": self.documents.detail(parts[1])}
        if parts == ["customer-history"]:
            # Đúng phần lịch sử AI sẽ đọc khi khách này bình luận ở phiên LIVE mới.
            live = dict(self.catalog.snapshot or {})
            live["room_id"] = ""
            return {"ok": True, "history": self._customer_history(clean(query.get("key"), 300), live, "", "")}
        if parts == ["version"]:
            from version import app_version
            return {"ok": True, "version": app_version()}
        raise ValueError("not_found")

    def _whisper_downloader(self):
        downloader = getattr(self, "_whisper_download", None)
        if downloader is None:
            from whisper_models import ModelDownloader
            downloader = self._whisper_download = ModelDownloader()
        return downloader

    def system_post(self, path, payload):
        parts = [part for part in path.split("/") if part]
        if parts == ['backup', 'v2', 'start']:
            from backup_crypto import _password_bytes
            password = payload.get('password')
            _password_bytes(password)
            # Password is captured only in the job closure, never persisted in
            # the job database, manifest, progress or result.
            return ReplyEngine._guarded_v2_create(self, password)
        if parts == ['backups', 'delete', 'preview']:
            from backup_delete import preview
            ids = payload.get('ids')
            if payload.get('all') is True:
                if ids is not None:
                    raise ValueError('backup_ids_invalid')
                ids = [path.name for path in BACKUP_DIR.iterdir()
                       if path.is_dir() and not path.name.startswith('.') and (path / 'manifest.json').is_file()]
            return preview(BACKUP_DIR, ids)
        if parts == ['backups', 'delete', 'recover']:
            from backup_delete import recover
            return self._system_jobs().start('backup_delete_recover', lambda progress: recover(BACKUP_DIR, progress))
        if parts == ['backups', 'delete']:
            from backup_delete import preview, execute
            checked = preview(BACKUP_DIR, payload.get('ids'))
            if payload.get('preview_token') != checked['preview_token']:
                raise ValueError('backup_preview_changed')
            return self._system_jobs().start('backup_delete', lambda progress:
                execute(BACKUP_DIR, checked['ids'], checked['preview_token'], progress))
        if parts == ['backup', 'start']:
            requested_parts = self._backup_parts(payload)
            return self._system_jobs().start('backup', lambda progress:
                self.backup_now_and_refresh({'parts': requested_parts}))
        if len(parts) == 3 and parts[0] == "processors":
            plugin_id, action = parts[1], parts[2]
            if action == "settings":
                return self.processors.save(plugin_id, payload.get("values") or {})
            if action == "reset":
                return self.processors.reset(plugin_id)
            if action == "try":
                return self.processors.try_it(plugin_id, payload)
            if action == "notes" and plugin_id == "model-deepseek":
                return self.processors.save_notes(payload.get("text"), bool(payload.get("archive_legacy")))
            if action == "legacy-dismiss" and plugin_id == "model-deepseek":
                return self.processors.dismiss_legacy_prompt()
        if len(parts) == 2 and parts[0] == "whisper":
            import whisper_models
            downloader = self._whisper_downloader()
            action, model_id = parts[1], clean(payload.get("model"), 40)
            if action == "download":
                downloader.start(model_id)
            elif action == "cancel":
                downloader.cancel()
            elif action == "select":
                whisper_models.select(model_id, clean(payload.get("custom_path"), 400))
            elif action == "delete":
                if (downloader.status().get("state") == "downloading"
                        and downloader.status().get("model") == model_id):
                    raise ValueError("download_in_progress")
                whisper_models.delete(model_id)
            elif action == "refresh":
                whisper_models.hardware(refresh=True)
            else:
                raise ValueError("not_found")
            return whisper_models.state(downloader)
        if parts == ["backup"]:
            return self.backup_now_and_refresh(payload)
        if len(parts) == 3 and parts[0] == "backups" and parts[2] == "restore":
            return self.restore_backup({**payload, "backup_id": parts[1]})
        if parts == ["open"]:
            return self.open_target(clean(payload.get("target"), 40))
        if parts == ["shop-info"]:
            return self.save_shop_info(payload.get("values") or {})
        if parts == ["documents", "search"]:
            return {"ok": True, "results": self.documents.search(clean(payload.get("comment"), 500))}
        if len(parts) == 3 and parts[0] == "documents":
            doc_id, action = parts[1], parts[2]
            if action == "update":
                return {"ok": True, "document": self.documents.update(doc_id, payload)}
            if action == "delete":
                return self.documents.delete(doc_id)
            if action == "distill":
                return {"ok": True, "job": self.documents.distill(doc_id, self.learner,
                                                                    self.learner._catalog_brief)}
        if parts == ["profiles", "create"]:
            return self.workspace.create_profile(payload)
        raise ValueError("not_found")

    def sales_playbook_state(self):
        return {"ok": True, "entries": sales_playbook.load(), "max_entries": sales_playbook.MAX_ENTRIES,
                "max_bytes": sales_playbook.MAX_BYTES}

    def sales_playbook_save(self, payload):
        """Ghi sổ tay tình huống (validate + nguyên tử). Sai dạng => ValueError (400)."""
        entries = sales_playbook.save(payload)
        return {"ok": True, "entries": entries, "max_entries": sales_playbook.MAX_ENTRIES,
                "max_bytes": sales_playbook.MAX_BYTES}

    def save_shop_info(self, values):
        clean_values = {key: clean(values.get(key), 1200) for key, _label, _hint in SHOP_INFO_FIELDS
                        if clean(values.get(key), 1200)}
        atomic_json(SHOP_INFO_PATH, clean_values)
        self.shop_info = load_shop_info()
        return {"ok": True, "fields": SHOP_INFO_FIELDS, "values": self.shop_info}

    # Chỉ mở những chỗ biết trước — không bao giờ mở đường dẫn/URL do trang gửi lên.
    OPEN_TARGETS = {
        "webview2": "https://developer.microsoft.com/microsoft-edge/webview2/#download",
        "deepseek_keys": "https://platform.deepseek.com/api_keys",
        "deepseek_topup": "https://platform.deepseek.com/top_up",
        "gemini_keys": "https://aistudio.google.com/apikey",
        "chrome_extensions": "chrome://extensions",
        "ffmpeg": "https://www.gyan.dev/ffmpeg/builds/",
        "sadcaptcha_dashboard": "https://www.sadcaptcha.com/dashboard",
        "telegram_botfather": "https://t.me/BotFather",
        "telegram_userinfobot": "https://t.me/userinfobot",
    }

    def open_target(self, target):
        from config import BACKUP_DIR, DATA_DIR, WHISPER_MODELS_DIR
        folders = {"data": DATA_DIR.parent, "backups": BACKUP_DIR, "logs": LOG_DIR,
                   "models": WHISPER_MODELS_DIR, "raw_stream": LOG_DIR / "raw_stream"}
        if target in folders:
            folder = Path(folders[target])
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))  # noqa: S606 — thư mục cố định của chính ứng dụng
            return {"ok": True, "opened": str(folder)}
        url = self.OPEN_TARGETS.get(target)
        if not url:
            raise ValueError("open_target_unknown")
        import webbrowser
        webbrowser.open(url)
        return {"ok": True, "opened": url}
