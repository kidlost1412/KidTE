"""Trạng thái/điều khiển: health, live control, plugin, dashboard, sao lưu/khôi phục, quyền riêng tư, cảnh báo, heartbeat, hàng đợi gửi, chẩn đoán, cài đặt, gói kiến thức.

Tách nguyên văn từ lớp ReplyEngine trong server.py (không đổi logic).
LƯU Ý: các phương thức ở đây được server.py "nhận nuôi" bằng
engine_mixins.adopt(..., globals()) nên tên toàn cục (ROOT, clean, report_problem,
LIVE_CATALOG_PATH...) được tra trong không gian tên của module server — giữ
nguyên hành vi cũ, kể cả monkeypatch.setattr(server, ...). Không import server ở đây.
"""
from __future__ import annotations

import threading

# ruff: noqa: F821  (tên toàn cục lấy từ server qua adopt)


class EngineStatusMixin:
    def close(self):
        jobs = getattr(self, '_job_store', None)
        if jobs is not None:
            jobs.close()
        if self._shopee_ai is not None:
            self._shopee_ai.close()
        learner = getattr(self, "learner", None)
        if learner:
            learner.close()
        self.delivery_store.close()
        self.comment_queue.close()
        if getattr(self, "privacy", None) is not None:
            self.privacy.close()
        self.memory.close()
        self.metrics.close()

    def shopee_ai(self):
        with self._lock:
            if self._shopee_ai is None:
                self._shopee_ai = ShopeeAiEngine(
                    self, getattr(self.memory, "db_path", MEMORY_DB),
                    MEMORY_JOURNAL_DIR / "shopee",
                    dose_rule_source=lambda sku: self._dose_rules_for(sku))
            return self._shopee_ai

    def _media_bindings(self):
        with self._lock:
            store = getattr(self, "_media_bindings_store", None)
            if store is None:
                from config import MEDIA_BINDINGS_DB
                from obs_source import MediaBindingStore
                store = self._media_bindings_store = MediaBindingStore(MEDIA_BINDINGS_DB)
            return store

    def _library_revision(self):
        try:
            return int(self._media_bindings().library_revision())
        except Exception:
            logging.getLogger("kid-te.core").warning("library_revision unavailable", exc_info=True)
            return 0

    def video_library_changed(self):
        """C04: every library change (upload/import/delete/publish) invalidates media identity."""
        try:
            return self._media_bindings().bump_library_revision()
        except Exception:
            logging.getLogger("kid-te.core").warning("library_revision bump failed", exc_info=True)
            return None

    def _dose_rules_for(self, sku):
        """D01/D05: read-only, short-lived read of the workspace dose_rules table."""
        import dose_rules
        with self.workspace._db() as db:
            return dose_rules.list_rules(db, sku)["rules"]

    def _profile_with_dose_rules(self, profile, skus):
        """Approved rules (never candidates) merged into the profile; unchanged when none."""
        import dose_rules
        try:
            merged = profile
            with self.workspace._db() as db:
                for sku in dict.fromkeys(s for s in skus if isinstance(s, str) and s.strip()):
                    merged = dose_rules.profile_with_approved_rules(db, merged, sku)
            return merged if merged != profile else profile
        except Exception:
            logging.getLogger("kid-te.core").warning("dose rules merge failed", exc_info=True)
            return profile

    @staticmethod
    def _accepts(fn, name):
        try:
            return name in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return False

    def _memory_summary(self):
        # Dashboard poll 5 giay: so lieu thong ke tre toi 45 giay, khong quet
        # toan bang interactions duoi khoa chung moi lan (F1).
        summary = self.memory.data_summary
        return summary(max_age=45) if self._accepts(summary, "max_age") else summary()

    def health(self, wait=True):
        checker = getattr(self.responder, "check_balance", None)
        if callable(checker) and not wait and self._accepts(checker, "wait"):
            # Dashboard khong bao gio cho mang: tra gia tri cache, lam moi nen (F6).
            status = checker(wait=False)
        else:
            status = (checker() if callable(checker)
                      else {"ok": True, "service": "kid-te-ai-standalone"})
        # Mot server CU con song va chiem cong 8793 thi ban moi khong bind
        # duoc, tien ich van noi chuyen voi ban cu — danh muc cu, ma cu, ma
        # khong co dau hieu gi. Ba truong nay lam cai do lo ra ngay.
        try:
            approved = len(getattr(self.knowledge, "records", []) or [])
        except Exception:
            approved = -1
        status.update({
            "pid": os.getpid(),
            "started_at": self.started_at,
            "approved_records": approved,
            "plugins": {
                "loaded": sorted(getattr(self, "plugin_runtime", None).manifests)
                if getattr(self, "plugin_runtime", None) is not None else [],
                "errors": dict(getattr(self, "plugin_errors", {})),
            },
            "safe_mode": bool(getattr(self, "safe_mode", False)),
            "version": _app_version(),
            "capabilities": self.delivery_capabilities(),
        })
        return status

    @staticmethod
    def backend_queue_mode():
        """Công tắc chung: tiện ích lấy việc qua queue/next và kiểm dữ kiện lúc gửi.
        Bật/tắt bằng data/machine/queue_mode.json {"enabled": true}; không cần khởi động lại."""
        if os.environ.get('KIDTE_FACT_REVISION_CHECK') == '1':
            return True
        try:
            return json.loads((ROOT / 'data' / 'machine' / 'queue_mode.json').read_text('utf-8')).get('enabled') is True
        except (OSError, ValueError, AttributeError):
            return False

    _LIVE_CONTROL_LOCK = threading.Lock()

    @staticmethod
    def _live_control_path():
        return ROOT / 'data' / 'machine' / 'live_control.json'

    def _live_control_load(self):
        cached = getattr(self, '_live_control_cache', None)
        if cached is not None:
            return cached
        data = {}
        try:
            raw = json.loads(self._live_control_path().read_text('utf-8'))
            if isinstance(raw, dict):
                data = raw
        except (OSError, ValueError):
            pass
        state = {}
        for platform in ('tiktok', 'shopee'):
            row = data.get(platform) if isinstance(data.get(platform), dict) else {}
            item = {'seq': int(row.get('seq') or 0) if str(row.get('seq') or 0).isdigit() else 0,
                    'ai_enabled': row.get('ai_enabled') if isinstance(row.get('ai_enabled'), bool) else None,
                    'applied': row.get('applied') is True,
                    'control_error': str(row.get('control_error') or '')[:200],
                    'updated_at': row.get('updated_at') if isinstance(row.get('updated_at'), (int, float)) else None}
            if platform == 'tiktok':
                item['auto_pin'] = row.get('auto_pin') if isinstance(row.get('auto_pin'), bool) else None
            state[platform] = item
        self._live_control_cache = state
        return state

    def live_control(self):
        """Lệnh bật/tắt AI/tự ghim từ app (tiện ích áp một lần theo seq)."""
        state = self._live_control_load()
        result = {}
        for platform, row in state.items():
            result[platform] = {key: value for key, value in row.items() if key != 'seq'}
        result['queue_mode'] = bool(self.backend_queue_mode())
        return result

    def set_live_control(self, payload):
        platform = payload.get('platform')
        if platform not in ('tiktok', 'shopee'):
            raise ValueError('invalid_platform')
        changes = {}
        if 'ai_enabled' in payload:
            if not isinstance(payload['ai_enabled'], bool):
                raise ValueError('ai_enabled_invalid')
            changes['ai_enabled'] = payload['ai_enabled']
        if 'auto_pin' in payload:
            if platform != 'tiktok' or not isinstance(payload['auto_pin'], bool):
                raise ValueError('auto_pin_invalid')
            changes['auto_pin'] = payload['auto_pin']
        if not changes:
            raise ValueError('live_control_empty')
        with self._LIVE_CONTROL_LOCK:
            state = {key: dict(value) for key, value in self._live_control_load().items()}
            row = state[platform]
            row.update(changes)
            row['seq'] = int(row.get('seq') or 0) + 1
            row['applied'] = False
            row['control_error'] = ''
            row['updated_at'] = time.time()
            path = self._live_control_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(path, state)
            self._live_control_cache = state
        return {'ok': True, 'live_control': self.live_control()}

    def _live_control_heartbeat(self, platform, payload):
        """Đánh dấu applied khi tiện ích báo control_applied_seq khớp; trả lệnh cho tiện ích."""
        state = self._live_control_load()
        row = state.get(platform)
        if not row or not row.get('seq'):
            return None
        acked = payload.get('control_applied_seq')
        if isinstance(acked, int) and not isinstance(acked, bool) and acked == row['seq'] and not row['applied']:
            with self._LIVE_CONTROL_LOCK:
                state = {key: dict(value) for key, value in state.items()}
                state[platform]['applied'] = True
                state[platform]['control_error'] = clean(payload.get('control_error'), 200)
                try:
                    atomic_json(self._live_control_path(), state)
                except OSError:
                    pass
                self._live_control_cache = state
                row = state[platform]
        control = {'seq': row['seq'], 'ai_enabled': row.get('ai_enabled'),
                   'issued_at': row.get('updated_at')}
        if platform == 'tiktok':
            control['auto_pin'] = row.get('auto_pin')
        return control

    def set_queue_mode(self, payload):
        if not isinstance(payload.get('enabled'), bool):
            raise ValueError('enabled_invalid')
        path = ROOT / 'data' / 'machine' / 'queue_mode.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, {'enabled': payload['enabled']})
        return {'ok': True, 'live_control': self.live_control()}

    @staticmethod
    def delivery_capabilities():
        return {'delivery_store_version': 2, 'message_claim': True,
                'multi_message_reply': True, 'comment_queue_api': True,
                'backend_queue_mode': ReplyEngine.backend_queue_mode()}

    def plugin_state(self):
        if self.plugin_runtime is None:
            return {"ok": True, "plugins": [], "errors": dict(self.plugin_errors)}
        result = self.plugin_runtime.snapshot()
        result["errors"] = dict(self.plugin_errors)
        return result

    def plugin_action(self, payload):
        if self.plugin_runtime is None:
            return {"ok": False, "error": "plugin_runtime_unavailable"}
        action = clean(payload.get("action"), 40)
        plugin_id = clean(payload.get("plugin_id"), 80)
        if action == "start":
            result = self.plugin_runtime.start(plugin_id)
        elif action == "stop":
            result = self.plugin_runtime.stop(plugin_id)
            if plugin_id == "dong-bo-video" and result.get("ok"):
                for platform in PLATFORMS:
                    self.set_video_position({"platform": platform, "available": False})
        elif action == "restart":
            result = self.plugin_runtime.restart(plugin_id)
        elif action == "safe_mode":
            result = self.plugin_runtime.set_safe_mode(payload.get("enabled") is True)
            self.safe_mode = self.plugin_runtime.safe_mode
        else:
            return {"ok": False, "error": "plugin_action_invalid"}
        if result.get("ok") and getattr(self, "workspace", None):
            self.workspace._touch("plugins", "dashboard")
        return result

    def dashboard_state(self):
        """Ảnh chụp vận hành cho UI, không bịa số khi nguồn chưa có."""
        mapping = self.mapping_state()
        products = mapping.get("products") or []
        mapped = sum(1 for item in products if item.get("profile_id"))
        backup = None
        try:
            from backup_policy import latest_backup_time
            from config import BACKUP_DIR
            # Timestamp lookup reads manifests only, never backup payloads.
            cached = getattr(self, "_backup_cache", None)
            if cached and time.monotonic() - cached[0] < 60:
                latest = cached[1]
            else:
                latest = latest_backup_time(BACKUP_DIR)
                self._backup_cache = (time.monotonic(), latest)
            if latest is not None:
                age = (datetime.now(timezone.utc) - latest.astimezone(timezone.utc)).total_seconds()
                backup = {"latest": latest.isoformat(), "overdue": age >= 12 * 3600}
        except Exception:
            backup = None
        health = self._video_health()
        video = {key: health.get(key) for key in (
            "state", "device", "device_id", "video_id", "margin",
            "position_s", "last_seen", "last_lost", "reason",
        )} if health else None
        extension = None
        heartbeat = self.extension_heartbeat
        if isinstance(heartbeat, dict):
            age = max(0.0, time.time() - float(heartbeat.get("received_at", 0)))
            extension = {**heartbeat, "age_s": round(age, 1), "connected": age <= 25}
        # Tong hop tren TOAN BO tap du lieu, khong phai tren mot trang 100 dong.
        # Ban cu lay list(..., limit=100) roi dem len(items): voi 234 hoi thoai
        # thi raw_stream_rows, business_session_count, ai_replied, manual_replied
        # va unanswered deu bi cat cung o 100.
        totals, review_jobs = {}, []
        video_library = None
        session_error = ""
        try:
            totals = self.workspace.totals()
            review_jobs = [row for row in self.video_analysis_jobs().get("jobs", [])
                           if row.get("state") == "review"]
            video_rows = self.workspace._videos()
            active_video_ids = {row["id"] for row in video_rows}
            video_library = {"count": len(video_rows),
                             "ready": sum(1 for row in video_rows if (row.get("events") or 0) > 0)}
            if video and video.get("video_id") not in active_video_ids:
                video = {**video, "state": "unavailable", "reason": "video_no_longer_in_library",
                         "video_id": "", "position_s": None, "margin": None}
        except Exception as exc:
            # Khong nuot loi thanh so 0: UI phai phan biet "chua doc duoc"
            # voi "that su bang 0".
            session_error = f"{type(exc).__name__}: {exc}"[:200]
        state = {
            "ok": True,
            "backend": {"state": "ready", "started_at": self.started_at},
            "captcha": self.captcha.status(),
            "ai": self.health(wait=False),
            "plugins": self.plugin_state(),
            "catalog": {
                "total": len(products), "mapped": mapped,
                "pending": len(products) - mapped,
                "unmapped": len(products) - mapped,
                "approved_profiles": len(mapping.get("profiles") or []),
                "synced_at": mapping.get("synced_at") or None,
            },
            "session": {
                "raw_stream_rows": totals.get("conversations"),
                "raw_file_rows": self.raw_stream.count,
                "business_session_count": totals.get("sessions"),
                "ai_replied": totals.get("ai_replied"),
                "manual_replied": totals.get("manual_replied"),
                "unanswered": totals.get("unanswered"),
                "todo": totals.get("todo"),
                "review": totals.get("review"),
                "missing_count": self.missing.count,
                "memory": self._memory_summary(),
                "metrics": self.metrics.snapshot(),
                "scope": "all_time",
                "counts_available": not session_error,
                "counts_error": session_error,
            },
            "backup": backup,
            "video": video,
            "live_channels": self.live_channels(),
            "video_review": {"pending_jobs": len(review_jobs),
                             "first_job_id": str(review_jobs[0].get("job_id") or "") if review_jobs else "",
                             "pending_events": sum((row.get("suggestions") or 0) for row in review_jobs)},
            "extension": extension,
            "extension_pairing": self.extension_pairing_state(),
            "recent_activity": self.workspace.recent_activity(),
            "learning": self._learning_summary(),
            "ai_queue": self._ai_queue_summary(),
            "restore_v2_enabled": self._restore_v2_flag(),
            "privacy": self._privacy_status(),
            "live_control": self.live_control(),
            "live_alerts": self.live_alerts(),
        }
        try:
            from live_readiness import build_readiness
            state["readiness"] = build_readiness(state, {"video_library": video_library,
                                                         "whisper_ready": self._whisper_ready_cached()})
        except Exception as exc:
            state["readiness"] = {"ok": False, "error": type(exc).__name__}
        return state

    def _whisper_ready_cached(self):
        """Model chép lời đã có chưa; đọc đĩa tối đa 1 lần/60 s."""
        cached = getattr(self, "_whisper_ready_cache", None)
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1]
        try:
            import whisper_models
            ready = bool(whisper_models.active_model_path())
        except Exception:
            ready = None
        self._whisper_ready_cache = (time.monotonic(), ready)
        return ready

    def _privacy_status(self):
        privacy = getattr(self, "privacy", None)
        if privacy is None or getattr(privacy, "memory_available", True):
            return {"memory_available": True, "warning": ""}
        return {"memory_available": False,
                "warning": "Bộ nhớ khách tạm tắt: khoá quyền riêng tư không đọc được. "
                           "AI vẫn trả lời nhưng không nhớ khách; kiểm tra tệp data/machine/privacy_key.",
                "detail": str(getattr(privacy, "unavailable_reason", ""))[:200]}

    def _restore_v2_flag(self):
        try:
            from restore_coordinator import restore_v2_enabled
            return bool(restore_v2_enabled(DATA_DIR / 'machine' / 'restore_v2.json'))
        except Exception:
            return False

    def _ai_queue_summary(self, window_s=86400):
        """Additive: recent-window queue counts, one grouped SQL per store."""
        empty = lambda: {"pending": 0, "oldest_age_s": 0, "expired": 0, "unknown": 0}
        by_platform = {"tiktok": empty(), "shopee": empty()}
        try:
            rows, now = self.comment_queue.window_counts(window_s)
            for row in rows:
                bucket = by_platform.get(row["platform"])
                if bucket is None:
                    continue
                if row["state"] in ("queued", "generating", "ready", "sending", "partially_sent"):
                    bucket["pending"] += row["n"]
                    if row["state"] == "queued" and row["oldest"] is not None:
                        bucket["oldest_age_s"] = max(bucket["oldest_age_s"], round(max(0, now - row["oldest"])))
                elif row["state"] == "expired":
                    bucket["expired"] += row["n"]
                elif row["state"] == "unknown":
                    bucket["unknown"] += row["n"]
            stores = {"tiktok": self.delivery_store, "shopee": getattr(getattr(self, "_shopee_ai", None), "delivery_store", None)}
            for name, store in stores.items():
                if store is not None:
                    by_platform[name]["unknown"] += store.unknown_count(window_s)
        except Exception:
            _note_swallowed("delivery_health_count_failed", once=True)
        total = {key: sum(b[key] for b in by_platform.values()) for key in ("pending", "expired", "unknown")}
        total["oldest_age_s"] = max(b["oldest_age_s"] for b in by_platform.values())
        return {**total, "by_platform": by_platform, "queue_mode": bool(self.backend_queue_mode())}

    @staticmethod
    def _video_health(max_age_s=30):
        """Trạng thái dịch vụ đồng bộ video. File cũ hơn 30 giây = dịch vụ đã dừng/chết:
        bản cũ vẫn đọc nó nên Tổng quan báo "Đang phát" mãi sau khi dịch vụ tắt."""
        path = ROOT / "runtime" / "services" / "dong-bo-video.health.json"
        try:
            if time.time() - path.stat().st_mtime > max_age_s:
                return {}
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _learning_summary(self):
        learning = getattr(self, "learning", None)
        if learning is None:
            return None
        try:
            stats = learning.stats()
            running = [r for r in learning.runs(limit=5) if r["status"] in {"queued", "running"}]
            return {"suggested": stats["by_status"]["suggested"], "active": stats["by_status"]["active"],
                    "used_total": stats["used_total"], "busy": bool(running),
                    "persona": (learning.persona() or {}).get("name", "")}
        except Exception:
            return None

    @staticmethod
    def backup_now(parts=None):
        from backup_policy import create_operational_backup
        return {"ok": True, **create_operational_backup(parts=parts)}

    @staticmethod
    def _backup_parts(payload):
        parts = payload.get("parts")
        if parts is None:
            return None
        if not isinstance(parts, list):
            raise ValueError("backup_parts_invalid")
        return [clean(item, 40) for item in parts]

    def derived_stores(self):
        return {"learning": getattr(self, "learning", None),
                "workspace": getattr(self, "workspace", None),
                "history": getattr(self, "customer_history", None)}

    def privacy_forget(self, payload):
        key = payload.get("customer_key") if isinstance(payload, dict) else None
        if not isinstance(key, str) or not key.strip() or len(key) > 240:
            raise ValueError("privacy_customer_key_invalid")
        return {"ok": True, **self.privacy.forget(key, memory=self.memory, derived=self.derived_stores())}

    def privacy_purge_ack(self, payload):
        if not isinstance(payload, dict) or payload.get("platform") not in PLATFORMS:
            raise ValueError("privacy_purge_ack_invalid")
        return {"ok": True, **self.privacy.ack_purges(payload.get("ids"), payload["platform"])}

    def privacy_request(self, request_id):
        found = self.privacy.request(request_id)
        if found is None:
            raise ValueError("privacy_request_not_found")
        return {"ok": True, **found}

    def _maintenance_block(self):
        maintenance = getattr(self, 'maintenance', None)
        return maintenance.blocked() if maintenance is not None else None

    def _ai_disabled_by_restore(self):
        maintenance = getattr(self, 'maintenance', None)
        return bool(maintenance is not None and maintenance.ai_disabled)

    def reenable_ai_after_restore(self, payload=None):
        self.maintenance.reenable()
        return {'ok': True, 'ai_disabled': False}

    def _restore_writers(self):
        """Registry of every writer that must be closed before the data dir is swapped."""
        from restore_coordinator import WriterRegistry
        registry = WriterRegistry()

        def closer(owner):
            def run():
                target = getattr(self, owner, None)
                if target is not None:
                    target.close()
            return run
        for name in ('memory', 'delivery_store', 'comment_queue', 'learning', 'privacy'):
            registry.register(name, closer(name))
        registry.register('workspace', lambda: getattr(getattr(self, 'workspace', None), 'close', lambda: None)())
        registry.register('captcha_uploads', lambda: getattr(getattr(self, 'captcha', None), 'close_uploads', lambda: None)())
        registry.register('media_bindings', lambda: setattr(self, '_media_bindings_store', None))
        registry.register('shopee_ai', lambda: (self._shopee_ai.close() if self._shopee_ai is not None else None,
                                                setattr(self, '_shopee_ai', None)))
        return registry

    def _restore_migrate(self, staged):
        """Init each store against the STAGED dir so schema migrations run before the swap."""
        import tempfile
        from comment_queue import CommentQueue
        from learning import LearningStore
        staged = Path(staged)
        memory_db = staged / MEMORY_DB.relative_to(DATA_DIR)
        if memory_db.is_file():
            with tempfile.TemporaryDirectory(prefix='kidte-migrate-') as journal:
                CustomerMemory(memory_db, journal, channel='restore-migrate').close()
            for factory in (DeliveryStore, CommentQueue):
                factory(memory_db).close()
        learning_db = staged / 'ai_learning.sqlite3'
        if learning_db.is_file():
            LearningStore(learning_db).close()

    def _restore_reinit(self):
        from comment_queue import CommentQueue
        from learning import LearningStore
        self.memory = CustomerMemory(MEMORY_DB, MEMORY_JOURNAL_DIR, channel="live-re-ai",
                                     max_prior_turns=MEMORY_MAX_TURNS)
        self.memory.privacy = PrivacyService(self.memory.db_path, ROOT / "data" / "machine" / "privacy_key")
        self.privacy = self.memory.privacy
        self.delivery_store = DeliveryStore(self.memory.db_path, privacy=self.privacy)
        self.comment_queue = CommentQueue(self.memory.db_path, privacy=self.privacy)
        self.learning = LearningStore(self.learning.db_path)
        self.learning.privacy = self.privacy
        self.learner.store = self.learning
        self.workspace = WorkspaceStore(self, root=self.workspace.root)
        self.workspace.privacy = self.privacy
        self.memory.derived_stores = self.derived_stores
        self.customer_history = CustomerHistory(getattr(self.raw_stream, "dir", LOG_DIR / "raw_stream"),
                                                memory=self.memory)
        self._media_bindings_store = None
        self._shopee_ai = None
        self._backup_cache = None

    def restore_backup_v2(self, payload):
        from backup import BackupError
        from backup_v2 import BackupV2
        from restore_coordinator import RestoreCoordinator, RestoreError, restore_v2_enabled
        if not restore_v2_enabled(DATA_DIR / 'machine' / 'restore_v2.json'):
            raise ValueError('restore_v2_disabled')
        backup_id = clean(payload.get('backup_id'), 120)
        password = payload.get('password')
        if not isinstance(password, str) or not password:
            raise ValueError('backup_password_required')
        coordinator = RestoreCoordinator(
            data_dir=DATA_DIR, store=BackupV2(ROOT / 'backups' / 'v2'), maintenance=self.maintenance,
            is_live=self._is_live_running, writers=self._restore_writers(),
            reinit=self._restore_reinit, migrate=self._restore_migrate,
            privacy_db=self.memory.db_path, privacy_key_path=ROOT / 'data' / 'machine' / 'privacy_key')
        try:
            return coordinator.restore(backup_id, password)
        except (RestoreError, BackupError) as exc:
            raise ValueError(str(exc)) from None

    def restore_backup(self, payload):
        if self._is_live_running():
            raise ValueError('maintenance_required')
        if payload.get('v2') is True:
            return self.restore_backup_v2(payload)
        from backup_policy import restore_operational_backup
        result = restore_operational_backup(clean(payload.get("backup_id"), 80),
                                            parts=self._backup_parts(payload))
        # Nạp lại mọi thứ đọc từ các file vừa được ghi đè.
        self.knowledge.reload()
        self.shop_info = load_shop_info()
        learning = getattr(self, "learning", None)
        if learning is not None:
            learning._bump()
        processors = getattr(self, "processors", None)
        if processors is not None:
            processors.store.reload()
            processors.apply_all()
        documents = getattr(self, "documents", None)
        if documents is not None:
            documents._invalidate()
        self.workspace._touch("all", "dashboard")
        self._backup_cache = None
        return result

    def backup_now_and_refresh(self, payload=None):
        result = self.backup_now(self._backup_parts(payload or {}))
        self._backup_cache = None
        return result

    def _live_alerts(self):
        alerts = getattr(self, "_live_alerts_state", None)
        if alerts is None:
            from live_alerts import LiveAlerts
            alerts = self._live_alerts_state = LiveAlerts()
        return alerts

    def _note_ai_failure(self, reasons, http_status=None, platform="tiktok"):
        """O3: đếm lỗi AI liên tiếp; lần đầu chuyển sang báo động thì ghi nhật ký + thông báo Windows."""
        before = self._live_alerts().ai_alert()
        alert = self._live_alerts().ai_failed(reasons, http_status, platform)
        if alert and not before:
            report_problem("live_alert_ai", f"{alert['message']} {alert['action']}", severity="error")
            if alert["code"] in ("ai_out_of_balance", "ai_key_rejected"):
                # Số dư/khoá đã đổi: cache số dư hết hạn để pill AI kiểm lại ngay lần tới.
                responder = getattr(self, "responder", None)
                lock = getattr(responder, "_balance_lock", None)
                if lock is not None and hasattr(responder, "_balance_checked_at"):
                    with lock:
                        responder._balance_checked_at = 0.0

    def _note_comment_seen(self, platform):
        """O4: mốc bình luận cuối backend nhận được (Shopee không gửi trạng thái bộ thu)."""
        seen = getattr(self, "_comment_seen_at", None)
        if seen is None:
            seen = self._comment_seen_at = {}
        seen[platform] = time.time()

    def live_alerts(self):
        """Báo động đang có khi LIVE (O3 AI hỏng, O4 không nhận bình luận TikTok/Shopee)."""
        from config import COMMENT_SILENCE_ALERT_S
        out = []
        ai = self._live_alerts().ai_alert()
        if ai:
            out.append(ai)
        heartbeats = getattr(self, "platform_heartbeats", {}) or {}
        heartbeat = heartbeats.get("tiktok") or {}
        comments = self._live_alerts().comment_alert(heartbeat, COMMENT_SILENCE_ALERT_S,
                                                     heartbeat.get("live_since") or 0)
        if comments:
            out.append(comments)
        shopee = heartbeats.get("shopee") or {}
        # Shopee: tiện ích chỉ đẩy bình luận về backend khi bật AI; tắt AI thì không đủ căn cứ.
        if shopee.get("live_running") is True and shopee.get("ai_enabled") is True:
            last = (getattr(self, "_comment_seen_at", None) or {}).get("shopee") or 0
            synthetic = {**shopee, "collector": {"ok": True, "bridge": True,
                                                 "last_comment_at_ms": int(last * 1000)}}
            comments = self._live_alerts().comment_alert(synthetic, COMMENT_SILENCE_ALERT_S,
                                                         shopee.get("live_since") or 0, platform="shopee")
            if comments:
                out.append(comments)
        return out

    def heartbeat(self, payload):
        platform = payload.get("platform", "tiktok")
        if platform not in PLATFORMS:
            raise ValueError("invalid_platform")
        allowed = {
            "live_running": payload.get("live_running") is True,
            "ai_enabled": payload.get("ai_enabled") is True and not self._ai_disabled_by_restore(),
            "ai_ready": payload.get("ai_ready") is True,
            "observer": payload.get("observer") is True,
            "pin_running": payload.get("pin_running") is True if "pin_running" in payload else None,
            "reply_queue": max(0, min(int(payload.get("reply_queue", 0) or 0), 10000)),
            "observe_queue": max(0, min(int(payload.get("observe_queue", 0) or 0), 10000)),
            "send_paused_until_ms": max(0, int(payload.get("send_paused_until_ms", 0) or 0)),
            "send_pause_reason": clean(payload.get("send_pause_reason"), 160),
            "session_id": clean(payload.get("session_id"), 100),
            "host": _clean_host(payload.get("host")),
            "tab_id": clean(payload.get("tab_id"), 64),
            "received_at": time.time(),
        }
        collector = payload.get("collector")
        if isinstance(collector, dict):
            # O4: trạng thái thu bình luận của tiện ích (cầu ChatMessage + bộ thu).
            try:
                last_ms = max(0, int(collector.get("last_comment_at_ms") or 0))
            except (TypeError, ValueError):
                last_ms = 0
            allowed["collector"] = {"ok": collector.get("ok") is not False,
                                    "bridge": collector.get("bridge") is not False,
                                    "last_comment_at_ms": last_ms}
        previous = self.platform_heartbeats.get(platform) or {}
        # Mốc bắt đầu LIVE (theo heartbeat) để tính "bao lâu chưa có bình luận".
        if allowed["live_running"]:
            allowed["live_since"] = (previous.get("live_since") if previous.get("live_running")
                                     else None) or allowed["received_at"]
        # E2: mọi tab shop.tiktok.com đều có thể gửi heartbeat. Tab KHÔNG điều khiển
        # (session rỗng / chưa sẵn sàng) không được đè heartbeat còn mới của tab điều
        # khiển — nếu không claim/queue/next trả delivery_control_inactive. Cùng tab
        # (tab_id trùng) luôn cập nhật, để tắt AI có hiệu lực ngay.
        weak = not allowed["session_id"] or not allowed["ai_ready"]
        # Hai heartbeat cùng không có tab_id (tiện ích cũ) coi như cùng tab: giữ hành vi cũ.
        same_tab = allowed["tab_id"] == (previous.get("tab_id") or "")
        previous_fresh = time.time() - float(previous.get("received_at") or 0) < 30
        ignored = (weak and not same_tab and previous_fresh
                   and bool(previous.get("session_id")) and previous.get("ai_ready") is True)
        response = {"ok": True, "received_at": allowed["received_at"]}
        if ignored:
            response["ignored"] = "not_controller"
        else:
            self.platform_heartbeats[platform] = allowed
            if platform == "tiktok":
                self.extension_heartbeat = allowed
        try:
            purge = self.privacy.pending_purges(platform)
        except Exception:
            purge = []
        if purge:
            response["privacy_purge"] = purge
        try:
            control = self._live_control_heartbeat(platform, payload)
        except Exception:
            control = None
        if control:
            response["control"] = control
        return response

    def delivery_operation(self, platform, operation, payload):
        if operation in {'claim', 'dispatched'}:
            blocked = self._maintenance_block()
            if blocked:
                return blocked
        if platform not in PLATFORMS or not isinstance(payload, dict):
            raise ValueError('delivery_request_invalid')
        keys = ('turn_id', 'room_id') if operation == 'status' else ('turn_id', 'room_id', 'message_id')
        if any(not isinstance(payload.get(key), str) or not payload[key] or len(payload[key]) > 160
               for key in keys):
            raise ValueError('delivery_identity_invalid')
        store = self.delivery_store if platform == 'tiktok' else self.shopee_ai().delivery_store
        turn = store.reconcile(payload['turn_id'])
        if not turn or (turn['platform'], turn['room_id']) != (platform, payload['room_id']):
            return {'ok': False, 'error': 'delivery_identity_mismatch'}
        if operation == 'status':
            return {'ok': True, 'turn': turn}
        if operation in {'claim', 'dispatched'}:
            heartbeat = self.platform_heartbeats.get(platform, {})
            if (not heartbeat.get('live_running') or not heartbeat.get('ai_enabled')
                    or not heartbeat.get('ai_ready') or time.time() - heartbeat.get('received_at', 0) > 30
                    or heartbeat.get('session_id') != payload['room_id']):
                return {'ok': False, 'error': 'delivery_control_inactive'}
            if operation == 'claim':
                # Tắt mặc định: lượt soạn lại chỉ được gửi khi tiện ích lấy việc qua queue/next;
                # ở đường cũ, kiểm tra này sẽ huỷ câu trả lời mà không có lượt thay thế.
                check = self.backend_queue_mode()
                stored = json.loads(turn['fact_revision']) if check and turn.get('fact_revision') else None
                facts = self._current_facts(platform, payload['room_id'], stored) if stored else None
                claim = store.claim(payload['turn_id'], payload['message_id'], platform,
                                    payload['room_id'], current_facts=facts)
                if claim.get('error') in {'revision_changed', 'revision_stale'}:
                    recomposed = claim['recompose'] and self._recompose(platform, turn)
                    if not recomposed:
                        store.cancel_turn(payload['turn_id'], 'revision_stale')
                        self.comment_queue.settle(payload['turn_id'], 'cancelled')
                        claim['error'] = 'revision_stale'
                return claim
            token = payload.get('claim_token')
            if not isinstance(token, str) or not token:
                raise ValueError('delivery_claim_token_invalid')
            if not any(m['message_id'] == payload['message_id'] for m in turn['messages']):
                return {'ok': False, 'error': 'delivery_message_not_found'}
            return {'ok': store.dispatched(payload['message_id'], token)}
        if operation == 'receipt':
            confirmed = store.confirm_message(payload['turn_id'], payload['message_id'], platform,
                payload['room_id'], payload.get('content_hash'), payload.get('receipt'))
            if confirmed:
                state = store.get(payload['turn_id'])['state']
                if state == 'sent':
                    memory = self.memory if platform == 'tiktok' else self.shopee_ai().memory
                    final_message = store.get(payload['turn_id'])['messages'][-1]
                    confirmed = memory.confirm_reply_sent(turn['interaction_id'], turn['turn_id'],
                        json.loads(final_message['receipt_json']), allow_previous_session=True)
                elif state == 'partially_sent':
                    memory = self.memory if platform == 'tiktok' else self.shopee_ai().memory
                    confirmed = memory.mark_reply_delivery(turn['interaction_id'], turn['turn_id'],
                        'partially_sent', allow_previous_session=True)
                if confirmed:
                    sent_times = [m['sent_at'] for m in store.get(payload['turn_id'])['messages'] if m['sent_at'] is not None]
                    self.comment_queue.settle(payload['turn_id'], state,
                        first_sent_at=min(sent_times) if sent_times else None)
            return {'ok': confirmed}
        if operation == 'failure':
            marked = store.mark_message_unknown(payload['turn_id'], payload['message_id'], platform,
                payload['room_id'], payload.get('content_hash'))
            if marked:
                state = store.get(payload['turn_id'])['state']
                memory = self.memory if platform == 'tiktok' else self.shopee_ai().memory
                memory.mark_reply_delivery(turn['interaction_id'], turn['turn_id'], state,
                    'message_delivery_unknown', allow_previous_session=True)
                sent_times = [m['sent_at'] for m in store.get(payload['turn_id'])['messages'] if m['sent_at'] is not None]
                self.comment_queue.settle(payload['turn_id'], state,
                    first_sent_at=min(sent_times) if sent_times else None)
            return {'ok': marked}
        raise ValueError('delivery_operation_invalid')

    def _enqueue_one(self, platform, room, comment):
        if not isinstance(comment, dict):
            raise ValueError('queue_payload_invalid')
        user = comment.get('user_id', '')
        sec_uid = comment.get('sec_uid', '') if platform == 'tiktok' else ''
        unique_id = comment.get('unique_id', '') if platform == 'tiktok' else ''
        if not any((user, sec_uid, unique_id)) or any(not isinstance(v, str) for v in (user, sec_uid, unique_id)):
            raise ValueError('queue_customer_invalid')
        if platform == 'tiktok' and not isinstance(comment.get('live_context', {}), dict):
            raise ValueError('queue_live_context_invalid')
        supplied_room = comment.get('session_id') if platform == 'shopee' else (comment.get('live_context') or {}).get('room_id')
        if supplied_room and supplied_room != room:
            raise ValueError('queue_room_mismatch')
        memory = self.memory if platform == 'tiktok' else self.shopee_ai().memory
        customer_key = memory.customer_key(platform_user_id=user, sec_uid=sec_uid,
            unique_id=unique_id, platform=platform)[0]
        return self.comment_queue.enqueue(platform, room, customer_key, comment)

    def queue_operation(self, platform, operation, payload):
        if operation in {'enqueue', 'next'}:
            blocked = self._maintenance_block()
            if blocked:
                return blocked
        room = payload.get('room_id')
        if not isinstance(room, str) or not room or len(room) > 160:
            raise ValueError('queue_room_invalid')
        if operation == 'status':
            return {'ok': True, **self.comment_queue.summary(platform)}
        if operation == 'cancel':
            comment_id = payload.get('comment_id')
            return {'ok': True, **self.comment_queue.cancel(platform, room, comment_id)}
        if operation == 'enqueue':
            self._note_comment_seen(platform)
            batch = payload.get('comment_payloads')
            if batch is not None:
                # Lô nhiều comment: giữ đúng thứ tự FIFO, mỗi comment có kết quả riêng.
                if not isinstance(batch, list) or not batch or len(batch) > 50:
                    raise ValueError('queue_payload_invalid')
                results = []
                for item in batch:
                    try:
                        results.append({'ok': True, **self._enqueue_one(platform, room, item)})
                    except ValueError as error:
                        results.append({'ok': False, 'error': str(error)})
                return {'ok': True, 'results': results}
            return {'ok': True, **self._enqueue_one(platform, room, payload.get('comment_payload'))}
        if operation == 'next':
            heartbeat = self.platform_heartbeats.get(platform, {})
            if (not heartbeat.get('live_running') or not heartbeat.get('ai_enabled')
                    or not heartbeat.get('ai_ready') or time.time() - heartbeat.get('received_at', 0) > 30
                    or heartbeat.get('session_id') != room):
                return {'ok': False, 'error': 'delivery_control_inactive'}
            ready = self.comment_queue.ready(platform, room)
            if ready:
                turn_id = ready['result'].get('turn_id', ready['result'].get('reply_id', ''))
                store = self.delivery_store if platform == 'tiktok' else self.shopee_ai().delivery_store
                turn = store.reconcile(turn_id)
                if turn and turn['state'] != 'ready':
                    sent_times = [m['sent_at'] for m in turn['messages'] if m['sent_at'] is not None]
                    # Sending is recovered through message status/claim; a lost
                    # dispatch becomes unknown and must not call the model again.
                    if turn['state'] != 'sending':
                        self.comment_queue.settle(turn_id, turn['state'], first_sent_at=min(sent_times) if sent_times else None)
                    ready = self.comment_queue.ready(platform, room)
                if ready:
                    return {'ok': True, 'available': True, 'recovered': True, **ready}
            work = self.comment_queue.claim_next(platform, room)
            if work is None:
                return {'ok': True, 'available': False}
            try:
                comment = work['payload']
                if platform == 'shopee':
                    comment = {**comment, 'session_id': room}
                    result = self.shopee_ai().reply(comment)
                else:
                    comment = {**comment, 'live_context': {**(comment.get('live_context') or {}), 'room_id': room}}
                    result = self.handle(comment)
                accepted = self.comment_queue.finish_generation(work['queue_id'], work['worker_token'], result)
                if not accepted:
                    return {'ok': True, 'available': False, 'reason': 'queue_invalidated'}
                if time.time() >= work['expires_at']:
                    result = {'ok': True, 'should_send': False, 'reason': 'expired'}
                return {'ok': True, 'available': True, 'queue_id': work['queue_id'],
                        'result': result, 'source_comment': work['payload']}
            except Exception:
                self.comment_queue.finish_generation(work['queue_id'], work['worker_token'],
                    {'ok': False, 'should_send': False, 'reason': 'generation_failed'})
                raise
        raise ValueError('queue_operation_invalid')

    def queued_reply(self, platform, payload):
        blocked = self._maintenance_block()
        if blocked:
            return blocked
        if platform == 'tiktok' and not isinstance(payload.get('live_context'), dict):
            raise ValueError('queue_live_context_invalid')
        room = payload.get('session_id') if platform == 'shopee' else (payload.get('live_context') or {}).get('room_id')
        queued = self.queue_operation(platform, 'enqueue', {'room_id': room, 'comment_payload': payload})
        if queued.get('reason'):
            return {'ok': True, 'should_send': False, 'reason': queued['reason']}
        if queued.get('state') in {'expired', 'cancelled', 'sent', 'unknown'}:
            return {'ok': True, 'should_send': False, 'reason': queued['state']}
        selected = self.queue_operation(platform, 'next', {'room_id': room})
        if not selected.get('ok'):
            return selected
        # Comment không phải đầu hàng: chờ, không nhận nhầm lượt trả lời của comment khác.
        if not selected.get('available') or selected.get('queue_id') != queued.get('queue_id'):
            return {'ok': True, 'should_send': False, 'reason': 'queue_waiting',
                    'queue_id': queued.get('queue_id'), 'state': queued.get('state')}
        return {**selected['result'], 'source_comment': selected['source_comment']}

    def _network_check(self):
        probes = [_net.probe(host) for host in ("api.deepseek.com", "huggingface.co")]
        failed = [row for row in probes if not row.get("ok")]
        proxy = _net.proxy_info()
        lines = []
        for row in probes:
            if row.get("ok"):
                lines.append(f"{row['host']}: DNS + TLS đều qua")
            else:
                lines.append(f"{row['host']}: lỗi ở bước {'DNS' if row.get('step') == 'dns' else 'TLS/kết nối'}"
                             f" — {row.get('message', '')} [{row.get('detail', '')}]")
        lines.append("Proxy: " + (", ".join(f"{k}={v}" for k, v in proxy.items()) if proxy else "không cấu hình"))
        return not failed, "; ".join(lines)

    def diagnostics_state(self):
        from diagnostics import fix, run_setup_checks
        result = run_setup_checks(key_configured=getattr(self.responder, "client", None) is not None)
        checks = list(result.get("checks") or [])

        def add(check_id, label, ok, detail, impact, how):
            row = {"id": check_id, "label": label, "status": "ok" if ok else "warn",
                   "detail": detail, "impact": "" if ok else impact, "fixable": False}
            if not ok:
                row["fix"] = how
            checks.append(row)

        checker = getattr(self.responder, "check_balance", None)
        if callable(checker) and getattr(self.responder, "client", None) is not None:
            balance = checker()
            reason = balance.get("reason", "")
            add("deepseek-balance", "Số dư DeepSeek", bool(balance.get("ok")),
                "Còn tiền, sẵn sàng" if balance.get("ok") else balance.get("error", "Không kiểm tra được"),
                "AI không trả lời được khách",
                fix("Nạp thêm tiền vào tài khoản DeepSeek." if reason == "deepseek_insufficient_balance"
                    else "Kiểm tra mạng Internet của máy, rồi bấm Kiểm tra lại.",
                    "Mở trang nạp tiền" if reason == "deepseek_insufficient_balance" else "",
                    **({"open": "deepseek_topup"} if reason == "deepseek_insufficient_balance" else {})))
        try:
            net_ok, net_detail = self._network_check()
        except Exception as exc:
            net_ok, net_detail = False, f"Không kiểm tra được: {type(exc).__name__}"
        add("network", "Kết nối mạng", net_ok, net_detail,
            "Không kiểm tra được khóa DeepSeek, không tải được model giọng nói",
            fix("Xem dòng lỗi: DNS = mạng/DNS; TLS = diệt virus/proxy quét HTTPS hoặc Windows thiếu chứng chỉ gốc."
                " Mở trình duyệt vào https://api.deepseek.com một lần rồi bấm Kiểm tra lại."))
        plugins = {row.get("id"): row for row in self.plugin_state().get("plugins", [])}
        video_sync = plugins.get("dong-bo-video") or {}
        running = video_sync.get("state") == "dang_chay"
        add("runtime-video-sync", "Dịch vụ đồng bộ video", running,
            "Đang nghe âm thanh" if running else "Đang tắt",
            "Tiện ích không tự ghim sản phẩm theo lịch của video",
            fix("Bật dịch vụ trước khi phát video LIVE.", "Mở Trợ lý tự động", page="automation", sub=""))
        heartbeat = self.extension_heartbeat or {}
        age = time.time() - float(heartbeat.get("received_at") or 0)
        connected = age <= 25
        add("runtime-extension", "Tiện ích Chrome đang kết nối", connected,
            ("Đã kết nối; LIVE đang bật" if heartbeat.get("live_running") else
             "Đã kết nối; LIVE và AI đang tắt") if connected else "Chưa nhận được tín hiệu",
            "Không gửi được câu trả lời và không ghim được sản phẩm",
            fix("Mở Chrome → trang TikTok LIVE Manager. Chưa cài tiện ích thì bấm nút bên cạnh.",
                "Hướng dẫn cài tiện ích", page="system", sub="setup"))
        if running:
            health = self._video_health()
            healthy_audio = (health.get("state") in {"watching", "detected", "running"}
                             and bool(health.get("device_id")))
            add("runtime-audio-signal", "Âm thanh video đang phát", healthy_audio,
                "Đang nghe được âm thanh video" if healthy_audio else "Chưa nghe được",
                "Chưa xác định được video và vị trí đang phát",
                fix("Phát video, rồi vào Âm thanh bấm \"Nghe thử\" để chọn đúng nguồn.",
                    "Mở Âm thanh", page="system", sub="audio"))
        result["checks"] = checks
        result["counts"] = {status: sum(row.get("status") == status for row in checks)
                            for status in ("ok", "warn", "error")}
        result["ok"] = result["counts"]["error"] == 0
        return result

    @staticmethod
    def diagnostics_fix():
        from diagnostics import fix_safe_setup
        return fix_safe_setup()

    def support_report_preview(self):
        from support_report import preview_support_report
        return preview_support_report()

    def support_report_create(self):
        from support_report import create_support_report
        runtime = {"plugins": self.plugin_state(), "safe_mode": self.safe_mode,
                   "started_at": self.started_at}
        return create_support_report(runtime)

    def audio_report(self):
        """Báo cáo thiết bị âm thanh + đánh dấu thiết bị đã lưu không còn trên máy."""
        try:
            from audio_capture import enumerate_report, is_forbidden_device
            report = enumerate_report()
        except Exception as exc:
            report = {"devices": [], "error_code": "enumeration_failed", "error_detail": type(exc).__name__,
                      "soundcard_ok": False}
            is_forbidden_device = lambda _item: False
        report["devices"] = [{"id": item["id"], "name": item["name"],
                              "loopback": bool(item.get("loopback")),
                              "disabled": is_forbidden_device(item),
                              "reason": "Dock USB bị cấm để tránh lỗi giữa LIVE"
                                        if is_forbidden_device(item) else ""}
                             for item in report.get("devices") or []]
        ids = {item["id"] for item in report["devices"]}
        missing = []
        saved = str(getattr(self, "audio_device_id", AUDIO_DEVICE_ID) or "")
        if saved and saved != "auto" and not saved.isdigit() and saved not in ids:
            missing.append({"source": "audio_device_id", "id": saved})
        mode_device_invalid = False
        try:
            tiktok = (self.live_sources.read(public=True) or {}).get("tiktok") or {}
        except Exception:
            tiktok = {}
        device_id = str(tiktok.get("device_id") or "")
        if device_id and device_id not in ids:
            missing.append({"source": "live_sources.tiktok", "id": device_id})
        if tiktok.get("mode") == "device":
            usable = {item["id"] for item in report["devices"] if item["loopback"] and not item["disabled"]}
            mode_device_invalid = device_id not in usable
        report["missing_saved_devices"] = missing
        report["suggested_device"] = None
        if mode_device_invalid:
            name = report.get("default_speaker_name")
            candidates = [item for item in report["devices"] if item["loopback"] and not item["disabled"]]
            pick = next((item for item in candidates if name and item["name"] == name), None) or                 (candidates[0] if candidates else None)
            if pick:
                report["suggested_device"] = {"id": pick["id"], "name": pick["name"]}
        self._log_audio_report(report)
        return report

    def _log_audio_report(self, report):
        key = (report.get("error_code"), report.get("error_detail"))
        if key == getattr(self, "_audio_report_key", ("ok", "")):
            self._audio_report_key = key
            return
        self._audio_report_key = key
        if key[0] != "ok":
            services = report.get("audio_service") or {}
            logging.getLogger("kid-te.audio").warning(json.dumps({
                "event": "audio_enumeration_failed", "error_code": key[0], "error_detail": key[1],
                "soundcard_ok": report.get("soundcard_ok"), "windows_build": report.get("windows_build"),
                "audio_service": services, "device_count": len(report.get("devices") or [])},
                ensure_ascii=False))

    def settings_state(self):
        lister = getattr(self.responder, "list_models", None)
        models, model_error = [], ""
        try:
            models = lister() if callable(lister) else [DEEPSEEK_MODEL]
        except Exception as exc:
            model_error = type(exc).__name__
        report = self.audio_report()
        devices = report["devices"]
        if self.gemini.configured and self.gemini.model_source == "fallback":
            threading.Thread(target=self.gemini.detect_model, daemon=True).start()   # dò nền, không chặn trang
        return {"ok": True, "models": models,
                "selected_model": getattr(self.responder, "model", DEEPSEEK_MODEL),
                "model_labels": dict(getattr(self.responder, "model_labels", None) or {}),
                "model_error": model_error,
                "api_key_configured": getattr(self.responder, "client", None) is not None,
                "gemini_key_configured": self.gemini.configured, "gemini_model": self.gemini.explicit_model,
                "gemini_model_effective": self.gemini.model, "gemini_model_source": self.gemini.model_source,
                "tiktok_user_ids": ",".join(sorted(TIKTOK_HOST_USER_IDS)),
                "tiktok_unique_ids": ",".join(sorted(TIKTOK_HOST_UNIQUE_IDS)),
                "audio_devices": devices, "audio_report": report,
                "selected_audio_device": getattr(self, "audio_device_id", AUDIO_DEVICE_ID),
                "knowledge_source": KNOWLEDGE_PACKAGE_SOURCE,
                "knowledge_active_file": "shared/product_knowledge.json",
                "knowledge_seed": self.knowledge_seed_status(),
                "version": _app_version(), "data_root": str(ROOT)}

    def remove_listing(self, payload, restore=False):
        pid = clean(payload.get("product_id"), 160)
        product = next((p for p in self.catalog.snapshot.get("products", []) if str(p.get("product_id")) == pid), None)
        if restore:
            product = product_blocklist.removed_products().get(pid)
        if not product:
            raise ValueError("Không tìm thấy sản phẩm này trong danh sách.")
        product_blocklist.set_removed(product, not restore)
        self.workspace._touch("profiles", "products", "dashboard")
        return {"ok": True, "product_id": pid, "restored": restore}

    def gemini_models(self, payload=None):
        payload = payload or {}
        typed = str(payload.get("api_key") or "").strip()
        provider = GeminiProvider(typed, self.gemini.explicit_model) if typed else self.gemini
        return provider.available_models(force=bool(payload.get("refresh")))

    def save_settings(self, payload):
        model = clean(payload.get("model"), 120)
        api_key = str(payload.get("api_key") or "").strip()
        tiktok_user_ids = payload.get("tiktok_user_ids")
        tiktok_unique_ids = payload.get("tiktok_unique_ids")
        knowledge_source = payload.get("knowledge_source")
        audio_device_id = payload.get("audio_device_id")
        setter = getattr(self.responder, "set_model", None)
        configure_key = getattr(self.responder, "configure_api_key", None)
        result = {"ok": True, "restart_required": False}
        if audio_device_id is not None:
            audio_device_id = str(audio_device_id or "").strip()
            devices = self.settings_state().get("audio_devices", [])
            selected = next((item for item in devices if item.get("id") == audio_device_id), None)
            if not selected or selected.get("disabled") or not selected.get("loopback"):
                raise ValueError("audio_device_not_allowed")
            from config_settings import save_audio_device
            previous_device = self.audio_device_id
            previous_env = os.environ.get("KID_TE_AUDIO_DEVICE")
            saved = save_audio_device(audio_device_id)
            self.audio_device_id = audio_device_id
            os.environ["KID_TE_AUDIO_DEVICE"] = audio_device_id
            runtime = getattr(self, "plugin_runtime", None)
            if runtime:
                state = next((item for item in runtime.snapshot().get("plugins", [])
                              if item.get("id") == "dong-bo-video"), {})
                if state.get("state") == "dang_chay":
                    restarted = runtime.restart("dong-bo-video")
                    if not restarted.get("ok"):
                        # Không để một lựa chọn thiết bị lỗi làm dừng bộ đồng bộ
                        # đang chạy. Khôi phục cả file cấu hình, tiến trình và
                        # môi trường của tiến trình cha trước khi báo lỗi cho UI.
                        save_audio_device(previous_device)
                        self.audio_device_id = previous_device
                        if previous_env is None:
                            os.environ.pop("KID_TE_AUDIO_DEVICE", None)
                        else:
                            os.environ["KID_TE_AUDIO_DEVICE"] = previous_env
                        runtime.restart("dong-bo-video")
                        raise ValueError("audio_device_restart_failed")
            result.update(saved)
        if tiktok_user_ids is not None or tiktok_unique_ids is not None:
            from config_settings import save_tiktok_identity
            identity = save_tiktok_identity(tiktok_user_ids or "", tiktok_unique_ids or "")
            result.update(identity)
        if knowledge_source is not None:
            from config_settings import save_knowledge_source
            result.update(save_knowledge_source(knowledge_source))
        gemini_key = str(payload.get("gemini_api_key") or "").strip()
        gemini_model = str(payload.get("gemini_model") or "").strip()
        if gemini_model:
            from config_settings import save_gemini_model
            result.update(save_gemini_model(gemini_model))
            self.gemini.configure(model=gemini_model)
        elif "gemini_model" in payload and self.gemini.explicit_model:
            from config_settings import clear_gemini_model        # xóa ô = quay về tự dò
            result.update(clear_gemini_model())
            self.gemini.configure(model="")
        if gemini_key:
            from config_settings import save_gemini_key
            result.update(save_gemini_key(gemini_key))
            self.gemini.configure(api_key=gemini_key)
        if gemini_key or gemini_model == "" and "gemini_model" in payload:
            self.gemini.detect_model(force=bool(gemini_key))     # lưu khóa mới -> dò model ngay (không ném lỗi)
        if gemini_key or "gemini_model" in payload:
            result["gemini_model_effective"] = self.gemini.model
            result["gemini_model_source"] = self.gemini.model_source
        if api_key:
            if not callable(configure_key):
                return {"ok": False, "error": "model_provider_not_configurable"}
            try:
                configure_key(api_key)
            except Exception as exc:
                failure = _key_failure(exc)
                if failure is None:
                    raise
                return failure
            from config_settings import save_api_key
            result.update(save_api_key(api_key))
        if not model:
            return result
        if not callable(setter):
            return {"ok": False, "error": "model_provider_not_configurable"}
        selected = setter(model)
        from config_settings import save_model
        result.update(save_model(selected))
        return result

    def check_key(self, payload):
        """Kiểm tra khóa DeepSeek/Gemini (khóa trong ô nhập, hoặc khóa đã lưu nếu để trống). Không trả lại khóa."""
        import key_check
        from config import DEEPSEEK_BASE_URL
        provider = str(payload.get("provider") or "").strip().lower()
        typed = str(payload.get("api_key") or "").strip()
        if provider == "deepseek":
            key = typed or str(getattr(self.responder, "api_key", "") or "")
            return key_check.check_deepseek(key, DEEPSEEK_BASE_URL)
        if provider == "gemini":
            key = typed or self.gemini.api_key
            out = key_check.check_gemini(key, self.gemini.explicit_model)
            if out.get("status") == "ok" and key == self.gemini.api_key and not self.gemini.explicit_model:
                self.gemini.detect_model(force=True)          # khóa đang dùng: cập nhật model tự chọn luôn
            return out
        raise ValueError("key_provider_unknown")

    def test_audio_device(self, payload):
        device_id = str(payload.get("audio_device_id") or "").strip()
        devices = self.settings_state().get("audio_devices", [])
        selected = next((item for item in devices if item.get("id") == device_id), None)
        if not selected or selected.get("disabled") or not selected.get("loopback"):
            raise ValueError("audio_device_not_allowed")
        from audio_capture import RollingCapture
        with RollingCapture(device_id, 1.0) as capture:
            time.sleep(1.25)
            samples = capture.latest()
            error = capture.error
        if error:
            return {"ok": True, "heard": False,
                    "message": "Không nghe được từ thiết bị này. Hãy thử thiết bị khác."}
        if samples is None:
            return {"ok": True, "heard": False,
                    "message": "Chưa nhận được âm thanh. Hãy phát video rồi thử lại."}
        level = float((samples * samples).mean() ** 0.5)
        heard = level >= 0.0001
        return {"ok": True, "heard": heard, "level": round(level, 6),
                "message": ("Đã nghe thấy âm thanh video từ thiết bị này."
                            if heard else
                            "Thiết bị hoạt động nhưng âm thanh đang im lặng.")}

    def open_extension_setup(self, platform="tiktok"):
        if platform not in PLATFORMS:
            raise ValueError("invalid_platform")
        from windows_integration import open_extension_setup
        return open_extension_setup(EXTENSION_DIR if platform == "tiktok" else Path(EXTENSION_DIR).parent / "extension-shopee")

    def _is_live_running(self):
        """Có phiên LIVE thật đang chạy không, theo heartbeat mới nhất của
        tiện ích — dùng chung cho mọi thao tác cấm làm giữa lúc đang bán
        (cập nhật gói kiến thức, cập nhật/rollback plugin)."""
        heartbeats = list(getattr(self, 'platform_heartbeats', {}).values())
        heartbeats.append(getattr(self, 'extension_heartbeat', None) or {})
        for heartbeat in heartbeats:
            if heartbeat.get('live_running') is not True:
                continue
            try:
                age = time.time() - float(heartbeat.get('received_at', 0) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(age) and 0 <= age <= 25:
                return True
        return False

    def update_knowledge_package(self, payload):
        """Nhập gói kiến thức chung. `che_do="xem_truoc"` chỉ xem, không ghi."""
        source = str(payload.get("source") or KNOWLEDGE_PACKAGE_SOURCE).strip()
        if payload.get("seed") is True:
            # Gói đi kèm bộ cài: đường dẫn do server quyết định, không tin client.
            source = str(KNOWLEDGE_SEED_PATH)
        import knowledge_package as goi_kt
        if str(payload.get("che_do") or "").strip() == "xem_truoc":
            return goi_kt.xem_truoc(goi_kt.read_source(source), APPROVED_CATALOG_PATH)
        quyet_dinh = payload.get("quyet_dinh_xung_dot")
        if quyet_dinh is not None and not isinstance(quyet_dinh, dict):
            raise ValueError("quyet_dinh_xung_dot phải là object")
        result = goi_kt.update_from_source(
            source, APPROVED_CATALOG_PATH, live_running=self._is_live_running(),
            quyet_dinh_xung_dot=quyet_dinh)
        self.knowledge.reload()
        return result

    def knowledge_seed_status(self):
        import knowledge_package as goi_kt
        try:
            return goi_kt.seed_status(KNOWLEDGE_SEED_PATH, APPROVED_CATALOG_PATH)
        except Exception as exc:       # trạng thái chỉ để hiển thị, không được làm hỏng Cài đặt
            return {"state": "no_seed", "seed_version": "", "installed_version": "",
                    "error": str(exc)}

    def ensure_seed_knowledge(self):
        """Máy mới chọn bộ mặc định hoặc tự thêm; không tự nạp dữ liệu trước lựa chọn.

        Máy đã có catalog giữ nguyên. Việc nạp bộ hồ sơ do DefaultProfiles xử lý.
        """
        import knowledge_package as goi_kt
        if not APPROVED_CATALOG_PATH.exists():
            return {"ok": True, "status": "choice_required"}
        result = goi_kt.seed_first_run(KNOWLEDGE_SEED_PATH, APPROVED_CATALOG_PATH,
                                       live_running=self._is_live_running())
        if result.get("status") == "applied":
            self.knowledge.reload()
        return result

    def undo_knowledge_package(self, _payload=None):
        import knowledge_package as goi_kt
        if self._is_live_running():
            raise ValueError("không hoàn tác gói kiến thức trong phiên LIVE")
        result = goi_kt.hoan_tac(APPROVED_CATALOG_PATH)
        self.knowledge.reload()
        return result

    def export_knowledge_package(self, payload):
        import knowledge_package as goi_kt
        version = str((payload or {}).get("version") or "").strip()
        if not version:
            raise ValueError("thiếu version cho gói kiến thức")
        goi = goi_kt.xuat_goi(APPROVED_CATALOG_PATH, version, (payload or {}).get("publisher", ""))
        # Ghi ra file để mang sang máy khác (ô "Nguồn gói" ở máy kia đọc file này).
        safe = re.sub(r"[^0-9A-Za-z._-]+", "-", version).strip("-") or "goi"
        out_dir = Path(ROOT) / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"goi-kien-thuc-{safe}.json"
        atomic_json(path, goi)
        return {"ok": True, "path": str(path), "products": sum(
            len(n.get("san_pham") or []) for n in (goi.get("content") or {}).get("nhom_san_pham") or [])}
