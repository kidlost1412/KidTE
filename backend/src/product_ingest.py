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
import html
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
import unicodedata
from collections import deque
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from ai_context import system_with_brief, task_block
from ai_providers import AIProviderError, GeminiProvider, choose_provider
from documents import extract_text
from product_web import canonical_url, listing_url, parse_product_page

# ------------------------------------------------------------------ giới hạn
MAX_IMAGES = 8
MAX_FILES = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 100 * 1024 * 1024         # danh mục PDF nhiều ảnh có thể nặng hàng chục MB
MAX_TOTAL_BYTES = 160 * 1024 * 1024
MAX_UPLOADS_BYTES = 320 * 1024 * 1024      # tổng tệp đang chờ trong bộ nhớ
UPLOAD_TTL = 3600
MAX_VISION_BYTES = 15 * 1024 * 1024        # Gemini nhận tối đa ~20 MB/yêu cầu (base64 +33%)
MAX_PASTED_CHARS = 60_000
MAX_SOURCE_CHARS = 60_000
MAX_PROMPT_CHARS = 150_000
MAX_PRODUCTS = 60
URL_MAX_BYTES = 2 * 1024 * 1024
URL_IMAGE_MAX_BYTES = 5 * 1024 * 1024
URL_TIMEOUT = 10
URL_MAX_REDIRECTS = 3
CRAWL_MAX_PAGES = 100                      # gồm danh mục, phân trang và trang chi tiết
CRAWL_MAX_CHARS = 1_000_000
CRAWL_MAX_SECONDS = 300
EXTRACT_BATCH_PAGES = 4
EXTRACT_BATCH_CHARS = 40_000
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
                url = urllib.parse.urljoin(base_url, html.unescape(match.group(1)).strip())
                if urllib.parse.urlsplit(url).scheme in ("http", "https"):
                    return url
    return ""


def is_root_url(url):
    """Địa chỉ trang chủ (không đường dẫn con, không tham số): mặc định bật quét thêm trang sản phẩm."""
    parts = urllib.parse.urlsplit(str(url or "").strip())
    return parts.path in ("", "/") and not parts.query


def _host_key(host):
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def find_product_links(page_html, base_url, limit=CRAWL_MAX_PAGES):
    """Same-site catalog, pagination and product-card links, without cart actions."""
    return parse_product_page(str(page_html or ""), base_url, limit)["links"]


def fetch_page(url, with_image=True):
    """-> {url, text, og_image (bytes JPEG đã nén | None), links (trang sản phẩm cùng web)}.

    Giới hạn 2 MB, 10 giây, <= 3 lần chuyển hướng. ``with_image=False`` bỏ qua ảnh đại diện.
    """
    data, _truncated, _ctype = _read_limited(url, URL_MAX_BYTES, ("text/html", "text/plain", "application/xhtml"))
    from documents import _decode
    raw_html = _decode(data)
    details = parse_product_page(raw_html, url)
    try:
        text = details["product_text"] or extract_text("page.html", data)
    except ValueError as exc:
        raise IngestError("Trang web này gần như không có chữ để đọc") from exc
    image = None
    image_url = ""
    # Product galleries/lazy images take precedence over site-wide Open Graph logos.
    candidates = details["image_urls"] if details["product_page"] else []
    if with_image:
        for candidate in candidates:
            try:
                blob, truncated, _ = _read_limited(candidate, URL_IMAGE_MAX_BYTES, ("image/",))
                if not truncated:
                    image = normalize_image(blob)
                    image_url = candidate
                    break
            except (IngestError, ValueError):
                continue
    links = details["links"]
    return {"url": url, "text": text, "og_image": image, "image_url": image_url, **details, "links": links,
            "truncated": _truncated}


