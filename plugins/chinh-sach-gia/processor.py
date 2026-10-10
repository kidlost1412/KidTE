"""Chính sách giá thuần; không I/O, không import plugin khác."""
from __future__ import annotations

import re

from knowledge import clean, fold


# Những cách nói đã thực sự làm mất đơn trong LIVE ngày 10/09: AI bác con số
# khách đang nhìn thấy trên tài khoản của họ. Yêu cầu viết lại, không loại bỏ câu trả lời ở đầu ra.
FORBIDDEN_PRICE_PHRASES = (
    "không có", "khong co", "không phải giá", "không phải là giá",
    "giá đúng là", "làm gì có", "sai rồi", "bạn nhầm", "anh nhầm",
    "chị nhầm", "em không thấy",
)
# Chi nhung cach noi KHANG DINH khach dang co ma. "anh xem tài khoản có
# hông" hay "TikTok hỗ trợ mã lớn" thi khong sao — do la moi kiem tra.
# Chieu nguoc lai cung sai — KHANG DINH khach dang co ma — nhung no khong
# phu thuoc vao price_signal nen nam rieng o VOUCHER_ASSERTIONS o day.
VOUCHER_ASSERTIONS = (
    "đang có mã", "dang co ma", "có mã rồi", "co ma roi",
    "tài khoản anh có mã", "tài khoản chị có mã", "tài khoản mình có mã",
    "chắc chắn có mã", "ai cũng có mã", "đều có mã", "có sẵn mã",
)
# Đơn vị quy cách đứng ngay sau một con số: số đó là dung tích/khối lượng,
# không phải giá. "can 5 lít", "chai 500ml", "gói 1kg", "bình 20l".
_PACKAGE_UNIT_AFTER = re.compile(
    r"\s*(lít|lit|l|ml|kg|gr|gram|g|cc)(?![a-zà-ỹ])", re.I)
# Khach ke lai mot lan mua DA XONG. Khac han khach dang hoi de mua.
_ALREADY_BOUGHT = re.compile(
    r"(vừa|mới|đã|hôm\s*(?:qua|trước)|bữa\s*trước|lần\s*trước|tuần\s*trước)"
    r"[^.!?]{0,24}?"
    r"(nhận|mua|đặt|order|lấy|chốt|ship|giao)"
    r"|(?:đơn|hàng)\s*(?:của\s*)?(?:em|anh|chị|mình|tui|tôi)\b"
    r"|(?:mua|đặt|nhận)\s*(?:rồi|xong)\b", re.I,
)
_DENIAL_QUESTION = re.compile(r"\b(có|còn|bán)\b.{0,40}\b(không|ko|hông|hong|k)\b", re.I)
_DENIAL_ANSWER = re.compile(r"^\s*(dạ\s*)?(không|ko|chưa)\s*(có|bán|còn)\b", re.I)
# Khách ĐÃ MUA mà AI vẫn thúc mua thêm: câu chăm sóc sau bán bị hỏng.
PUSHY_PHRASES = ("chốt liền", "đặt liền", "chốt đi", "đặt đi", "mua liền", "chốt ngay")
DEFAULT_VOUCHER_PITCH = ("gốc 289k đó anh, mà nay TikTok hỗ trợ mã lớn lắm, "
                         "anh xem tài khoản có hông rồi đặt liền nha")

