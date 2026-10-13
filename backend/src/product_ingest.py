"""Thêm sản phẩm bằng AI: gom file/ảnh/địa chỉ web/chữ dán -> AI đọc -> hồ sơ NHÁP.

Luồng: ``prepare_sources`` (đọc + giới hạn đầu vào) -> ``ProductIngest.extract`` (chọn
Gemini/DeepSeek, hỏi JSON chặt, kiểm tra, thử lại 1 lần) -> chủ shop sửa trong ngăn kéo
-> ``create_draft`` (hồ sơ nháp, KHÔNG duyệt) -> ``suggest_skus`` (gợi ý gắn với giỏ hàng,
chủ shop xác nhận bằng ``link_draft``).

Quy tắc cứng: giá, khuyến mãi, quà tặng KHÔNG bao giờ vào hồ sơ (chúng chỉ theo giỏ hàng
hiện hành). Trường không đọc rõ phải là null và bị đánh dấu "cần xem".
"""
from __future__ import annotations

import base64
import io
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from ai_context import system_with_brief, task_block
from ai_providers import AIProviderError, choose_provider
from documents import extract_text

# ------------------------------------------------------------------ giới hạn
MAX_IMAGES = 8
MAX_FILES = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 28 * 1024 * 1024
MAX_PASTED_CHARS = 60_000
MAX_SOURCE_CHARS = 30_000
MAX_PROMPT_CHARS = 90_000
URL_MAX_BYTES = 2 * 1024 * 1024
URL_IMAGE_MAX_BYTES = 5 * 1024 * 1024
URL_TIMEOUT = 10
URL_MAX_REDIRECTS = 3
CONFIDENCE_LOW = 0.6
SESSION_TTL = 3600
MAX_SESSIONS = 8

TEXT_FIELDS = ("ten_san_pham", "nha_san_xuat", "mo_ta_ngan", "phan_khuc", "quy_cach_dong_goi",
               "hieu_qua_chinh", "luu_y")
LIST_FIELDS = ("cong_dung", "doi_tuong_cay_trong", "huong_dan_su_dung")
PAIRS_FIELD = "thanh_phan_dang_ky"            # {tên hoạt chất: hàm lượng}
FIELD_ORDER = ("ten_san_pham", "nha_san_xuat", "quy_cach_dong_goi", "thanh_phan_dang_ky", "cong_dung",
               "doi_tuong_cay_trong", "huong_dan_su_dung", "luu_y", "mo_ta_ngan", "phan_khuc",
               "hieu_qua_chinh")
ALL_FIELDS = frozenset(FIELD_ORDER)

# Giá / khuyến mãi / quà: chỉ lấy từ giỏ hàng. Bắt cả khi model lỡ chép.
_PRICE = re.compile(
    r"(\d[\d.,]*\s*(?:đ|₫|vnđ|vnd|đồng)\b)|(\bgiá\s*(?:bán|niêm\s*yết|chỉ|từ|khuyến|sốc|\d|:))|(khuyến\s*mãi|khuyến\s*mại)|(giảm\s*giá)"
    r"|(\bfree\s*ship|miễn\s*phí\s*(?:vận\s*chuyển|ship))|(mua\s*\d+\s*tặng)|(\bvoucher\b)|(\bcombo\s*ưu\s*đãi)",
    re.I)


class IngestError(ValueError):
    pass


class ExtractionInvalid(ValueError):
    pass


def has_price(text):
    return bool(_PRICE.search(str(text or "")))


# ------------------------------------------------------------------ ảnh
def normalize_image(data, max_side=1600):
    """Kiểm tra ảnh thật và nén về JPEG <= 1600px (nhẹ cho Gemini, đã xoay đúng chiều)."""
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format not in {"PNG", "JPEG", "WEBP"}:
                raise IngestError("Chỉ nhận ảnh JPG, PNG, WEBP")
            probe.verify()
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
    except IngestError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as exc:
        raise IngestError("Ảnh bị hỏng hoặc không đọc được") from exc
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=88)
    return out.getvalue()