# ------------------------------------------------------------------ nguồn đầu vào
def _truthy(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def prepare_sources(payload, fetch=None, uploads=None, crawl_fetch=None, stats=None):
    """payload: {files:[{name,data}], images:[{name,data}], url, text, crawl} -> list nguồn.

    Nguồn: {id, kind: file|pdf_scan|image|url|text|og_image, label, text?, mime?, data?}
    ``og_image`` chỉ là ảnh ứng viên (không gửi cho model đọc). ``crawl`` (mặc định bật với trang chủ)
    quét thêm tối đa CRAWL_MAX_PAGES trang sản phẩm cùng web; ``stats`` (dict) nhận số liệu quét.
    """
    if fetch is None:
        fetch = fetch_page
        crawl_fetch = crawl_fetch or fetch_page
    crawl_fetch = crawl_fetch or fetch
    uploads = uploads or (lambda upload_id: None)
    payload = payload if isinstance(payload, dict) else {}
    files = payload.get("files") or []
    images = payload.get("images") or []
    url = str(payload.get("url") or "").strip()
    pasted = str(payload.get("text") or "").strip()
    crawl_on = bool(url) and _truthy(payload.get("crawl"), is_root_url(url) or listing_url(url))
    if stats is not None:
        stats.update({"enabled": crawl_on, "found": 0, "read": 0, "failed": 0,
                      "products": 0, "images": 0, "limited": False})
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
        return sources[-1]["id"]

    def add_page(page):
        source_id = add("url", page["url"], text=str(page["text"])[:MAX_SOURCE_CHARS],
                        product_page=bool(page.get("product_page")), product_name=page.get("product_name", ""))
        if page.get("og_image"):
            add("og_image", page.get("product_name") or "Ảnh đại diện trên trang web",
                mime="image/jpeg", data=page["og_image"], page_source_id=source_id,
                page_url=page["url"], image_url=page.get("image_url", ""))
            if stats is not None:
                stats["images"] += 1
        return source_id

    def blob_of(item, default):
        if not isinstance(item, dict):
            raise IngestError("Đầu vào không hợp lệ")
        name = Path(str(item.get("name") or default)).name[:120]
        if item.get("upload_id"):
            data = uploads(str(item["upload_id"]))
            if data is None:
                raise IngestError(f"Tệp “{name}” đã hết hạn trên máy, hãy chọn lại tệp rồi bấm đọc lại")
        else:
            data = _b64(item.get("data"))
        return name, data

    for item in files:
        name, data = blob_of(item, "tai-lieu")
        total += len(data)
        if not data or len(data) > MAX_FILE_BYTES:
            raise IngestError(f"Tệp “{name}” rỗng hoặc quá lớn (tối đa {MAX_FILE_BYTES // 1048576} MB)")
        try:
            add("file", name, text=extract_text(name, data)[:MAX_SOURCE_CHARS])
        except ValueError as exc:
            if name.lower().endswith(".pdf") and "ảnh chụp" in str(exc):
                add("pdf_scan", name, mime="application/pdf", data=data)     # PDF scan -> Gemini
            else:
                raise IngestError(f"{name}: {exc}") from None
    for item in images:
        name, data = blob_of(item, "anh")
        total += len(data)
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise IngestError(f"Ảnh “{name}” rỗng hoặc quá lớn (tối đa 10 MB)")
        add("image", name, mime="image/jpeg", data=normalize_image(data))
    if total > MAX_TOTAL_BYTES:
        raise IngestError(f"Tổng dung lượng quá lớn (tối đa {MAX_TOTAL_BYTES // 1048576} MB)")
    if url:
        page = fetch(url)
        add_page(page)
        if crawl_on:
            links = list(page.get("links") or [])
            if stats is not None:
                stats["found"] = len(links)
            _crawl_product_pages(links, crawl_fetch, add_page, stats, page["url"])
    if pasted:
        add("text", "Chữ dán vào", text=pasted[:MAX_PASTED_CHARS])
    if stats is not None:
        stats["products"] = sum(bool(s.get("product_page")) for s in sources)
    return sources


def _crawl_product_pages(links, fetcher, add_page, stats=None, base_url=""):
    """Follow catalog/pagination first, then detail pages, deduplicating every URL."""
    catalogs, products = deque(), deque()
    seen = {canonical_url(base_url, base_url).rstrip("/")}
    home = _host_key(urllib.parse.urlsplit(base_url).hostname)
    found = set()
    def enqueue(items):
        for item in items:
            url = canonical_url(item, base_url)
            if not url or _host_key(urllib.parse.urlsplit(url).hostname) != home:
                continue
            key = url.rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            found.add(url)
            (catalogs if listing_url(url) else products).append(url)
    enqueue(links)
    used, read, product_count = 0, 0, 0
    started = time.monotonic()
    while catalogs or products:
        if read >= CRAWL_MAX_PAGES or used >= CRAWL_MAX_CHARS or product_count >= MAX_PRODUCTS \
                or time.monotonic() - started >= CRAWL_MAX_SECONDS:
            if stats is not None:
                stats["limited"] = True
            break
        link = (catalogs if catalogs else products).popleft()
        read += 1
        try:
            page = fetcher(link)
        except (IngestError, OSError, ValueError):
            if stats is not None:
                stats["failed"] += 1
            continue
        room = min(MAX_SOURCE_CHARS, CRAWL_MAX_CHARS - used)
        body = str(page.get("text") or "")[:room]
        if not body.strip():
            if stats is not None:
                stats["failed"] += 1
            continue
        page = {**page, "url": str(page.get("url") or link), "text": body}
        add_page(page)
        used += len(body)
        product_count += bool(page.get("product_page"))
        enqueue(page.get("links") or [])
        if stats is not None:
            stats["read"] += 1
            stats["products"] = product_count
            stats["limited"] = stats["limited"] or bool(page.get("truncated"))
    if stats is not None:
        stats["found"] = len(found)


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
        "Chủ shop đưa vào tài liệu, ảnh nhãn hoặc trang web (có thể là MỘT sản phẩm hoặc cả DANH MỤC nhiều sản phẩm); "
        "rút ra hồ sơ kỹ thuật từng sản phẩm để chủ shop duyệt.",
        inputs="Các nguồn đánh số n1, n2... (chữ trích từ tệp/web/chữ dán, và ảnh/PDF scan đính kèm theo thứ tự).",
        output="MỘT đối tượng JSON duy nhất, không giải thích ngoài JSON.")
    rules = f"""Cấu trúc trả về (mỗi SẢN PHẨM KHÁC NHAU là một phần tử của mảng "san_pham"; chỉ một sản phẩm thì mảng có một phần tử):
{{"san_pham": [{{"ten_san_pham": "...", "nha_san_xuat": "...", ..., "nguon": "n1", "can_xem": ["luu_y"]}}]}}
- Mỗi phần tử là một đối tượng PHẲNG: tên trường -> giá trị trực tiếp (không bọc thêm lớp).
- "nguon": mã nguồn (n1, n2...) nơi lấy thông tin của sản phẩm đó.
- "can_xem": danh sách tên trường mà bạn phải đoán/không chắc (đọc mờ, nguồn mâu thuẫn).

Các trường (đúng các tên này, không thêm tên khác):
{FIELD_HELP}

Quy tắc bắt buộc (RIÊNG nhiệm vụ này, thay cho phần "hiểu biết chung" của bối cảnh chung; đây là hồ sơ để chủ shop duyệt, không phải câu trả lời cho khách):
- Tài liệu là danh mục/nhiều sản phẩm: liệt kê MỌI sản phẩm có tên riêng, mỗi sản phẩm một phần tử. Không gộp các sản phẩm khác nhau; cùng một sản phẩm xuất hiện ở nhiều nguồn thì gộp thành MỘT phần tử.
- Bỏ qua phần giới thiệu công ty, mục lục, liên hệ: đó không phải sản phẩm.
- KHÔNG ghi giá, khuyến mãi, quà tặng, mã giảm giá, miễn phí vận chuyển, số lượng tồn: chúng chỉ lấy từ giỏ hàng của shop.
- Chỉ chép những gì có trong nguồn, không bịa, không dùng hiểu biết chung để điền khoảng trống.
- Cùng hoạt chất xuất hiện ở phần đăng ký và bổ sung: giữ TẤT CẢ dòng, đặt tên rõ vai trò (vd Zn đăng ký, Zn bổ sung), không ghi đè hoặc cộng hàm lượng. Thiếu đơn vị thì để nguyên số và đánh can_xem, không tự thêm %.
- Chữ mờ/không đọc chắc chắn, hoặc nguồn không nói: đặt trường là null.
- Mọi trường đều phải có mặt trong mỗi sản phẩm (null nếu không có). Viết tiếng Việt như nguồn."""
    return system_with_brief(task, rules)


