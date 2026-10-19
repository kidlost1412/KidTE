"""Trí nhớ AI / học phong cách (/api/learning*).

Tách nguyên văn từ lớp ReplyEngine trong server.py (không đổi logic).
LƯU Ý: các phương thức ở đây được server.py "nhận nuôi" bằng
engine_mixins.adopt(..., globals()) nên tên toàn cục (ROOT, clean, report_problem,
LIVE_CATALOG_PATH...) được tra trong không gian tên của module server — giữ
nguyên hành vi cũ, kể cả monkeypatch.setattr(server, ...). Không import server ở đây.
"""
from __future__ import annotations


# ruff: noqa: F821  (tên toàn cục lấy từ server qua adopt)


class EngineLearningMixin:
    # ================================================================ Trí nhớ AI
    def learning_state(self):
        learning = self.learning
        catalog = {clean(p.get("product_id"), 160): p for p in self._learning_catalog()}
        items = learning.items()
        titles = {pid: f"{p.get('name', '')} {p.get('title', '')}" for pid, p in catalog.items()}
        from learning import ambiguous_terms
        for item in items:
            product = catalog.get(item["product_id"]) or {}
            item["product_name"] = product.get("name") or ""
            item["bag_index"] = product.get("bag_index") or 0
            if item["kind"] == "alias":
                # Hiện cảnh báo: từ khoá trùng tên sản phẩm khác → AI KHÔNG dùng để tự chọn SKU.
                item["ambiguous_terms"] = ambiguous_terms(item["terms"], titles, item["product_id"])
        lister = getattr(self.responder, "list_models", None)
        try:
            models = lister() if callable(lister) else []
        except Exception:
            models = []
        labels = getattr(self.responder, "model_labels", None) or {}
        return {"ok": True, "items": items, "personas": learning.personas(),
                "active_persona": (learning.persona() or {}).get("persona_id", ""),
                "stats": learning.stats(), "settings": learning.settings(),
                "runs": learning.runs("relearn", limit=10),
                "reply_model": getattr(self.responder, "model", DEEPSEEK_MODEL),
                "models": [{"id": m, "label": labels.get(m, m)} for m in models],
                "model_ready": getattr(self.responder, "client", None) is not None}

    def learning_run(self, run_id):
        run = self.learning.run(run_id)
        if not run:
            raise ValueError("learning_run_not_found")
        if run["kind"] == "distill":
            ids = run["output"].get("item_ids") or []
            run["items"] = [i for i in (self.learning.item(x) for x in ids) if i]
            catalog = {clean(p.get("product_id"), 160): p for p in self._learning_catalog()}
            for item in run["items"]:
                product = catalog.get(item["product_id"]) or {}
                item["product_name"] = product.get("name") or ""
                item["bag_index"] = product.get("bag_index") or 0
        return {"ok": True, "run": run}

    def learning_post(self, action, payload):
        learning, learner = self.learning, self.learner
        if action == "items":
            item_id = clean(payload.get("item_id"), 40)
            if item_id:
                item = learning.update_item(item_id, payload.get("changes") or {})
                return {"ok": True, "item": item}
            new_id = learning.add_item(
                kind=clean(payload.get("kind") or "fact", 10), statement=payload.get("statement"),
                product_id=clean(payload.get("product_id"), 160), terms=payload.get("terms") or [],
                status="active", confidence=1.0, source="manual",
                created_by=(learning.persona() or {}).get("persona_id", ""))
            self.workspace._touch("dashboard")
            return {"ok": True, "item": learning.item(new_id)}
        if action == "delete":
            return {"ok": True, "deleted": learning.delete_items(payload.get("item_ids") or [])}
        if action == "delete-runs":
            return {"ok": True, "deleted": learning.delete_runs(payload.get("run_ids") or [])}
        if action == "status":
            status = clean(payload.get("status"), 20)
            count = learning.set_status(payload.get("item_ids") or [], status)
            return {"ok": True, "updated": count}
        if action == "relearn":
            run_id = learner.submit_relearn(trigger="manual")
            return {"ok": True, "run_id": run_id}
        if action == "apply":
            accepted = payload.get("accepted")
            if accepted is not None:
                if not isinstance(accepted, list) or len(accepted) > 500:
                    raise ValueError("learning_change_unknown")
                accepted = [clean(x, 80) for x in accepted if isinstance(x, str)]
            return learner.apply_relearn(clean(payload.get("run_id"), 40),
                                         include_styles=payload.get("include_styles", True) is not False,
                                         accepted=accepted)
        if action == "undo":
            return learner.undo_relearn(clean(payload.get("run_id"), 40))
        if action == "discard":
            return learner.discard_run(clean(payload.get("run_id"), 40))
        if action == "distill":
            # Dạy thẳng một điều (không qua Hộp thư): "wingold 20 là đạm cá amino".
            note = clean(payload.get("note"), 400)
            if len(note) < 4:
                raise ValueError("learning_note_required")
            run_id = learner.submit_distill({"conversation_id": "", "note": note,
                                             "answer": clean(payload.get("answer"), 600),
                                             "question": clean(payload.get("question"), 400),
                                             "product_id": clean(payload.get("product_id"), 160),
                                             "persona_id": (learning.persona() or {}).get("persona_id", "")})
            return {"ok": True, "run_id": run_id}
        if action == "backfill":
            # Các câu đã dạy TRƯỚC khi có trí nhớ AI: rút kiến thức một lượt.
            done = {r["ref_id"] for r in learning.runs("distill", limit=2000) if r["ref_id"]}
            with self.workspace._db() as db:
                rows = [dict(r) for r in db.execute(
                    "SELECT * FROM approved_reply_examples WHERE status='active'").fetchall()]
            try:
                conversations = {str(r.get("id")): r for r in self.workspace._derived_live_rows("conversations")}
            except Exception:
                conversations = {}
            run_ids = []
            for row in rows:
                if row["conversation_id"] in done:
                    continue
                conv = conversations.get(row["conversation_id"]) or {}
                run_ids.append(learner.submit_distill({
                    "conversation_id": row["conversation_id"], "question": row["question"],
                    "ai_reply": clean(conv.get("reply_text"), 600), "answer": row["approved_answer"],
                    "note": "" if row["reason"] == "Chủ shop dạy lại trong Hộp thư" else row["reason"],
                    "question_type": row["question_type"], "product_id": row["product_id"],
                    "persona_id": row.get("persona_id") or "",
                }))
            return {"ok": True, "queued": len(run_ids), "run_ids": run_ids}
        if action == "persona-propose":
            # Người trực tả mình → DeepSeek đề xuất phong cách + 3 câu mẫu. CHƯA lưu: UI cho sửa rồi mới lưu.
            if getattr(self.responder, "client", None) is None:
                raise ValueError("model_not_ready")
            try:
                return {"ok": True, "proposal": learner.propose_persona(payload.get("profile"))}
            except ValueError:
                raise
            except Exception as exc:  # noqa: BLE001 - lỗi mạng/khoá không được thành 500
                logging.getLogger("kid-te.core").warning("persona propose failed: %s", type(exc).__name__)
                raise ValueError("persona_proposal_failed") from None
        if action == "persona":
            if payload.get("delete"):
                return learning.delete_persona(clean(payload.get("persona_id"), 80))
            return {"ok": True, "persona": learning.save_persona(payload)}
        if action == "base-rules":
            return self.base_rules_action(payload)
        if action == "settings":
            return {"ok": True, "settings": learning.save_settings(payload)}
        if action == "try":
            return self.learning_try(payload)
        raise ValueError("learning_action_unknown")

    def learning_prompt_view(self, persona_id=""):
        """Toàn bộ lời nhắc theo thứ tự gửi cho một người trực (xem được, sửa từng phần)."""
        persona = self.learning.persona(clean(persona_id, 80))
        style = _persona_style_text(persona) if persona else ""
        return prompt_view.build_view(
            shipped=_model_processor.default_system_prompt(), notes=_model_processor.ai_notes(),
            notes_header=_model_processor.NOTES_HEADER, notes_max=_model_processor.MAX_NOTES_CHARS,
            persona=persona, voice=style, builtin_voice=(persona or {}).get("builtin_voice", ""),
            style_max=MAX_STYLE_CHARS)

    def base_rules_action(self, payload):
        """Lưu / đặt lại / xử lý thông báo cập nhật cho luật bán hàng gốc."""
        shipped = _model_processor.default_system_prompt()
        action = clean(payload.get("action"), 20)
        out = {}
        if action == "save":
            out = prompt_view.save_override(payload.get("text"), shipped)
        elif action == "reset":
            prompt_view.reset()
        else:
            prompt_view.resolve(action, shipped)
        return {**self.learning_prompt_view(payload.get("persona_id") or ""), "saved": bool(out),
                "appended": out.get("appended", 0)}

    def learning_try(self, payload):
        """Chạy đúng đường trả lời thật nhưng KHÔNG ghi bình luận, KHÔNG gửi."""
        comment = clean(payload.get("comment"), 500)
        if len(comment) < 2:
            raise ValueError("comment_required")
        live = dict(self.catalog.snapshot or {})
        live.setdefault("products", [])
        pin = clean(payload.get("pin_id"), 160)
        if pin:
            live["pin_id"] = pin
        live.setdefault("pin_id", "")
        if not live["products"]:
            return {"ok": True, "reply": "", "reason": "no_live_catalog",
                    "explain": {"missing": ["chua_co_danh_muc_live"]}}
        sel = self._select_product(comment, live)
        product = sel["product"] or {}
        explain = {
            "product": {"product_id": product.get("product_id", ""), "title": clean(product.get("title"), 160)},
            "learned_product": bool(sel["learned_product"]),
            "has_profile": bool(sel["record"]),
            "missing": sel["missing"],
        }
        if sel["missing"]:
            return {"ok": True, "reply": "", "reason": "missing_data", "explain": explain}
        context, _signal = self._reply_context(comment, live, sel, "", mark_used=False,
                                               persona_id=clean(payload.get("persona_id"), 80))
        explain.update({
            "learned": context.get("kien_thuc_da_hoc") or [],
            "rules": context.get("quy_tac_da_hoc") or [],
            "examples": [{"question": e.get("question"), "answer": e.get("approved_answer")}
                         for e in context.get("operator_approved_reply_examples") or []],
            "persona": (context.get("phong_cach_nguoi_truc") or {}).get("ten", ""),
        })
        if getattr(self.responder, "client", None) is None and hasattr(self.responder, "client"):
            return {"ok": True, "reply": "", "reason": "model_not_ready", "explain": explain}
        started = time.monotonic()
        max_body = max(20, _model_processor.max_chars() - 12)
        try_system = _model_processor.compose_system(_TRY_TASK)
        result = self.responder.reply("Khách", comment, context, max_body, system_override=try_system)
        # Cùng lớp kiểm chính sách như lúc LIVE, để "Thử AI" phản ánh đúng câu sẽ gửi.
        correction = self._combined_correction(
            self._policy_violation(comment, result, _signal),
            self._invented_price(result, live, sel["approved"], _signal, sel.get("product")),
            self._invented_promo(result, sel.get("product"), sel["approved"]),
            self._variety_correction(result))
        if correction:
            drafted = result
            result = self.responder.reply("Khách", comment, context, max_body, correction=correction,
                                          system_override=try_system)
            if result.get("action") != "reply":
                result = drafted
            result = self._drop_invented_promo(result, sel.get("product"), sel["approved"])
            result = self._drop_invented_price(result, live, sel["approved"], _signal, sel.get("product"))
            if (self._policy_violation(comment, result, _signal)
                    or self._invented_price(result, live, sel["approved"], _signal, sel.get("product"))):
                logging.getLogger("kid-te.core").warning(
                    "TikTok preview reply advisory", extra={"event":"ai_reply_advisory"})
        metrics = dict(result.get("metrics") or {})
        metrics["policy_retry"] = bool(correction)
        metrics["total_ms"] = int((time.monotonic() - started) * 1000)
        return {"ok": True, "reply": result.get("reply", "") if result.get("action") == "reply" else "",
                "reason": "" if result.get("action") == "reply" else "model_skipped",
                "missing_fields": result.get("missing_fields") or [], "metrics": metrics, "explain": explain}

    def _reply_angle_hint(self, topics=(), customer_key=""):
        """Gợi ý góc/cách mở hợp nội dung câu khách (phần theo lượt, không phá cache phần tĩnh)."""
        try:
            if not context_budget.enabled("reply_angles"):
                return ""
            picker = getattr(self, "_angle_picker", None)
            if picker is None:
                picker = self._angle_picker = reply_angles.AnglePicker()
            return picker.hint(topics=topics, customer=customer_key)
        except Exception:  # noqa: BLE001 - gợi ý hỏng không được chặn câu trả lời
            logging.getLogger("kid-te.core").warning("reply angle hint failed", exc_info=True)
            return ""

    def _recent_sent_replies(self, max_age_s=1200, limit=6):
        replies = getattr(self, "_sent_replies", None)
        if not replies:
            return []
        cutoff = time.monotonic() - max_age_s
        return [body for at, body in list(replies)[-limit:] if at >= cutoff]

    @staticmethod
    def _persona_context(learning, persona):
        """Phần CỐ ĐỊNH theo người trực: phong cách + quy tắc chung đã học."""
        out = {"kien_thuc_policy": (
            "kien_thuc_da_hoc là điều CHỦ SHOP đã dạy (tên gọi khác của sản phẩm, dữ kiện, "
            "cách xử lý). Coi là đúng và ưu tiên hơn suy đoán của bạn; nếu mâu thuẫn với "
            "giá/liều/quy cách trong dữ liệu sản phẩm hiện hành thì theo dữ liệu hiện hành. "
            "Dùng tự nhiên trong câu trả lời, đừng nói kiểu 'theo ghi chú'. "
            "quy_tac_da_hoc và phong_cach_nguoi_truc là XU HƯỚNG chủ shop muốn, không phải "
            "khuôn bắt buộc cho mọi câu: chỉ áp dụng khi đúng tình huống; mỗi câu tối đa MỘT "
            "lời chốt/lời mời; chỉ hỏi ngược (cây gì, bao nhiêu) khi thật cần để tư vấn tiếp. "
            "cau_da_gui_gan_day là các câu vừa gửi trong LIVE: đừng lặp lại y chang cách mở, "
            "cách chốt hay nguyên câu."
        )}
        # Giọng/tính cách/xưng hô CHỈ nằm ở đây (lời nhắc hệ thống đã trung tính). Người trực mặc định luôn có
        # giọng dựng sẵn nên lớp này luôn có mặt.
        style = _persona_style_text(persona) if persona else ""
        if persona and (style or persona.get("description")):
            out["phong_cach_nguoi_truc"] = {
                "ten": persona.get("name", ""),
                "mo_ta": persona.get("description", ""),
                "huong_dan": style,
                "uu_tien": "Đây là nguồn duy nhất về giọng, tính cách, xưng hô; luật GIÁ và dữ kiện cứng vẫn thắng nếu mâu thuẫn.",
            }
        rules = learning.rules(persona.get("persona_id", "") if persona else "")
        if rules:
            out["quy_tac_da_hoc"] = rules
        return out

    def _learning_catalog(self):
        try:
            return self.video_catalog().get("products") or []
        except Exception:
            return []