def thumbnail_data_url(jpeg_bytes, side=200):
    with Image.open(io.BytesIO(jpeg_bytes)) as image:
        image = image.convert("RGB")
        image.thumbnail((side, side), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        image.save(out, "JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def _b64(value):
    text = str(value or "")
    if text.startswith("data:"):
        text = text.split(",", 1)[-1]
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise IngestError("Dữ liệu tệp không hợp lệ") from exc


# ------------------------------------------------------------------ đọc địa chỉ web
def _resolve(host):
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def check_public_http_url(url):
    """Chỉ http/https tới máy chủ công cộng (chặn localhost, mạng nội bộ, link-local)."""
    parts = urllib.parse.urlsplit(str(url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise IngestError("Chỉ nhận địa chỉ web bắt đầu bằng http:// hoặc https://")
    try:
        addresses = _resolve(parts.hostname)
    except OSError as exc:
        raise IngestError("Không tìm thấy trang web này (kiểm tra địa chỉ và mạng)") from exc
    if not addresses:
        raise IngestError("Không tìm thấy trang web này")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast \
                or ip.is_unspecified:
            raise IngestError("Không đọc được địa chỉ nội bộ")
    return parts.geturl()


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = URL_MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public_http_url(newurl)           # mỗi bước nhảy đều phải công cộng
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(url, timeout=URL_TIMEOUT):
    from net import ssl_context
    opener = urllib.request.build_opener(
        _SafeRedirect(), urllib.request.HTTPSHandler(context=ssl_context()))
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (KidTE product reader)", "Accept": "text/html,image/*;q=0.8,*/*;q=0.5"})
    return opener.open(request, timeout=timeout)


def _read_limited(url, limit, kinds):
    safe = check_public_http_url(url)
    try:
        response = _open_url(safe, URL_TIMEOUT)
    except IngestError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise IngestError("Không tải được trang web này (quá thời gian hoặc bị chặn)") from exc
    try:
        ctype = str(response.headers.get("Content-Type", "")).split(";")[0].strip().lower()
        if not any(ctype.startswith(kind) for kind in kinds):
            raise IngestError("Địa chỉ này không phải trang web/ảnh đọc được")
        data = response.read(limit + 1)
    finally:
        try:
            response.close()
        except Exception:
            pass
    return data[:limit], len(data) > limit, ctype


_OG = re.compile(r"""<meta\b[^>]*>""", re.I)


def og_image_url(html_text, base_url):
    for tag in _OG.findall(html_text):
        if re.search(r"""(?:property|name)\s*=\s*["']og:image(?::url)?["']""", tag, re.I):
            match = re.search(r"""content\s*=\s*["']([^"']+)["']""", tag, re.I)
            if match:
                url = urllib.parse.urljoin(base_url, match.group(1).strip())
                if urllib.parse.urlsplit(url).scheme in ("http", "https"):
                    return url
    return ""


def fetch_page(url):
    """-> {url, text, og_image (bytes JPEG đã nén | None)}. Giới hạn 2 MB, 10 giây, <= 3 lần chuyển hướng."""
    data, _truncated, _ctype = _read_limited(url, URL_MAX_BYTES, ("text/html", "text/plain", "application/xhtml"))
    from documents import _decode
    raw_html = _decode(data)
    try:
        text = extract_text("page.html", data)
    except ValueError as exc:
        raise IngestError("Trang web này gần như không có chữ để đọc") from exc
    image = None
    image_url = og_image_url(raw_html, url)
    if image_url:
        try:
            blob, truncated, _ = _read_limited(image_url, URL_IMAGE_MAX_BYTES, ("image/",))
            if not truncated:
                image = normalize_image(blob)
        except (IngestError, ValueError):
            image = None
    return {"url": url, "text": text, "og_image": image}


# ------------------------------------------------------------------ nguồn đầu vào
def prepare_sources(payload, fetch=None):
    """payload: {files:[{name,data}], images:[{name,data}], url, text} -> list nguồn.

    Nguồn: {id, kind: file|pdf_scan|image|url|text|og_image, label, text?, mime?, data?}
    ``og_image`` chỉ là ảnh ứng viên (không gửi cho model đọc).
    """
    fetch = fetch or fetch_page
    payload = payload if isinstance(payload, dict) else {}
    files = payload.get("files") or []
    images = payload.get("images") or []
    url = str(payload.get("url") or "").strip()
    pasted = str(payload.get("text") or "").strip()
    if not isinstance(files, list) or not isinstance(images, list):
        raise IngestError("Đầu vào không hợp lệ")
    if len(files) > MAX_FILES:
        raise IngestError(f"Tối đa {MAX_FILES} tệp tài liệu mỗi lần")
    if len(images) > MAX_IMAGES:
        raise IngestError(f"Tối đa {MAX_IMAGES} ảnh mỗi lần")
    if not (files or images or url or pasted):
        raise IngestError("Chưa có gì để đọc: tải tệp/ảnh, nhập địa chỉ web hoặc dán chữ")
    sources, total = [], 0

    def add(kind, label, **extra):
        sources.append({"id": f"n{len(sources) + 1}", "kind": kind, "label": label, **extra})

    for item in files:
        name = Path(str(item.get("name") or "tai-lieu")).name[:120]
        data = _b64(item.get("data"))
        total += len(data)
        if not data or len(data) > MAX_FILE_BYTES:
            raise IngestError(f"Tệp “{name}” rỗng hoặc quá lớn (tối đa 20 MB)")
        try:
            add("file", name, text=extract_text(name, data)[:MAX_SOURCE_CHARS])
        except ValueError as exc:
            if name.lower().endswith(".pdf") and "ảnh chụp" in str(exc):
                add("pdf_scan", name, mime="application/pdf", data=data)     # PDF scan -> Gemini
            else:
                raise IngestError(f"{name}: {exc}") from None
    for item in images:
        name = Path(str(item.get("name") or "anh")).name[:120]
        data = _b64(item.get("data"))
        total += len(data)
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise IngestError(f"Ảnh “{name}” rỗng hoặc quá lớn (tối đa 10 MB)")
        add("image", name, mime="image/jpeg", data=normalize_image(data))
    if total > MAX_TOTAL_BYTES:
        raise IngestError("Tổng dung lượng quá lớn (tối đa 28 MB)")
    if url:
        page = fetch(url)
        add("url", page["url"], text=str(page["text"])[:MAX_SOURCE_CHARS])
        if page.get("og_image"):
            add("og_image", "Ảnh đại diện trên trang web", mime="image/jpeg", data=page["og_image"])
    if pasted:
        add("text", "Chữ dán vào", text=pasted[:MAX_PASTED_CHARS])
    return sources


def needs_vision(sources):
    return any(s["kind"] in ("image", "pdf_scan") for s in sources)


def vision_files(sources):
    return [{"mime": s["mime"], "data": s["data"]} for s in sources if s["kind"] in ("image", "pdf_scan")]


# ------------------------------------------------------------------ prompt
FIELD_HELP = """\
ten_san_pham: tên thương mại đúng như trên nhãn (text)
nha_san_xuat: công ty sản xuất/đăng ký (text)
quy_cach_dong_goi: dung tích/khối lượng, vd "chai 500ml", "gói 1kg" (text)
thanh_phan_dang_ky: thành phần/hàm lượng đăng ký, mảng [{"ten": "...", "ham_luong": "..."}]
cong_dung: mảng câu ngắn, mỗi ý một dòng
doi_tuong_cay_trong: mảng tên cây/vật nuôi phù hợp
huong_dan_su_dung: mảng câu: liều lượng, cách pha, thời điểm dùng (chép ĐÚNG con số)
luu_y: lưu ý an toàn, bảo quản, thời gian cách ly (text)
mo_ta_ngan: 1-2 câu tóm tắt trung tính (text)
phan_khuc: nhóm công dụng chung, vd "phân bón lá", "kích rễ" (text)
hieu_qua_chinh: hiệu quả chính nhà sản xuất nêu (text)"""


def build_system():
    task = task_block(
        "ĐỌC TÀI LIỆU / NHÃN / TRANG WEB ĐỂ TẠO HỒ SƠ SẢN PHẨM NHÁP",
        "Chủ shop đưa vào tài liệu, ảnh nhãn hoặc trang web của MỘT sản phẩm; rút ra hồ sơ kỹ thuật để chủ shop duyệt.",
        inputs="Các nguồn đánh số n1, n2... (chữ trích từ tệp/web/chữ dán, và ảnh/PDF scan đính kèm theo thứ tự).",
        output="MỘT đối tượng JSON duy nhất, không giải thích ngoài JSON.")
    rules = f"""\
Cấu trúc trả về:
{{"san_pham": {{"<tên_trường>": {{"gia_tri": ..., "do_tin_cay": 0.0-1.0, "nguon": "n1"}}, ...}}}}

Các trường (đúng các tên này, không thêm tên khác):
{FIELD_HELP}

Quy tắc bắt buộc:
- KHÔNG ghi giá, khuyến mãi, quà tặng, mã giảm giá, miễn phí vận chuyển, số lượng tồn: chúng chỉ lấy từ giỏ hàng của shop.
- Chỉ chép những gì có trong nguồn, không bịa, không dùng hiểu biết chung để điền khoảng trống.
- Chữ mờ/không đọc chắc chắn, hoặc nguồn không nói: đặt "gia_tri": null và "do_tin_cay": 0.
- do_tin_cay: 0.9+ khi đọc rõ ràng; 0.5-0.8 khi phải đoán/suy ra; dưới 0.5 khi mơ hồ.
- "nguon": mã nguồn (n1, n2...) nơi lấy thông tin; nhiều nguồn mâu thuẫn thì chọn nhãn chính thức và hạ do_tin_cay.
- Mọi trường đều phải có mặt (null nếu không có). Viết tiếng Việt như nguồn."""
    return system_with_brief(task, rules)


def build_user_text(sources):
    parts, used = [], 0
    for s in sources:
        if s["kind"] in ("file", "url", "text"):
            body = s["text"][:max(0, min(MAX_SOURCE_CHARS, MAX_PROMPT_CHARS - used))]
            used += len(body)
            kind = {"file": "tệp", "url": "trang web", "text": "chữ dán"}[s["kind"]]
            parts.append(f"=== NGUỒN {s['id']} ({kind}: {s['label']}) ===\n{body}")
        elif s["kind"] in ("image", "pdf_scan"):
            parts.append(f"=== NGUỒN {s['id']} ({'ảnh' if s['kind'] == 'image' else 'PDF scan'}: {s['label']}) "
                         "— đính kèm theo đúng thứ tự ===")
    return "\n\n".join(parts) + "\n\nHãy trả về JSON hồ sơ sản phẩm."


# ------------------------------------------------------------------ kiểm tra JSON
def parse_json(text):
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ExtractionInvalid("không phải JSON") from None
        try:
            value = json.loads(raw[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ExtractionInvalid("JSON hỏng") from exc
    if not isinstance(value, dict):
        raise ExtractionInvalid("JSON gốc phải là đối tượng")
    return value


def _clean_str(value, limit=1000):
    return " ".join(str(value or "").split())[:limit]


def _as_lines(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = [part for part in re.split(r"[\r\n]+", value)]
    if not isinstance(value, list):
        raise ExtractionInvalid("trường danh sách sai kiểu")
    return [_clean_str(v) for v in value if isinstance(v, (str, int, float)) and _clean_str(v)][:50]


def _as_pairs(value):
    if value is None:
        return {}
    pairs = {}
    if isinstance(value, dict):
        value = [{"ten": k, "ham_luong": v} for k, v in value.items()]
    if isinstance(value, str):
        value = [{"ten": line, "ham_luong": ""} for line in re.split(r"[\r\n;]+", value) if line.strip()]
    if not isinstance(value, list):
        raise ExtractionInvalid("thành phần sai kiểu")
    for item in value:
        if isinstance(item, str):
            item = {"ten": item, "ham_luong": ""}
        if not isinstance(item, dict):
            raise ExtractionInvalid("thành phần sai kiểu")
        name = _clean_str(item.get("ten") or item.get("name"), 160)
        if name:
            pairs[name] = _clean_str(item.get("ham_luong") or item.get("amount"), 500)
    return dict(list(pairs.items())[:50])


def _scrub(key, value):
    """Bỏ mọi dòng/câu dính giá, khuyến mãi. Trả (giá trị mới, có_bị_lọc)."""
    if value is None:
        return None, False
    if key in LIST_FIELDS:
        kept = [line for line in value if not has_price(line)]
        return kept, len(kept) != len(value)
    if key == PAIRS_FIELD:
        kept = {k: v for k, v in value.items() if not has_price(k) and not has_price(v)}
        return kept, len(kept) != len(value)
    if has_price(value):
        sentences = re.split(r"(?<=[.!?])\s+", value)
        kept = " ".join(s for s in sentences if not has_price(s)).strip()
        return kept, True
    return value, False


def validate_extraction(raw, source_ids=()):
    """Chuỗi/dict của model -> kết quả chuẩn. Sai cấu trúc -> ExtractionInvalid."""
    data = parse_json(raw) if not isinstance(raw, dict) else raw
    body = data.get("san_pham") if isinstance(data.get("san_pham"), dict) else data
    if not any(key in body for key in ALL_FIELDS):
        raise ExtractionInvalid("thiếu các trường hồ sơ")
    source_ids = set(source_ids)
    fields, confidence, nguon, filtered = {}, {}, {}, False
    for key in FIELD_ORDER:
        cell = body.get(key)
        if isinstance(cell, dict) and ("gia_tri" in cell or "value" in cell):
            value = cell.get("gia_tri", cell.get("value"))
            conf, src = cell.get("do_tin_cay", cell.get("confidence")), cell.get("nguon")
        else:                                  # model trả giá trị trần
            value, conf, src = cell, None, None
        try:
            conf = max(0.0, min(1.0, float(conf))) if conf is not None else 0.5
        except (TypeError, ValueError):
            conf = 0.5
        if key in LIST_FIELDS:
            value = _as_lines(value) or None
        elif key == PAIRS_FIELD:
            value = _as_pairs(value) or None
        else:
            if isinstance(value, (list, dict)):
                value = _clean_str(", ".join(map(str, value)) if isinstance(value, list) else json.dumps(value))
            value = _clean_str(value, 4000 if key != "ten_san_pham" else 300) or None
        value, dropped = _scrub(key, value)
        filtered = filtered or dropped
        if value in ("", [], {}):
            value = None
        if value is None:
            conf = 0.0
        src = str(src or "")
        fields[key], confidence[key] = value, round(conf, 2)
        nguon[key] = src if (not source_ids or src in source_ids) else ""
    uncertain = [k for k in FIELD_ORDER if fields[k] is None or confidence[k] < CONFIDENCE_LOW]
    return {"fields": fields, "confidence": confidence, "nguon": nguon,
            "uncertain": uncertain, "filtered_price": filtered}


# ------------------------------------------------------------------ dịch vụ
def _webp_800(jpeg_bytes):
    with Image.open(io.BytesIO(jpeg_bytes)) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((800, 800), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        image.save(out, "WEBP", quality=88, method=6)
    return out.getvalue()


class ProductIngest:
    def __init__(self, engine, gemini, deepseek, fetch=None, clock=time.time):
        self.engine = engine
        self.gemini = gemini
        self.deepseek = deepseek
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions = {}

    # -- phiên giữ ảnh đã tải để chọn làm ảnh sản phẩm
    def _remember(self, sources):
        now = self._clock()
        with self._lock:
            for sid in [k for k, v in self._sessions.items() if now - v["at"] > SESSION_TTL]:
                del self._sessions[sid]
            while len(self._sessions) >= MAX_SESSIONS:
                del self._sessions[min(self._sessions, key=lambda k: self._sessions[k]["at"])]
            session_id = uuid.uuid4().hex
            self._sessions[session_id] = {"at": now, "images": {
                s["id"]: s["data"] for s in sources if s["kind"] in ("image", "og_image")}}
        return session_id

    def status(self):
        return {"gemini_configured": bool(self.gemini and self.gemini.configured),
                "deepseek_configured": bool(self.deepseek and self.deepseek.configured),
                "gemini_model": getattr(self.gemini, "model", "")}

    def extract(self, payload):
        sources = prepare_sources(payload, fetch=self._fetch)
        try:
            provider = choose_provider(needs_vision(sources), self.gemini, self.deepseek)
        except AIProviderError as exc:
            raise IngestError(str(exc)) from exc
        system, user = build_system(), build_user_text(sources)
        files = vision_files(sources) if provider.supports_files else []
        ids = [s["id"] for s in sources]
        try:
            result = self._ask(provider, system, user, files, ids)
        except AIProviderError as exc:
            raise IngestError(str(exc)) from exc
        result.update({
            "provider": provider.name, "model": getattr(provider, "model", ""),
            "session_id": self._remember(sources),
            "sources": [{"id": s["id"], "kind": s["kind"], "label": s["label"],
                         **({"thumb": thumbnail_data_url(s["data"])} if s["kind"] in ("image", "og_image") else {})}
                        for s in sources]})
        return result

    @staticmethod
    def _ask(provider, system, user, files, ids):
        raw = provider.generate(system, user, files)
        try:
            return validate_extraction(raw, ids)
        except ExtractionInvalid as first:
            retry = (user + f"\n\nLẦN TRƯỚC JSON KHÔNG HỢP LỆ ({first}). Trả lại ĐÚNG cấu trúc "
                     '{"san_pham": {"<trường>": {"gia_tri", "do_tin_cay", "nguon"}}}, chỉ JSON.')
            raw = provider.generate(system, retry, files)
            try:
                return validate_extraction(raw, ids)
            except ExtractionInvalid as second:
                raise IngestError("AI trả về kết quả không đúng dạng hai lần liên tiếp (" + str(second)
                                  + "). Thử lại hoặc đổi nguồn rõ chữ hơn.") from None

    # -- tạo hồ sơ nháp
    def create_draft(self, payload):
        payload = payload if isinstance(payload, dict) else {}
        fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
        name = _clean_str(fields.get("ten_san_pham"), 300)
        if len(name) < 2:
            raise IngestError("Cần có tên sản phẩm trước khi lưu nháp")
        changes = {}
        for key in FIELD_ORDER:
            if key == "ten_san_pham":
                continue
            value = fields.get(key)
            if value in (None, "", [], {}):
                continue
            if key in LIST_FIELDS:
                value = _as_lines(value)
            elif key == PAIRS_FIELD:
                value = _as_pairs(value)
            else:
                value = _clean_str(value, 4000)
            value, _ = _scrub(key, value)
            if value not in (None, "", [], {}):
                changes[key] = value
        source_url = _clean_str(payload.get("source_url"), 500)
        if source_url.startswith(("http://", "https://")):
            changes["nguon_chinh_thuc"] = source_url
        workspace = self.engine.workspace
        created = workspace.create_profile({"ten_san_pham": name})
        record_id = created["id"]
        if changes:
            workspace.patch("profiles", record_id, {"changes": changes, "reason": "Đọc bằng AI (hồ sơ nháp)"})
        image_saved = False
        image_ref = payload.get("image") or {}
        if image_ref.get("source_id"):
            blob = self._session_image(payload.get("session_id"), image_ref["source_id"])
            if blob:
                path = self.staged_image_path(record_id, create=True)
                path.write_bytes(_webp_800(blob))
                image_saved = True
        return {"ok": True, "id": record_id, "status": "draft", "approved_for_live": False,
                "image_saved": image_saved, "suggestions": self.suggest_skus(record_id)}

    def _session_image(self, session_id, source_id):
        with self._lock:
            session = self._sessions.get(str(session_id or ""))
            return session["images"].get(str(source_id)) if session else None

    # -- ảnh nháp
    def _image_dir(self):
        return Path(self.engine.workspace.root) / "data" / "ingest_images"

    def staged_image_path(self, record_id, create=False):
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(record_id))
        folder = self._image_dir()
        if create:
            folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{safe}.webp"
        if not create and not path.is_file():
            raise ValueError("product_image_not_found")
        return path

    # -- gợi ý gắn SKU từ giỏ hàng
    def suggest_skus(self, record_id, limit=5):
        import listing_match
        engine = self.engine
        records = {str(r.get("san_pham_id") or ""): r for r in engine.knowledge.all_records()}
        draft = records.get(record_id)
        if not draft:
            return []
        rivals = [r for r in engine.knowledge.mappable_records()
                  if str(r.get("san_pham_id") or "") != record_id]
        own = listing_match.ProfileIndex([draft])
        others = listing_match.ProfileIndex(rivals) if rivals else None
        out = []
        for row in engine.mapping_state().get("products", []):
            if row.get("profile_id"):
                continue
            title = row.get("title", "")
            match = listing_match.match_listing(title, own)
            if match["profile_id"] != record_id or match["tier"] == listing_match.TIER_NONE:
                continue
            reasons = list(match["reasons"])
            rival = listing_match.match_listing(title, others) if others else None
            if rival and rival["profile_id"] and rival["score"] > match["score"]:
                reasons.append(f"Hồ sơ có sẵn “{rival['profile_name']}” khớp hơn — kiểm tra kỹ")
            out.append({"product_id": row["product_id"], "title": title,
                        "bag_index": row.get("bag_index", 0), "score": match["score"],
                        "tier": match["tier"], "size_match": match["size_match"], "reasons": reasons})
        out.sort(key=lambda item: (item["size_match"] is not False, item["score"]), reverse=True)
        return out[:limit]

    # -- chủ shop xác nhận SKU
    def link_draft(self, record_id, product_id, use_image=True):
        workspace = self.engine.workspace
        workspace.set_draft_product(record_id, product_id)
        image_applied = False
        if use_image:
            try:
                path = self.staged_image_path(record_id)
            except ValueError:
                path = None
            if path:
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                workspace.save_product_image(product_id, {"data_url": "data:image/webp;base64," + encoded})
                image_applied = True
        return {"ok": True, "id": record_id, "product_id": product_id, "image_applied": image_applied,
                "next": "Duyệt hồ sơ ở tab “Gắn hồ sơ cho sản phẩm” → Hồ sơ nháp để AI được dùng."}