def build_user_text(sources):
    parts, used = [], 0
    for s in sources:
        if s["kind"] in ("file", "url", "text"):
            body = s["text"][:max(0, min(MAX_SOURCE_CHARS, MAX_PROMPT_CHARS - used))]
            used += len(body)
            kind = {"file": "tệp", "url": "trang web", "text": "chữ dán"}[s["kind"]]
            detail_hint = (f"\nTrang chi tiết của MỘT sản phẩm: {s.get('product_name')}. "
                           "Tạo một hồ sơ cho đúng sản phẩm này, dùng tên đầy đủ trên trang.\n"
                           if s.get("product_page") else "")
            parts.append(f"=== NGUỒN {s['id']} ({kind}: {s['label']}) ===\n{detail_hint}{body}")
        elif s["kind"] in ("image", "pdf_scan"):
            parts.append(f"=== NGUỒN {s['id']} ({'ảnh' if s['kind'] == 'image' else 'PDF scan'}: {s['label']}) "
                         "— đính kèm theo đúng thứ tự ===")
    return "\n\n".join(parts) + "\n\nHãy trả về JSON danh sách hồ sơ sản phẩm."


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
            amount = _clean_str(item.get("ham_luong") or item.get("amount"), 500)
            if name in pairs and pairs[name] != amount:
                base, index = name, 2
                while name in pairs:
                    name = f"{base[:145]} (dòng {index})"
                    if pairs.get(name) == amount:
                        break
                    index += 1
            pairs[name] = amount
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


