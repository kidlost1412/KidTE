"""DeepSeek replies for the separate Shopee LIVE extension."""
from __future__ import annotations

import re
import sqlite3
import threading
import uuid
import logging
import math
import time
from collections import OrderedDict
import product_blocklist

import fact_revision
import context_budget
import customer_card
import router
import sales_playbook
import smalltalk
import reply_schema
from customer_memory import CustomerMemory
from delivery_store import DeliveryStore, utf16_length
from knowledge import clean, fold, shared_learning_product_ids, KnowledgeBase, general_knowledge_hint
from shopee_commerce import select_product, price_context, price_violation, processor, product_family_candidates
from live_conversation import resolve_question, conversation_state
from conversation_context import shared_shop_context, platform_video_context, product_reference, EXAMPLE_POLICY
from ai_context import system_with_brief, task_block


# Khối nhiệm vụ cho từng đường gọi DeepSeek của Shopee LIVE (APP_BRIEF luôn đứng đầu, xem ai_context.py).
SHOPEE_REPLY_TASK = task_block(
    "Trả lời bình luận Shopee LIVE",
    "khách đang xem Shopee LIVE vừa bình luận; viết MỘT câu trả lời ngắn đúng giọng người đang trực.",
    inputs="nền cố định của phiên + dữ liệu riêng của lượt (SKU Shopee đã chọn, giá, trí nhớ khách) + câu khách.",
    output="một JSON object {action: reply|skip, reply|messages|missing_fields}.")
SHOPEE_DOSE_TASK = task_block(
    "Chọn quy tắc liều (Shopee LIVE)",
    "khách hỏi liều pha; chọn ĐÚNG MỘT rule_id trong các quy tắc đã duyệt (mã tự tính số).",
    inputs="câu khách, thể tích bình, danh sách quy tắc liều đã duyệt.",
    output="JSON chọn rule_id theo ghi chú bên dưới.")
SHOPEE_RANGE_TASK = task_block(
    "Liệt kê dòng sản phẩm (Shopee LIVE)",
    "khách hỏi chung về một dòng sản phẩm, chưa chọn SKU; nêu các lựa chọn có thật.",
    inputs="câu khách, candidate_skus, lịch sử đúng khách.",
    output="JSON reply/messages ngắn, không nêu giá, liều hay ưu đãi.")


SHOPEE_SYSTEM_PROMPT = """Bạn là người đang tư vấn trong Shopee LIVE của shop. Hiểu câu hiện tại theo lịch sử đúng khách, sản phẩm đang hỏi và sản phẩm đang ghim. Dùng kiến thức chung đã duyệt. Giá chỉ lấy từ price.quotable_price của SKU Shopee đã chọn, không lấy giá TikTok hoặc giá trong bài học cũ. Nếu có giá thì báo trực tiếp, không bảo khách bấm vào xem giá. Không nhắc nickname vì tiện ích tự gắn @. Chỉ trả JSON reply hoặc skip, không Markdown, không bịa dữ kiện."""
# Hai chuỗi này nằm trong NỀN CỐ ĐỊNH của prompt: dùng chung để hai làn byte-identical.
SHOPEE_PRICE_POLICY = "Báo trực tiếp giá SKU đang hỏi trong price.quotable_price; không tự tính giá sau mã. Không bác giá khách thấy."
SHOPEE_SCOPE_WARNING = "Chỉ biết giỏ LIVE đã đồng bộ; không suy ra hàng ngoài giỏ, tồn kho, phí ship hay voucher cá nhân."
HARD_FACT = re.compile(r"\b(giá|bao nhiêu tiền|mã giảm|voucher|khuyến mãi|ưu đãi|ship|giao hàng|phí vận chuyển|còn hàng|hết hàng|tồn kho)\b", re.I)
GREETING = re.compile(r"^(chào|hello|hi|xin chào|cảm ơn|cám ơn|ok|dạ)[\s!?.]*$", re.I)
PRICE_QUESTION = re.compile(r"\b(?:gia|bao (?:nhieu|nhu|nhiu|tien)|bn|nhieu tien)\b")
PROMOTION_QUESTION = re.compile(r"\b(?:tang|khuyen mai|qua|uu dai|mua \d+)\b")


from dose_selection import (DOSE_RULE_NOTE, DOSE_HANDOFF, usable_dose_rules,
    reply_dose_numbers_ok, select_dose_rule, clarify_listing, clarify_count_for)