# Người vận hành chỉnh ở Trợ lý tự động → Chính sách giá.
SETTINGS_SCHEMA = [
    {"key": "nhac_ma_tiktok", "type": "bool", "default": True, "group": "Khi khách hỏi giá",
     "label": "Nói giá xong thì nhắc khách xem mã TikTok trên tài khoản",
     "help": "Tắt thì AI chỉ báo giá gốc, không thêm câu mời kiểm tra mã."},
    {"key": "mau_nhac_ma", "type": "text", "default": DEFAULT_VOUCHER_PITCH, "group": "Khi khách hỏi giá",
     "label": "Câu mẫu (AI học nhịp, không chép nguyên văn)", "max_length": 300, "required": True,
     "help": "Không ghi số tiền mã, số lượng mã hay hạn giờ — AI không được bịa những thứ đó."},
    {"key": "chan_tu_tinh_gia", "type": "bool", "default": True, "group": "Soát câu trả lời",
     "label": "Yêu cầu viết lại số tiền AI tự tính (vd 2 can = 1.110k)",
     "help": "Giá sau mã mỗi tài khoản một khác, nên AI chỉ được nói giá gốc của MỘT sản phẩm."},
    {"key": "chan_bac_gia_khach", "type": "bool", "default": True, "group": "Soát câu trả lời",
     "label": "Yêu cầu viết lại câu bác bỏ mức giá khách đang thấy",
     "help": "Khách báo giá thấp hơn là do mã trên tài khoản họ — bác bỏ là mất đơn (LIVE 10/09)."},
    {"key": "cum_tu_bac_gia", "type": "list", "default": list(FORBIDDEN_PRICE_PHRASES), "group": "Soát câu trả lời",
     "label": "Cụm từ bị coi là bác giá", "depends": "chan_bac_gia_khach",
     "help": "Mỗi dòng một cụm. Chỉ áp dụng khi khách vừa tự báo một mức giá."},
    {"key": "chan_khang_dinh_co_ma", "type": "bool", "default": True, "group": "Soát câu trả lời",
     "label": "Yêu cầu viết lại câu khẳng định chắc khách đang có mã",
     "help": "AI không nhìn được ví của khách; chỉ được mời khách tự kiểm tra."},
    {"key": "cum_tu_khang_dinh_ma", "type": "list", "default": list(VOUCHER_ASSERTIONS), "group": "Soát câu trả lời",
     "label": "Cụm từ bị coi là khẳng định có mã", "depends": "chan_khang_dinh_co_ma"},
    {"key": "cham_soc_sau_ban", "type": "bool", "default": True, "group": "Soát câu trả lời",
     "label": "Khách đã mua rồi thì không được thúc mua thêm",
     "help": "Vd khách hỏi \"vừa mua 90k giờ thấy 77k là sao\" — đó là câu chăm sóc, không phải chốt đơn."},
    {"key": "cum_tu_thuc_mua", "type": "list", "default": list(PUSHY_PHRASES), "group": "Soát câu trả lời",
     "label": "Cụm từ bị coi là thúc mua", "depends": "cham_soc_sau_ban"},
    {"key": "chan_noi_khong_co_hang", "type": "bool", "default": True, "group": "Soát câu trả lời",
     "label": "Yêu cầu viết lại câu nói shop \"không có / không bán\" mặt hàng khách hỏi",
     "help": "AI chỉ thấy một phần danh mục nên không đủ căn cứ để nói không có."},
]
SETTINGS = {field["key"]: (list(field["default"]) if isinstance(field["default"], list)
                           else field["default"]) for field in SETTINGS_SCHEMA}


def configure(values):
    SETTINGS.update(values or {})


def price_to_vnd(value):
    """Best-effort parser for prices shoppers type in Vietnamese live chat."""
    text = clean(value, 80).casefold().replace("₫", "")
    match = re.fullmatch(
        r"\s*(\d+(?:[.,]\d+)*)\s*(k|nghìn|ngàn|tr|triệu)?\s*", text, re.I,
    )
    if not match:
        return None
    raw, unit = match.groups()
    unit = (unit or "").casefold()
    if unit in {"k", "nghìn", "ngàn"}:
        try:
            return int(round(float(raw.replace(",", ".")) * 1_000))
        except ValueError:
            return None
    if unit in {"tr", "triệu"}:
        try:
            return int(round(float(raw.replace(",", ".")) * 1_000_000))
        except ValueError:
            return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    amount = int(digits)
    return amount * 1_000 if 10 <= amount <= 999 else amount