def _salvage_products(raw):
    """JSON bị cắt giữa chừng (danh mục dài): lấy các sản phẩm đã đủ dấu đóng."""
    text = str(raw or "")
    start = text.find("[")
    if start < 0:
        return []
    decoder, pos, out = json.JSONDecoder(), start + 1, []
    while pos < len(text):
        while pos < len(text) and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(text) or text[pos] != "{":
            break
        try:
            item, pos = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            break
        out.append(item)
    return out


def _product_items(raw):
    """Chuỗi/dict/list của model -> (list đối tượng sản phẩm thô, bị_cắt)."""
    if isinstance(raw, (dict, list)):
        value = raw
    else:
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw or "").strip(), flags=re.I)
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            value = None
            if start >= 0 and end > start:
                try:
                    value = json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    value = None
            if value is None:
                items = _salvage_products(text)
                if not items:
                    raise ExtractionInvalid("không phải JSON") from None
                return items, True
    truncated = False
    if isinstance(value, dict):
        for key in ("san_pham", "san_pham_list", "products", "danh_sach_san_pham"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
        else:
            value = [value.get("san_pham")] if isinstance(value.get("san_pham"), dict) else [value]
    if not isinstance(value, list):
        raise ExtractionInvalid("JSON gốc phải là đối tượng hoặc danh sách")
    return [item for item in value if isinstance(item, dict)], truncated


def _wrap_flat(product):
    """Phần tử phẳng {trường: giá trị, nguon, can_xem} -> dạng ô {gia_tri, do_tin_cay, nguon}."""
    if not isinstance(product.get("san_pham"), dict) and not any(isinstance(product.get(k), dict) and (
            "gia_tri" in product[k] or "value" in product[k]) for k in ALL_FIELDS):
        flagged = {str(x) for x in product.get("can_xem") or [] if isinstance(x, str)}
        try:
            base = max(0.0, min(1.0, float(product.get("do_tin_cay", 0.85))))
        except (TypeError, ValueError):
            base = 0.85
        src = str(product.get("nguon") or "")
        return {key: {"gia_tri": product.get(key), "do_tin_cay": 0.4 if key in flagged else base, "nguon": src}
                for key in ALL_FIELDS if key in product}
    return product.get("san_pham") if isinstance(product.get("san_pham"), dict) else product


def validate_products(raw, source_ids=()):
    """Kết quả của model (một hoặc nhiều sản phẩm) -> {"products": [kết quả chuẩn...], "truncated": bool}.

    Sản phẩm không tên bị bỏ, trùng tên (không phân biệt hoa thường) gộp một. Không còn sản phẩm nào -> ExtractionInvalid.
    """
    items, truncated = _product_items(raw)
    products, seen = [], set()
    for item in items:
        try:
            result = validate_extraction(_wrap_flat(item), source_ids)
        except ExtractionInvalid:
            continue
        name = result["fields"].get("ten_san_pham")
        key = " ".join(str(name or "").lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        products.append(result)
        if len(products) >= MAX_PRODUCTS:
            truncated = True
            break
    if not products:
        raise ExtractionInvalid("không thấy sản phẩm nào có tên")
    return {"products": products, "truncated": truncated}


# ------------------------------------------------------------------ dịch vụ
def _webp_800(jpeg_bytes):
    with Image.open(io.BytesIO(jpeg_bytes)) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((800, 800), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        image.save(out, "WEBP", quality=88, method=6)
    return out.getvalue()


def _name_key(value):
    text = unicodedata.normalize("NFKD", str(value or "").lower().replace("đ", "d"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def extraction_batches(sources):
    """Use detail pages as authoritative sources, instead of re-extracting catalog cards."""
    details = [s for s in sources if s.get("product_page") and s["kind"] == "url"]
    if not details:
        return [[s for s in sources if s["kind"] != "og_image"]]
    extra = [s for s in sources if s["kind"] in ("file", "text", "image", "pdf_scan")]
    batches = [extra] if extra else []
    batch, used = [], 0
    for source in details:
        length = len(source.get("text") or "")
        if batch and (len(batch) >= EXTRACT_BATCH_PAGES or used + length > EXTRACT_BATCH_CHARS):
            batches.append(batch)
            batch, used = [], 0
        batch.append(source)
        used += length
    if batch:
        batches.append(batch)
    return batches


def attach_product_sources(products, sources):
    """Bind an image to its own detail page; never auto-share a home/catalog image."""
    pages = {s["id"]: s for s in sources if s["kind"] == "url"}
    detail_pages = [s for s in pages.values() if s.get("product_page")]
    for product in products:
        name = _name_key(product["fields"].get("ten_san_pham"))
        exact = [s for s in detail_pages if _name_key(s.get("product_name")) == name]
        source = exact[0] if len(exact) == 1 else pages.get(product["nguon"].get("ten_san_pham"))
        if not source and len(detail_pages) == 1 and len(products) == 1:
            source = detail_pages[0]
        product["source_url"] = source["label"] if source else ""
        # Website candidates are scoped to this page. Uploads remain selectable manually.
        product["image_source_ids"] = [s["id"] for s in sources if s["kind"] == "image" or (
            s["kind"] == "og_image" and source and source.get("product_page")
            and s.get("page_source_id") == source["id"])]
        web_images = [s["id"] for s in sources if s["kind"] == "og_image" and
                      s["id"] in product["image_source_ids"]]
        product["default_image_source_id"] = web_images[0] if web_images else ""


class ProductIngest:
    def __init__(self, engine, gemini, deepseek, fetch=None, clock=time.time, crawl_fetch=None):
        self.engine = engine
        self.gemini = gemini
        self.deepseek = deepseek
        self._fetch = fetch
        self._crawl_fetch = crawl_fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions = {}
        self._uploads = {}

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

    # -- tệp tải lên thô (không base64) chờ AI đọc
    def add_upload(self, name, data):
        name = Path(str(name or "tep")).name[:120] or "tep"
        if not data:
            raise IngestError(f"Tệp “{name}” rỗng")
        if len(data) > MAX_FILE_BYTES:
            raise IngestError(f"Tệp “{name}” quá lớn (tối đa {MAX_FILE_BYTES // 1048576} MB)")
        now = self._clock()
        with self._lock:
            for uid in [k for k, v in self._uploads.items() if now - v["at"] > UPLOAD_TTL]:
                del self._uploads[uid]
            while self._uploads and sum(len(v["data"]) for v in self._uploads.values()) + len(data) > MAX_UPLOADS_BYTES:
                del self._uploads[min(self._uploads, key=lambda k: self._uploads[k]["at"])]
            upload_id = uuid.uuid4().hex
            self._uploads[upload_id] = {"at": now, "name": name, "data": data}
        return {"upload_id": upload_id, "name": name, "size": len(data)}

    def _upload_bytes(self, upload_id):
        with self._lock:
            item = self._uploads.get(str(upload_id))
            return item["data"] if item else None

    def _pick_provider(self, wanted, sources):
        vision = needs_vision(sources)
        wanted = str(wanted or "auto").lower()
        if wanted not in ("auto", "gemini", "deepseek"):
            raise IngestError("Nhà cung cấp AI không hợp lệ (chọn Gemini hoặc DeepSeek)")
        try:
            if wanted == "deepseek":
                if vision:
                    raise IngestError("DeepSeek chỉ đọc chữ, không đọc được ảnh hoặc PDF scan. "
                                      "Chọn Gemini, hoặc bỏ ảnh/PDF scan.")
                if not (self.deepseek and self.deepseek.configured):
                    raise IngestError("Chưa có khóa DeepSeek. Dán khóa ở Cài đặt hoặc chọn Gemini.")
                return self.deepseek
            if wanted == "gemini":
                if not (self.gemini and self.gemini.configured):
                    raise IngestError("Chưa có khóa Gemini. Vào Cài đặt, dán khóa vào ô “Key Gemini (đọc ảnh/PDF)”, "
                                      "hoặc chọn DeepSeek để đọc tài liệu có chữ.")
                return self.gemini
            return choose_provider(vision, self.gemini, self.deepseek)
        except AIProviderError as exc:
            raise IngestError(str(exc)) from exc

    def extract(self, payload):
        crawl = {}
        sources = prepare_sources(payload, fetch=self._fetch, uploads=self._upload_bytes,
                                  crawl_fetch=self._crawl_fetch, stats=crawl)
        wanted = payload.get("provider") if isinstance(payload, dict) else ""
        provider = self._pick_provider(wanted, sources)
        if provider.name == "gemini" and isinstance(payload, dict) and "gemini_model" in payload:
            model = str(payload.get("gemini_model") or "").strip()
            if model and not re.fullmatch(r"gemini-[A-Za-z0-9._-]{1,100}", model):
                raise IngestError("Tên model Gemini không hợp lệ. Chọn một model trong danh sách.")
            # Per-read choice does not overwrite the owner's global setting/provider.
            provider = GeminiProvider(self.gemini.api_key, model)
        system = build_system()
        files = vision_files(sources) if provider.supports_files else []
        if sum(len(f["data"]) for f in files) > MAX_VISION_BYTES:
            raise IngestError("Ảnh/PDF scan gửi cho Gemini quá nặng (tối đa 15 MB cộng lại). "
                              "Bỏ bớt tệp, hoặc dùng bản PDF có chữ.")
        batches = extraction_batches(sources)
        result = {"products": [], "truncated": False}
        seen = set()
        for index, batch in enumerate(batches):
            try:
                part = self._ask(provider, system, build_user_text(batch),
                                 vision_files(batch) if provider.supports_files else [], [s["id"] for s in batch])
            except (AIProviderError, IngestError) as exc:
                if not result["products"]:
                    raise IngestError(str(exc)) from exc
                result["truncated"] = True
                result["extraction_warning"] = str(exc)
                result["batches_remaining"] = len(batches) - index
                break
            attach_product_sources(part["products"], sources)
            result["truncated"] = result["truncated"] or part["truncated"]
            for product in part["products"]:
                key = _name_key(product["fields"].get("ten_san_pham"))
                if key not in seen:
                    seen.add(key)
                    result["products"].append(product)
                elif product.get("default_image_source_id"):
                    # Detail-page evidence wins over a duplicate from an uploaded catalog.
                    at = next(i for i, row in enumerate(result["products"])
                              if _name_key(row["fields"].get("ten_san_pham")) == key)
                    result["products"][at] = product
            if len(result["products"]) > MAX_PRODUCTS:
                result["products"] = result["products"][:MAX_PRODUCTS]
                result["truncated"] = True
                break
        crawl["batches"] = len(batches)
        result["truncated"] = result["truncated"] or bool(crawl.get("limited"))
        first = result["products"][0]
        result.update({
            **first,                              # tương thích: trường của sản phẩm đầu tiên ở gốc
            "provider": provider.name,
            "model": getattr(provider, "last_model", "") or getattr(provider, "model", ""),   # model đã trả lời
            "crawl": crawl,
            "session_id": self._remember(sources),
            "sources": [{"id": s["id"], "kind": s["kind"], "label": s["label"],
                         **{k: s[k] for k in ("page_source_id", "page_url", "image_url") if k in s},
                         **({"thumb": thumbnail_data_url(s["data"])} if s["kind"] in ("image", "og_image") else {})}
                        for s in sources]})
        return result

    @staticmethod
    def _ask(provider, system, user, files, ids):
        raw = provider.generate(system, user, files)
        try:
            return validate_products(raw, ids)
        except ExtractionInvalid as first:
            retry = (user + f"\n\nLẦN TRƯỚC JSON KHÔNG HỢP LỆ ({first}). Trả lại ĐÚNG cấu trúc "
                     '{"san_pham": [{"ten_san_pham": "...", ..., "nguon": "n1", "can_xem": []}]}, chỉ JSON.')
            raw = provider.generate(system, retry, files)
            try:
                return validate_products(raw, ids)
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
        image_ref = payload.get("image") or {}
        staged_image = None
        if image_ref.get("source_id"):
            blob = self._session_image(payload.get("session_id"), image_ref["source_id"])
            if not blob:
                raise IngestError("Ảnh đã hết hạn trong phiên đọc. Hãy đọc lại nguồn rồi lưu nháp.")
            staged_image = _webp_800(blob)
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
        if staged_image:
            path = self.staged_image_path(record_id, create=True)
            path.write_bytes(staged_image)
            image_saved = True
            workspace._touch("profiles", "products", "dashboard")
        return {"ok": True, "id": record_id, "status": "draft", "approved_for_live": False,
                "image_saved": image_saved, "suggestions": self.suggest_skus(record_id)}

    def _session_image(self, session_id, source_id):
        with self._lock:
            session = self._sessions.get(str(session_id or ""))
            if not session or self._clock() - session["at"] > SESSION_TTL:
                return None
            return session["images"].get(str(source_id))

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
