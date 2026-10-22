"""Các bước của luồng trả lời TikTok (router, chọn SKU, gọi model, chốt liều, hỏi lại SKU, dữ kiện lúc gửi).

Tách nguyên văn từ lớp ReplyEngine trong server.py (không đổi logic).
LƯU Ý: các phương thức ở đây được server.py "nhận nuôi" bằng
engine_mixins.adopt(..., globals()) nên tên toàn cục (ROOT, clean, report_problem,
LIVE_CATALOG_PATH...) được tra trong không gian tên của module server — giữ
nguyên hành vi cũ, kể cả monkeypatch.setattr(server, ...). Không import server ở đây.
"""
from __future__ import annotations

import re

# ruff: noqa: F821  (tên toàn cục lấy từ server qua adopt)


class ReplyPipelineMixin:
    # ============================================ B06: dữ kiện kiểm lại lúc gửi thật
    def _video_lock(self, platform):
        position = self.video_positions[platform].current()
        return position.get('lock_id', ''), position.get('revision', 0)

    _PURCHASE_QTY = re.compile(r"\b\d+\s*(?:chai|can|lo|goi|thung|bich|hop)\b")

    def _tiktok_router(self, comment, original_comment, live, turns, sel, pending_question):
        """Router nhỏ (gói B): chỉ lấp chỗ trống, mã chắc chắn luôn thắng. Tắt/lỗi => {}.

        Trả {meta, result, used, product?, clarify_ids?, smalltalk?, topics?}.
        """
        if not router.enabled():
            return {}
        try:
            product = sel["product"]
            pin = live.get("pin_id", "")
            reason = _selection_processor.explain_selection(comment, live["products"], pin).get("reason")
            # Nhắc quy cách chung ("5 lít") khớp nhiều SKU: mã tự nhận mơ hồ (ambiguous_first) thì không coi là chắc.
            if (pending_question or sel["learned_product"]
                    or (reason != "ambiguous_first" and has_explicit_product_reference(original_comment, product))):
                return {}
            if turns:
                conversation_product, _ = select_conversation_product(self, comment, live["products"], pin, turns)
                if conversation_product:
                    return {}
            rows = [p for p in live.get("products", []) if isinstance(p, dict) and p.get("product_id")]
            names = [(clean(p.get("product_id"), 160), self._item_short_name(p)) for p in rows]
            result, meta = router.consult(self.responder, original_comment, catalog_names=names,
                                          recent_turns=turns[:2] if turns else [], pinned=pin, platform="tiktok")
            out = {"meta": meta, "result": result, "used": False}
            if not result or result["confidence"] < router.MIN_CONFIDENCE:
                return out
            gap_first = product is None or reason == "ambiguous_first"
            gap_pinned = reason == "ambiguous_keeps_pinned" and bool(sel["missing"])
            ids, kind = result["product_ids"], result["kind"]
            by_id = {clean(p.get("product_id"), 160): p for p in rows}
            if kind in ("product", "purchase") and (gap_first or gap_pinned):
                if len(ids) >= 2 or result["clarify"]:
                    out["clarify_ids"] = ids
                elif len(ids) == 1 and gap_first and ids[0] in by_id:
                    out["product"] = by_id[ids[0]]
            if not result["pure_smalltalk"]:
                out["veto_smalltalk"] = True
            if result["topics"]:
                out["topics"] = result["topics"]
            out["used"] = bool(out.get("product") or out.get("clarify_ids") is not None)
            return out
        except Exception:
            logging.getLogger("kid-te.core").warning("router stage failed", exc_info=True)
            return {}

    def _tiktok_sku_ambiguous(self, comment, live, turns, sel):
        """A04: SKU-dependent question (price/dose/promotion) with no pin, no evidence and no
        conversation/learned product -> clarify instead of guessing the top-scored product."""
        try:
            products = [p for p in live.get("products", []) if isinstance(p, dict) and p.get("product_id")]
            intents = sel.get("intents") or {}
            if (len(products) < 2 or sel.get("learned_product")
                    or not (intents.get("price") or intents.get("dose") or intents.get("promotion"))):
                return False
            if _selection_processor.explain_selection(comment, live["products"], live.get("pin_id", "")).get("reason") != "ambiguous_first":
                return False
            if turns:
                conversation_product, _ = select_conversation_product(self, comment, live["products"], live.get("pin_id", ""), turns)
                if conversation_product:
                    return False
            return True
        except Exception:
            logging.getLogger("kid-te.core").warning("sku ambiguity check failed", exc_info=True)
            return False

    def _tiktok_model_turn(self, ask, nickname, comment, context, sel, intents, max_body, price_signal, live,
                           turns):
        """Cổng liều -> trả lời -> (nếu cần) gọi lại một lần. Trả (result, dose_gate, retried);
        retried = "advisory" khi bản gọi lại vẫn còn vi phạm (chỉ ghi nhật ký, không chặn)."""
        try:
            dose_gate = self._tiktok_dose_gate(nickname, comment, context, sel, intents, max_body, ask=ask)
        except Exception:
            logging.getLogger('kid-te.core').warning('tiktok dose gate failed', exc_info=True)
            dose_gate = None
        if dose_gate and dose_gate.get('handoff'):
            result = {'action': 'reply', 'reply': dose_selection.dose_handoff(), 'metrics': {}}
        else:
            result = ask(nickname, comment, context, max_body)
            if dose_gate and dose_gate.get('rows') and result.get('action') == 'reply':
                if not dose_selection.reply_dose_numbers_ok(clean(result.get('reply'), 2000), dose_gate['rows']):
                    result = ask(nickname, comment, context, max_body,
                        correction='Chỉ được nêu liều đúng bằng verified_dose_calculations; không tự tính hay nêu số liều khác.')
                    if (result.get('action') != 'reply'
                            or not dose_selection.reply_dose_numbers_ok(clean(result.get('reply'), 2000), dose_gate['rows'])):
                        result = {'action': 'reply', 'reply': dose_selection.dose_handoff(), 'metrics': {}}
        own_replies = [clean(t.get("response"), 300) for t in reversed(turns or [])
                       if isinstance(t, dict) and t.get("delivery_status") == "sent" and t.get("response")]
        correction = self._combined_correction(
            self._policy_violation(comment, result, price_signal),
            self._invented_price(result, live, sel["approved"], price_signal, sel.get("product")),
            self._invented_promo(result, sel.get("product"), sel["approved"]),
            self._variety_correction(result, own_replies))
        if dose_gate and dose_gate.get('handoff'):
            correction = ''
        retried = False
        if correction:
            # Chỉ thử lại một lần, và chỉ ở những lượt hiếm/đắt giá này. Thà
            # tốn thêm một lượt gọi còn hơn bác con số khách đang nhìn thấy.
            retried = True
            drafted = result
            result = ask(nickname, comment, context, max_body, correction=correction)
            if result.get("action") != "reply":
                result = drafted
            result = self._drop_invented_promo(result, sel.get("product"), sel["approved"])
            result = self._drop_invented_price(result, live, sel["approved"], price_signal, sel.get("product"))
            if (self._policy_violation(comment, result, price_signal)
                    or self._invented_price(result, live, sel["approved"], price_signal, sel.get("product"))
                    or reply_variety.admits_fault(result.get("reply"))):
                retried = "advisory"
        return result, dose_gate, retried

    def _tiktok_dose_gate(self, nickname, comment, context, sel, intents, max_body, ask=None):
        """D05: model picks rule_id among APPROVED rules; code verifies + computes. None = no approved rules."""
        if not intents.get("dose"):
            return None
        product = sel.get("product") or {}
        pid = clean(product.get("product_id"), 160)
        keys = [clean((sel.get("approved") or {}).get("san_pham_id"), 80), "tiktok:" + pid if pid else "", pid]
        usable = {}
        for sku in dict.fromkeys(k for k in keys if k):
            try:
                usable = dose_selection.usable_dose_rules(self._dose_rules_for(sku), sku)
            except Exception:
                logging.getLogger("kid-te.core").warning("dose rule source failed", exc_info=True)
                usable = {}
            if usable:
                break
        if not usable:
            return None
        liters = _dose_processor.target_tank_liters(comment)
        if liters <= 0:
            return None
        system = _model_processor.compose_system(_DOSE_TASK, dose_selection.DOSE_RULE_NOTE)

        reply = ask or self.responder.reply

        def call(request, correction):
            kwargs = {"system_override": system}
            if correction:
                kwargs["correction"] = correction
            return reply(nickname, comment, request, max_body, **kwargs)
        return dose_selection.select_dose_rule(
            call, _dose_processor, usable, liters, "TikTok LIVE", context.get("conversation"), context)

    def _tiktok_clarify_sku(self, payload, ref, live, turns, comment, original_comment, nickname, unique_id,
                            received_at, recompose, candidate_ids=None, extra_metrics=None):
        """A04: ambiguous product -> model-phrased clarify, capped at 2 per family, then top-3 list.
        Returns None when there is nothing to offer (caller keeps the old missing_data path)."""
        rows = [p for p in live.get("products", []) if isinstance(p, dict) and p.get("product_id") and p.get("title")]
        if not rows:
            return None
        prior = (turns[0].get("conversation_state") if turns else None) or {}
        counts = prior.get("clarify_count") if isinstance(prior.get("clarify_count"), dict) else {}
        family_id, top = "unknown", []
        try:
            catalog_rows = [{"sku": "tiktok:" + clean(p.get("product_id"), 160), "title": clean(p.get("title"), 220)} for p in rows]
            pin_id = clean(live.get("pin_id"), 160)
            pref = product_reference("tiktok", catalog_rows, original_comment,
                {"sku": "tiktok:" + pin_id} if pin_id else None, prior) or {}
            family_id = pref.get("family_id") or "unknown"
            by_sku = {"tiktok:" + clean(p.get("product_id"), 160): p for p in rows}
            top = [by_sku[c["sku"]] for c in pref.get("candidates", []) if c.get("sku") in by_sku][:3]
        except Exception:
            logging.getLogger("kid-te.core").warning("product_ref failed", exc_info=True)
        if candidate_ids:
            # Router đã khoanh vùng ứng viên: đứng trước, rồi bù bằng ứng viên của mã.
            by_id = {clean(p.get("product_id"), 160): p for p in rows}
            picked = [by_id[pid] for pid in candidate_ids if pid in by_id]
            top = (picked + [p for p in top if p not in picked])[:3]
        if not top:
            top = rows[:3]
        multi = payload.get("protocol_version") == 2
        tag = "@" + (unique_id or nickname)
        limit = 150 if multi else _model_processor.max_chars()
        tag_len = utf16_length(tag) + 1
        template = "Anh/chị đang hỏi sản phẩm hoặc dung tích nào ạ?"
        count = dose_selection.clarify_count_for(counts, family_id)
        # Luôn hỏi model (có recent_turns); trước đây lượt 3+ lặp câu mẫu.
        model_metrics = {}
        if True:
            text = ""
            try:
                result = self.responder.reply(nickname, original_comment, {
                    "platform": "TikTok LIVE", "ask": "clarify_sku",
                    "conversation": {"current_message": original_comment, "resolved_question": comment},
                    "catalog_candidates": [{"product_id": clean(p.get("product_id"), 160), "title": clean(p.get("title"), 220)} for p in top],
                    "recent_turns": [{'khach': t.get('comment', ''), 'shop': t.get('response', '')} for t in (turns or [])[:3] if isinstance(t, dict)][::-1],
                    "rule": _CLARIFY_RULE},
                    max(1, limit - tag_len),
                    system_override=_model_processor.compose_system(_CLARIFY_TASK, _CLARIFY_RULE))
                fallback_reason = "model_action_not_reply"
                model_metrics = (result.get("metrics") if isinstance(result, dict) else None) or {}
                if isinstance(result, dict) and result.get("action") == "reply":
                    body = clean(result.get("reply"), 2000)
                    if not body:
                        fallback_reason = "model_empty"
                    elif utf16_length(f"{tag} {body}") > limit:
                        fallback_reason = "model_too_long"
                    elif _clarify_price_violation(self, body, {}, {}):
                        fallback_reason = "model_price_violation"
                    else:
                        text = body
            except Exception:
                fallback_reason = "model_error"
                logging.getLogger("kid-te.core").warning("tiktok clarify model failed", exc_info=True)
            listing = ""
            if not text:
                listing = dose_selection.clarify_listing([p.get("title") for p in top], tag_len, template, limit)
                text = listing
            new_counts = {**counts, family_id: count + 1}
        if utf16_length(f"{tag} {text}") > limit:
            text = template
        clarify_fallback = "" if not listing else ("template" if text == template else "listing")
        if clarify_fallback:
            logging.getLogger("kid-te.core").info("tiktok clarify fallback=%s reason=%s candidates=%d",
                                                  clarify_fallback, fallback_reason, len(top))
        reply_id = uuid.uuid4().hex
        with self._lock:
            self._pending_reply_bodies[reply_id] = text
        metrics = {"reply_source": "clarification", "clarify_count": new_counts.get(family_id, 0),
                   **{k: model_metrics[k] for k in _COST_KEYS + ("model", "model_calls") if k in model_metrics},
                   **(extra_metrics or {})}
        if clarify_fallback:
            metrics["clarify_fallback"] = clarify_fallback
            metrics["clarify_fallback_reason"] = fallback_reason
        self.memory.record_reply(ref.get("interaction_id", ""), text, complete=False,
            product={}, turn_id=reply_id, metrics=metrics)
        self.memory.note_conversation_state(ref.get("interaction_id", ""), conversation_state(
            ["product", "variant"], observed_pin=live.get("pin_id", ""), product_family=family_id,
            candidate_skus=["tiktok:" + clean(p.get("product_id"), 160) for p in top], clarify_count=new_counts))
        durable_turn = None
        if live.get("room_id"):
            try:
                durable_turn = self.delivery_store.prepare(reply_id, "tiktok", live["room_id"],
                    ref.get("interaction_id", ""), [f"{tag} {text}"], expires_at=received_at / 1000 + 120,
                    fact_revision=self._reply_fact_revision("tiktok", live, None, [], original_comment,
                        payload.get("video_context")), recompose_count=recompose)
            except ValueError as exc:
                if str(exc) in {"delivery_interaction_missing", "delivery_customer_deleted"}:
                    with self._lock:
                        self._pending_reply_bodies.pop(reply_id, None)
                    return self._tracked_result({"ok": True, "should_send": False, "reason": "memory_disabled"})
                raise
        return self._tracked_result({
            "ok": True, "should_send": True, "reply": text, "max_chars": _model_processor.max_chars(),
            "customer_key": ref.get("customer_key", ""), "interaction_id": ref.get("interaction_id", ""),
            "reply_id": reply_id, "metrics": metrics,
            **({"protocol_version": 2, "messages": durable_turn["messages"], "turn_id": reply_id,
                "room_id": live["room_id"], "expires_at": durable_turn["expires_at"]}
               if multi and durable_turn else {}),
        })

    def _reply_fact_revision(self, platform, live, product, recommendations, comment, video, schema=None):
        by_id = {clean(item.get('product_id'), 160): item for item in live.get('products', [])
                 if isinstance(item, dict)}
        ids = [clean((product or {}).get('product_id'), 160)]
        ids += [clean((item.get('live_product') or {}).get('product_id'), 160)
                for item in recommendations if isinstance(item, dict)]
        ids = [i for i in ids if i in by_id]
        pin_id = clean(live.get('pin_id'), 160)
        explicit = bool(_selection_processor.has_explicit_product_reference(comment, product))
        video_dependent = isinstance(video, dict) and any(
            video.get(key) for key in ('transcript_excerpt', 'current_topic', 'scripted_product_id',
                                       'reviewed_script_facts'))
        lock_id, revision = self._video_lock(platform)
        built = fact_revision.build(by_id, product_ids=ids, pin_id=pin_id,
            pin_dependent=bool(product) and product.get('product_id') == pin_id and not explicit,
            video_dependent=video_dependent, explicit=explicit, lock_id=lock_id, revision=revision)
        return reply_schema.annotate_revision(built, schema)

    def _current_facts(self, platform, room_id, stored):
        if platform == 'tiktok':
            # Cùng danh mục AI đã dùng lúc soạn (giỏ video Studio / chủ shop chọn).
            snapshot = (self.catalog.ai_catalog() if hasattr(self.catalog, 'ai_catalog')
                        else self.catalog.snapshot) or {}
            rows = {clean(i.get('product_id'), 160): i for i in snapshot.get('products', [])
                    if isinstance(i, dict)}
            pin_id = clean(snapshot.get('pin_id'), 160)
        else:
            catalog = self.shopee_mapping_state().get('catalog', {})
            rows = {clean(i.get('product_id'), 160): i for i in catalog.get('products', [])
                    if isinstance(i, dict)}
            pin_id = self.shopee_ai().latest_pin.get(room_id, stored.get('pin_id', ''))
        lock_id, revision = self._video_lock(platform)
        return fact_revision.current(stored, rows, pin_id=pin_id, lock_id=lock_id, revision=revision)

    def _recompose(self, platform, turn):
        """Regenerate a revision-changed turn once; True when a replacement is queued."""
        queued = self.comment_queue.by_turn(turn['turn_id'])
        if (not queued or (queued['platform'], queued['room_id']) != (platform, turn['room_id'])
                or time.time() >= queued['expires_at']):
            return False
        payload = {**queued['payload'], '_recompose_count': turn['recompose_count'] + 1}
        try:
            if platform == 'shopee':
                result = self.shopee_ai().reply({**payload, 'session_id': turn['room_id']})
            else:
                # Stale snapshots must not overwrite the live catalog or reuse old video text.
                payload.pop('video_context', None)
                payload['live_context'] = {'room_id': turn['room_id']}
                result = self.handle(payload)
        except Exception:
            logging.getLogger('kid-te.core').exception('recompose failed')
            return False
        if not (isinstance(result, dict) and result.get('should_send')
                and result.get('turn_id', result.get('reply_id'))):
            return False
        return self.comment_queue.replace_turn(turn['turn_id'], result)