def customer_price_signal(comment, product, memory_context=""):
    """Describe a shopper-observed price without treating it as merchant truth.

    TikTok can apply account-specific vouchers/coins at checkout, so a shopper's
    lower figure is a buying signal, not automatically an error to correct.
    """
    text = clean(comment, 500)
    folded = text.casefold()
    price_word = bool(re.search(r"\b(giá|đang|còn|thấy|hiện|mã|voucher)\b", folded))
    candidates = re.finditer(
        r"(?<!\w)(\d+(?:[.,]\d+)*)\s*(k|nghìn|ngàn|tr|triệu|₫)?(?!\w)",
        folded, re.I,
    )
    reported = None
    reported_text = ""
    for match in candidates:
        number, unit = match.group(1), match.group(2)
        # "đạm cá can 5 lít giá nhiêu": số 5 là QUY CÁCH chứ không phải giá.
        # Bản cũ đọc thành "khách báo giá 5đ" rồi trả lời "giá 5 ngon quá,
        # tài khoản anh đang có mã rồi" — vừa vô nghĩa vừa bịa chuyện mã.
        if not unit and _PACKAGE_UNIT_AFTER.match(folded, match.end()):
            continue
        token = f"{number}{unit or ''}"
        parsed = price_to_vnd(token)
        # Không có đơn vị mà ra dưới 1.000₫ thì chắc chắn không phải giá.
        if parsed and parsed < 1000 and not unit:
            continue
        if parsed and (unit or price_word):
            reported, reported_text = parsed, token
            break
    if reported is None and re.fullmatch(r"\s*\d{2,3}\s*", folded):
        # A bare number is only promoted to a price when the same customer's
        # recent turns clearly contain a price discussion.
        if re.search(r"\b(giá|bao nhiêu|\d+\s*k)\b", memory_context.casefold()):
            reported, reported_text = price_to_vnd(folded), folded.strip()
    if reported is None:
        return {}

    def known_price(field):
        value = clean((product or {}).get(field), 80)
        return price_to_vnd(value) if value else None

    original = known_price("original_price")
    sale = known_price("sale_price") or known_price("price")
    reference = sale or original
    relation = ("lower_than_shop_visible_price" if reference and reported < reference
                else "higher_than_shop_visible_price" if reference and reported > reference
                else "matches_or_unknown")
    bought_already = bool(_ALREADY_BOUGHT.search(folded))
    signal = {
        "kind": "shopper_visible_price_report", "reported_price_text": reported_text,
        "reported_price_vnd": reported,
        "shop_original_price": clean((product or {}).get("original_price"), 80),
        "relation": relation, "da_mua_roi": bought_already,
        "interpretation": (
            "Đây là giá khách đang nhìn thấy, có thể là giá cuối sau mã TikTok, "
            "xu/ưu đãi theo tài khoản hoặc tại checkout; không tự động xem là khách nói sai."
        ),
        "response_goal": (
            "CẤM phủ nhận con số khách nói. Công nhận là mã theo tài khoản của "
            "khách, rồi thúc chốt ngay. Không bịa số lượng mã, hạn giờ hay khẳng "
            "định mọi tài khoản đều có."
        ),
        "forbidden_phrases": tuple(SETTINGS["cum_tu_bac_gia"]),
    }
    if bought_already:
        # 12/09: khach noi "vua nhan 1 lo TC01 gia 90k ma gio xem lai 77k la
        # sao nhi" — khach DA MUA va dang thac mac gia tut. AI tra loi "77k la
        # gia sau ma cua anh do, chot lien di nha", tuc la thuc mot nguoi vua
        # mua xong di mua tiep, va lo luon chuyen ho dang kho chiu.
        signal["interpretation"] = (
            "Khách ĐÃ MUA RỒI và đang thắc mắc vì sao giá bây giờ khác lúc họ mua. "
            "Đây KHÔNG phải khách đang cân nhắc mua."
        )
        signal["response_goal"] = (
            "Đây là câu chăm sóc sau bán, không phải câu chốt đơn. TUYỆT ĐỐI "
            "không thúc khách đặt thêm, không nói 'chốt liền'. Ghi nhận thắc mắc "
            "một cách tử tế, giải thích ngắn rằng giá trên live và mã theo từng "
            "tài khoản thay đổi theo từng thời điểm, và mời khách nhắn shop để "
            "được hỗ trợ. Không hứa hoàn tiền hay bù giá."
        )
    return signal