class ShopeeAiEngine:
    def __init__(self, owner, db_path, journal_dir, dose_rule_source=None):
        self.owner = owner
        # callable(sku) -> list of dose_rules rows (dicts); None keeps the old dose path.
        self.dose_rule_source = dose_rule_source
        self.memory = CustomerMemory(db_path, journal_dir, channel="shopee-live-ai",
                                     session_id="shopee_" + uuid.uuid4().hex[:12],
                                     platform="shopee")
        self._import_legacy_memory()
        self.delivery_store = DeliveryStore(self.memory.db_path)
        self.seen = OrderedDict()
        self.pending = OrderedDict()
        self.completed = OrderedDict()
        self.lock = threading.RLock()
        self.latest_pin = {}  # session_id -> pinned product_id seen on the latest comment
        self._tls = threading.local()

    def _import_legacy_memory(self):
        legacy = self.memory.db_path.with_name("shopee_customers.sqlite3")
        if legacy == self.memory.db_path or not legacy.is_file():
            return
        # Keep the original file; importing again is harmless because IDs are stable.
        with sqlite3.connect(self.memory.db_path) as db:
            db.execute("ATTACH DATABASE ? AS legacy_shopee", (str(legacy),))
            for table in ("sessions", "customers", "interactions", "customer_facts", "memory_opt_outs"):
                columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
                old_columns = {row[1] for row in db.execute(f"PRAGMA legacy_shopee.table_info({table})")}
                common = [name for name in columns if name in old_columns]
                if common:
                    names = ",".join('"' + name + '"' for name in common)
                    db.execute(f"INSERT OR IGNORE INTO {table} ({names}) SELECT {names} FROM legacy_shopee.{table}")

    def close(self):
        self.delivery_store.close()
        self.memory.close()

    def reply(self, payload):
        received = payload.get('received_at_ms', time.time() * 1000)
        if (not isinstance(received, (int, float)) or isinstance(received, bool)
                or not math.isfinite(received) or received > time.time() * 1000 + 5000):
            return {'ok': False, 'should_send': False, 'error': 'received_at_invalid'}
        if time.time() * 1000 - received >= 120000:
            return {'ok': True, 'should_send': False, 'reason': 'expired'}
        session = clean(payload.get('session_id'), 30)
        if session and not payload.get('_recompose_count'):
            self.latest_pin[session] = clean(payload.get('product_id'), 30)
        self._tls.payload = payload
        try:
            result = self._reply(payload)
        finally:
            self._tls.payload = None
        if result.get('should_send'):
            self.delivery_store.limit_expiry(result['turn_id'], received / 1000 + 120)
            if time.time() * 1000 - received >= 120000:
                self.delivery({'session_id': payload.get('session_id'), 'turn_id': result['turn_id'],
                               'error': 'expired before send'})
                return {'ok': True, 'should_send': False, 'reason': 'expired'}
            result['expires_at'] = received / 1000 + 120
            if payload.get('protocol_version') == 2:
                turn = self.delivery_store.get(result['turn_id'])
                result.update(protocol_version=2, room_id=turn['room_id'], messages=turn['messages'])
        return result

    def _reply(self, payload):
        session_id = clean(payload.get("session_id"), 30)
        video_id = clean(payload.get("video_id"), 160)
        comment_id = clean(payload.get("comment_id"), 160)
        user_id = clean(payload.get("user_id"), 160)
        nickname = clean(payload.get("nickname"), 160)
        comment = clean(payload.get("comment"), 600)
        shop_id = clean(payload.get("shop_id"), 30)
        product_id = clean(payload.get("product_id"), 30)
        title = clean(payload.get("title"), 300)
        valid_pin = (shop_id.isdigit() and product_id.isdigit()) or (not shop_id and not product_id)
        if not (session_id.isdigit() and valid_pin
                and video_id and comment_id and user_id and nickname and len(comment) >= 2):
            return {"ok": True, "should_send": False, "reason": "invalid_comment"}
        mapping_state = self.owner.shopee_mapping_state()
        mapping = mapping_state.get("videos", {}).get(video_id, {})
        allowed = {f"{row['shop_id']}:{row['product_id']}"
                   for source in (mapping.get("mappings", {}), mapping.get("alternates", {}))
                   for row in source.values() if isinstance(row, dict)}
        if product_id and f"{shop_id}:{product_id}" not in allowed:
            return {"ok": True, "should_send": False, "reason": "product_not_mapped"}
        key = (session_id, comment_id)
        durable_comment_id = f"shopee:{session_id}:{comment_id}"[:160]
        with self.lock:
            retry_unanswered = (payload.get("retry_unanswered") is True
                                and self.memory.can_retry_unanswered(durable_comment_id, user_id, comment))
            if not payload.get('_recompose_count') and (
                    key in self.seen or (self.memory.has_comment_id(durable_comment_id) and not retry_unanswered)):
                return {"ok": True, "should_send": False, "reason": "duplicate"}
            self.seen[key] = None
            while len(self.seen) > 5000:
                self.seen.popitem(last=False)
        identity = {"platform_user_id": user_id, "display_name": nickname}
        if self.memory.forget_if_requested(comment, **identity):
            return {"ok": True, "should_send": False, "reason": "memory_deleted"}
        ref = self.memory.record_comment(comment, comment_id=durable_comment_id,
                                         data_source="shopee-live", room_id=f"shopee:{session_id}", **identity)
        ref['observed_pin'] = f'shopee:{product_id}' if product_id else ''
        if ref.get('opted_out'):
            return {'ok': True, 'should_send': False, 'reason': 'memory_disabled'}
        observer = getattr(self.owner, "observe", None)
        if callable(observer):
            observer({"events": [{"platform": "shopee", "data_source": "shopee-live",
                "msg_id": durable_comment_id, "room_id": f"shopee:{session_id}",
                "session_id": f"shopee:{session_id}", "user_id": user_id,
                "nickname": nickname, "text": comment, "pin_id": f"shopee:{product_id}",
                "ai_enabled": True, "ai_action": "queued_for_ai"}]})
        catalog = mapping_state.get("catalog", {})
        products = [item for item in product_blocklist.filter_products(catalog.get("products", [])) if isinstance(item, dict)
                    and not re.search(r"khong ban|hang tang kem", fold(item.get("title", "")))]
        if not products and product_id:
            products = [{"shop_id":shop_id, "product_id":product_id, "title":title}]
        turns = self.memory.recent_live_turns(ref.get("customer_key", ""), f"shopee:{session_id}:", ref.get("interaction_id", ""))
        original_comment = comment
        if not payload.get('_recompose_count') and customer_card.recent_duplicate(turns, original_comment):
            return {"ok": True, "should_send": False, "reason": "duplicate_question"}
        comment, pending_question = resolve_question(comment, turns)
        if pending_question:
            self.memory.note_resolved_question(ref.get('interaction_id', ''), comment)
        family, candidates = product_family_candidates(original_comment, products)
        if family and candidates:
            return self._reply_product_family(payload, ref, key, original_comment, family, candidates, turns)
        chosen, selection_reason = select_product(self.owner, comment, products, product_id, turns)
        routed, router_metrics, router_intents = {}, {}, {}
        if not chosen and not pending_question:
            routed = self._shopee_router(comment, original_comment, products, product_id, turns)
            if routed.get("meta") is not None:
                router_metrics = router.metrics_from(routed["meta"], routed.get("result"), routed.get("used"),
                                                     "router" if routed.get("product") else "code")
            router_intents = router.topics_to_intents(routed.get("topics"))
            if routed.get("product"):
                chosen, selection_reason = routed["product"], "router"
        if not chosen:
            if routed.get("smalltalk"):
                lane_reply = self._router_smalltalk_reply(
                    payload, key, ref, comment, original_comment, video_id, turns, router_metrics, products,
                    router_result=routed.get("result"))
                if lane_reply is not None:
                    return lane_reply
            if routed.get("clarify_ids") is not None:
                router_metrics["selection_source"] = "router_clarify"
            return self._clarify_no_sku(payload, ref, key, comment, original_comment, products, turns,
                                        candidate_ids=routed.get("clarify_ids"), extra_metrics=router_metrics)
        title, product_id, shop_id = chosen["title"], chosen["product_id"], chosen["shop_id"]
        price = price_context(chosen, catalog, session_id)
        memory_context = self.memory.context_for_reply(ref.get("customer_key", ""), ref.get("interaction_id", ""), nickname)
        policy = processor(self.owner, "chinh-sach-gia")
        signal = policy.customer_price_signal(comment, {**chosen, "original_price": price.get("quotable_price", "")}, memory_context)
        if signal:
            signal = {k:v.replace('TikTok','Shopee') if isinstance(v,str) else v for k,v in signal.items()}
        asks_price = bool(PRICE_QUESTION.search(fold(comment)))
        asks_promotion = bool(PROMOTION_QUESTION.search(fold(comment)))
        if asks_price and not price.get("quotable_price"):
            if price.get("needs_variant"):
                return self._prepare_reply(session_id,nickname,"Mình lấy phân loại hoặc dung tích nào để em báo đúng giá nha?",ref,None,title,product_id,metrics={},reply_source="clarification", awaiting_fields=['variant'])
            return {"ok":True,"should_send":False,"reason":"shopee_price_missing", "missing_fields":["gia_shopee_hien_hanh"]}
        record = self.owner.knowledge.match(title)
        full_approved = self.owner.knowledge.context(record) if record else {}
        # Product knowledge and approved shop promotions are shared. Catalog
        # prices and the selected listing still come from the current platform.
        technical_fields = {"san_pham_id", "ten_san_pham", "thanh_phan_dang_ky",
                            "cong_dung", "luu_y", "hieu_qua_chinh",
                            "doi_tuong_cay_trong", "mo_ta_ngan", "huong_dan_su_dung", "dose_rules"}
        approved = {key: value for key, value in full_approved.items()
                    if key in technical_fields and value}
        promotion = clean(full_approved.get('live_promotion')
            or (full_approved.get('ho_so_da_duyet') or {}).get('promotion'), 500)
        if asks_promotion and promotion:
            requested = re.search(r"\b(\d+)\s*(?:chai|can|lo)\b", fold(comment))
            quantities = {int(match.group(1)) for match in re.finditer(
                r'\bmua\s+(\d+)\b[^.;]{0,100}?\btang(?:\s+them)?\s+(\d+)\b', fold(promotion))}
            if requested and int(requested.group(1)) in quantities:
                return self._prepare_reply(session_id, nickname, promotion, ref, record, title,
                    product_id, metrics={}, reply_source="verified_promotion")
        if asks_promotion and not promotion:
            requested = re.search(r"\b(\d+)\s*(chai|can|lo)\b", fold(comment))
            detail = f"{requested.group(1)} {requested.group(2)} " if requested else ""
            template = (f"Em chưa có thông tin quà tặng cho {detail}{clean(title, 60)} ạ." if requested
                else f"Mình lấy bao nhiêu {clean(title, 60)} để em kiểm tra quà tặng ạ?")
            awaiting = [] if requested else ['quantity']
            # A stated quantity with no approved promotion stays a deterministic "no info" fact.
            modeled = '' if requested else self._model_text(payload, key, nickname, original_comment, {
                'platform': 'Shopee LIVE', 'ask': 'promotion_unknown',
                'conversation': {'current_message': original_comment, 'resolved_question': comment},
                'current_product': {'title': title, 'product_id': product_id},
                'promotion_known': False, 'requested_quantity': requested.group(0) if requested else '',
                'rule': 'Shop chưa có thông tin quà tặng đã duyệt. Không bịa quà/ưu đãi; nếu thiếu số lượng thì hỏi số lượng.'})
            return self._prepare_reply(session_id, nickname, modeled or template, ref, record, title,
                product_id, metrics={}, reply_source="promotion_clarification", awaiting_fields=awaiting)
        intents = KnowledgeBase.intents(comment)
        # Thiếu hồ sơ kỹ thuật không còn chặn: model dùng kiến thức chung (chỉ gợi ý nhỏ theo lượt).
        soft_gaps = KnowledgeBase.general_knowledge_gaps(comment, chosen, approved)
        if not approved and not GREETING.fullmatch(comment) and "ho_so_san_pham" not in soft_gaps:
            soft_gaps.append("ho_so_san_pham")
        shown_approved = approved
        if context_budget.enabled("intent_fields"):
            try:
                shown_approved = context_budget.project_profile(approved, {**intents, **router_intents})
            except Exception:
                logging.getLogger("kid-te.core").warning("shopee intent fields failed", exc_info=True)
                shown_approved = approved
        lane = False
        if context_budget.enabled("smalltalk_lane"):
            try:
                prior = (turns[0].get("conversation_state") if turns else None) or {}
                terms = smalltalk.catalog_terms([p.get("title") for p in products if isinstance(p, dict)])
                lane = bool(not pending_question and not asks_price and not asks_promotion and not signal
                            and not any(intents.values())
                            and not (isinstance(prior, dict) and prior.get("awaiting_fields"))
                            and not smalltalk.previous_turn_open(turns)
                            and smalltalk.is_small_talk(original_comment, terms)
                            and smalltalk.is_small_talk(comment, terms))
            except Exception:
                logging.getLogger("kid-te.core").warning("shopee smalltalk check failed", exc_info=True)
                lane = False
        context = {
            "conversation": {"current_message": original_comment, "resolved_question": comment,
                "pending_question": pending_question.get('comment', '') if pending_question else '',
                "rule": "Trả lời câu hỏi đang tiếp diễn, không giới thiệu công dụng khi khách đang làm rõ sản phẩm cho câu hỏi trước."},
            "platform": "Shopee LIVE",
            "current_product": {"title": title, "shop_id": shop_id, "product_id": product_id},
            "price": price, "price_policy": SHOPEE_PRICE_POLICY,
            "customer_price_signal": signal,
            "product_selection": {"reason":selection_reason, "recent_customer_turns":turns},
            "approved_product_knowledge": shown_approved,
            "shopee_promotion": promotion,
            "customer_memory": self.memory.context_for_reply(
                ref.get("customer_key", ""), ref.get("interaction_id", ""), nickname),
            "catalog_scope_warning": SHOPEE_SCOPE_WARNING,
            "video_context": platform_video_context(self.owner, 'shopee', video_id),
        }
        reading = router.reading(routed.get("result"))
        if reading and not lane:
            context["doc_y_khach"] = reading
        if not lane:
            self._add_product_ref(context, products, original_comment, product_id, turns)
            if context_budget.enabled("intent_fields"):
                context.pop("product_ref", None)  # diem ung vien noi bo: model khong can
        learning = getattr(self.owner, "learning", None)
        persona_id = ''
        if learning is not None and not lane:
            persona = learning.persona()
            persona_id = (persona or {}).get("persona_id", "")
            product_ids = shared_learning_product_ids(self.owner.knowledge, record, f"shopee:{product_id}")
            learned = learning.relevant(comment, product_ids=product_ids, persona_id=persona_id)
            if learned:
                context["kien_thuc_da_hoc"] = learned
                learning.mark_used([item["id"] for item in learned])
            context.update(self.owner._persona_context(learning, persona))
        if not lane:
            context.update(shared_shop_context(self.owner, comment, f'shopee:{product_id}', persona_id))
            self._add_playbook_stable(context)
        if context_budget.enabled("compact_catalog"):
            try:
                context_budget.drop_duplicate_policies(
                    context, processor(self.owner, "model-deepseek").has_prompt_override())
            except Exception:
                logging.getLogger("kid-te.core").warning("shopee policy dedupe failed", exc_info=True)
        dose_rows = processor(self.owner, 'lieu-dung').verified_dose_calculations(
            comment, full_approved, dose_intent=KnowledgeBase.intents(comment)['dose'])
        if dose_rows:
            context['verified_dose_calculations'] = dose_rows
        dose_gate = None
        if self.dose_rule_source is not None and KnowledgeBase.intents(comment)['dose']:
            dose_gate = self._dose_rule_gate(key, nickname, comment, context, product_id)
            if dose_gate and dose_gate.get('handoff'):
                return self._prepare_reply(session_id, nickname, DOSE_HANDOFF, ref, record, title,
                    product_id, metrics={}, reply_source="dose_handoff")
        documents = getattr(self.owner, "documents", None)
        if documents is not None and not lane:
            context["tai_lieu_lien_quan"] = documents.search(comment, extra_terms=title)
        history = getattr(self.owner, "customer_history", None)
        if history is not None and not lane:
            context["lich_su_khach"] = history.for_customer(ref.get("customer_key", ""),
                current_room=f"shopee:{session_id}", current_msg_id=durable_comment_id,
                current_text=comment, product_titles={f"shopee:{p['product_id']}":p["title"] for p in products})
        card = self._card(ref)
        if lane:
            context = self._smalltalk_context(context, learning, comment=comment, turns=turns, card=card,
                                              reading=reading)
        sales_playbook.add_card_and_hint(context, card, [original_comment, comment],
                                         (routed.get("result") or {}).get("acts") or (), turns=turns)
        general_hint = None if lane else general_knowledge_hint(soft_gaps)
        if general_hint:
            context["kien_thuc_chung"] = general_hint
        tag = "@" + nickname
        body_limit = max(1, 150 - utf16_length(tag) - 1)
        multi = payload.get('protocol_version') == 2
        if multi:
            context['delivery_protocol_version'] = 2
        common = processor(self.owner, "model-deepseek").system_prompt().replace('TikTok','Shopee')
        system = system_with_brief(SHOPEE_SYSTEM_PROMPT, common,
            "Quy tắc giá Shopee ở đầu và price.quotable_price ưu tiên hơn mọi ví dụ. Không nói giá khác hoặc bảo khách bấm xem giá. Nếu thiếu dữ kiện giao hàng/tồn kho/ưu đãi thì skip; không suy ra từ giá.",
            SHOPEE_REPLY_TASK)
        result = self._call_model(key, nickname, comment, context, body_limit, system_override=system)
        correction = price_violation(self.owner, clean(result.get("reply"), 2000), price, signal) if result.get("action") == "reply" else ""
        if correction:
            drafted = result
            result = self._call_model(key, nickname, comment, context, body_limit, system_override=system, correction=correction)
            if result.get("action") != "reply":
                result = drafted
            if result.get("action") == "reply" and price_violation(self.owner, clean(result.get("reply"), 2000), price, signal):
                logging.getLogger("kid-te.core").warning(
                    "Shopee reply advisory interaction_id=%s", ref.get("interaction_id", ""),
                    extra={"event":"ai_reply_advisory"})
        if result.get("action") != "reply":
            logging.getLogger("kid-te.core").info(
                "Shopee AI skipped comment_id=%s product_id=%s fields=%s", durable_comment_id,
                product_id, result.get("missing_fields") or [], extra={"event":"shopee_ai_skipped"})
            if asks_price and price.get("quotable_price") and not asks_promotion and not signal:
                return self._prepare_reply(session_id, nickname,
                    f"Giá gốc là {price['quotable_price']} nha.",
                    ref, record, title, product_id, metrics=result.get("metrics"), reply_source="verified_price")
            return {"ok": True, "should_send": False, "reason": "model_skipped",
                    "missing_fields": result.get("missing_fields") or []}
        if dose_gate and dose_gate.get('rows'):
            if not reply_dose_numbers_ok(clean(result.get("reply"), 2000), dose_gate['rows']):
                result = self._call_model(key, nickname, comment, context, body_limit, system_override=system,
                    correction="Chỉ được nêu liều đúng bằng verified_dose_calculations; không tự tính hay nêu số liều khác.")
                if result.get("action") != "reply" or not reply_dose_numbers_ok(clean(result.get("reply"), 2000), dose_gate['rows']):
                    return self._prepare_reply(session_id, nickname, DOSE_HANDOFF, ref, record, title,
                        product_id, metrics={}, reply_source="dose_handoff")
        schema = reply_schema.safe_normalize(result)
        # Schema chỉ bổ sung; đầu ra không chuẩn hoá được vẫn đi đường cũ (kiểm tra cũ bên dưới giữ nguyên).
        if schema:
            result = {**result, 'awaiting_fields': schema['awaiting_fields'] or result.get('awaiting_fields')}
        body = clean(result.get("reply"), 2000)
        bodies = result.get('messages', [body]) if multi else [body]
        if (not isinstance(bodies, list) or not 1 <= len(bodies) <= 3 or
                any(not isinstance(row, str) or not row.strip() or utf16_length(f'{tag} {row}') > 150 for row in bodies)):
            return {"ok": True, "should_send": False, "reason": "reply_invalid"}
        return self._prepare_reply(session_id, nickname, bodies, ref, record, title, product_id,
                                   metrics={**(reply_schema.audit_metrics(result.get("metrics"), schema) or {}), **router_metrics},
                                   reply_source="deepseek",
                                   awaiting_fields=result.get('awaiting_fields'), schema=schema)

    def _card(self, ref):
        """Thẻ khách Shopee (the_khach); tắt cờ / không dữ liệu / lỗi => None."""
        shorten = getattr(self.owner, "_short_product_name", None)
        try:
            return customer_card.for_customer(
                self.memory, ref.get("customer_key", ""), ref.get("interaction_id", ""),
                shortener=(lambda title: shorten(clean(title, 220))) if callable(shorten) else None)
        except Exception:
            logging.getLogger("kid-te.core").warning("shopee customer card failed", exc_info=True)
            return None

    def _add_playbook_stable(self, context):
        try:
            override = processor(self.owner, "model-deepseek").has_prompt_override()
            context.update(sales_playbook.stable_context(override))
        except Exception:
            logging.getLogger("kid-te.core").warning("shopee playbook stable failed", exc_info=True)
        return context

    def _smalltalk_context(self, context, learning, *, comment="", turns=None, card=None, reading=None):
        """Ngữ cảnh xã giao trên Shopee; lỗi => giữ ngữ cảnh đầy đủ.

        persona_all_lanes BẬT: cùng NỀN CỐ ĐỊNH như làn đầy đủ (giọng, quy tắc, sổ tay, thông tin shop,
        chính sách giá/phạm vi) để chung tiền tố cache; phần theo lượt nhỏ. TẮT: bản tối giản cũ."""
        try:
            small = {"platform": context.get("platform"), "conversation": context.get("conversation"),
                     "customer_memory": context_budget.short_memory(context.get("customer_memory")),
                     "thong_tin_shop": context_budget.short_shop_info(getattr(self.owner, "shop_info", {}))}
            persona = learning.persona() if learning is not None else None
            if not context_budget.enabled("persona_all_lanes"):
                if context.get("video_context"):
                    small["video_context"] = context["video_context"]
                if persona:
                    style = self.owner._persona_context(learning, persona).get("phong_cach_nguoi_truc")
                    if style:
                        small["phong_cach_nguoi_truc"] = style
                return {k: v for k, v in small.items() if v}
            stable = {"price_policy": SHOPEE_PRICE_POLICY, "approved_example_policy": EXAMPLE_POLICY,
                      "catalog_scope_warning": SHOPEE_SCOPE_WARNING,
                      "thong_tin_shop": getattr(self.owner, "shop_info", {})}
            if learning is not None:
                stable.update(self.owner._persona_context(learning, persona))
            self._add_playbook_stable(stable)
            override = processor(self.owner, "model-deepseek").has_prompt_override()
            if context_budget.enabled("compact_catalog"):
                context_budget.drop_duplicate_policies(stable, override)
            turn = {"platform": context.get("platform"), "conversation": context.get("conversation"),
                    "video_context": context.get("video_context"), "doc_y_khach": reading,
                    "luot_gan_day": customer_card.recent_turns_view(turns)}
            if not card:
                turn["customer_memory"] = context_budget.short_memory(context.get("customer_memory"))
            lane_examples = getattr(self.owner, "_lane_examples", None)
            if callable(lane_examples):
                turn["operator_approved_reply_examples"] = lane_examples(
                    comment, (persona or {}).get("persona_id", ""))
            return {**{k: v for k, v in turn.items() if v}, **stable}
        except Exception:
            logging.getLogger("kid-te.core").warning("shopee smalltalk context failed", exc_info=True)
            return context

    def _dose_rule_gate(self, key, nickname, comment, context, product_id):
        """None = no approved rules (old behaviour). Else {'rows':[...]} or {'handoff':True}."""
        sku = 'shopee:' + str(product_id)
        try:
            usable = usable_dose_rules(self.dose_rule_source(sku), sku)
        except Exception:
            logging.getLogger('kid-te.core').warning('dose rule source failed', exc_info=True)
            return None
        if not usable:
            return None
        proc = processor(self.owner, 'lieu-dung')
        liters = proc.target_tank_liters(comment)
        if liters <= 0:
            return None
        rule_note = DOSE_RULE_NOTE
        system = system_with_brief(SHOPEE_SYSTEM_PROMPT, rule_note, SHOPEE_DOSE_TASK)
        return select_dose_rule(
            lambda ask, correction: self._call_model(key, nickname, comment, ask, 150, system_override=system,
                **({'correction': correction} if correction else {})),
            proc, usable, liters, 'Shopee LIVE', context.get('conversation'), context)

    def _add_product_ref(self, context, products, comment, pin_id, turns):
        """Additive ProductRef key; failures never affect the reply path."""
        try:
            catalog = [{'sku': 'shopee:' + str(p.get('product_id', '')), 'title': p.get('title', '')}
                       for p in products if isinstance(p, dict) and p.get('product_id')]
            prior = (turns[0].get('conversation_state') if turns else None) or {}
            pin = {'sku': 'shopee:' + pin_id} if pin_id else None
            context['product_ref'] = product_reference('shopee', catalog, comment, pin, prior)
        except Exception:
            logging.getLogger('kid-te.core').warning('product_ref failed', exc_info=True)

    def _model_text(self, payload, key, nickname, comment, context):
        """One short model-written message, or '' so the caller uses its template."""
        try:
            limit = max(1, 150 - utf16_length('@' + nickname) - 1)
            system = system_with_brief(SHOPEE_SYSTEM_PROMPT, context.get('rule', ''), SHOPEE_REPLY_TASK)
            result = self.owner.responder.reply(nickname, comment, context, limit, system_override=system)
            if not isinstance(result, dict) or result.get('action') != 'reply':
                return ''
            body = clean(result.get('reply'), 2000)
            if not body:  # A06: tolerant — a schema-shaped output (messages only) still yields text
                schema = reply_schema.safe_normalize(result)
                first = next((m for m in (schema or {}).get('messages', []) if isinstance(m, str) and m.strip()), '')
                body = clean(first, 2000)
            while body and utf16_length(f'@{nickname} {body}') > 150:
                cut = max(body.rfind('. ', 0, len(body) - 1), body.rfind(', ', 0, len(body) - 1), body.rfind(' ', 0, len(body) - 1))
                body = body[:cut].rstrip(' ,') if cut > 0 else ''
            if not body or price_violation(self.owner, body, {}, {}):
                logging.getLogger('kid-te.core').info('shopee model text rejected')
                return ''
            return body
        except Exception:
            logging.getLogger('kid-te.core').warning('shopee model text failed', exc_info=True)
            return ''

    def _clarify_no_sku(self, payload, ref, key, comment, original_comment, products, turns,
                        candidate_ids=None, extra_metrics=None):
        nickname, session_id = payload['nickname'], payload['session_id']
        template = "Anh/chị đang hỏi sản phẩm hoặc dung tích nào ạ?"
        prior = (turns[0].get('conversation_state') if turns else None) or {}
        counts = prior.get('clarify_count') if isinstance(prior.get('clarify_count'), dict) else {}
        family_id = 'unknown'
        top = []
        try:
            probe = {}
            self._add_product_ref(probe, products, original_comment, '', turns)
            pref = probe.get('product_ref') or {}
            family_id = pref.get('family_id') or 'unknown'
            by_sku = {'shopee:' + str(p.get('product_id')): p for p in products if isinstance(p, dict)}
            top = [by_sku[c['sku']] for c in pref.get('candidates', []) if c.get('sku') in by_sku][:3]
        except Exception:
            pass
        if candidate_ids:
            # Router khoanh vùng ứng viên: đứng trước, rồi bù bằng ứng viên của mã.
            by_id = {str(p.get('product_id')): p for p in products if isinstance(p, dict)}
            picked = [by_id[pid] for pid in candidate_ids if pid in by_id]
            top = (picked + [p for p in top if p not in picked])[:3]
        if not top:
            top = [p for p in products if isinstance(p, dict) and p.get('title')][:3]
        count = clarify_count_for(counts, family_id)
        tag_len = utf16_length('@' + nickname) + 1
        modeled = self._model_text(payload, key, nickname, original_comment, {
            'platform': 'Shopee LIVE', 'ask': 'clarify_sku',
            'conversation': {'current_message': original_comment, 'resolved_question': comment},
            'catalog_candidates': [{k: p.get(k, '') for k in ('product_id', 'title')} for p in top],
            'recent_turns': [{'khach': t.get('comment', ''), 'shop': t.get('response', '')} for t in (turns or [])[:3] if isinstance(t, dict)][::-1],
            'rule': "Mã chưa chắc khách nói sản phẩm nào. Đọc recent_turns: nếu khách đang nối tiếp sản phẩm vừa bàn (vd vừa hỏi can 5l đạm cá rồi hỏi 'đi với thuốc sâu ko' = hỏi pha đạm cá với thuốc sâu) thì trả lời luôn bằng dữ liệu + kiến thức chung. Nếu chỉ còn vài lựa chọn (vd chai 1L hay can 5L) thì hỏi tự nhiên kèm tên cụ thể. Không lặp câu hỏi lại y như lượt trước; không nêu giá/ưu đãi khi chưa rõ sản phẩm."})
        if not modeled:
            # Model hỏng: nêu tên cụ thể thay vì lặp mãi một câu hỏi chung chung.
            modeled = clarify_listing([p.get('title') for p in top], tag_len, template) if top else ''
        new_counts = {**counts, family_id: count + 1}
        return self._prepare_reply(session_id, nickname, modeled or template, ref, None, '', '', metrics=dict(extra_metrics or {}),
            reply_source='clarification', awaiting_fields=['product', 'variant'], clarify_count=new_counts)

    def _shopee_router(self, comment, original_comment, products, pin_id, turns):
        """Router nhỏ (gói B) khi mã không chọn được SKU. Tắt/lỗi => {}.

        Trả {meta, result, used, product?, clarify_ids?, smalltalk?, topics?}.
        """
        if not router.enabled():
            return {}
        try:
            rows = [p for p in products if isinstance(p, dict) and p.get('product_id') and p.get('title')]
            shorten = getattr(self.owner, '_short_product_name', None)
            names = [(str(p['product_id']), shorten(clean(p['title'], 220)) if callable(shorten) else clean(p['title'], 60))
                     for p in rows]
            result, meta = router.consult(self.owner.responder, original_comment, catalog_names=names,
                                          recent_turns=turns[:2] if turns else [], pinned=str(pin_id or ''),
                                          platform='shopee')
            out = {'meta': meta, 'result': result, 'used': False}
            if not result or result['confidence'] < router.MIN_CONFIDENCE:
                return out
            ids, kind = result['product_ids'], result['kind']
            by_id = {str(p['product_id']): p for p in rows}
            if kind in ('product', 'purchase'):
                if len(ids) >= 2 or result['clarify']:
                    out['clarify_ids'] = ids
                elif len(ids) == 1 and ids[0] in by_id:
                    out['product'] = by_id[ids[0]]
            if result['pure_smalltalk']:
                out['smalltalk'] = True
            if result['topics']:
                out['topics'] = result['topics']
            out['used'] = bool(out.get('product') or out.get('clarify_ids') is not None)
            return out
        except Exception:
            logging.getLogger('kid-te.core').warning('shopee router stage failed', exc_info=True)
            return {}

    def _router_smalltalk_reply(self, payload, key, ref, comment, original_comment, video_id, turns, router_metrics,
                                products=(), router_result=None):
        """Router báo xã giao, mã không tìm thấy ý hỏi/giá/SKU nào: dùng làn xã giao (không sản phẩm).
        Không chắc/lỗi => None (đi đường cũ: hỏi lại SKU)."""
        try:
            if not context_budget.enabled('smalltalk_lane'):
                return None
            # Làn xã giao chỉ khi mã VÀ router cùng đồng ý; lệch nhau thì đi đường cũ (đầy đủ/hỏi lại).
            terms = smalltalk.catalog_terms([p.get('title') for p in products if isinstance(p, dict)])
            if not (smalltalk.is_small_talk(original_comment, terms) and smalltalk.is_small_talk(comment, terms)):
                return None
            folded = fold(comment)
            prior = (turns[0].get('conversation_state') if turns else None) or {}
            if (any(KnowledgeBase.intents(comment).values()) or PRICE_QUESTION.search(folded)
                    or PROMOTION_QUESTION.search(folded) or re.search(r'\b\d+\s*(?:chai|can|lo|goi|thung|bich|hop)\b', folded)
                    or (isinstance(prior, dict) and prior.get('awaiting_fields'))
                    or smalltalk.previous_turn_open(turns)):
                return None
            nickname, session_id = payload['nickname'], payload['session_id']
            context = {'platform': 'Shopee LIVE',
                       'conversation': {'current_message': original_comment, 'resolved_question': comment,
                                        'pending_question': '',
                                        'rule': 'Câu xã giao: đáp ngắn, thân thiện; không nêu dữ kiện sản phẩm, giá, tồn kho, ưu đãi.'},
                       'customer_memory': self.memory.context_for_reply(
                           ref.get('customer_key', ''), ref.get('interaction_id', ''), nickname),
                       'video_context': platform_video_context(self.owner, 'shopee', video_id)}
            card = self._card(ref)
            context = self._smalltalk_context(context, getattr(self.owner, 'learning', None),
                                              comment=comment, turns=turns, card=card,
                                              reading=router.reading(router_result))
            sales_playbook.add_card_and_hint(context, card, [original_comment, comment],
                                             (router_result or {}).get('acts') or ())
            multi = payload.get('protocol_version') == 2
            if multi:
                context['delivery_protocol_version'] = 2
            body_limit = max(1, 150 - utf16_length('@' + nickname) - 1)
            common = processor(self.owner, 'model-deepseek').system_prompt().replace('TikTok', 'Shopee')
            system = system_with_brief(SHOPEE_SYSTEM_PROMPT, common, SHOPEE_REPLY_TASK)
            result = self._call_model(key, nickname, comment, context, body_limit, system_override=system)
            if not isinstance(result, dict) or result.get('action') != 'reply':
                return None
            body = clean(result.get('reply'), 2000)
            bodies = result.get('messages', [body]) if multi else [body]
            if (not isinstance(bodies, list) or not 1 <= len(bodies) <= 3
                    or any(not isinstance(row, str) or not row.strip() or utf16_length(f'@{nickname} {row}') > 150
                           or price_violation(self.owner, row, {}, {}) for row in bodies)):
                return None
            metrics = {**(result.get('metrics') or {}), **router_metrics, 'context_lane': 'smalltalk',
                       'router_used': True}
            return self._prepare_reply(session_id, nickname, bodies, ref, None, '', '', metrics=metrics,
                                       reply_source='deepseek')
        except Exception:
            logging.getLogger('kid-te.core').warning('shopee router smalltalk failed', exc_info=True)
            return None

    def _call_model(self, key, *args, **kwargs):
        try:
            return self.owner.responder.reply(*args, **kwargs)
        except Exception:
            # No reply was prepared or sent; a bounded retry of this exact
            # customer's unchanged comment may recover a transient outage.
            with self.lock:
                self.seen.pop(key, None)
            raise

    def _reply_product_family(self, payload, ref, key, comment, family, candidates, turns):
        nickname, session_id = payload['nickname'], payload['session_id']
        context = {'platform': 'Shopee LIVE', 'intent': 'product_range',
            'conversation': {'current_message': comment, 'resolved_question': comment},
            'product_selection': {'product_family': family, 'selected_sku': '',
                'candidate_skus': [{key: item.get(key, '') for key in ('shop_id', 'product_id', 'title')}
                                   for item in candidates],
                'observed_pin': ref.get('observed_pin', ''), 'recent_customer_turns': turns},
            'customer_memory': self.memory.context_for_reply(ref.get('customer_key', ''), ref.get('interaction_id', ''), nickname),
            'range_rule': 'Khách hỏi danh sách dòng sản phẩm, chưa chọn SKU. Trình bày các lựa chọn có thật trong candidate_skus, diễn đạt tự nhiên theo câu hiện tại và lịch sử. Không hỏi lại câu chung về sản phẩm. Không coi sản phẩm ghim là sản phẩm khách hỏi. Chưa cấp giá, tồn kho, liều hay ưu đãi; không tự thêm các dữ kiện đó.'}
        context.update(shared_shop_context(self.owner, comment, '', ''))
        self._add_product_ref(context, candidates, comment, clean(payload.get('product_id'), 30), turns)
        multi = payload.get('protocol_version') == 2
        if multi:
            context['delivery_protocol_version'] = 2
        limit = max(1, 150 - utf16_length('@' + nickname) - 1)
        system = system_with_brief(SHOPEE_SYSTEM_PROMPT, context['range_rule'], SHOPEE_RANGE_TASK)
        result = self._call_model(key, nickname, comment, context, limit, system_override=system)
        if result.get('action') != 'reply':
            return {'ok': True, 'should_send': False, 'reason': 'model_skipped',
                    'missing_fields': result.get('missing_fields') or []}
        schema = reply_schema.safe_normalize(result)
        bodies = result.get('messages', [result.get('reply', '')]) if multi else [result.get('reply', '')]
        if (not isinstance(bodies, list) or not 1 <= len(bodies) <= (3 if multi else 1) or
                any(not isinstance(body, str) or not body.strip() or utf16_length('@' + nickname + ' ' + body) > 150 for body in bodies)):
            return {'ok': True, 'should_send': False, 'reason': 'reply_invalid'}
        # No price was supplied for a range answer; invented money must never
        # turn into a sendable response merely because a different SKU is pinned.
        correction = price_violation(self.owner, ' '.join(bodies), {}, {})
        if correction:
            result = self._call_model(key, nickname, comment, context, limit, system_override=system, correction=correction)
            bodies = result.get('messages', [result.get('reply', '')]) if multi else [result.get('reply', '')]
            if (result.get('action') != 'reply' or not isinstance(bodies, list) or not 1 <= len(bodies) <= (3 if multi else 1) or
                    any(not isinstance(body, str) or not body.strip() or utf16_length('@' + nickname + ' ' + body) > 150 for body in bodies) or
                    price_violation(self.owner, ' '.join(bodies), {}, {})):
                return {'ok': True, 'should_send': False, 'reason': 'range_facts_invalid'}
        return self._prepare_reply(session_id, nickname, bodies, ref, None, '', '',
            metrics=reply_schema.audit_metrics(result.get('metrics'), schema), reply_source='deepseek_catalog_range',
            awaiting_fields=result.get('awaiting_fields'), product_family=family, schema=schema,
            candidate_skus=['shopee:' + item['product_id'] for item in candidates])

    def _prepare_reply(self, session_id, nickname, body, ref, record, title, product_id, *, metrics, reply_source, awaiting_fields=None, product_family='', candidate_skus=None, clarify_count=None, schema=None):
        tag = "@" + nickname
        bodies = body if isinstance(body, list) else [body]
        if not 1 <= len(bodies) <= 3 or any(utf16_length(f'{tag} {row}') > 150 for row in bodies):
            return {"ok": True, "should_send": False, "reason": "reply_invalid"}
        body = ' '.join(bodies)
        turn_id = uuid.uuid4().hex
        interaction_id = ref.get("interaction_id", "")
        self.memory.record_reply(interaction_id, body, complete=False,
                                 product={"product_id": f"shopee:{product_id}" if product_id else '', "title": title}, turn_id=turn_id,
                                 metrics=metrics)
        self.memory.note_conversation_state(interaction_id, conversation_state(awaiting_fields,
            product_id=f'shopee:{product_id}' if product_id else '', observed_pin=ref.get('observed_pin', ''),
            product_family=product_family, candidate_skus=candidate_skus,
            clarify_count=clarify_count))
        try:
            self.delivery_store.prepare(turn_id, "shopee", session_id, interaction_id,
                                        [f"{tag} {row}" for row in bodies],
                                        fact_revision=self._fact_revision(product_id, title, candidate_skus, schema),
                                        recompose_count=int((getattr(self._tls, 'payload', None) or {}).get('_recompose_count') or 0))
        except ValueError as exc:
            if str(exc) in {"delivery_interaction_missing", "delivery_customer_deleted"}:
                return {"ok": True, "should_send": False, "reason": "memory_disabled"}
            raise
        with self.lock:
            self.pending[turn_id] = {"session_id": session_id, "interaction_id": interaction_id,
                                     "body": body}
            while len(self.pending) > 500:
                self.pending.popitem(last=False)
        return {"ok": True, "should_send": True, "reply": f"{tag} {bodies[0]}" if len(bodies) == 1 else '',
                "turn_id": turn_id, "interaction_id": interaction_id, "max_chars": 150,
                "reply_source": reply_source}

    def _fact_revision(self, product_id, title, candidate_skus, schema=None):
        """Catalog rows + pin/video dependency this reply used (B06)."""
        payload = getattr(self._tls, 'payload', None) or {}
        catalog = self.owner.shopee_mapping_state().get('catalog', {})
        by_id = {clean(item.get('product_id'), 30): item for item in catalog.get('products', [])
                 if isinstance(item, dict)}
        ids = [product_id] if product_id else [str(sku).split(':')[-1] for sku in candidate_skus or []]
        ids = [i for i in ids if i in by_id]
        pin = clean(payload.get('product_id'), 30)
        explicit = bool(product_id) and bool(processor(self.owner, 'chon-san-pham').has_explicit_product_reference(
            clean(payload.get('comment'), 600), {'title': title}))
        video = platform_video_context(self.owner, 'shopee', clean(payload.get('video_id'), 160))
        position = self.owner.current_video_position('shopee') if callable(
            getattr(self.owner, 'current_video_position', None)) else {}
        built = fact_revision.build(by_id, product_ids=ids, pin_id=pin,
            pin_dependent=bool(product_id) and product_id == pin and not explicit,
            video_dependent=bool(video), explicit=explicit,
            lock_id=position.get('lock_id', ''), revision=position.get('revision', 0))
        return reply_schema.annotate_revision(built, schema)

    def delivery(self, payload):
        turn_id = clean(payload.get("turn_id"), 160)
        session_id = clean(payload.get("session_id"), 30)
        receipt = clean(payload.get("message_id"), 160)
        with self.lock:
            durable = self.delivery_store.get(turn_id)
            if not durable or durable["platform"] != "shopee" or durable["room_id"] != session_id:
                return {"ok": False, "error": "unknown_turn"}
            interaction_id = durable["interaction_id"]
            if len(durable['messages']) != 1:
                return {'ok': False, 'error': 'message_receipt_required'}
            if receipt:
                evidence = {"shopee_message_id": receipt}
                state = "sent"
                ok = self.memory.confirm_reply_sent(interaction_id, turn_id,
                    receipt=evidence, allow_previous_session=True)
            else:
                detail = clean(payload.get("error"), 250)
                state = ("unknown" if payload.get("status") == "unknown" else
                         "expired" if detail == "expired before send" else
                         "cancelled" if detail in {"Preview only", "AI disabled before send"} else "failed")
                evidence = {"error": detail}
                ok = self.memory.mark_reply_delivery(interaction_id, turn_id, state, detail,
                                                     allow_previous_session=True)
            if ok:
                ok = self.delivery_store.record_legacy_delivery(turn_id, "shopee", session_id, evidence, state)
            # Keep the pending receipt if persistence fails; a retry must never
            # send the comment again or lose its only association with the turn.
            if ok:
                self.pending.pop(turn_id, None)
                queue = getattr(self.owner, 'comment_queue', None)
                if queue is not None:
                    queue.settle(turn_id, state)
                self.completed[turn_id] = {"session_id": session_id, "receipt": receipt}
                while len(self.completed) > 500:
                    self.completed.popitem(last=False)
            return {"ok": ok, "sent": ok and bool(receipt)}
