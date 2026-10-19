"""Gán hồ sơ SKU (mapping), video/phân tích video, nguồn live, Shopee mapping, workspace.

Tách nguyên văn từ lớp ReplyEngine trong server.py (không đổi logic).
LƯU Ý: các phương thức ở đây được server.py "nhận nuôi" bằng
engine_mixins.adopt(..., globals()) nên tên toàn cục (ROOT, clean, report_problem,
LIVE_CATALOG_PATH...) được tra trong không gian tên của module server — giữ
nguyên hành vi cũ, kể cả monkeypatch.setattr(server, ...). Không import server ở đây.
"""
from __future__ import annotations


# ruff: noqa: F821  (tên toàn cục lấy từ server qua adopt)


class EngineCatalogMixin:
    # ------------------------------------------------------------------
    # Công cụ gán hồ sơ cho SKU. Mục tiêu: chủ dự án tự duyệt được sản phẩm
    # mới mà không cần sửa JSON tay và không cần khởi động lại giữa phiên.
    # ------------------------------------------------------------------
    def _pinned_product_ids(self):
        """product_id có mặt trong lịch ghim của ít nhất một video (cache 30 giây)."""
        cached = getattr(self, "_pinned_cache", None)
        if cached and time.monotonic() - cached[0] < 30:
            return cached[1]
        ids = set()
        for timeline in self.video_timelines().get("timelines", {}).values():
            products = timeline.get("products") or {}
            for event in timeline.get("events") or []:
                for key in (event.get("product"), event.get("alternate_with")):
                    entry = products.get(key) or {}
                    if entry.get("product_id"):
                        ids.add(str(entry["product_id"]))
        self._pinned_cache = (time.monotonic(), ids)
        return ids

    def mapping_state(self):
        live = self.catalog.snapshot or {}
        records = self.knowledge.all_records()
        image_sources = self._product_image_sources()
        self._prefetch_mapping_images(image_sources)
        hidden = self.knowledge.hidden_ids()
        # SKU còn nối tới hồ sơ đã xoá: coi như chưa gắn (khôi phục hồ sơ thì nối lại).
        assigned = {sid: code for sid, code in self.knowledge.mapping_assignments().items()
                    if code not in hidden}
        approved_codes = self.knowledge.approved_product_codes()
        profiles = [{
            "id": clean(record.get("san_pham_id"), 80),
            "name": clean(record.get("ten_san_pham"), 160),
            "approved": clean(record.get("san_pham_id"), 80) in approved_codes,
            "assigned_to": next(
                (shop_id for shop_id, sp in assigned.items()
                 if sp == clean(record.get("san_pham_id"), 80)), "",
            ),
            "preview": self._profile_preview(record),
            "is_combo": record.get("is_combo") is True,
            "package": clean(record.get("quy_cach_dong_goi"), 80),
            "created_local": record.get("created_local") is True,
            "status": ("draft" if record.get("created_local") is True
                       and record.get("approved_for_live") is not True else "approved"),
            "approvable": is_approvable(record),
            "completeness": profile_completeness(record),
            "draft_for": clean(record.get("draft_for_product_id"), 160),
            "image_url": self._profile_image_url(record),
        } for record in self.knowledge.mappable_records()]
        by_id = {item["id"]: item for item in profiles}
        pinned_ids = self._pinned_product_ids()
        products = []
        for item in live.get("products") or []:
            product_id = clean(item.get("product_id"), 160)
            title = clean(item.get("title"), 400)
            current = assigned.get(product_id, "")
            products.append({
                "product_id": product_id,
                "title": title,
                "bag_index": item.get("bag_index") or 0,
                "original_price": clean(item.get("original_price"), 80),
                "in_pin_schedule": product_id in pinned_ids,
                "image_url": self._listing_image_url(product_id, image_sources),
                "profile_id": current,
                "profile_name": by_id.get(current, {}).get("name", ""),
                "suggestions": [] if current else self._suggest_profiles(title, records),
            })
        products.sort(key=lambda row: (not row["in_pin_schedule"], row["bag_index"]))
        return {
            "ok": True,
            "room_id": clean(live.get("room_id"), 160),
            "synced_at": clean(live.get("synced_at"), 60),
            "products": products,
            "profiles": profiles,
            "history_count": self.knowledge.mapping_history_count(),
        }

    @staticmethod
    def _profile_preview(record):
        keep = (
            "ten_san_pham", "quy_cach_dong_goi", "thanh_phan_dang_ky",
            "cong_dung", "huong_dan_su_dung", "luu_y", "live_promotion",
        )
        return json.dumps(
            {key: record.get(key) for key in keep if record.get(key)},
            ensure_ascii=False, indent=2,
        )

    @staticmethod
    def _suggest_profiles(title, records, limit=2):
        """Gợi ý để bấm một phát, nhưng người duyệt vẫn là chủ dự án.

        Đòi hỏi ít nhất một từ RIÊNG của đúng hồ sơ đó ("furagin", "roots",
        "tonic"). Nếu chỉ trùng những từ dùng chung như "amino", "huu co" thì
        không gợi ý — gợi ý sai còn tệ hơn không gợi ý, vì nó mời bấm nhầm.
        """
        names = {}
        spread = {}
        for record in records:
            key = clean(record.get("san_pham_id"), 80)
            names[key] = name_tokens(" ".join((
                clean(record.get("ten_san_pham")),
                clean(record.get("listing_aliases")),
            )))
            for token in names[key]:
                spread[token] = spread.get(token, 0) + 1
        wanted = name_tokens(title)
        ranked = []
        for record in records:
            key = clean(record.get("san_pham_id"), 80)
            overlap = wanted & names.get(key, set())
            if len(overlap) < 2:
                continue
            if not any(spread.get(token, 9) == 1 for token in overlap):
                continue
            ranked.append((len(overlap), {
                "id": key,
                "name": clean(record.get("ten_san_pham"), 160),
                "reason": "Trùng các từ riêng: " + ", ".join(sorted(overlap)),
            }))
        ranked.sort(key=lambda row: row[0], reverse=True)
        return [row[1] for row in ranked[:limit]]

    def save_mapping(self, payload):
        assignments = payload.get("assignments")
        if not isinstance(assignments, dict):
            return {"ok": False, "error": "assignments_required"}
        changed = self.knowledge.apply_mapping(assignments)
        self.knowledge.reload()
        return {"ok": True, "changed": changed}

    def auto_match_mapping(self, payload=None):
        """Gợi ý hồ sơ cho MỌI listing chưa gắn. Chỉ đọc: không ghi ánh xạ nào."""
        import listing_match
        state = self.mapping_state()
        records = [record for record in self.knowledge.mappable_records()
                   if is_approvable(record)]
        unmapped = [{"product_id": row["product_id"], "title": row["title"]}
                    for row in state["products"] if not row["profile_id"]]
        results = listing_match.auto_match(unmapped, records)
        by_id = {row["product_id"]: row for row in state["products"]}
        for item in results:
            item["bag_index"] = by_id.get(item["product_id"], {}).get("bag_index", 0)
        counts = {tier: sum(1 for item in results if item["tier"] == tier)
                  for tier in (listing_match.TIER_SURE, listing_match.TIER_REVIEW,
                               listing_match.TIER_NONE)}
        return {"ok": True, "results": results, "counts": counts,
                "total": len(results), "synced_at": state.get("synced_at", "")}

    def bulk_draft_profiles(self, payload):
        return self.workspace.bulk_draft(payload)

    def approve_profiles(self, payload):
        """Duyệt hồ sơ nháp (và gắn listing nếu có). Hồ sơ chưa đủ nội dung bị chặn."""
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items or len(items) > 200:
            return {"ok": False, "error": "items_required"}
        results, assignments = [], {}
        records = {clean(r.get("san_pham_id"), 80): r for r in self.knowledge.mappable_records()}
        for item in items:
            profile_id = clean((item or {}).get("profile_id"), 80)
            product_id = clean((item or {}).get("product_id"), 160)
            record = records.get(profile_id)
            if not record or record.get("created_local") is not True:
                results.append({"profile_id": profile_id, "ok": False, "error": "not_local_profile"})
            elif not is_approvable(record):
                results.append({"profile_id": profile_id, "ok": False,
                                "error": "profile_not_approvable"})
            else:
                if product_id:
                    assignments[product_id] = profile_id
                else:
                    self.knowledge.approve_local_profile(profile_id)
                results.append({"profile_id": profile_id, "ok": True,
                                "product_id": product_id})
        if assignments:
            self.knowledge.apply_mapping(assignments)
        self.knowledge.reload()
        return {"ok": True, "results": results,
                "approved": sum(1 for r in results if r["ok"])}

    def undo_mapping(self):
        undone = self.knowledge.undo_last_mapping()
        if undone:
            self.knowledge.reload()
        return {"ok": True, "undone": undone}

    def video_timelines(self):
        """Lich ghim cua tung video, doc tu data/videos/*/timeline.json.

        Tien ich nap cai nay luc khoi dong roi ap dung theo video_id ma bo
        nghe nhan ra, nen doi video khong con phai sua ma nguon schedule.js.
        """
        from video_library import list_videos
        timelines, broken = {}, []
        for item in list_videos(check_fingerprint=False):
            if item["timeline"] and not item["errors"]:
                timelines[item["video_id"]] = item["timeline"]
            else:
                broken.append({"video_id": item["video_id"],
                               "errors": item["errors"]})
        return {"ok": True, "timelines": timelines, "broken": broken,
                "count": len(timelines)}

    def video_analysis_jobs(self):
        return self.video_analysis.list_jobs()

    def video_analysis_status(self, job_id, include_draft=False):
        return {"ok": True, **self.video_analysis.status(
            job_id, include_draft=include_draft,
        )}

    def video_upload_start(self, payload):
        return self.video_analysis.start_upload(
            payload.get("filename"), payload.get("total_size"),
            payload.get("source_probe", ""),
        )

    def video_upload_resume(self, payload):
        """Tra job dang do CHI KHI van tay noi dung khop (F06)."""
        job = self.video_analysis.resumable_job(
            payload.get("filename"), payload.get("total_size"),
            payload.get("source_probe", ""),
        )
        return {"ok": True, "job": job}

    def video_upload_finish(self, payload):
        result = self.video_analysis.finish_upload(
            payload.get("job_id"), payload.get("title", ""),
        )
        _library_changed(self)
        return result

    def video_analysis_resume(self, payload):
        return self.video_analysis.resume(payload.get("job_id"))

    def video_analysis_publish(self, payload):
        review = self.workspace.video_job_detail(payload.get("job_id"))
        draft = review.get("draft") or {}
        review_events = draft.get("events") if isinstance(draft.get("events"), list) else []
        if any(str(row.get("review_status") or "pending") == "pending" for row in review_events):
            raise ValueError("video_review_has_pending_events")
        chosen = [row for row in review_events if row.get("review_status") != "rejected"]
        # `publish()` chỉ tin bản duyệt ĐÃ LƯU trên đĩa và bắt buộc revision +
        # operation_id (chống bấm hai lần tạo hai video). Bản trước gọi sai chữ
        # ký (4 tham số vị trí) nên nút phát hành luôn ném TypeError -> 500.
        expected = str(payload.get("expected_revision") or review.get("revision") or "")
        operation_id = str(payload.get("operation_id") or "").strip() or uuid.uuid4().hex
        result = self.video_analysis.publish(
            payload.get("job_id"), payload.get("title", ""), chosen,
            operation_id=operation_id, expected_revision=expected,
        )
        _library_changed(self)
        return result

    def set_video_position(self, payload):
        platform = payload.get("platform", "tiktok")
        if platform not in PLATFORMS:
            raise ValueError("invalid_platform")
        if payload.get("available") is False and not (payload.get('state') == 'uncertain' and payload.get('video_id')):
            with self.video_positions[platform]._lock:
                self.video_positions[platform]._state = {}
            return {"ok": True, "accepted": True}
        if platform == "shopee" and payload.get("source") != "obs_media":
            raise ValueError("shopee_requires_obs_measurement")
        if platform == "tiktok" and payload.get("source") == "obs_media":
            raise ValueError("obs_source_belongs_to_shopee")
        return self.video_positions[platform].update(payload)

    def current_video_position(self, platform="tiktok"):
        if platform not in PLATFORMS:
            raise ValueError("invalid_platform")
        return {**self.video_positions[platform].current(), "platform": platform}

    def live_channels(self):
        settings = self.live_sources.read(public=True)
        channels = {}
        for platform in PLATFORMS:
            heartbeat = self.platform_heartbeats.get(platform) or {}
            age = max(0, time.time() - heartbeat.get("received_at", 0))
            position = self.current_video_position(platform)
            health = read_channel_health(ROOT, platform)
            channels[platform] = {"settings": settings[platform], "position": position,
                "health": health, "extension": {**heartbeat, "age_s": round(age, 1), "connected": age <= 25}}
            if platform == "shopee":
                state = self.shopee_mapping_state()
                channels[platform]["catalog"] = {"products_count": len(state.get("catalog", {}).get("products", [])),
                                                "session_id": state.get("catalog", {}).get("session_id", "")}
        return channels

    def extension_pairing_state(self):
        pairing = getattr(self, "extension_pairing", None)
        return pairing.status() if pairing is not None else {}

    def reset_extension_pairing(self, platform):
        pairing = getattr(self, "extension_pairing", None)
        if pairing is None:
            raise ValueError("extension_pairing_unavailable")
        return pairing.reset(platform)

    def live_sources_state(self):
        return {"ok": True, "channels": self.live_channels()}

    def save_live_sources(self, payload):
        settings = payload.get("settings") or {}
        if payload.get("platform") == "tiktok" and settings.get("mode") == "device":
            device = next((item for item in self.settings_state().get("audio_devices", [])
                           if item.get("id") == settings.get("device_id")), None)
            if not device or device.get("disabled") or not device.get("loopback"):
                raise ValueError("audio_device_not_allowed")
        bindings = settings.get("bindings", {})
        if bindings and any(video_id not in self.video_timelines()["timelines"] for video_id in bindings.values()):
            raise ValueError("video_not_in_library")
        self.live_sources.save(payload)
        self.set_video_position({"platform": payload["platform"], "available": False})
        return self.live_sources_state()

    def shopee_mapping_state(self):
        return self.shopee_mapping_store.all()

    def save_shopee_mappings(self, payload):
        return self.shopee_mapping_store.save(
            payload, self.video_timelines().get("timelines") or {})

    def sync_shopee_catalog(self, payload):
        result = self.shopee_mapping_store.sync(payload, self.video_timelines().get("timelines") or {})
        if result.get("ok"):
            # One start attempt per app session; a later manual stop stays stopped.
            with self._lock:
                prepare = not getattr(self, "_shopee_video_prepared", False)
                self._shopee_video_prepared = True
            if prepare:
                runtime = getattr(self, "plugin_runtime", None)
                try:
                    result["video_sync"] = runtime.start("dong-bo-video") if runtime else {
                        "ok": False, "error": "plugin_runtime_unavailable"}
                except Exception as exc:
                    result["video_sync"] = {"ok": False, "error": str(exc)}
        return result

    def workspace_list(self, resource, query=None):
        return self.workspace.list(resource, query)

    def workspace_detail(self, resource, record_id):
        return self.workspace.detail(resource, record_id)

    def workspace_patch(self, resource, record_id, payload):
        if resource == "facts":
            return self.workspace.update_fact(record_id, (payload.get("changes") or {}).get("fact_value"))
        result = self.workspace.patch(resource, record_id, payload)
        if resource == "videos":
            _library_changed(self)
        return result

    def workspace_correct(self, payload):
        return self.workspace.correct(payload)

    def workspace_undo_correction(self, correction_id):
        return self.workspace.undo_correction(correction_id)

    def workspace_rollback(self, resource, record_id):
        result = self.workspace.rollback(resource, record_id)
        if resource == "videos":
            _library_changed(self)
        return result

    def workspace_delete(self, resource, record_id, payload=None):
        result = self.workspace.delete(resource, record_id, payload)
        if resource == "videos":
            _library_changed(self)
        return result

    def workspace_restore(self, resource, record_id):
        result = self.workspace.restore(resource, record_id)
        if resource == "videos":
            _library_changed(self)
        return result

    def workspace_trash(self, query=None):
        return self.workspace.trash(query)

    def workspace_purge_expired(self):
        return self.workspace.purge_expired()

    def workspace_save_video_draft(self, video_id, payload):
        return self.workspace.save_video_draft(video_id, payload)

    def workspace_approve_reply_example(self, conversation_id, payload):
        learning = getattr(self, "learning", None)
        if learning is not None and not payload.get("persona_id"):
            payload = {**payload, "persona_id": (learning.persona() or {}).get("persona_id", "")}
        return self.workspace.approve_reply_example(conversation_id, payload)

    def workspace_disable_reply_example(self, conversation_id, payload):
        return self.workspace.disable_reply_example(conversation_id, payload)

    def workspace_save_video_job_draft(self, job_id, payload):
        return self.workspace.save_video_job_draft(job_id, payload)

    def workspace_save_product_image(self, product_id, payload):
        return self.workspace.save_product_image(product_id, payload)

    def workspace_delete_product_image(self, product_id):
        return self.workspace.delete_product_image(product_id)

    def workspace_dose_test(self, profile_id, payload):
        try:
            liters = float(payload.get("liters"))
        except (TypeError, ValueError):
            raise ValueError("dose_liters_invalid") from None
        if liters <= 0 or liters > 10000:
            raise ValueError("dose_liters_invalid")
        profile = self.workspace.detail("profiles", profile_id)["data"]
        profile = self._profile_with_dose_rules(
            profile, [profile_id, clean((profile or {}).get("san_pham_id"), 80)])
        rows = verified_dose_calculations(
            f"bình {liters:g} lít pha bao nhiêu", profile, dose_intent=True,
        )
        return {"ok": True, "liters": liters, "results": [{
            "amount": row.get("calculation", ""),
            "source": row.get("approved_source_direction", ""),
            "unit": row.get("don_vi", ""),
        } for row in rows]}

    def workspace_resolve_video_duplicate(self, video_id, payload):
        result = self.workspace.resolve_video_duplicate(video_id, payload)
        if isinstance(payload, dict) and payload.get("action") == "merge":
            _library_changed(self)
        return result
