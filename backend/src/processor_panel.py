"""Trang chi tiết từng bộ xử lý của AI: giải thích, cài đặt, thử ngay.

Không có logic nghiệp vụ riêng: mọi phép "thử" gọi ĐÚNG hàm mà lượt trả lời
thật dùng, nên kết quả trên màn hình là kết quả AI sẽ có khi LIVE.
"""
from __future__ import annotations

from config import RAW_STREAM_DIR, SYSTEM_PROMPT_OVERRIDE_PATH
from knowledge import clean


ABOUT = {
    "chinh-sach-gia": {
        "what": "Quyết định AI được nói con số giá nào, và chặn những câu nói sai về giá trước khi gửi.",
        "how": [
            "Khi khách hỏi giá: AI chỉ được nói giá gốc — ưu tiên giá gốc đã duyệt trong hồ sơ, rồi giá gốc TikTok trả về, cuối cùng là giá niêm yết. Không bao giờ nói giá sale đang chạy.",
            "Khách tự báo một mức giá (\"mình thấy 77k\"): coi là giá sau mã trên tài khoản của khách, không bác bỏ.",
            "Câu AI soạn ra vi phạm một luật bên dưới → AI phải viết lại một lần; vẫn sai thì KHÔNG gửi, câu đó hiện trong Hộp thư để người trực xử lý.",
        ],
        "links": [["Hồ sơ sản phẩm (giá gốc đã duyệt)", "catalog", "profiles"], ["Hộp thư — câu bị chặn", "inbox", "unanswered"]],
        "try": "price",
    },
    "chon-san-pham": {
        "what": "Đoán khách đang hỏi về sản phẩm nào để AI lấy đúng hồ sơ, giá và liều của sản phẩm đó.",
        "how": [
            "Tên gọi khác chủ shop đã dạy (Trí nhớ AI) được xét trước tiên.",
            "Sau đó chấm điểm từng sản phẩm trong giỏ theo chữ khách gõ: chữ hiếm (tc01, humic) nặng ký, chữ có ở nhiều tiêu đề (phân, cây) gần như không tính, chữ ở đuôi tiêu đề (từ khoá SEO) tính rất nhẹ.",
            "Điểm cao nhất chưa đạt ngưỡng → giữ sản phẩm đang ghim, vì đó là thứ khách đang nhìn thấy.",
        ],
        "links": [["Tên gọi khác đã dạy", "brain", "items"], ["Giỏ hàng đang bán", "catalog", ""]],
        "try": "select",
    },
    "lieu-dung": {
        "what": "Tính liều pha cho đúng cỡ bình khách hỏi, chỉ dựa trên hướng dẫn in trên nhãn đã duyệt.",
        "how": [
            "Đọc các dòng hướng dẫn kiểu \"20ml cho bình 16–25 lít\", \"50gr vào bình 16–30 lít\", \"bình 16 lít: 25–30ml\".",
            "Bình khách hỏi nằm trong khoảng của nhãn → nói đúng số trên nhãn. Ngoài khoảng → quy đổi từ mốc gần nhất và đánh dấu là quy đổi.",
            "Combo: tính riêng từng thành phần, không bao giờ cộng dồn.",
            "Sản phẩm không có dòng hướng dẫn đọc được → AI không tự đoán liều, câu hỏi vào Hộp thư.",
        ],
        "links": [["Sửa hướng dẫn sử dụng trong hồ sơ", "catalog", "profiles"]],
        "try": "dose",
    },
    "model-deepseek": {
        "what": "Gửi toàn bộ ngữ cảnh (sản phẩm, giá, liều, trí nhớ khách, điều đã dạy) cho DeepSeek và nhận câu trả lời.",
        "how": [
            "Phần cố định của phiên (danh mục, chính sách, phong cách) đi trước để DeepSeek dùng bộ nhớ đệm — rẻ và nhanh hơn nhiều.",
            "Mạng chập chờn → thử lại một lần. Vẫn lỗi → không gửi gì (không bao giờ gửi câu đoán mò).",
            "Câu dài quá khung TikTok → nhờ AI tự viết gọn; bất đắc dĩ mới cắt ở hết câu gần nhất.",
        ],
        "links": [["Khoá API & chọn model", "system", "setup"], ["Thử AI với câu thật", "brain", "try"]],
        "try": "reply",
    },
    "thu-du-lieu": {
        "what": "Ghi lại mọi bình luận của phiên LIVE (kể cả khi AI tắt) — nguồn dữ liệu của Hộp thư, Khách hàng và việc AI tự học.",
        "how": [
            "Mỗi ngày một file trong thư mục dữ liệu; bình luận trùng (hai tab, gửi lại sau mất mạng) bị loại.",
            "Câu chủ shop tự gõ được đánh dấu để AI học giọng và kiến thức sau mỗi LIVE.",
        ],
        "links": [["Hộp thư", "inbox", ""], ["Khách hàng", "customers", ""]],
        "try": "",
    },
}