def price_context(product, approved=None):
    """Con số duy nhất được phép nói khi khách hỏi giá.

    Chủ dự án chốt: luôn nói giá gốc, KHÔNG nói giá sale đang chạy trên live,
    rồi đẩy khách tự kiểm tra mã TikTok trên tài khoản của họ.

    Thứ tự lấy giá: giá gốc chủ dự án khai trong hồ sơ đã duyệt > giá gốc API
    trả về > giá shop đang niêm yết. Bậc đầu tồn tại vì API TikTok không trả
    original_price cho mọi SKU (Đạm cá 5L là một ví dụ), mà im lặng ở câu hỏi
    giá thì mất đơn.
    """
    declared = clean((approved or {}).get("gia_goc_duyet"), 80)
    original = clean((product or {}).get("original_price"), 80)
    # Hom nay TikTok chi tra original_price khi CO giam gia, nen khong co no
    # nghia la gia niem yet chinh la gia goc. Neu mai nay API tra price khac
    # sale_price thi price moi la gia truoc giam — lay price, khong lay sale.
    # TUYET DOI khong duoc rot xuong sale_price: do la gia dang giam tren
    # live, khong phai gia duoc phep noi. Thieu ca 3 bac tren thi coi nhu
    # khong co du lieu, khong doan bang sale_price.
    listed = clean((product or {}).get("price"), 80)
    quotable = declared or original or listed
    if not quotable:
        return {}
    source = "gia_goc_da_duyet" if declared else "original_price" if original else "shop_listed_price"
    out = {
        "quotable_price": quotable, "quotable_price_source": source,
        "rule": (
            "Khi khách hỏi giá, chỉ được nói đúng con số quotable_price. TUYỆT ĐỐI "
            "không nói giá sale/giá đang giảm trên live, không tự tính giá sau mã, "
            "không nói 'còn ... k'."
        ),
    }
    if SETTINGS["nhac_ma_tiktok"]:
        out["voucher_pitch"] = (
            "Ngay sau khi nói giá, nhắc khách tự kiểm tra mã trên tài khoản vì hôm "
            f"nay TikTok hỗ trợ mã lớn, rồi thúc đặt liền. Mẫu nhịp: '{SETTINGS['mau_nhac_ma']}'. "
            "Không bịa số tiền mã, số lượng mã hay hạn giờ."
        )
    else:
        out["voucher_pitch"] = "Chỉ báo giá gốc; KHÔNG nhắc khách kiểm tra mã hay voucher."
    return out


def price_policy_text():
    """Chính sách giá đặt ở phần prompt cố định của phiên."""
    text = ("Chỉ nói con số trong price.quotable_price. Không nói giá sale, "
            "không tự tính giá sau mã. ")
    if SETTINGS["nhac_ma_tiktok"]:
        text += "Nói giá xong thì đẩy khách kiểm tra mã TikTok trên tài khoản của họ rồi chốt. "
    return text + ("Nếu khách tự báo một mức giá thấp hơn, công nhận đó là mã của tài "
                   "khoản họ và thúc chốt.")


def public_product(item, pinned=False):
    """Bản rút gọn đưa cho model: không có giá sale để khỏi lỡ miệng nói ra."""
    item = item or {}
    return {
        "product_id": clean(item.get("product_id"), 160),
        "title": clean(item.get("title"), 220),
        "original_price": clean(item.get("original_price"), 80),
        "promotion": clean(item.get("promotion"), 600),
        "stock_num": item.get("stock_num") or 0, "is_pinned": bool(pinned),
    }


def asserts_voucher(reply):
    """Câu có khẳng định chắc nịch là khách đang có mã không."""
    if not SETTINGS["chan_khang_dinh_co_ma"]:
        return ""
    folded = fold(reply)
    return next((phrase for phrase in SETTINGS["cum_tu_khang_dinh_ma"] if fold(phrase) in folded), "")


def violates_price_policy(reply, signal):
    if not signal or not SETTINGS["chan_bac_gia_khach"]:
        return ""
    folded = fold(reply)
    return next((phrase for phrase in SETTINGS["cum_tu_bac_gia"] if fold(phrase) in folded), "")


def pushy_after_purchase(reply, signal):
    """Khách đã mua rồi mà câu trả lời vẫn thúc mua: trả cụm từ vi phạm."""
    if not SETTINGS["cham_soc_sau_ban"] or not (signal or {}).get("da_mua_roi"):
        return ""
    folded = fold(reply)
    return next((phrase for phrase in SETTINGS["cum_tu_thuc_mua"] if fold(phrase) in folded), "")


def denies_catalog(comment, reply):
    """AI chỉ thấy 5 hồ sơ trên hàng chục SKU nên không đủ tư cách nói 'không có'."""
    if not SETTINGS["chan_noi_khong_co_hang"]:
        return False
    return bool(_DENIAL_QUESTION.search(clean(comment, 300)) and
                _DENIAL_ANSWER.search(clean(reply, 300)))
