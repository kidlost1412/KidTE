"""Hậu kiểm giá/quà/khuyến mãi của câu trả lời (thuật toán giữ nguyên).

Tách nguyên văn từ lớp ReplyEngine trong server.py (không đổi logic).
LƯU Ý: các phương thức ở đây được server.py "nhận nuôi" bằng
engine_mixins.adopt(..., globals()) nên tên toàn cục (ROOT, clean, report_problem,
LIVE_CATALOG_PATH...) được tra trong không gian tên của module server — giữ
nguyên hành vi cũ, kể cả monkeypatch.setattr(server, ...). Không import server ở đây.
"""
from __future__ import annotations

import re

# ruff: noqa: F821  (tên toàn cục lấy từ server qua adopt)


class ReplyGuardsMixin:

    # "555k", "1.110k", "640.000đ", "1,2 triệu" — số tiền trong câu trả lời.
    # Nhánh "triệu" đứng TRƯỚC nhánh số trần: "1 triệu 6", "1 triệu 110", "1tr2", "1 triệu rưỡi" (R1).
    _PRICE_TOKEN = re.compile(
        r"(?<![\d.,])(?P<mil>\d{1,3}(?:[.,]\d{1,3})?)\s?(?:tr|trieu)(?![a-z])"
        r"(?:\s?(?P<tail>\d{1,3})(?![\d.,]?\d)(?:\s?k(?![a-z]))?"
        r"(?!\s*(?:chai|can|cai|lit|ml|kg|goi|thung|hop|bao|ngay|thang|tuan|lan|cay|%))"
        r"|\s(?P<half>ruoi)(?![a-z]))?"
        r"|(?<![\d.,])(\d{1,3}(?:[.,]\d{3})+|\d{1,4})(?:\s?(k|nghin|ngan)(?![a-z])|\s?(d|vnd|dong)(?![a-z])"
        r"|(?=[.,]?\s|$))")

    # Số đứng trước đơn vị đo/đếm là quy cách, liều, diện tích — không phải tiền:
    # "1.000 lít", "2.500 m2", "20.000 cây", "1k lít" (rà soát 17/10 K7).
    # Đơn vị đo RÕ: miễn cả khi có k ("1k lít"). Đơn vị đếm chỉ miễn cho số có dấu
    # ngăn nghìn không k ("20.000 cây"); "150k chai" là giá mỗi chai. Không có
    # "con"/"lan" ("còn", "lần" bỏ dấu) để "150k còn 5 suất" vẫn bị bắt.
    # Đơn vị so trên chữ CÓ DẤU (R3): bỏ dấu thì "hợp"→hop, "cần"→can, "bình", "gọi", "bảo",
    # "sao", "gốc" trùng đơn vị và "Giá 450.000 hợp lý" lọt. Bản không dấu chỉ khi đoạn đó
    # cũng viết không dấu, và chỉ các đơn vị không trùng từ thường (_PLAIN_UNIT).
    _MEASURE_UNIT = re.compile(r"\s*(?:lít|lit|ml|cc|kg|g|gr|gam|gram|m2|m3|ha|hecta)(?!\w)")
    _COUNT_UNIT = re.compile(r"\s*(?:cây|gốc|sào|bao|chai|can|gói|thùng|hộp|bịch|túi|bình(?!\s+(?:thường|tĩnh|yên|dân|luận|chọn))|phuy|tấn|%)(?!\w)")
    _PLAIN_UNIT = re.compile(r"\s*(?:lit|ml|kg|m2|ha|cc)(?![a-z0-9])")
    # "1,5k", "2.5k": số lẻ nghìn.
    _DECIMAL_K = re.compile(r"(?<![\d.,])(\d{1,3}[.,]\d{1,2})\s?(?:k|nghin|ngan)(?![a-z])")
    # "tổng 370 luôn", "hết 1110": số trần sau từ cộng tiền là tiền (nghìn đồng) — chỉ khi sau số
    # là hết câu/dấu câu/từ đệm; "hết 10 ngày", "tổng 12 tháng", "hết 50 cái" không phải tiền (R2).
    _TOTAL_BARE = re.compile(r"\b(?:tong(?: cong)?|het|tinh ra|thanh tien|tat ca la)\s+(\d{2,4})(?![\d.,]?\d)"
                             r"(?=\s*(?:$|[.,!?;)]|(?:nha|nhe|nghe|nhen|luon|a|thoi|do|ha|roi|anh|chi|em|ban|"
                             r"ca nha)(?![a-z])))")

    # Số tiền viết bằng chữ (M.6): "ba trăm nghìn", "một trăm tám lăm nghìn", "hai triệu rưỡi",
    # "năm chục", "trăm rưỡi". Chỉ tính khi kèm đơn vị tiền (nghìn/ngàn/k/triệu/đồng); "X chục"
    # và "trăm rưỡi" đứng cuối câu/trước từ đệm (tiếng lóng giá) — "hai chai", "ba ngày" không bắt.
    _WORD_DIGITS = {"mot": 1, "hai": 2, "ba": 3, "bon": 4, "tu": 4, "nam": 5, "lam": 5, "sau": 6,
                    "bay": 7, "tam": 8, "chin": 9}
    _WORD_TOKEN = re.compile(r"[a-z]+|\d+|[.,!?;:()\n]")
    _WORD_END = frozenset({"nha", "nhe", "nghe", "nhen", "luon", "a", "thoi", "do", "ha", "roi", "anh", "chi",
                           "em", "ban", "ca", "a.", "thui", "ne", "nhaa", "ak"})
    # Chỉ từ không trùng chữ thường khi bỏ dấu ("hợp", "cần", "còn", "hả" không có ở đây).
    _WORD_COUNTED = frozenset({"nguoi", "khach", "luot", "don", "cay", "chai", "lit", "ml", "kg", "thung", "m2"})
    _CLAUSE_GIA = re.compile(r"\bgia\b(?!\s+(?:dinh|tri|vi|han|cong|su))")

    @classmethod
    def _word_group(cls, toks, i):
        """Số 1–999 bằng chữ bắt đầu ở toks[i] → (giá trị, vị trí sau, cờ) hoặc None."""
        digits, n = cls._WORD_DIGITS, len(toks)

        def dig(k):
            return digits.get(toks[k]) if k < n else None
        value, j, hundreds, flags = 0, i, False, set()
        if dig(j) is not None and j + 1 < n and toks[j + 1] == "tram":
            value, j, hundreds = dig(j) * 100, j + 2, True
        elif j < n and toks[j] == "tram":
            value, j, hundreds = 100, j + 1, True
        if hundreds:
            if j < n and toks[j] == "ruoi":
                return value + 50, j + 1, {"ruoi"}
            if j + 1 < n and toks[j] in ("linh", "le") and dig(j + 1) is not None:
                return value + dig(j + 1), j + 2, flags
        if dig(j) is not None and j + 1 < n and toks[j + 1] in ("muoi", "chuc"):
            if toks[j + 1] == "chuc" and not hundreds:
                flags.add("chuc")
            value, j = value + dig(j) * 10, j + 2
            if dig(j) is not None and "chuc" not in flags:
                value, j = value + dig(j), j + 1
        elif j < n and toks[j] == "muoi":
            value, j = value + 10, j + 1
            if dig(j) is not None:
                value, j = value + dig(j), j + 1
        elif dig(j) is not None:
            if hundreds and dig(j + 1) is not None:          # "trăm tám lăm" = 185
                value, j = value + dig(j) * 10 + dig(j + 1), j + 2
            elif hundreds:                                    # "hai trăm tư" = 240
                value, j = value + dig(j) * 10, j + 1
            else:
                value, j = dig(j), j + 1
        if j == i:
            return None
        return value, j, flags

    @classmethod
    def _word_amounts(cls, folded):
        toks = cls._WORD_TOKEN.findall(folded)
        n, i, out = len(toks), 0, set()

        def end_at(k):
            return k >= n or toks[k] in cls._WORD_END or not toks[k].isalnum()
        while i < n:
            group = cls._word_group(toks, i)
            if not group:
                i += 1
                continue
            value, j, flags = group
            unit = toks[j] if j < n else ""
            if unit == "chuc" and j + 1 < n and toks[j + 1] in ("nghin", "ngan"):   # "năm chục nghìn"
                value, j, unit = value * 10, j + 1, toks[j + 1]
            if unit in ("nghin", "ngan", "k", "trieu") and j + 1 < n and toks[j + 1] in cls._WORD_COUNTED:
                i = j + 1                                   # "ba nghìn người", "hai triệu lượt xem"
            elif unit in ("nghin", "ngan", "k"):
                out.add(value * 1000)
                i = j + 1
            elif unit == "trieu":
                amount, j = value * 1_000_000, j + 1
                if j < n and toks[j] == "ruoi":
                    amount, j = amount + 500_000, j + 1
                else:
                    tail = cls._word_group(toks, j)
                    if tail and tail[1] < n and toks[tail[1]] in ("nghin", "ngan", "k"):
                        amount, j = amount + tail[0] * 1000, tail[1] + 1
                    elif tail and end_at(tail[1]):
                        t = tail[0]
                        amount += t * (100_000 if t < 10 else 10_000 if t < 100 else 1000)
                        j = tail[1]
                out.add(amount)
                i = j
            elif unit in ("dong", "d", "vnd"):
                out.add(value)
                i = j + 1
            elif flags & {"chuc", "ruoi"} and end_at(j):           # "năm chục", "trăm rưỡi"
                out.add(value * 1000)
                i = j
            else:
                i += 1
        return out

    @classmethod
    def _unit_after(cls, low, folded, pos, units):
        """Đơn vị ngay sau vị trí pos: so trên chữ có dấu; không dấu chỉ khi đoạn đó viết không dấu."""
        if len(low) == len(folded):
            m = units.match(low, pos)
            if m:
                return True
            m = cls._PLAIN_UNIT.match(folded, pos)
            return bool(m and low[pos:m.end()] == folded[pos:m.end()])
        return bool(cls._PLAIN_UNIT.match(folded, pos))

    @classmethod
    def _goc_after_price(cls, low, folded, start, end):
        """Giá 450.000 gốc: "gốc" sau số là giá gốc (tiền) khi cùng mệnh đề có "giá" phía trước."""
        if not re.match(r"\s*goc(?![a-z])", folded[end:]):
            return False
        clause = re.split(r"[;!?\n]|[.,](?=\s|$)", folded[:start])[-1]
        return bool(cls._CLAUSE_GIA.search(clause))

    @classmethod
    def _price_values(cls, text):
        values = set()
        low = unicodedata.normalize("NFC", clean(text)).casefold().replace("₫", "d")
        folded = fold(low)
        for m in cls._PRICE_TOKEN.finditer(folded):
            if m.group("mil"):
                amount = float(m.group("mil").replace(",", ".")) * 1_000_000
                tail = m.group("tail")
                if tail:
                    amount += int(tail) * 10 ** (6 - len(tail))
                elif m.group("half"):
                    amount += 500_000
                values.add(int(round(amount)))
                continue
            digits, k, dong = m.group(4), m.group(5), m.group(6)
            has_sep = bool(re.search(r"[.,]\d{3}", digits))
            if not (k or dong or has_sep):
                continue                    # số trần (vd "20", "150") không phải giá
            if not dong and k and cls._unit_after(low, folded, m.end(), cls._MEASURE_UNIT):
                continue                    # "1k lít": quy cách/liều
            if not dong and not k and (cls._unit_after(low, folded, m.end(4), cls._MEASURE_UNIT)
                                       or cls._unit_after(low, folded, m.end(4), cls._COUNT_UNIT)):
                if not cls._goc_after_price(low, folded, m.start(4), m.end(4)):
                    continue                # "1.000 lít", "20.000 cây"
            number = int(re.sub(r"[.,]", "", digits))
            values.add(number * 1000 if k else number)
        for m in cls._DECIMAL_K.finditer(folded):
            values.add(int(round(float(m.group(1).replace(",", ".")) * 1000)))
        for m in cls._TOTAL_BARE.finditer(folded):
            number = int(m.group(1))
            values.add(number * 1000 if number < 1000 else number)
        values |= cls._word_amounts(folded)
        # >= 1.000đ: bắt cả "1k", "5k", "5 nghìn" (C2). Số trần không đơn vị đã bỏ ở trên.
        return {v for v in values if v >= 1_000}

    def _invented_price(self, result, live, approved, price_signal, product=None):
        """Câu trả lời có số tiền KHÔNG phải giá của sản phẩm nào → tự tính/bịa.

        Thấy thật 23/09: "cho 2 can đạm cá 5 lít" → "hết 1.110k". Giá sau mã theo
        tài khoản khác nhau nên tổng tự nhân là sai trước mặt khách.
        """
        if result.get("action") != "reply" or not _price_processor.SETTINGS["chan_tu_tinh_gia"]:
            return ""
        said = self._price_values(result.get("reply") or "")
        if not said:
            return ""
        allowed = self._allowed_prices(live, approved, price_signal, product)
        extra = sorted(said - allowed)
        if not extra:
            return ""
        return (f"Câu vừa soạn có số tiền {extra[0]:,}đ không phải giá gốc của sản phẩm nào (có vẻ tự "
                "nhân/cộng hoặc tự đoán). Chỉ nói đúng giá gốc của MỘT sản phẩm trong price.quotable_price; "
                "khách mua nhiều thì mời chọn số lượng trong giỏ, TikTok tự tính tổng theo mã của họ.").replace(",", ".")

    # Câu thay thế khi sau sinh lại vẫn còn số tiền bịa: không có số nào, vẫn có ích.
    _SAFE_PRICE_LINE = "Giá anh/chị xem trong giỏ hàng giúp em nha."

    def _safe_price_line(self, product, approved):
        """Có giá gốc của đúng SKU thì nói đúng giá đó; không có thì mời xem giỏ (không số, không hứa mã)."""
        quotable = (clean((approved or {}).get("gia_goc_duyet"), 40)
                    or clean((product or {}).get("original_price"), 40))
        if not quotable:
            listed, sale = clean((product or {}).get("price"), 40), clean((product or {}).get("sale_price"), 40)
            quotable = listed if listed and listed != sale else ""
        return f"Giá gốc món này {quotable} nha anh/chị." if quotable else self._SAFE_PRICE_LINE

    def _drop_invented_price(self, result, live, approved, price_signal, product=None):
        """Sau lần sinh lại vẫn còn số tiền bịa: bỏ riêng câu có số đó; không còn gì thì câu an toàn (K6)."""
        if result.get("action") != "reply" or not _price_processor.SETTINGS["chan_tu_tinh_gia"]:
            return result
        allowed = self._allowed_prices(live, approved, price_signal, product)

        def bad(sentence):
            return bool(self._price_values(sentence) - allowed)
        reply = result.get("reply") or ""
        messages = result.get("messages") if isinstance(result.get("messages"), list) else []
        if not bad(reply) and not any(bad(str(m)) for m in messages):
            return result
        kept = [s for s in self._SENTENCE_SPLIT.split(reply) if s and not bad(s)]
        logging.getLogger("kid-te.core").warning("dropped invented price sentences",
                                                 extra={"event": "ai_price_dropped"})
        out = dict(result)
        out["reply"] = " ".join(kept).strip() or self._safe_price_line(product, approved)
        if messages:
            msgs = [m for m in (" ".join(k for k in self._SENTENCE_SPLIT.split(str(m)) if k and not bad(k)).strip()
                                for m in messages) if m]
            out["messages"] = msgs or [out["reply"]]
        return out

    def _allowed_prices(self, live, approved, price_signal, product=None):
        allowed = set()
        for item in live.get("products") or []:
            item = item or {}
            allowed |= self._price_values(clean(item.get("original_price"), 80) + " d")
            listed = self._price_values(clean(item.get("price"), 80) + " d")
            sale = self._price_values(clean(item.get("sale_price"), 80) + " d")
            if not (sale and listed == sale):   # price == sale_price là giá sale: không được nói (C1)
                allowed |= listed
        # Số tiền trong khuyến mãi CHỈ của đúng SKU đang bàn ("mua 2 giảm 20k"), không phải mọi SKU (R7).
        allowed |= self._price_values(clean((product or {}).get("promotion"), 3000))
        allowed |= self._price_values(clean((approved or {}).get("gia_goc_duyet"), 80) + " d")
        allowed |= self._price_values(self._promo_text(approved))
        allowed |= self._price_values(json.dumps(price_signal or {}, ensure_ascii=False))
        return allowed

    def _promo_text(self, approved):
        """Chỉ phần khuyến mãi đã duyệt + thông tin shop (không phải cả hồ sơ kỹ thuật)."""
        approved = approved or {}
        parts = [clean(approved.get("live_promotion"), 3000),
                 clean((approved.get("ho_so_da_duyet") or {}).get("promotion"), 3000),
                 json.dumps(getattr(self, "shop_info", {}) or {}, ensure_ascii=False)]
        return " ".join(p for p in parts if p)

    @staticmethod
    def _combined_correction(*problems):
        """Gộp mọi vi phạm vào MỘT lời nhắc sinh lại (C12)."""
        return " ".join(p for p in problems if p)

    # Quà/khuyến mãi khẳng định trong câu trả lời (C5). Chỉ chặn khi AI KHẲNG ĐỊNH
    # mà dữ liệu (promotion của SKU, hồ sơ đã duyệt, thông tin shop) không có.
    # So khớp trên chữ CÓ DẤU: "tặng" khác "tăng" (bỏ dấu thì trùng).
    _PROMO_KINDS = (
        ("qua", re.compile(r"(tặng kèm|tặng thêm|quà tặng|có quà|kèm quà|mua \d+\s*\S*\s*tặng|tặng \d+|tặng ngay|được tặng|"
                           r"sẽ tặng|shop tặng|em tặng|gửi tặng|gửi kèm (?:\d|một|thêm|quà|chai|gói|can|lọ)|"
                           r"(?<!\w)biếu(?!\w)|khuyến mãi thêm)"),
         re.compile(r"tặng|quà|gift|biếu|kèm")),
        ("freeship", re.compile(r"(freeship|free ship|miễn phí (?:vận chuyển|ship|giao)|miễn ship)"),
         re.compile(r"freeship|free ship|miễn phí (?:vận chuyển|ship|giao)|miễn ship|free_?shipping")),
    )
    # So trên chữ CÓ DẤU: "thuốc bột 80%" không phải "bớt 80%"; "giảm 30% liều" là nông học (K8).
    # R5: "giảm tới/hẳn/luôn 20%", "ưu đãi 20%", "20 phần trăm"; "giảm 20% liều/lượng nước" vẫn là nông học.
    _PROMO_PERCENT = re.compile(r"(?<!\w)(?:giảm|sale|off|bớt|ưu đãi|khuyến mãi|chiết khấu)\s*(?:giá\s*)?"
                                r"(?:(?:đến|tới|hẳn|luôn|ngay|thêm|còn)\s*)?(\d{1,2})\s*(?:%|phần trăm(?!\w))"
                                r"(?!\s*(?:liều|lượng|nước|hàm|độ|bệnh|sâu|nấm|năng suất))"
                                r"|(?<!\w)(\d{1,2})\s*%\s*off(?!\w)")
    _AGRO_WORDS = re.compile(r"(?<!\w)(liều|hàm lượng|nồng độ|pha|phun|bón|tưới|bệnh|sâu|nấm|hoạt chất)(?!\w)")
    # Tách cả tại "nhưng/mà/còn/tuy nhiên/song": "Chưa có khuyến mãi nhưng em tặng kèm 1 chai" — vế
    # phủ định chỉ miễn cho chính nó, vế hứa quà sau vẫn bị xét (R4).
    _CLAUSE_SPLIT = re.compile(r"[,;:]\s*|(?<=\?)\s*|\s+(?=(?:nhưng|mà|còn|tuy nhiên|song)(?!\w))", re.I)
    _SENTENCE_SPLIT = re.compile(r"(?<=[.!?\n])\s+")
    # Câu hỏi / phủ định / hẹn kiểm tra: không phải khẳng định có quà.
    _PROMO_HEDGE = re.compile(r"\b(chua co|khong co|ko co|k co|chua thay|kiem tra|check|hoi lai|de em xem|"
                              r"em xem lai|neu co|co khong|hong co)\b")

    # "không có quà", "chưa có chương trình tặng": phủ định trong dữ liệu không phải căn cứ (K9).
    # R6: không còn "hết" chung chung ("Hết hàng lẻ, tặng 1 chai…" là quà thật); tối đa 1 từ chen.
    _NEGATED_PROMO = re.compile(r"(?:không|chưa|ko)\s+(?:có\s+)?(?:\S+\s+)?(?:quà|tặng|freeship|free ship|"
                                r"miễn phí \S+|giảm|khuyến mãi|ưu đãi)\S*(?:\s+(?:quà|tặng|kèm|giá))*")

    def _promo_source(self, product, approved):
        # Căn cứ CHỈ từ dữ liệu khuyến mãi của đúng SKU/hồ sơ + thông tin shop,
        # không phải cả hồ sơ kỹ thuật ("5%" hàm lượng không phải giảm 5%).
        parts = [clean((product or {}).get("promotion"), 3000), self._promo_text(approved)]
        source = unicodedata.normalize("NFC", " ".join(parts)).casefold()
        return self._NEGATED_PROMO.sub(" ", source)

    def _promo_sentences(self, reply, source):
        """Các câu khẳng định quà/%/freeship không có căn cứ trong dữ liệu."""
        bad = []
        for sentence in self._SENTENCE_SPLIT.split(reply or ""):
            if not fold(sentence):
                continue
            hit = False
            # Xét theo MỆNH ĐỀ: "shop tặng kèm 1 chai nha, anh lấy mấy chai ạ?" — dấu
            # hỏi ở mệnh đề sau không miễn cho lời hứa quà ở mệnh đề trước.
            for clause in self._CLAUSE_SPLIT.split(sentence):
                folded = fold(clause)
                if not folded or clause.strip().endswith("?") or self._PROMO_HEDGE.search(folded):
                    continue
                low = unicodedata.normalize("NFC", clause).casefold()
                if any(claim.search(low) and not basis.search(source)
                       for _kind, claim, basis in self._PROMO_KINDS):
                    hit = True
                if self._AGRO_WORDS.search(low):
                    continue
                for m in self._PROMO_PERCENT.finditer(low):
                    pct = m.group(1) or m.group(2)
                    if not re.search(rf"(?<!\d){pct}\s*(?:%|phần trăm)", source):
                        hit = True
            if hit:
                bad.append(sentence)
        return bad

    def _invented_promo(self, result, product, approved):
        if result.get("action") != "reply":
            return ""
        bad = self._promo_sentences(result.get("reply") or "", self._promo_source(product, approved))
        if not bad:
            return ""
        return (f"Câu vừa soạn khẳng định quà/khuyến mãi không có trong dữ liệu: «{clean(bad[0], 120)}». "
                "Chỉ nói quà tặng, % giảm, freeship khi có trong promotion của sản phẩm, hồ sơ đã duyệt hoặc "
                "thong_tin_shop; không có thì bỏ ý đó, không bịa.")

    _SAFE_PROMO_LINE = "Món này hiện em chưa thấy quà tặng kèm, anh/chị xem thêm ưu đãi trong giỏ hàng giúp em nha."

    def _drop_invented_promo(self, result, product, approved):
        """Sau lần sinh lại vẫn bịa quà: bỏ riêng câu vi phạm; không còn gì thì skip."""
        if result.get("action") != "reply":
            return result
        reply = result.get("reply") or ""
        bad = self._promo_sentences(reply, self._promo_source(product, approved))
        if not bad:
            return result
        kept = [s for s in self._SENTENCE_SPLIT.split(reply) if s and s not in bad]
        logging.getLogger("kid-te.core").warning("dropped invented promo sentences=%d", len(bad),
                                                 extra={"event": "ai_promo_dropped"})
        out = dict(result)
        if not "".join(kept).strip():
            # Không im lặng: câu thay thế không hứa gì, vẫn trả lời khách (K8).
            kept = [self._SAFE_PROMO_LINE]
        out["reply"] = " ".join(kept).strip()
        if isinstance(out.get("messages"), list):
            msgs = [m for m in (" ".join(k for k in self._SENTENCE_SPLIT.split(str(m)) if k not in bad).strip()
                                for m in out["messages"]) if m]
            out["messages"] = msgs or [out["reply"]]
        return out

    def _variety_correction(self, result, own_replies=()):
        """Câu soạn xong mà xin lỗi/nhận lỗi công khai, hoặc lặp câu đã gửi gần đây -> lời nhắc sinh lại (1 lần)."""
        if result.get("action") != "reply":
            return ""
        reply = result.get("reply") or ""
        fault = reply_variety.admits_fault(reply)
        if fault:
            return (f"Câu vừa soạn có '{fault}' = công khai nhận lỗi/xin lỗi trước những người xem khác. Viết lại theo "
                    "LUẬT CỨNG: không xin lỗi, không nhận lỗi; ngạc nhiên, quan tâm, hỏi khách chuyện gì, mời nhắn "
                    "riêng TikTok Shop kèm lời hứa tra liền, bằng cách mở câu khác hẳn.")
        old = reply_variety.repeats_recent(reply, [*own_replies, *self._recent_sent_replies()])
        if old:
            return (f"Câu vừa soạn lặp lại cách nói của một câu đã gửi gần đây: «{old}». Viết lại bằng cách mở câu, cách khép, "
                    "từ nối và nhịp khác hẳn, vẫn đúng nội dung và dữ kiện.")
        return ""

    @staticmethod
    def _policy_violation(comment, result, price_signal):
        if result.get("action") != "reply":
            return ""
        reply = result.get("reply") or ""
        # Khach TU BAO dang thay gia thap thi cong nhan ho co ma la dung —
        # do chinh la viec can lam. Chi cam khi khach CHUA noi gi ve gia ma
        # AI da khang dinh ho co ma: luc do la bia, va khach bam vao khong
        # thay ma thi mat niem tin.
        bought_already = bool((price_signal or {}).get("da_mua_roi"))
        if bought_already:
            pushy = _price_processor.pushy_after_purchase(reply, price_signal)
            if pushy:
                return (
                    f"Khách ĐÃ MUA RỒI và đang thắc mắc về giá, mà câu vừa soạn "
                    f"lại thúc '{pushy}'. Viết lại theo hướng chăm sóc sau bán: "
                    "ghi nhận thắc mắc, nói ngắn rằng giá live và mã theo tài "
                    "khoản đổi theo từng thời điểm, mời khách nhắn shop hỗ trợ. "
                    "Không thúc mua thêm, không hứa hoàn tiền."
                )
        claimed = "" if (price_signal and not bought_already) else asserts_voucher(reply)
        if claimed:
            return (
                f"Câu vừa soạn khẳng định '{claimed}' — bạn không nhìn được ví "
                "của khách nên không được nói chắc. Viết lại theo kiểu mời khách "
                "tự kiểm tra: 'nay TikTok hỗ trợ mã lớn lắm, anh xem tài khoản "
                "có hông rồi đặt liền nha'."
            )
        phrase = violates_price_policy(reply, price_signal)
        if phrase:
            return (
                f"Câu vừa soạn chứa '{phrase}' để bác mức giá khách đang thấy. "
                "Viết lại: công nhận đó là mã TikTok theo tài khoản của khách và "
                "thúc khách đặt liền. Không phủ nhận con số của khách."
            )
        if denies_catalog(comment, reply):
            return (
                "Câu vừa soạn trả lời shop không có mặt hàng khách hỏi. Bạn chỉ "
                "thấy một phần danh mục nên không được kết luận như vậy. Viết lại "
                "bằng một câu hỏi ngắn để làm rõ, hoặc nêu dòng gần nhất đang bán."
            )
        return ""