REASON_TEXT = {
    "enough_evidence": "Chữ khách gõ khớp rõ với sản phẩm này.",
    "deictic_keeps_pinned": "Khách nói kiểu \"chai này / loại đó\" và không nêu mã riêng của sản phẩm khác → giữ sản phẩm đang ghim.",
    "ambiguous_keeps_pinned": "Chưa sản phẩm nào đủ điểm → giữ sản phẩm đang ghim.",
    "ambiguous_first": "Chưa sản phẩm nào đủ điểm và không có sản phẩm đang ghim → tạm lấy sản phẩm điểm cao nhất.",
    "no_products": "Chưa có giỏ hàng — mở TikTok LIVE Manager để tiện ích quét giỏ.",
}


class ProcessorPanel:
    def __init__(self, engine, modules, store):
        self.engine = engine
        self.modules = dict(modules)
        self.store = store
        self.apply_all()

    def apply_all(self):
        for plugin_id, module in self.modules.items():
            schema = getattr(module, "SETTINGS_SCHEMA", None)
            if schema:
                module.configure(self.store.values(plugin_id, schema))

    def _module(self, plugin_id):
        module = self.modules.get(plugin_id)
        if module is None or not getattr(module, "SETTINGS_SCHEMA", None):
            raise ValueError("processor_not_found")
        return module

    # ------------------------------------------------------------ dữ liệu phụ
    def _products(self):
        live = dict(self.engine.catalog.snapshot or {})
        pin = clean(live.get("pin_id"), 160)
        rows = [{"product_id": clean(p.get("product_id"), 160), "title": clean(p.get("title"), 160),
                 "pinned": clean(p.get("product_id"), 160) == pin}
                for p in live.get("products") or [] if isinstance(p, dict) and p.get("product_id")]
        return rows

    def _approved(self, product):
        shop_id = clean((product or {}).get("product_id"), 160)
        record = self.engine.knowledge.match((product or {}).get("title", ""), "", product_id=shop_id)
        return record, self.engine.knowledge.context(record, shop_id=shop_id)

    def _live_product(self, product_id):
        for item in (self.engine.catalog.snapshot or {}).get("products") or []:
            if isinstance(item, dict) and clean(item.get("product_id"), 160) == product_id:
                return item
        return None

    def _stats(self, plugin_id):
        if plugin_id == "thu-du-lieu":
            module = self.modules[plugin_id]
            stats = module.stream_stats(RAW_STREAM_DIR)
            recorder = getattr(self.engine, "raw_stream", None)
            stats["today_rows"] = getattr(recorder, "count", 0)
            stats["today_duplicates"] = getattr(recorder, "duplicates", 0)
            stats["folder"] = str(RAW_STREAM_DIR)
            return stats
        if plugin_id == "lieu-dung":
            return self._dose_coverage()
        if plugin_id == "model-deepseek":
            responder = self.engine.responder
            module = self.modules[plugin_id]
            try:
                balance = responder.check_balance() if hasattr(responder, "check_balance") else {}
            except Exception as exc:  # mạng lỗi không được làm hỏng cả trang
                balance = {"ok": False, "error": type(exc).__name__}
            return {"model": getattr(responder, "model", ""),
                    "model_label": (getattr(responder, "model_labels", None) or {}).get(getattr(responder, "model", ""), ""),
                    "key_configured": getattr(responder, "client", None) is not None,
                    "balance_ok": bool(balance.get("ok")), "balance_error": balance.get("error", ""),
                    "prompt_custom": SYSTEM_PROMPT_OVERRIDE_PATH.is_file(),
                    "prompt": module.system_prompt(), "prompt_default": module.default_system_prompt()}
        return {}

    def _dose_coverage(self):
        """Sản phẩm nào trong giỏ AI tính được liều, sản phẩm nào không (và vì sao)."""
        dose = self.modules["lieu-dung"]
        rows = []
        for item in (self.engine.catalog.snapshot or {}).get("products") or []:
            if not isinstance(item, dict) or not item.get("product_id"):
                continue
            record, approved = self._approved(item)
            directions = dose.approved_directions(approved)
            readable = [line for line in directions if dose.parse_dose_line(line)]
            if dose.is_combo_record(approved):
                readable = [line for comp in approved.get("combo_components") or []
                            for line in comp.get("huong_dan_su_dung") or [] if dose.parse_dose_line(line)]
            status = ("ok" if readable else "no_ratio" if directions else "no_profile" if not record
                      else "no_directions")
            rows.append({"product_id": clean(item.get("product_id"), 160), "title": clean(item.get("title"), 120),
                         "status": status, "directions": directions[:4]})
        order = {"no_ratio": 0, "no_directions": 1, "no_profile": 2, "ok": 3}
        rows.sort(key=lambda row: (order[row["status"]], row["title"]))
        return {"coverage": rows, "ok": sum(r["status"] == "ok" for r in rows), "total": len(rows)}

    # ------------------------------------------------------------ API
    def detail(self, plugin_id):
        module = self._module(plugin_id)
        schema = module.SETTINGS_SCHEMA
        manifest = (getattr(getattr(self.engine, "plugin_runtime", None), "manifests", None) or {}).get(plugin_id)
        about = ABOUT.get(plugin_id, {})
        return {"ok": True, "id": plugin_id, "name": getattr(manifest, "name", plugin_id),
                "version": getattr(manifest, "version", ""),
                "about": {"what": about.get("what", ""), "how": about.get("how", [])},
                "links": [{"label": label, "page": page, "sub": sub} for label, page, sub in about.get("links", [])],
                "schema": schema, "values": self.store.values(plugin_id, schema),
                "changed": self.store.changed_keys(plugin_id, schema),
                "try": about.get("try", ""), "products": self._products(),
                "stats": self._stats(plugin_id)}

    def save(self, plugin_id, updates):
        module = self._module(plugin_id)
        values = self.store.save(plugin_id, module.SETTINGS_SCHEMA, updates)
        module.configure(values)
        if plugin_id == "thu-du-lieu" and values.get("giu_so_ngay"):
            module.prune_old_streams(RAW_STREAM_DIR, values["giu_so_ngay"])
        return self.detail(plugin_id)

    def reset(self, plugin_id):
        module = self._module(plugin_id)
        module.configure(self.store.reset(plugin_id, module.SETTINGS_SCHEMA))
        return self.detail(plugin_id)

    def save_prompt(self, text):
        text = str(text or "").strip()
        if text:
            if len(text) > 60000:
                raise ValueError("prompt_too_long")
            SYSTEM_PROMPT_OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
            SYSTEM_PROMPT_OVERRIDE_PATH.write_text(text + "\n", encoding="utf-8")
        elif SYSTEM_PROMPT_OVERRIDE_PATH.is_file():
            SYSTEM_PROMPT_OVERRIDE_PATH.unlink()        # rỗng = về bản gốc
        return self.detail("model-deepseek")

    def try_it(self, plugin_id, payload):
        self._module(plugin_id)
        comment = clean(payload.get("comment"), 500)
        if plugin_id == "chinh-sach-gia":
            return self._try_price(comment, payload)
        if plugin_id == "chon-san-pham":
            return self._try_select(comment, payload)
        if plugin_id == "lieu-dung":
            return self._try_dose(comment, payload)
        if plugin_id == "model-deepseek":
            if len(comment) < 2:
                raise ValueError("comment_required")
            return self.engine.learning_try({"comment": comment, "pin_id": clean(payload.get("pin_id"), 160)})
        raise ValueError("processor_has_no_try")

    def _try_price(self, comment, payload):
        price = self.modules["chinh-sach-gia"]
        product = self._live_product(clean(payload.get("product_id"), 160))
        if not product:
            raise ValueError("product_required")
        _record, approved = self._approved(product)
        signal = price.customer_price_signal(comment, product, "") if comment else {}
        out = {"ok": True, "price": price.price_context(product, approved), "signal": signal,
               "product": {"title": clean(product.get("title"), 160),
                           "original_price": clean(product.get("original_price"), 40),
                           "sale_price": clean(product.get("sale_price") or product.get("price"), 40),
                           "declared": clean((approved or {}).get("gia_goc_duyet"), 40)}}
        reply = clean(payload.get("reply"), 300)
        if reply:
            live = dict(self.engine.catalog.snapshot or {})
            result = {"action": "reply", "reply": reply}
            problem = (self.engine._policy_violation(comment, result, signal)
                       or self.engine._invented_price(result, live, approved, signal))
            out["reply_check"] = {"blocked": bool(problem), "reason": problem}
        return out

    def _try_select(self, comment, payload):
        if len(comment) < 2:
            raise ValueError("comment_required")
        selector = self.modules["chon-san-pham"]
        live = dict(self.engine.catalog.snapshot or {})
        live.setdefault("products", [])
        pin = clean(payload.get("pin_id"), 160) or clean(live.get("pin_id"), 160)
        live["pin_id"] = pin
        explained = selector.explain_selection(comment, live["products"], pin)
        final = self.engine._select_product(comment, live) if live["products"] else {"product": None}
        chosen = final.get("product") or {}
        by_title = explained.get("product") or {}
        if final.get("learned_product"):
            reason = "Tên gọi khác chủ shop đã dạy trong Trí nhớ AI trỏ tới sản phẩm này."
        elif chosen and chosen.get("product_id") != by_title.get("product_id"):
            reason = "Khách hỏi kiểu \"nên dùng gì cho…\" → lấy sản phẩm phù hợp nhất theo hồ sơ."
        else:
            reason = REASON_TEXT.get(explained["reason"], explained["reason"])
        return {"ok": True, "words": explained["words"], "threshold": explained["threshold"],
                "ranked": explained["ranked"], "reason": reason,
                "chosen": {"product_id": clean(chosen.get("product_id"), 160), "title": clean(chosen.get("title"), 160)},
                "missing": final.get("missing") or []}

    def _try_dose(self, comment, payload):
        dose = self.modules["lieu-dung"]
        product = self._live_product(clean(payload.get("product_id"), 160))
        if not product:
            raise ValueError("product_required")
        _record, approved = self._approved(product)
        liters = dose.target_tank_liters(comment)
        return {"ok": True, "target_liters": liters, "directions": dose.approved_directions(approved),
                "is_combo": dose.is_combo_record(approved),
                "rows": dose.verified_dose_calculations(comment, approved, dose_intent=True)}
