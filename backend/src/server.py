from __future__ import annotations

import json
import hashlib
import inspect
import logging
import math
import os
import re
import random
import threading
import time
import uuid
import unicodedata
from collections import OrderedDict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import net as _net
_net.sanitize_env()

from config import (
    BACKUP_DIR, DATA_DIR,
    APPROVED_CATALOG_PATH, APPROVED_PROFILES_PATH, AUDIO_DEVICE_ID,
    DEEPSEEK_MODEL, GEMINI_API_KEY, GEMINI_MODEL, PROCESSOR_SETTINGS_PATH, EXTENSION_DIR, KNOWLEDGE_PACKAGE_SOURCE, KNOWLEDGE_SEED_PATH,
    LIVE_CATALOG_PATH, LIVE_RAW_CATALOG_PATH, LOG_DIR, MACHINE_CATALOG_MAPPING_PATH,
    MEMORY_DB, MEMORY_JOURNAL_DIR, MEMORY_MAX_TURNS, MISSING_DIR, PLUGINS_DIR, ROOT,
    SHOP_INFO_PATH, SHOPEE_LIVE_MAPPING_PATH, TIKTOK_HOST_UNIQUE_IDS, TIKTOK_HOST_USER_IDS, WEB_DIR,
)
from customer_history import CustomerHistory
from captcha_bridge import CaptchaBridge
from documents import DocumentLibrary
from ai_providers import DeepSeekTextProvider, GeminiProvider
from product_ingest import ProductIngest
from product_enrich import ProductEnrich
from combo_profiles import ComboProfiles
from product_defaults import DefaultProfiles
from customer_memory import CustomerMemory
from delivery_store import DeliveryStore, echo_matches, utf16_length
from privacy import PrivacyService
import reply_schema
from conversation_context import product_reference
from comment_queue import CommentQueue
import fact_revision
from live_conversation import resolve_question, conversation_state
from conversation_context import shared_shop_context, EXAMPLE_POLICY
from shopee_commerce import select_product as select_conversation_product
from metrics_store import MetricsStore
from processor_loader import load_processor
from problems import report_problem
from version import app_version as _app_version
from processor_panel import ProcessorPanel
from processor_settings import ProcessorSettings
from shopee_mapping import ShopeeMappingStore
from shopee_ai import ShopeeAiEngine
from shopee_commerce import price_violation as _clarify_price_violation
import dose_selection
import context_budget
import product_blocklist
import customer_card
import router
import sales_playbook
import reply_variety
import reply_angles

# C14: câu báo đã xoá ghi nhớ, đổi kiểu mỗi lần thay vì một câu nguyên văn.
FORGET_REPLIES = (
    "Em xoá phần ghi nhớ về mình rồi nha.",
    "Xong rồi nha, em đã xoá hết dữ liệu ghi nhớ của mình.",
    "Ok nha, phần em ghi nhớ về mình em xoá sạch rồi đó.",
    "Em đã xoá thông tin ghi nhớ của mình rồi, yên tâm nha.",
    "Dạ xong, dữ liệu em lưu về mình đã được xoá rồi nha.",
)
import smalltalk
from live_sources import LiveSources, PLATFORMS, read_channel_health
from extension_pairing import ExtensionPairing
from video_analysis import (
    UploadOffsetMismatch,
    UploadPartCorrupt,
    VideoAnalysisManager,
)
from workspace import WorkspaceStore
import prompt_view
from learning import Learner, LearningStore, MAX_STYLE_CHARS, style_text as _persona_style_text
from ai_context import task_block as _task_block
from media_assets import ProductImageCache, VideoAssetManager, product_cover_sources
from swallowed import note as _note_swallowed
from knowledge import (
    KnowledgeBase, clean, fold, general_knowledge_hint, has_explicit_product_reference, is_approvable,
    name_tokens, profile_completeness,
    select_live_product, _selection_processor, shared_learning_product_ids,
)


class LoopbackHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    # socketserver mac dinh listen(5): WebView2 mo mot loat ket noi song song
    # (6 CSS + module + API) luc khoi dong, hang doi day thi Windows RST ket noi
    # -> stylesheet mat. Dung hang doi lon + thread rieng cho moi ket noi.
    request_queue_size = 128
    daemon_threads = True
    # O9/F13: trần số kết nối xử lý đồng thời. Vượt trần thì trả 503 ngay thay vì đẻ thêm luồng
    # (tiện ích lỗi/vòng lặp gọi dồn không làm cạn tài nguyên app). Kết nối keep-alive rảnh
    # tự đóng sau Handler.timeout nên luồng được trả lại.
    max_concurrent_requests = 64
    _BUSY_BODY = b'{"ok": false, "error": "server_busy"}'

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(self.max_concurrent_requests)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self._reject_busy(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def _reject_busy(self, request):
        logging.getLogger("kid-te.http").warning("http server busy: rejected connection",
                                                 extra={"event": "http_server_busy"})
        try:
            request.settimeout(0.5)
            try:
                request.recv(65536)   # đọc đầu yêu cầu: đóng khi còn dữ liệu chưa đọc thì Windows RST
            except OSError:
                pass
            request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                            b"Retry-After: 1\r\nConnection: close\r\nContent-Length: "
                            + str(len(self._BUSY_BODY)).encode() + b"\r\n\r\n" + self._BUSY_BODY)
        except OSError:
            pass                      # bên gọi đã đi: không còn ai để báo
        finally:
            self.shutdown_request(request)


def versioned_operator_page(html, version):
    """Gan ?v=<ban> vao asset de ban cap nhat khong tron file cu/moi."""
    version = re.sub(r"[^0-9A-Za-z._-]", "", str(version or "")) or "0"
    html = re.sub(r'((?:href|src)="/assets/[^"?]+)"', lambda m: f'{m.group(1)}?v={version}"', html)
    meta = f'<meta name="kid-te-version" content="{version}">'
    anchor = '<meta charset="utf-8">'
    return html.replace(anchor, anchor + chr(10) + "  " + meta, 1)


def _key_failure(exc):
    """Map an API-key validation failure to a stable error payload, or None if unknown."""
    try:
        import openai
    except Exception:
        return None
    host = "api.deepseek.com"
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return {"ok": False, "error": "key_rejected",
                "message": "Khoá bị từ chối (sai hoặc hết hạn)", "detail": type(exc).__name__}
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
        info = _net.classify(exc.__cause__ or exc, host)
        return {"ok": False, "error": "key_" + info["code"],
                "message": info["message"], "detail": info["detail"]}
    return None


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def request_json_object(raw):
    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('request_number_must_be_finite')
        return result
    def invalid_constant(value):
        raise ValueError('request_number_must_be_finite')
    result = json.loads(raw.decode('utf-8'), parse_float=finite_float, parse_constant=invalid_constant)
    if not isinstance(result, dict):
        raise ValueError('request_body_must_be_object')
    return result


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def load_shop_info(path=None):
    """Thong tin cong khai cua shop (dia chi, hotline...) cho AI tra loi.

    Thieu file thi tra ve rong chu khong vo: AI se im o cau hoi dia chi dung
    nhu truoc, con moi thu khac van chay.
    """
    path = Path(path or SHOP_INFO_PATH)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items()
            if not str(key).startswith("_") and value}


# Thông tin CÔNG KHAI của shop cho AI (khách hỏi ở đâu, số điện thoại…).
SHOP_INFO_FIELDS = [
    ("ten_cong_ty", "Tên shop / công ty", ""),
    ("dia_chi_mua_hang", "Địa chỉ", ""),
    ("hotline", "Hotline", ""),
    ("hotline_phu", "Hotline phụ", ""),
    ("email", "Email", ""),
    ("website", "Website", ""),
    ("facebook", "Facebook", ""),
    ("tiktok", "TikTok", ""),
    ("giao_hang", "Giao hàng / đổi trả", "vd Ship toàn quốc 2–4 ngày, đổi trả trong 7 ngày"),
    ("cach_noi", "Dặn AI cách nói", "vd Khách hỏi ở đâu thì nói tỉnh + ship toàn quốc, không đọc cả dòng địa chỉ"),
]


# Bộ xử lý nghiệp vụ nằm trong plugins/<id>/processor.py (nạp theo đường dẫn).
_dose_processor = load_processor(
    PLUGINS_DIR / "lieu-dung" / "processor.py", "lieu-dung",
)
verified_dose_calculations = _dose_processor.verified_dose_calculations


_price_processor = load_processor(
    PLUGINS_DIR / "chinh-sach-gia" / "processor.py", "chinh-sach-gia",
)
customer_price_signal = _price_processor.customer_price_signal
price_context = _price_processor.price_context
public_product = _price_processor.public_product
asserts_voucher = _price_processor.asserts_voucher
violates_price_policy = _price_processor.violates_price_policy
denies_catalog = _price_processor.denies_catalog


def _muc_chac_chan(margin, min_margin):
    """Mo ta muc chac chan bang loi, khong quy margin thanh phan tram.

    `margin` la khoang cach giua ung vien tot nhat va ung vien ke tiep, khong
    phai xac suat. Giao dien cu hien `Math.round(margin*100)%` — voi gia tri
    that 0.0722 (vua dat nguong 0.06, tuc la MOT lan khop hop le) no bao
    "Do khop 7%", khien nguoi dung tuong la gan nhu khong khop.
    """
    try:
        gia_tri = float(margin)
        nguong = float(min_margin) or 0.06
    except (TypeError, ValueError):
        return {"match_quality": "khong_ro", "match_label": "Chua xac dinh"}
    if gia_tri >= nguong * 2:
        return {"match_quality": "ro", "match_label": "Khop ro"}
    if gia_tri >= nguong:
        return {"match_quality": "vua_du", "match_label": "Khop vua du"}
    return {"match_quality": "yeu", "match_label": "Chua du chac chan"}


class VideoPositionStore:
    """Vi tri video do bang dau van tay am thanh, do tool ben ngoai bao vao.

    Backend chi giu lai va phat lai cho tien ich; no khong nghe am thanh, nen
    tool dong bo co the tat/bat doc lap ma khong anh huong viec tra loi.
    """

    def __init__(self, max_age_s=25.0):
        self._lock = threading.Lock()
        self.max_age_s = float(max_age_s)
        self._state = {}

    def update(self, payload):
        try:
            position = float(payload.get("position_s"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "position_s_required"}
        if not math.isfinite(position) or position < 0:
            return {"ok": False, "error": "position_s_invalid"}

        def number(key, default=0.0):
            try:
                value = float(payload.get(key, default))
                return value if math.isfinite(value) else default
            except (TypeError, ValueError):
                return default

        with self._lock:
            self._state = {
                "video_id": clean(payload.get("video_id"), 80),
                "position_s": round(position, 3),
                "confidence": round(number("confidence"), 4),
                "margin": round(number("margin"), 4),
                "min_margin": round(number("min_margin", 0.06), 4),
                "video_duration_s": round(number("video_duration_s"), 3),
                "source": clean(payload.get("source"), 60) or "audio_fingerprint",
                "measured_at": now_iso(),
                "monotonic": time.monotonic(),
                "state": clean(payload.get('state'), 30) or 'locked',
                "lock_id": clean(payload.get('lock_id'), 80),
                "revision": number('revision'),
                "verification_age_s": number('verification_age_s'),
            }
        return {"ok": True, "accepted": True}

    def current(self):
        """Tra ve vi tri da cong bu thoi gian troi qua ke tu luc do duoc."""
        with self._lock:
            state = dict(self._state)
        if not state:
            return {"ok": True, "available": False, "reason": "no_measurement"}
        age = time.monotonic() - state.pop("monotonic")
        if age > self.max_age_s:
            return {"ok": True, "available": False, "reason": "stale",
                    "age_s": round(age, 2), **state}
        duration = state.get("video_duration_s") or 0.0
        position = state["position_s"] + age
        if duration > 1:
            position %= duration
        state["position_s"] = round(position, 3)
        state["age_s"] = round(age, 2)
        state['heartbeat_age_s'] = round(age, 2)
        state['verification_age_s'] = round(state.get('verification_age_s', 0) + age, 2)
        state.update(_muc_chac_chan(state.get("margin"), state.get("min_margin")))
        return {"ok": True, "available": state.get('state') == 'locked', **state}




_data_processor = load_processor(
    PLUGINS_DIR / "thu-du-lieu" / "processor.py", "thu-du-lieu",
)
RawStreamRecorder = _data_processor.RawStreamRecorder


def _clean_host(value):
    """Chủ phòng do tiện ích tự nhận (GetLiveRoomInfo / tiếng vọng); rỗng nếu không có."""
    if not isinstance(value, dict):
        return None
    host = {key: clean(value.get(key), 240 if key == "sec_uid" else 160)
            for key in ("user_id", "sec_uid", "unique_id", "nickname", "source")}
    return host if (host["user_id"] or host["sec_uid"] or host["unique_id"]) else None


def _is_room_host(live_context, user_id, sec_uid, unique_id):
    host = _clean_host((live_context or {}).get("host") if isinstance(live_context, dict) else None)
    if not host:
        return False
    return bool((user_id and user_id == host["user_id"]) or (sec_uid and sec_uid == host["sec_uid"])
                or (unique_id and host["unique_id"] and unique_id.casefold() == host["unique_id"].casefold()))


class LiveCatalogStore:
    """Nhiều giỏ sản phẩm, mỗi tài khoản LIVE một giỏ (live_carts.json).

    Khoá giỏ = owner_key của chủ phòng: ``uid:<user_id>`` (ưu tiên, số định danh
    TikTok không đổi), rồi ``sec:<sec_uid>``, cuối cùng ``handle:<unique_id>``
    (handle đổi được nên chỉ dùng khi thiếu hai cái trên). Payload không có chủ
    phòng (bản tiện ích cũ) -> giỏ đang hoạt động. ``snapshot`` luôn là giỏ đang
    hoạt động: giỏ của tài khoản đang LIVE, không LIVE thì giỏ chủ shop chọn.
    live_products_current.json giữ làm bản sao giỏ đang hoạt động (quay về bản cũ
    vẫn chạy); lần đầu chạy nó được di chuyển thành giỏ của owner_key ghi trong
    file, không có owner thì thành giỏ "Mặc định".
    """

    DEFAULT_CART = "default"
    LIVE_ACTIVE_SECONDS = 600
    DELETED_KEEP = 20
    # Quét không rõ chủ (trước LIVE TikTok không trả host): chỉ cập nhật một giỏ có
    # sẵn khi tập product_id trùng ít nhất chừng này (Jaccard); khác hẳn -> giỏ mới.
    MATCH_RATIO = 0.8
    HISTORY_KEEP = 10

    def __init__(self):
        self._lock = threading.RLock()
        self._carts = {}
        self._selected = ""
        self._history = {}
        self._load()
        self._load_history()

    # ---- lưu trữ -------------------------------------------------------
    @property
    def carts_path(self):
        return LIVE_CATALOG_PATH.with_name("live_carts.json")

    @property
    def deleted_path(self):
        return LIVE_CATALOG_PATH.with_name("live_carts_deleted.json")

    @property
    def history_path(self):
        return LIVE_CATALOG_PATH.with_name("live_carts_history.json")

    def _load_history(self):
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            # Hỏng: để riêng một bản cứu tay, không ghi đè lên nó.
            try:
                self.history_path.replace(self.history_path.with_name(self.history_path.name + ".corrupt"))
            except OSError:
                pass
            return
        carts = data.get("carts") if isinstance(data, dict) else None
        if isinstance(carts, dict):
            self._history = {clean(key, 200): [e for e in entries if isinstance(e, dict)
                                               and isinstance(e.get("snapshot"), dict)][:self.HISTORY_KEEP]
                             for key, entries in carts.items() if clean(key, 200) and isinstance(entries, list)}

    def _write_history(self):
        atomic_json(self.history_path, {"schema_version": 1, "carts": self._history})

    def _push_history(self, key, snapshot, reason):
        """Giữ bản trước của một giỏ (tối đa HISTORY_KEEP bản, mới nhất trước)."""
        if not (snapshot or {}).get("products"):
            return False
        entry = {"saved_at": now_iso(), "reason": reason,
                 "product_count": len(snapshot.get("products") or []), "snapshot": snapshot}
        self._history[key] = ([entry] + list(self._history.get(key) or []))[:self.HISTORY_KEEP]
        return True

    def cart_history(self, key=""):
        with self._lock:
            key = clean(key, 200) or self.active_key()
            return [{k: v for k, v in entry.items() if k != "snapshot"}
                    for entry in self._history.get(key) or []]

    def restore_previous(self, key=""):
        """Khôi phục bản trước gần nhất của một giỏ. Bản hiện tại vào lịch sử (bấm lại = hoàn tác)."""
        with self._lock:
            key = clean(key, 200) or self.active_key()
            if key not in self._carts:
                raise ValueError("cart_not_found")
            if key == self.live_key():
                raise ValueError("cart_is_live")
            entries = list(self._history.get(key) or [])
            if not entries:
                raise ValueError("cart_history_empty")
            entry = entries.pop(0)
            self._history[key] = entries
            self._push_history(key, self._carts[key].get("snapshot") or {}, "before_restore")
            self._write_history()
            self._carts[key]["snapshot"] = entry["snapshot"]
            self._save()
            return self.carts_state()

    def _load(self):
        path = self.carts_path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = None
        except (OSError, ValueError):
            data = False
        if isinstance(data, dict) and isinstance(data.get("carts"), dict):
            self._carts = {clean(key, 200): self._clean_cart(cart)
                           for key, cart in data["carts"].items()
                           if clean(key, 200) and isinstance(cart, dict)}
            self._selected = clean(data.get("selected"), 200)
            if self._load_legacy_image_sources():
                self._save()
            return
        if data is not None:
            # File hỏng: giữ nguyên một bản để cứu tay, rồi dựng lại từ bản sao cũ.
            try:
                path.replace(path.with_name(path.name + ".corrupt"))
            except OSError:
                pass
        self._migrate_legacy()

    def _migrate_legacy(self):
        try:
            legacy = json.loads(LIVE_CATALOG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(legacy, dict) or not (legacy.get("products") or legacy.get("owner_key")):
            return
        key = clean(legacy.get("owner_key"), 200) or self.DEFAULT_CART
        self._carts = {key: {"name": "", "host": {}, "created_at": clean(legacy.get("synced_at"), 60) or now_iso(),
                             "migrated_from": "live_products_current.json", "snapshot": legacy}}
        self._selected = key
        self._load_legacy_image_sources()
        self._save()

    def _load_legacy_image_sources(self):
        """Seed old carts from the last API response before another sync replaces it."""
        try:
            raw = json.loads(Path(LIVE_RAW_CATALOG_PATH).read_text(encoding="utf-8"))
            covers = product_cover_sources(raw.get("raw_api_data"))
        except (OSError, ValueError, AttributeError):
            return False
        changed = False
        for cart in self._carts.values():
            images = dict(cart.get("image_sources") or {})
            for product in (cart.get("snapshot") or {}).get("products") or []:
                pid = str(product.get("product_id") or "") if isinstance(product, dict) else ""
                if pid in covers:
                    images.setdefault(pid, covers[pid])
            if images != (cart.get("image_sources") or {}):
                cart["image_sources"] = images
                changed = True
        return changed

    @staticmethod
    def _clean_cart(cart):
        host = cart.get("host") if isinstance(cart.get("host"), dict) else {}
        snap = cart.get("snapshot") if isinstance(cart.get("snapshot"), dict) else {}
        return {**cart, "name": clean(cart.get("name"), 80), "host": host, "snapshot": snap}

    def _save(self):
        atomic_json(self.carts_path, {"schema_version": 1, "selected": self._selected, "carts": self._carts})
        active = self._carts.get(self.active_key())
        if active is not None:
            atomic_json(LIVE_CATALOG_PATH, active.get("snapshot") or {})

    # ---- chọn giỏ ------------------------------------------------------
    def _is_live(self, snap):
        return bool(snap.get("room_id")) and self._age_seconds(snap.get("synced_at")) < self.LIVE_ACTIVE_SECONDS

    def live_key(self):
        live = [(str(c["snapshot"].get("synced_at") or ""), key) for key, c in self._carts.items()
                if c["snapshot"].get("room_id") and self._is_live(c["snapshot"])]
        return max(live)[1] if live else ""

    def active_key(self):
        with self._lock:
            live = self.live_key()
            if live:
                return live
            if self._selected in self._carts:
                return self._selected
            return next(iter(self._carts), self.DEFAULT_CART)

    @staticmethod
    def _product_ids(snapshot):
        return {str(p.get("product_id")) for p in (snapshot or {}).get("products") or []
                if isinstance(p, dict) and str(p.get("product_id") or "").strip()}

    def _new_scan_key(self):
        base = "scan:" + datetime.now().strftime("%Y%m%d-%H%M%S")
        key, n = base, 2
        while key in self._carts:
            key, n = f"{base}-{n}", n + 1
        return key

    def _unowned_key(self, incoming_room, profile_id, incoming_ids=frozenset()):
        """Giỏ cho payload không rõ chủ phòng, KHÔNG BAO GIỜ đè giỏ của nguồn khác.

        Trước LIVE TikTok không trả host nên mọi lần quét đều rơi vào giỏ đang hoạt
        động: tab tài khoản 2 quét 39 SP đã đè mất giỏ 19 SP của tab 1 (chủ shop 21/10).
        Giờ: cùng phòng LIVE -> giỏ của phòng đó; giỏ đích rỗng -> ghi; tập product_id
        trùng >= MATCH_RATIO với một giỏ -> cập nhật giỏ đó; khác hẳn -> giỏ mới.
        """
        if incoming_room:
            same_room = [(str(c["snapshot"].get("synced_at") or ""), key) for key, c in self._carts.items()
                         if clean(c["snapshot"].get("room_id"), 160) == incoming_room]
            if same_room:
                return max(same_room)[1]
        base = self._legacy_unowned_key(incoming_room, profile_id)
        current = self._product_ids((self._carts.get(base) or {}).get("snapshot"))
        if not current or not incoming_ids:
            return base

        def overlap(ids):
            return len(ids & incoming_ids) / len(ids | incoming_ids) if ids else 0.0

        if overlap(current) >= self.MATCH_RATIO:
            return base
        best = max(((overlap(self._product_ids(c["snapshot"])), key) for key, c in self._carts.items()
                    if key != base), default=(0.0, ""))
        if best[0] >= self.MATCH_RATIO:
            return best[1]
        return self._new_scan_key()

    def _legacy_unowned_key(self, incoming_room, profile_id):
        """Giỏ ứng viên cho payload không rõ chủ phòng (giỏ trước LIVE thường thiếu host).

        Trước đây luôn rơi về giỏ đang hoạt động = giỏ của tài khoản đang LIVE, nên
        quét trước LIVE ở trình duyệt khác (tài khoản khác) bị guard live_active chặn.
        Giờ: đang có giỏ LIVE do profile trình duyệt KHÁC ghi -> giỏ riêng của profile
        này. Cùng profile (hoặc giỏ LIVE cũ chưa ghi profile) -> như cũ.
        """
        active = self.active_key()
        live = self.live_key()
        if incoming_room or not profile_id or not live or active != live:
            return active
        live_profile = clean(self._carts[live]["snapshot"].get("profile_id"), 80)
        if live_profile and live_profile != profile_id:
            return f"profile:{profile_id}"
        return active

    def _resolve_key(self, host):
        """Giỏ của chủ phòng này. So user_id trước, rồi sec_uid, rồi handle."""
        key = self._owner_key(host)
        if not key:
            return ""
        if key in self._carts:
            return key
        for cart_key, cart in self._carts.items():
            known = dict(cart.get("host") or {})
            if not known and cart_key.startswith("uid:"):
                known = {"user_id": cart_key[4:]}
            for field in ("user_id", "sec_uid", "unique_id"):
                mine, theirs = clean(host.get(field), 240), clean(known.get(field), 240)
                if mine and theirs:
                    if mine.casefold() == theirs.casefold():
                        return cart_key
                    break
        return key

    @staticmethod
    def _view(raw):
        raw = dict(raw or {})
        products = product_blocklist.filter_products(raw.get("products") or [])
        raw.update({"products": products, "product_count": len(products)})
        if raw.get("pin_id") and not any(str(p.get("product_id")) == str(raw["pin_id"]) for p in products):
            raw["pin_id"] = ""
        return raw

    @property
    def _snapshot(self):
        cart = self._carts.get(self.active_key())
        return (cart or {}).get("snapshot") or {}

    @property
    def snapshot(self):
        return self._view(self._snapshot)

    @snapshot.setter
    def snapshot(self, value):
        with self._lock:
            if not value and not self._carts:
                return  # chưa có giỏ nào: không dựng giỏ rỗng
            key = self.active_key()
            cart = self._carts.setdefault(key, {"name": "", "host": {}, "created_at": now_iso(), "snapshot": {}})
            cart["snapshot"] = value if isinstance(value, dict) else {}
            if not self._selected:
                self._selected = key

    def cart_snapshot(self, key=""):
        """Danh mục (đã lọc) của một giỏ; rỗng -> giỏ đang hoạt động."""
        with self._lock:
            key = clean(key, 200)
            if key and key not in self._carts:
                raise ValueError("cart_not_found")
            return self._view((self._carts.get(key or self.active_key()) or {}).get("snapshot"))

    def product_cart_keys(self):
        with self._lock:
            return {key: {str(p.get("product_id")) for p in (c["snapshot"].get("products") or [])
                          if isinstance(p, dict)} for key, c in self._carts.items()}

    def product_image_sources(self):
        """Covers from every cart remain available when another account syncs."""
        with self._lock:
            sources = {}
            candidates = []
            for cart in self._carts.values():
                ids = {str(p.get("product_id")) for p in cart["snapshot"].get("products") or []
                       if isinstance(p, dict)}
                revisions = cart.get("image_revisions") or {}
                for pid, urls in (cart.get("image_sources") or {}).items():
                    if pid in ids:
                        candidates.append((int(revisions.get(pid) or 0),
                                           str(cart.get("images_synced_at") or ""), pid, urls))
            for _, _, pid, urls in sorted(candidates, key=lambda row: row[:2], reverse=True):
                sources.setdefault(pid, []).extend(u for u in urls if u not in sources.get(pid, []))
            return sources

    @staticmethod
    def _display_name(key, cart):
        host = cart.get("host") or {}
        if cart.get("name"):
            return cart["name"]
        if host.get("unique_id"):
            return "@" + host["unique_id"]
        if host.get("nickname"):
            return host["nickname"]
        if key.startswith("profile:"):
            return "Giỏ trước LIVE (trình duyệt khác)"
        if key.startswith("scan:"):
            try:
                at = datetime.strptime(key[5:20], "%Y%m%d-%H%M%S")
                return f"Giỏ quét lúc {at:%H:%M %d/%m}"
            except ValueError:
                return "Giỏ quét"
        return "Mặc định" if key == "default" else key

    def cart_name(self, key=""):
        with self._lock:
            key = clean(key, 200) or self.active_key()
            return self._display_name(key, self._carts.get(key) or {})

    def carts_state(self):
        with self._lock:
            active, live = self.active_key(), self.live_key()
            rows = []
            for key, cart in self._carts.items():
                snap = self._view(cart["snapshot"])
                host = cart.get("host") or {}
                rows.append({"key": key, "name": self._display_name(key, cart),
                             "custom_name": bool(cart.get("name")),
                             "unique_id": clean(host.get("unique_id"), 160),
                             "nickname": clean(host.get("nickname"), 160),
                             "product_count": snap["product_count"],
                             "synced_at": clean(snap.get("synced_at"), 60),
                             "phase": clean(snap.get("phase"), 20), "live": key == live,
                             "active": key == active, "selected": key == self._selected,
                             "history_count": len(self._history.get(key) or []),
                             "history_at": clean(((self._history.get(key) or [{}])[0]).get("saved_at"), 60)})
            return {"ok": True, "active": active, "selected": self._selected, "live": live, "carts": rows}

    def select(self, key):
        with self._lock:
            key = clean(key, 200)
            if key not in self._carts:
                raise ValueError("cart_not_found")
            if key != self._selected:
                self._selected = key
                self._save()
            return self.carts_state()

    def rename(self, key, name):
        with self._lock:
            key = clean(key, 200)
            if key not in self._carts:
                raise ValueError("cart_not_found")
            self._carts[key]["name"] = clean(name, 80).strip()
            self._save()
            return self.carts_state()

    def delete(self, key):
        """Xoá một giỏ. Bản đã xoá được giữ trong live_carts_deleted.json (cứu tay)."""
        with self._lock:
            key = clean(key, 200)
            if key not in self._carts:
                raise ValueError("cart_not_found")
            if key == self.live_key():
                raise ValueError("cart_is_live")
            try:
                trash = json.loads(self.deleted_path.read_text(encoding="utf-8"))
                trash = trash if isinstance(trash, list) else []
            except (OSError, ValueError):
                trash = []
            trash = ([{"key": key, "deleted_at": now_iso(), "cart": self._carts[key]}] + trash)[:self.DELETED_KEEP]
            atomic_json(self.deleted_path, trash)
            del self._carts[key]
            if self._selected == key:
                self._selected = next(iter(self._carts), "")
            self._save()
            return self.carts_state()

    # ---- ghi danh mục --------------------------------------------------
    def sync(self, snapshot, user_initiated=False):
        """Đồng bộ danh mục vào giỏ của chủ phòng, KHÔNG BAO GIỜ để payload rỗng xoá dữ liệu.

        Trước đây mỗi bình luận đều gọi hàm này; một snapshot lỗi là đủ để ghi
        đè danh mục thành 0 sản phẩm và AI mất sạch giá tới lần đồng bộ sau.
        Giỏ của tài khoản khác không bị đụng tới.
        """
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        # Sản phẩm chủ shop loại khỏi AI: bỏ ngay từ cửa, mọi đường trả lời đều không thấy.
        # Retain raw listing metadata so local removal can be undone after a sync.
        # Every reader receives the filtered view.
        products = snapshot.get("products") or []
        incoming_room = clean(snapshot.get("room_id"), 160)
        phase = "live" if incoming_room else (clean(snapshot.get("phase"), 20) or "prelive")
        host = snapshot.get("host") or snapshot.get("owner")
        host = host if isinstance(host, dict) else {}
        owner_key = self._owner_key(host)
        with self._lock:
            # Quyết định bỏ qua và ghi nằm cùng một khoá (tránh 2 sync đè sai).
            profile_id = clean(snapshot.get("profile_id"), 80)
            key = self._resolve_key(host) or self._unowned_key(
                incoming_room, profile_id, frozenset(self._product_ids(snapshot)))
            cart = self._carts.get(key)
            current = dict((cart or {}).get("snapshot") or {})
            current_owner = clean(current.get("owner_key"), 200)
            compact = {
                "schema_version": 1,
                "synced_at": now_iso(),
                "room_id": incoming_room,
                "phase": phase,
                "owner_key": owner_key or current_owner,
                "profile_id": profile_id or clean(current.get("profile_id"), 80),
                "pin_id": clean(snapshot.get("pin_id"), 160),
                "product_count": len(products),
                "price_policy": (
                    "Khi khách hỏi giá mở, mặc định dùng original_price. Giá khách tự báo "
                    "có thể là giá cuối theo voucher/xu/tài khoản và không được tự động bác bỏ."
                ),
                "products": products,
            }
            if (not incoming_room and current.get("room_id") and current.get("products")
                    and (not owner_key or not current_owner or owner_key == current_owner)
                    and self._age_seconds(current.get("synced_at")) < self.LIVE_ACTIVE_SECONDS):
                # Giỏ prelive không được đè catalog của phiên LIVE còn hoạt động.
                kept = self._view(current)
                kept.update(skipped="live_active", cart_key=key)
                return kept
            filtered = self._view(current)
            if not products and filtered.get("products") and (
                    not incoming_room or current.get("room_id") == incoming_room):
                return {**filtered, "cart_key": key}
            # Chỉ chạm đĩa khi nội dung thật sự đổi: trước đây mỗi bình luận
            # ghi lại ~22KB, cộng 125KB raw mỗi lần extension thử cài lại.
            changed = cart is None or self._fingerprint(compact) != self._fingerprint(current)
            created = cart is None
            if user_initiated and not created and changed and self._push_history(key, current, "manual_scan"):
                # Quét tay ghi đè giỏ: giữ bản trước để "Khôi phục bản trước".
                self._write_history()
            if cart is None:
                cart = self._carts[key] = {"name": "", "host": {}, "created_at": compact["synced_at"], "snapshot": {}}
            known = {field: clean(host.get(field), 240 if field == "sec_uid" else 160)
                     for field in ("user_id", "sec_uid", "unique_id", "nickname")}
            merged = {**cart.get("host", {}), **{k: v for k, v in known.items() if v}}
            if merged != cart.get("host"):
                cart["host"] = merged
                changed = True
            cart["snapshot"] = compact
            raw_data = snapshot.get("raw_api_data") or {}
            covers = product_cover_sources(raw_data)
            # Ordinary comment syncs carry no covers. Preserve the last known URLs.
            previous_images = cart.get("image_sources") or {}
            images = {**previous_images, **covers}
            ids = {str(p.get("product_id")) for p in products if isinstance(p, dict)}
            images = {pid: urls for pid, urls in images.items() if pid in ids}
            if images != previous_images:
                cart["image_sources"] = images
                cart["images_synced_at"] = compact["synced_at"]
                cart["images_revision"] = max(
                    (int(c.get("images_revision") or 0) for c in self._carts.values()), default=0) + 1
                previous_revisions = cart.get("image_revisions") or {}
                cart["image_revisions"] = {
                    pid: (cart["images_revision"] if urls != previous_images.get(pid)
                          else int(previous_revisions.get(pid) or 0))
                    for pid, urls in images.items()}
                changed = True
            if not self._selected:
                self._selected = key
            if changed:
                self._save()
            if raw_data:
                atomic_json(LIVE_RAW_CATALOG_PATH, {"schema_version": 1, "synced_at": compact["synced_at"],
                                                    "room_id": incoming_room, "cart_key": key,
                                                    "raw_api_data": raw_data})
            return {**self._view(compact), "cart_key": key, "cart_created": created}

    def raw_products(self, key=""):
        """Sản phẩm đã lưu CHƯA lọc tombstone (để thùng rác hiện ảnh/giá)."""
        with self._lock:
            cart = self._carts.get(clean(key, 200) or self.active_key()) or {}
            return list((cart.get("snapshot") or {}).get("products") or [])

    def drop_products(self, product_ids, key=""):
        """Gỡ hẳn sản phẩm khỏi danh mục đã lưu của MỘT giỏ (một lần ghi). Trả bản cũ để hoàn tác."""
        ids = {str(i) for i in product_ids}
        with self._lock:
            key = clean(key, 200) or self.active_key()
            cart = self._carts.get(key)
            before = dict((cart or {}).get("snapshot") or {})
            products = [p for p in before.get("products") or [] if str(p.get("product_id")) not in ids]
            if cart is None or len(products) == len(before.get("products") or []):
                return before
            after = {**before, "products": products, "product_count": len(products)}
            if after.get("pin_id") in ids:
                after["pin_id"] = ""
            cart["snapshot"] = after
            self._save()
            return before

    def restore_raw(self, snapshot, key=""):
        with self._lock:
            key = clean(key, 200) or self.active_key()
            cart = self._carts.setdefault(key, {"name": "", "host": {}, "created_at": now_iso(), "snapshot": {}})
            cart["snapshot"] = snapshot
            self._save()

    @staticmethod
    def _owner_key(host):
        if not isinstance(host, dict):
            return ""
        for field, prefix in (("user_id", "uid"), ("sec_uid", "sec"), ("unique_id", "handle")):
            value = clean(host.get(field), 160)
            if value:
                return f"{prefix}:{value.lower() if field == 'unique_id' else value}"
        return ""

    @staticmethod
    def _age_seconds(stamp):
        try:
            then = datetime.fromisoformat(str(stamp))
            now = datetime.now(then.tzinfo) if then.tzinfo else datetime.now()
            return (now - then).total_seconds()
        except (TypeError, ValueError):
            return float("inf")

    @staticmethod
    def _fingerprint(snapshot):
        return json.dumps(
            {key: value for key, value in (snapshot or {}).items()
             if key != "synced_at"},
            ensure_ascii=False, sort_keys=True,
        )


class MissingReporter:
    def __init__(self):
        MISSING_DIR.mkdir(parents=True, exist_ok=True)
        self.path = MISSING_DIR / f"missing_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
        self._lock = threading.Lock()
        self.count = 0

    def record(self, payload, reasons, product=None):
        row = {
            "timestamp": now_iso(),
            "comment_id": clean(payload.get("comment_id"), 160),
            "user_id": clean(payload.get("user_id"), 160),
            "username": clean(payload.get("unique_id"), 160),
            "comment": clean(payload.get("comment"), 500),
            "missing_fields": list(reasons),
            "product_id": clean((product or {}).get("product_id"), 160),
            "product_title": clean((product or {}).get("title"), 500),
            "action": "not_sent",
        }
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.count += 1
        return row




_model_processor = load_processor(
    PLUGINS_DIR / "model-deepseek" / "processor.py", "model-deepseek",
)
DeepSeekResponder = _model_processor.DeepSeekResponder

# Rà soát 17/10 C4: số đo chi phí của MỘT lượt = cộng mọi lần gọi model (cổng liều, trả lời, gọi lại...).
TURN_BUDGET_S = 25.0       # C8: hạn tổng mọi lần gọi model của một lượt TikTok
_COST_KEYS = ("prompt_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
              "completion_tokens", "reasoning_tokens", "model_ms")
_PRECISE_WORDS = re.compile(r"\b(cach ly|thoi gian cach ly|bao lau thi thu hoach)\b")


def _sum_model_metrics(base, calls):
    """metrics của lần gọi cuối + tổng chi phí mọi lần gọi trong lượt (không ghi đè)."""
    metrics = dict(base or {})
    calls = [m for m in calls if isinstance(m, dict) and m]
    if len(calls) <= 1 and (not calls or calls[0] is base):
        return metrics
    for key in _COST_KEYS:
        metrics[key] = sum(int(m.get(key) or 0) for m in calls)
    metrics["model_calls"] = sum(int(m.get("model_calls") or 1) for m in calls)
    return metrics


def _sampling_kind(comment, intents, context):
    """P9: lượt giá/liều/cách ly/pha trộn/quà -> 'precise' (nhiệt độ thấp); còn lại để cài đặt chủ shop."""
    intents = intents or {}
    if (intents.get("dose") or intents.get("mixing") or intents.get("price") or intents.get("promotion")
            or (context or {}).get("verified_dose_calculations") or _PRECISE_WORDS.search(fold(comment or ""))):
        return "precise"
    return ""

# Khối nhiệm vụ cho các lời gọi DeepSeek phụ (APP_BRIEF luôn đứng đầu, xem ai_context.py).
_DOSE_TASK = _task_block(
    "Chọn quy tắc liều",
    "khách hỏi liều pha; chọn ĐÚNG MỘT rule_id trong các quy tắc đã duyệt (mã tự tính số, AI không tự tính).",
    inputs="câu khách, thể tích bình, danh sách quy tắc liều đã duyệt.",
    output="JSON chọn rule_id theo ghi chú bên dưới.")
_TRY_TASK = _task_block(
    "Thử AI (xem trước, không gửi lên LIVE)",
    "chủ shop gõ thử một câu khách để xem AI sẽ trả lời gì; chạy ĐÚNG đường trả lời thật.",
    inputs="giống lượt trả lời thật: NỀN CỐ ĐỊNH + dữ liệu lượt + câu khách thử.",
    output="một JSON object {action: reply|skip, ...} như khi LIVE.")
_CLARIFY_TASK = _task_block(
    "Làm rõ sản phẩm khách hỏi",
    "mã chưa chắc khách nói sản phẩm nào; viết một câu trả lời hoặc câu hỏi lại tự nhiên.",
    inputs="câu khách, vài lượt gần nhất (recent_turns), tối đa 3 sản phẩm ứng viên.",
    output="JSON reply ngắn đúng giọng người trực.")
_CLARIFY_RULE = ("Mã chưa chắc khách nói sản phẩm nào. Đọc recent_turns: nếu khách đang nối tiếp sản phẩm vừa bàn "
                 "(vd vừa hỏi can 5l đạm cá rồi hỏi 'đi với thuốc sâu ko' = hỏi pha đạm cá với thuốc sâu) thì trả lời "
                 "luôn bằng dữ liệu + kiến thức chung. Nếu chỉ còn vài lựa chọn (vd chai 1L hay can 5L) thì hỏi tự nhiên "
                 "kèm tên cụ thể. Không lặp câu hỏi lại y như lượt trước; không nêu giá/ưu đãi khi chưa rõ sản phẩm.")


def _library_changed(engine):
    """C04 hook that tolerates stub engines without the method."""
    hook = getattr(engine, "video_library_changed", None)
    if callable(hook):
        hook()


class _V2Writers:
    """Writer coordination for backup v2: create and delete never overlap."""
    def __init__(self):
        self._lock = threading.Lock()
        self._creating = 0
        self._deleting = False

    def begin_create(self):
        with self._lock:
            if self._deleting:
                raise ValueError('backup_busy')
            self._creating += 1

    def end_create(self):
        with self._lock:
            self._creating = max(0, self._creating - 1)

    def begin_delete(self):
        with self._lock:
            if self._deleting or self._creating:
                raise ValueError('backup_busy')
            self._deleting = True

    def end_delete(self):
        with self._lock:
            self._deleting = False


_V2_WRITERS = _V2Writers()


def _awaiting_answer(turns):
    """Lượt gần nhất của khách là câu shop hỏi lại (chọn SP/dung tích/số lượng) còn chờ trả lời."""
    state = (turns[0].get("conversation_state") if turns and isinstance(turns[0], dict) else None) or {}
    return isinstance(state, dict) and bool(state.get("awaiting_fields"))


# Phần thân ReplyEngine được tách theo vùng sang các module mixin (xem engine_mixins.py).
from engine_mixins import adopt as _adopt_mixin  # noqa: E402
from engine_status import EngineStatusMixin  # noqa: E402
from engine_catalog import EngineCatalogMixin  # noqa: E402
from reply_pipeline import ReplyPipelineMixin  # noqa: E402
from engine_learning import EngineLearningMixin  # noqa: E402
from reply_guards import ReplyGuardsMixin  # noqa: E402
from engine_system import EngineSystemMixin  # noqa: E402
_adopt_mixin(EngineStatusMixin, globals())
_adopt_mixin(EngineCatalogMixin, globals())
_adopt_mixin(ReplyPipelineMixin, globals())
_adopt_mixin(EngineLearningMixin, globals())
_adopt_mixin(ReplyGuardsMixin, globals())
_adopt_mixin(EngineSystemMixin, globals())


class ReplyEngine(EngineStatusMixin, EngineCatalogMixin, ReplyPipelineMixin, EngineLearningMixin, ReplyGuardsMixin, EngineSystemMixin):
    def __init__(
        self,
        responder=None,
        memory=None,
        knowledge=None,
        catalog_store=None,
        missing_reporter=None,
        raw_stream=None,
        metrics_store=None,
        video_analysis=None,
        shopee_mapping_store=None,
    ):
        self.responder = responder or DeepSeekResponder()
        self.memory = memory or CustomerMemory(
            MEMORY_DB, MEMORY_JOURNAL_DIR, channel="live-re-ai",
            max_prior_turns=MEMORY_MAX_TURNS,
        )
        if getattr(self.memory, "privacy", None) is None:
            self.memory.privacy = PrivacyService(
                self.memory.db_path, ROOT / "data" / "machine" / "privacy_key")
        self.privacy = self.memory.privacy
        self.delivery_store = DeliveryStore(self.memory.db_path, privacy=self.privacy)
        self.comment_queue = CommentQueue(self.memory.db_path, privacy=self.privacy)
        self.knowledge = knowledge or KnowledgeBase(
            APPROVED_CATALOG_PATH, APPROVED_PROFILES_PATH,
            mapping_path=MACHINE_CATALOG_MAPPING_PATH,
        )
        self.catalog = catalog_store or LiveCatalogStore()
        self.missing = missing_reporter or MissingReporter()
        self.raw_stream = raw_stream if raw_stream is not None else RawStreamRecorder()
        self.metrics = metrics_store or MetricsStore(LOG_DIR / "metrics")
        self.video_position = VideoPositionStore()
        self.video_positions = {"tiktok": self.video_position, "shopee": VideoPositionStore()}
        self.live_sources = LiveSources(ROOT / "data" / "machine" / "live_sources.json")
        self.platform_heartbeats = {}
        # B3: ghép tiện ích lần đầu theo từng sàn (ID unpacked khác nhau mỗi máy).
        self.extension_pairing = ExtensionPairing(ROOT / "data" / "machine" / "extension_pairing.json")
        self.shopee_mapping_store = shopee_mapping_store or ShopeeMappingStore(
            SHOPEE_LIVE_MAPPING_PATH)
        self._shopee_ai = None
        self.video_analysis = video_analysis or VideoAnalysisManager(
            catalog_provider=self.mapping_state,
            live_provider=self._is_live_running,
        )
        self.shop_info = load_shop_info()
        self.started_at = now_iso()
        self.seen = OrderedDict()
        self._lock = threading.Lock()
        from maintenance import MaintenanceMode
        self.maintenance = MaintenanceMode()
        self._pending_reply_bodies = {}
        self._recent_reply_bodies = OrderedDict()
        self.plugin_errors = {}
        self.plugin_runtime = None
        self.audio_device_id = AUDIO_DEVICE_ID
        self.safe_mode = False
        self.captcha = CaptchaBridge(ROOT / "captcha")
        self.extension_heartbeat = None
        catalog_path = self.knowledge.catalog_path
        workspace_root = (catalog_path.parent.parent.parent
                          if catalog_path.parent.name == "shared"
                          else catalog_path.parent)
        self.workspace = WorkspaceStore(self, root=workspace_root)
        # Thêm sản phẩm bằng AI: Gemini đọc ảnh/PDF, DeepSeek đọc chữ.
        self.gemini = GeminiProvider(GEMINI_API_KEY, GEMINI_MODEL)
        self.ingest = ProductIngest(self, self.gemini, DeepSeekTextProvider(lambda: self.responder))
        self.enrich = ProductEnrich(self, self.gemini)      # "Bổ sung bằng Google" (đề xuất, chủ shop duyệt)
        self.combo_profiles = ComboProfiles(self, self.ingest)
        self.product_defaults = DefaultProfiles(self, self.ingest)
        cache_root = Path(workspace_root) / "data" / "cache"
        self.video_assets = VideoAssetManager(cache_root)
        self.product_images = ProductImageCache(cache_root)
        # Trí nhớ AI: kiến thức đã học + phong cách từng người + vòng học lại.
        self.learning = LearningStore(Path(workspace_root) / "data" / "ai_learning.sqlite3")
        self.learner = Learner(
            self.learning, responder_provider=lambda: self.responder,
            catalog_provider=self._learning_catalog,
            examples_provider=self.workspace.learning_examples,
            activity_provider=lambda: self._last_comment_at,
        )
        self._last_comment_at = ""
        # Câu AI vừa gửi (đã xác nhận) — đưa vào prompt để không lặp y chang.
        self._sent_replies = deque(maxlen=12)
        # Tài liệu chủ shop tải lên cho AI đọc (Trí nhớ AI → Tài liệu).
        self.documents = DocumentLibrary(Path(workspace_root) / "data" / "tai_lieu",
                                         tokenizer=_selection_processor._tok)
        # Lịch sử từng khách theo phiên LIVE (đọc luồng bình luận thô + câu đã gửi).
        self.customer_history = CustomerHistory(getattr(self.raw_stream, "dir", LOG_DIR / "raw_stream"),
                                                memory=self.memory)
        # F02: provenance tags on new derived rows + forget reaches every derived store.
        self.learning.privacy = self.privacy
        self.workspace.privacy = self.privacy
        self.memory.derived_stores = self.derived_stores
        # Cài đặt người vận hành cho từng bộ xử lý (Trợ lý tự động → bấm vào bộ xử lý).
        self.processors = ProcessorPanel(self, {
            "chinh-sach-gia": _price_processor, "chon-san-pham": _selection_processor,
            "lieu-dung": _dose_processor, "model-deepseek": _model_processor,
            "thu-du-lieu": _data_processor,
        }, ProcessorSettings(PROCESSOR_SETTINGS_PATH))



    def video_media_path(self, video_id):
        """Locate an uploaded original by published id or approved source hash."""
        detail = self.workspace.detail("videos", video_id)
        timeline = detail["data"].get("timeline") or {}
        wanted_hash = str(timeline.get("source_sha256") or "").casefold()
        source_name = Path(str(timeline.get("source_file") or "")).name
        if source_name:
            download_candidate = Path.home() / "Downloads" / source_name
            if download_candidate.is_file() and self._media_source_matches(download_candidate, wanted_hash):
                return download_candidate
        for job_file in self.video_analysis.root.glob("*/job.json"):
            try:
                job = json.loads(job_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            matches = str(job.get("video_id") or "") == video_id
            matches = matches or (wanted_hash and str(job.get("source_sha256") or "").casefold() == wanted_hash)
            if not matches:
                continue
            candidate = job_file.parent / Path(str(job.get("filename") or "")).name
            if candidate.is_file() and self._media_source_matches(candidate, wanted_hash):
                return candidate
        raise ValueError("video_source_not_available")

    def _media_source_matches(self, path, expected_hash):
        if not expected_hash:
            return True  # Legacy source without a stored hash stays unverified.
        if not re.fullmatch(r'[a-f0-9]{64}', expected_hash):
            return False
        before = path.stat()
        key = (str(path.resolve()), before.st_size, before.st_mtime_ns)
        with self._lock:
            cache = getattr(self, '_media_hash_cache', None)
            if cache is None:
                cache = self._media_hash_cache = OrderedDict()
            hit = cache.get(key)
            if hit and time.monotonic() - hit[1] < 300:
                cache.move_to_end(key)
                return hit[0] == expected_hash
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return False
        value = digest.hexdigest()
        with self._lock:
            cache[key] = (value, time.monotonic())
            cache.move_to_end(key)
            while len(cache) > 128:
                cache.popitem(last=False)
        return value == expected_hash

    # ------------------------------------------------------------------
    # Tài nguyên cho giao diện: ảnh sản phẩm, danh mục giàu thông tin,
    # poster / filmstrip / waveform của video. Chỉ là dẫn xuất (data/cache).
    # ------------------------------------------------------------------
    _SIZE_RE = re.compile(
        r"(?<![\w.])(\d+(?:[.,]\d+)?\s?(?:ml|l|lít|lit|kg|g|gr|gram|gam))(?![\w])",
        re.IGNORECASE)
    _SEO_MARKERS = ("giup", "chuyen dung", "chuyen tri", "cong nghe", "co tac dung",
                    "danh cho", "dung cho", "bao hanh", "chinh hang", "hang chinh",
                    "nong duoc toan cau", "nong duoc", "toan cau")

    @classmethod
    def _short_product_name(cls, title, profile_name=""):
        """Tên đọc được trong 1 giây, lấy từ LISTING TikTok thật.

        Không dùng tên hồ sơ làm tên chính: một hồ sơ có thể dùng chung cho
        nhiều quy cách (hồ sơ "Đạm cá ... (5 lít)" gắn cả cho listing 1L), nên
        tên hồ sơ có thể nói sai dung tích của đúng sản phẩm đang ghim.
        """
        text = clean(title, 400) or clean(profile_name, 160)
        head = re.split(r"\s[-–]\s|\|", text, maxsplit=1)[0].strip() or text
        words = head.split()
        folded = [fold(w) for w in words]
        cut = len(words)
        for marker in cls._SEO_MARKERS:
            parts = marker.split()
            for i in range(1, len(folded) - len(parts) + 1):
                if folded[i:i + len(parts)] == parts:
                    cut = min(cut, i)
                    break
        words = words[:cut]
        for i, word in enumerate(words):
            if i >= 2 and word.endswith(","):
                words = words[:i] + [word.rstrip(",")]
                break
        for i, word in enumerate(words):
            if i >= 2 and cls._SIZE_RE.fullmatch(word.strip("(),")):
                words = words[:i + 1]
                break
        words = words[:7]
        short = " ".join(words).strip(" ,;:-") or text[:40]
        size = cls._SIZE_RE.search(text)
        if size and fold(size.group(1)).replace(" ", "") not in fold(short).replace(" ", ""):
            short = f"{short} · {size.group(1)}"
        return short[:72]

    def _product_image_sources(self):
        catalog = getattr(self, "catalog", None)
        if hasattr(catalog, "product_image_sources"):
            # The store already migrated legacy covers, filtered to surviving listings.
            sources = catalog.product_image_sources()
        else:
            sources = {}
            try:
                raw = json.loads(Path(LIVE_RAW_CATALOG_PATH).read_text(encoding="utf-8"))
                sources = product_cover_sources(raw.get("raw_api_data"))
            except (OSError, json.JSONDecodeError, AttributeError, TypeError):
                pass
        website = {clean(r.get("san_pham_id"), 80): str(r.get("featured_image_url") or "")
                   for r in self.knowledge.all_records()}
        for pid, profile_id in self.knowledge.mapping_assignments().items():
            url = website.get(profile_id)
            if url:
                sources.setdefault(str(pid), []).append(url)
        return sources

    @staticmethod
    def _profile_image_key(profile_id):
        return "profile-" + clean(profile_id, 80)

    def _profile_image_url(self, record):
        """URL ảnh hồ sơ qua cache máy; không trỏ thẳng ra web (chỉ khi đã có ảnh cache)."""
        pid = clean(record.get("san_pham_id"), 80)
        if not pid:
            return ""
        try:
            local = self.ingest.staged_image_path(pid)
            return f"/api/profile/thumb/{pid}?v={local.stat().st_mtime_ns}"
        except (ValueError, OSError):
            pass
        if not str(record.get("featured_image_url") or "").startswith(("http://", "https://")):
            return ""
        key = self._profile_image_key(pid)
        version = self.product_images.version(key)
        return f"/api/profile/thumb/{pid}?v={version or 'remote'}"

    def _listing_image_url(self, product_id, sources):
        """Ảnh gian hàng (hoặc ảnh tải lên) qua cache; "" nếu chưa có nguồn nào."""
        version = ""
        try:
            version = str(int(self.workspace.product_image_path(product_id).stat().st_mtime))
        except ValueError:
            version = self.product_images.version(product_id) or ("remote" if sources.get(product_id) else "")
        if not version:
            return ""
        return f"/api/product/thumb/{product_id}?v={version}"

    def _prefetch_mapping_images(self, sources):
        """Tải nền ảnh còn thiếu (listing + hồ sơ); không chặn trang. Bỏ qua ảnh đã cache."""
        for record in self.knowledge.all_records():
            url = str(record.get("featured_image_url") or "")
            pid = clean(record.get("san_pham_id"), 80)
            if pid and url.startswith(("http://", "https://")):
                sources[self._profile_image_key(pid)] = [url]
        self.product_images.prefetch(sources)

    def profile_thumb_path(self, profile_id):
        profile_id = clean(profile_id, 80)
        record = next((r for r in self.knowledge.all_records()
                       if clean(r.get("san_pham_id"), 80) == profile_id), None)
        if not record:
            raise ValueError("profile_image_not_found")
        try:
            return self.ingest.staged_image_path(profile_id)
        except ValueError:
            pass
        url = str((record or {}).get("featured_image_url") or "")
        if not url.startswith(("http://", "https://")):
            raise ValueError("profile_image_not_found")
        key = self._profile_image_key(profile_id)
        cached = self.product_images.cached(key)
        if cached:
            return cached
        fetched = self.product_images.fetch(key, [url])
        if not fetched:
            raise ValueError("profile_image_not_found")
        return fetched

    def product_thumb_path(self, product_id):
        product_id = clean(product_id, 160)
        try:
            return self.workspace.product_image_path(product_id)
        except ValueError:
            pass
        cached = self.product_images.cached(product_id)
        if cached:
            return cached
        # Chống trùng theo pid + chờ có hạn (≤ REQUEST_WAIT_SECONDS); link lỗi theo lùi thời gian.
        fetched = self.product_images.fetch_waiting(
            product_id, self._product_image_sources().get(product_id, []))
        if not fetched:
            raise ValueError("product_image_not_found")
        return fetched

    def video_catalog(self, cart=""):
        """Danh mục cho Studio video: một chỗ có đủ ảnh, giá, giỏ, hồ sơ.

        cart = khoá giỏ (tài khoản LIVE) Studio chọn; rỗng = giỏ đang hoạt động.
        """
        mapping = self.mapping_state(cart)
        live = (self.catalog.cart_snapshot(clean(cart, 200)) if clean(cart, 200)
                else self.catalog.snapshot) or {}
        by_id = {clean(item.get("product_id"), 160): item for item in live.get("products") or []}
        sources = self._product_image_sources()
        self.product_images.prefetch(sources)
        with self.workspace._db() as db:
            uploaded = {str(row["product_id"]): row["image_hash"][:12] for row in
                        db.execute("SELECT product_id,image_hash FROM product_images")}
        records = {clean(r.get("san_pham_id"), 80): r for r in self.knowledge.all_records()}
        products = []
        for row in mapping.get("products", []):
            pid = row["product_id"]
            item = by_id.get(pid, {})
            profile = records.get(row.get("profile_id") or "", {})
            version = uploaded.get(pid) or self.product_images.version(pid)
            has_image = bool(uploaded.get(pid) or version or sources.get(pid))
            products.append({
                "product_id": pid,
                "bag_index": int(row.get("bag_index") or 0),
                "title": row.get("title", ""),
                "name": self._short_product_name(row.get("title", ""), row.get("profile_name", "")),
                "price": clean(item.get("sale_price") or item.get("price"), 60),
                "original_price": clean(item.get("original_price"), 60),
                "promotion": clean(item.get("promotion"), 200),
                "stock": item.get("stock_num"),
                "is_pinned": bool(item.get("is_pinned")),
                "profile_id": row.get("profile_id", ""),
                "profile_name": row.get("profile_name", ""),
                "package": clean(profile.get("quy_cach_dong_goi"), 80),
                "image_url": (f"/api/product/thumb/{pid}?v={version or 'remote'}"
                              if has_image else ""),
            })
        products.sort(key=lambda p: (p["bag_index"] or 999, p["name"]))
        return {"ok": True, "room_id": mapping.get("room_id", ""),
                "synced_at": mapping.get("synced_at", ""), "cart_key": mapping.get("cart_key", ""),
                "cart_name": mapping.get("cart_name", ""), "products": products}

    def _asset_media_path(self, video_id="", job_id=""):
        if job_id:
            return self.video_job_media_path(job_id)
        return self.video_media_path(video_id)

    def video_assets_state(self, video_id="", job_id="", retry=False):
        try:
            path = self._asset_media_path(video_id, job_id)
        except ValueError:
            return {"ok": True, "state": "missing_media"}
        revision = self._library_revision()
        state = (self.video_assets.retry(path, library_revision=revision) if retry
                 else self.video_assets.describe(path, library_revision=revision))
        key = state.get("key", "")
        if key:
            base = f"/api/video/asset/{key}/"
            if state.get("poster"):
                state["poster_url"] = base + "poster.jpg"
            if state.get("filmstrip"):
                state["filmstrip_url"] = base + "filmstrip.jpg"
            if state.get("waveform"):
                state["waveform_url"] = base + "waveform.json"
        return state

    def video_job_media_path(self, job_id):
        state = self.video_analysis.status(job_id)
        path = self.video_analysis.root / job_id / Path(str(state.get("filename") or "")).name
        if not path.is_file():
            raise ValueError("video_source_not_available")
        return path

    def observe(self, payload):
        """Chỉ ghi lại luồng bình luận thô. Không gọi model, không gửi gì đi."""
        events = payload.get("events")
        if not isinstance(events, list):
            events = [payload]
        written = self.raw_stream.record(events)
        for event in events:
            if (isinstance(event, dict) and event.get("is_host")
                    and event.get("ai_action") in {"self_echo", "host_manual"}):
                self.reconcile_host_echo(event.get("room_id"), event.get("text"))
        for _ in events:
            self.metrics.record("comment")
        if written:
            self.workspace._touch("conversations", "sessions", "customers", "dashboard")
        return {"ok": True, "written": written, "total": self.raw_stream.count,
                "duplicates": self.raw_stream.duplicates}

    def _tracked_result(self, result, *, metrics=None, missing_fields=None):
        outcome = "replied" if result.get("should_send") else "skipped"
        if result.get("reason") == "policy_blocked":
            outcome = "policy_blocked"
        values = dict(metrics or result.get("metrics") or {})
        fields = list(missing_fields or result.get("missing_fields") or [])
        self.metrics.record(
            "reply", outcome=outcome, missing_fields=fields,
            model=clean(values.get("model"), 80),
            **{key: values[key] for key in (
                "model_ms", "total_ms", "prompt_tokens",
                "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
                "completion_tokens", "reasoning_tokens",
            ) if isinstance(values.get(key), (int, float))},
        )
        return result

    def sync_catalog(self, snapshot, user_initiated=False):
        incoming = [p for p in ((snapshot or {}).get("products") or []) if isinstance(p, dict)]             if isinstance(snapshot, dict) else []
        restored = []
        if user_initiated and incoming:
            # Người dùng bấm "lấy lại/quét": giỏ TikTok là nguồn đúng. Sản phẩm từng
            # bị xoá cục bộ (tombstone) mà TikTok vẫn trả về thì đưa lại danh mục.
            # Tự động đồng bộ vẫn giữ tombstone như trước.
            removed = product_blocklist.removed_products()
            for product in incoming:
                pid = str(product.get("product_id") or "").strip()
                if pid and pid in removed:
                    product_blocklist.set_removed(removed[pid], False)
                    restored.append(pid)
        saved = self.catalog.sync(snapshot, user_initiated=True) if user_initiated else self.catalog.sync(snapshot)
        cart_key = saved.get("cart_key", "")
        if user_initiated and cart_key and not saved.get("skipped") and hasattr(self.catalog, "select"):
            # Chủ shop vừa bấm quét ở tài khoản này: trang Sản phẩm chuyển sang giỏ đó.
            self.catalog.select(cart_key)
        self.workspace._touch("products", "catalog", "dashboard")
        result = {"ok": True, "product_count": saved["product_count"], "synced_at": saved["synced_at"],
                  "phase": saved.get("phase", ""), "received_count": len(incoming)}
        if cart_key:
            result["cart_key"] = cart_key
            if saved.get("cart_created"):
                result["cart_created"] = True
            if hasattr(self.catalog, "cart_name"):
                result["cart_name"] = self.catalog.cart_name(cart_key)
        if restored:
            result["restored_ids"] = restored
        if saved.get("skipped"):
            result["skipped"] = saved["skipped"]
            if user_initiated:
                name = self.catalog.cart_name(cart_key) if cart_key and hasattr(self.catalog, "cart_name") else ""
                who = f"Tài khoản {name} đang LIVE" if name else "Phiên LIVE đang hoạt động"
                result.update(ok=False, error=(
                    f"{who}: giỏ trước LIVE không ghi đè danh mục LIVE của tài khoản này. "
                    "Hãy quét từ tab LIVE dashboard của tài khoản đó."))
        return result

    def acknowledge_reply(self, payload):
        interaction_id = clean(payload.get("interaction_id"), 160)
        reply_id = clean(payload.get("reply_id"), 160)
        receipt = payload.get("receipt") if isinstance(payload.get("receipt"), dict) else {}
        durable = self.delivery_store.get(reply_id)
        if durable and len(durable['messages']) != 1:
            return {'ok': False, 'confirmed': False, 'error': 'message_receipt_required'}
        if durable and (durable['platform'] != 'tiktok' or durable['interaction_id'] != interaction_id):
            return {'ok': False, 'confirmed': False, 'error': 'reply_receipt_not_matched'}
        confirmed = self.memory.confirm_reply_sent(interaction_id, reply_id, receipt,
                                                   allow_previous_session=bool(durable))
        if confirmed and durable:
            confirmed = self.delivery_store.record_legacy_delivery(reply_id, 'tiktok',
                durable['room_id'], receipt, 'sent')
        if confirmed:
            self.comment_queue.settle(reply_id, 'sent')
            with self._lock:
                body = self._pending_reply_bodies.pop(reply_id, "")
                if not body and durable:
                    body = durable['messages'][0]['content']
                if body:
                    self._recent_reply_bodies[self._echo_key(body)] = time.monotonic()
                    self._sent_replies.append((time.monotonic(), body))
                    while len(self._recent_reply_bodies) > 500:
                        self._recent_reply_bodies.popitem(last=False)
            self.workspace._touch("conversations", "sessions", "dashboard")
        return {"ok": confirmed, "confirmed": confirmed,
                "error": "reply_receipt_not_matched" if not confirmed else ""}

    def update_reply_delivery(self, payload):
        interaction_id = clean(payload.get("interaction_id"), 160)
        reply_id = clean(payload.get("reply_id"), 160)
        status = clean(payload.get("status"), 24).casefold()
        detail = clean(payload.get("detail"), 300)
        durable = self.delivery_store.get(reply_id)
        if durable and len(durable['messages']) != 1:
            return {'ok': False, 'updated': False, 'error': 'message_receipt_required'}
        if durable and (durable['platform'] != 'tiktok' or durable['interaction_id'] != interaction_id):
            return {'ok': False, 'updated': False, 'error': 'reply_delivery_not_matched'}
        updated = self.memory.mark_reply_delivery(interaction_id, reply_id, status, detail,
                                                  allow_previous_session=bool(durable))
        if updated and durable:
            updated = self.delivery_store.record_legacy_delivery(reply_id, 'tiktok',
                durable['room_id'], {'detail': detail}, status)
        if updated:
            self.comment_queue.settle(reply_id, status)
            with self._lock:
                self._pending_reply_bodies.pop(reply_id, None)
            self.workspace._touch("conversations", "sessions", "dashboard")
        return {"ok": updated, "updated": updated,
                "error": "reply_delivery_not_matched" if not updated else ""}

    @staticmethod
    def _echo_key(value):
        text = clean(value, 500)
        text = re.sub(r"^@[^\s]+\s+", "", text).strip()
        return text.casefold()

    def reconcile_host_echo(self, room_id, text):
        """Comment của host trùng tin AI đã dispatched nhưng mất receipt -> chốt 'sent' (source echo)."""
        room_id = clean(room_id, 160)
        if not room_id or len(clean(text, 600)) < 4:
            return []
        try:
            turn_ids = self.delivery_store.reconcile_echo('tiktok', room_id, clean(text, 600))
        except Exception:
            return []
        for turn_id in turn_ids:
            turn = self.delivery_store.get(turn_id)
            if not turn:
                continue
            state = turn['state']
            sent_times = [m['sent_at'] for m in turn['messages'] if m['sent_at'] is not None]
            if state == 'sent':
                self.memory.confirm_reply_sent(turn['interaction_id'], turn_id,
                    json.loads(turn['messages'][-1]['receipt_json']), allow_previous_session=True)
            elif state == 'partially_sent':
                self.memory.mark_reply_delivery(turn['interaction_id'], turn_id, 'partially_sent',
                    'delivered_by_echo', allow_previous_session=True)
            self.comment_queue.settle(turn_id, state,
                first_sent_at=min(sent_times) if sent_times else None)
            with self._lock:
                self._pending_reply_bodies.pop(turn_id, None)
                for message in turn['messages']:
                    if message['state'] == 'sent':
                        self._recent_reply_bodies[self._echo_key(message['content'])] = time.monotonic()
        if turn_ids:
            self.workspace._touch("conversations", "sessions", "dashboard")
        return turn_ids

    def _is_self_echo(self, comment):
        key = self._echo_key(comment)
        if not key:
            return False
        cutoff = time.monotonic() - 10 * 60
        with self._lock:
            stale = [item for item, at in self._recent_reply_bodies.items() if at < cutoff]
            for item in stale:
                self._recent_reply_bodies.pop(item, None)
            if key in self._recent_reply_bodies:
                return True
            # TikTok hiển thị @nickname (nhiều từ/emoji) thay cho @unique_id.
            return any(echo_matches(body, comment) for at, body in self._sent_replies
                       if at >= cutoff)

    def _duplicate(self, comment_id):
        if not comment_id:
            return False
        with self._lock:
            if comment_id in self.seen:
                return True
            self.seen[comment_id] = True
            while len(self.seen) > 3000:
                self.seen.popitem(last=False)
        return False

    def _select_product(self, comment, live, turns=None, force_product=None):
        """Khách đang hỏi SKU nào + hồ sơ đã duyệt + dữ kiện còn thiếu.

        Dùng chung cho lượt trả lời thật và cho "Thử AI" (không gửi, không ghi).
        """
        learning = getattr(self, "learning", None)
        product = select_live_product(comment, live["products"], live["pin_id"])
        if turns:
            conversation_product, _ = select_conversation_product(
                self, comment, live['products'], live['pin_id'], turns)
            if conversation_product:
                product = conversation_product
        # TÊN GỌI KHÁC đã học ("win gold 20" = Đạm Cá Amino) quyết định sản
        # phẩm TRƯỚC model: nhờ vậy nạp đúng hồ sơ, giá, liều của SKU đó thay
        # vì để model đoán (22/09 nó đoán "Win Gold 20" là mã giảm giá).
        learned_product = None
        if learning is not None:
            live_by_id = {clean(item.get("product_id"), 160): item for item in live["products"]
                          if isinstance(item, dict)}
            titles = {pid: f"{self._short_product_name(clean(item.get('title'), 220))} {clean(item.get('title'), 220)}"
                      for pid, item in live_by_id.items()}
            learned_product = next((live_by_id[pid] for pid in learning.alias_products(comment, titles)
                                    if pid in live_by_id), None)
            if not _selection_processor.SETTINGS["uu_tien_ten_goi_da_hoc"]:
                learned_product = None
            if learned_product:
                # Tên gọi khác thường chỉ DÒNG sản phẩm ("win gold 20" = Đạm Cá
                # Amino, cả can 1L lẫn 5L). Nếu SKU đã chọn (đang ghim / khách nói
                # rõ dung tích) cùng dòng thì giữ nguyên — đó là thứ khách đang xem.
                if not (product and self._same_family(product, learned_product)):
                    product = learned_product
        if force_product and not learned_product:
            product = force_product  # router chỉ lấp chỗ trống mà mã không chắc
        intents = self.knowledge.intents(comment)
        recommendations = self.knowledge.recommendations(comment, live["products"])
        unresolved_recommendation = False
        if (intents["recommendation"] and not learned_product and not force_product
                and not has_explicit_product_reference(comment, product)):
            if recommendations:
                product = recommendations[0]["live_product"]
            else:
                unresolved_recommendation = True
        shop_id = (product or {}).get("product_id", "")
        record = self.knowledge.match(
            (product or {}).get("title", ""), comment, product_id=shop_id,
        )
        approved = self.knowledge.context(record, shop_id=shop_id)
        missing = self.knowledge.missing_fields(comment, product, approved)
        soft_gaps = KnowledgeBase.general_knowledge_gaps(comment, product, approved)
        if "lieu_dung" in soft_gaps and intents["dose"]:
            # D01: rule liều đã duyệt phải được gộp TRƯỚC khi xét thiếu dữ kiện (candidate thì không).
            merged = self._profile_with_dose_rules(approved or {}, [
                clean((approved or {}).get("san_pham_id"), 80),
                "tiktok:" + clean((product or {}).get("product_id"), 160),
                clean((product or {}).get("product_id"), 160)])
            if "lieu_dung" not in KnowledgeBase.general_knowledge_gaps(comment, product, merged):
                soft_gaps = [m for m in soft_gaps if m != "lieu_dung"]
        # Chủ shop đã DẠY đúng dữ kiện này cho đúng sản phẩm thì không cần gợi ý kiến thức chung.
        taught_gates = []
        if learning is not None and shop_id:
            for field in list(missing) + list(soft_gaps):
                if learning.covers(field, [shop_id]):
                    if field in missing:
                        missing.remove(field)
                    if field in soft_gaps:
                        soft_gaps.remove(field)
                    taught_gates.append(field)
        if unresolved_recommendation:
            # Rà soát 17/10 P2: không im lặng — model gợi ý từ danh mục/kiến thức hoặc hỏi thêm cây/bệnh.
            soft_gaps.append("san_pham_phu_hop")
        if "gia_goc" in missing:
            # K13: SKU thiếu giá -> vẫn trả lời (không nói số), mời xem giá trong giỏ.
            missing.remove("gia_goc")
            soft_gaps.append("gia_goc")
        return {"taught_gates": taught_gates,"product": product, "learned_product": learned_product, "intents": intents,
                "recommendations": recommendations, "record": record, "approved": approved,
                "soft_gaps": sorted(set(soft_gaps)), "missing": sorted(set(missing))}

    def _customer_history(self, customer_key, live, comment_id, comment):
        history = getattr(self, "customer_history", None)
        if history is None or not customer_key:
            return {}
        titles = {clean(p.get("product_id"), 160): self._short_product_name(clean(p.get("title"), 220))
                  for p in live.get("products") or [] if isinstance(p, dict)}
        try:
            return history.for_customer(customer_key, current_room=clean(live.get("room_id"), 160),
                                        current_msg_id=comment_id, current_text=comment,
                                        product_titles=titles)
        except Exception as exc:        # lịch sử hỏng không được chặn câu trả lời
            report_problem("customer_history_failed", "Không dựng được lịch sử khách", exc, severity="warning")
            return {}

    _FAMILY_GENERIC = frozenset({"can", "chai", "lit", "ml", "kg", "gr", "goi", "huu", "co", "loai",
                                 "l", "g", "hop", "tui", "bich", "binh"})

    @classmethod
    def _family_tokens(cls, item):
        name = cls._short_product_name(clean((item or {}).get("title"), 220))
        return {t for t in re.findall(r"[a-z0-9]+", fold(name))
                if not any(ch.isdigit() for ch in t) and t not in cls._FAMILY_GENERIC}

    @classmethod
    def _same_family(cls, left, right):
        """Cùng dòng, khác quy cách: "Đạm Cá Amino 1L" ~ "Đạm Cá Hữu Cơ Amino 5 Lít".

        Combo KHÔNG cùng dòng với sản phẩm lẻ; tập từ nhỏ phải nằm trọn trong
        tập lớn (không chỉ "trùng phần lớn") để "Đạm cá + Humic" không lọt vào.
        """
        if not left or not right:
            return False
        if clean(left.get("product_id"), 160) == clean(right.get("product_id"), 160):
            return True
        a, b = cls._family_tokens(left), cls._family_tokens(right)
        if not a or not b or ("combo" in a) != ("combo" in b):
            return False
        small, large = (a, b) if len(a) <= len(b) else (b, a)
        return len(small) >= 2 and small <= large

    def _prompt_catalog(self, live):
        """Danh mục cho nền cố định (dùng chung hai làn để nền byte-identical)."""
        compact_all_products = None
        if context_budget.enabled("compact_catalog"):
            try:
                compact_all_products = context_budget.compact_catalog(live["products"], self._item_short_name)
            except Exception:
                logging.getLogger("kid-te.core").warning("compact catalog failed", exc_info=True)
                compact_all_products = None
        if compact_all_products is None:
            compact_all_products = []
            for item in live["products"]:
                entry = public_product(item, False)
                entry.pop("is_pinned", None)
                entry.pop("stock_num", None)
                compact_all_products.append(entry)
        return compact_all_products

    @staticmethod
    def _scope_warning(live):
        return (f"Phiên đang bán {len(live['products'])} SKU nhưng chỉ một phần có "
                "hồ sơ kỹ thuật đã duyệt. Không được trả lời là shop KHÔNG CÓ một "
                "loại hàng nào; không chắc thì hỏi lại một câu ngắn.")

    def _customer_card(self, ref):
        """Thẻ khách `the_khach` cho lượt này; tắt cờ / không có dữ liệu / lỗi => None."""
        try:
            return customer_card.for_customer(
                self.memory, ref.get("customer_key", ""), ref.get("interaction_id", ""),
                shortener=lambda title: self._short_product_name(clean(title, 220)))
        except Exception:
            logging.getLogger("kid-te.core").warning("customer card failed", exc_info=True)
            return None

    @staticmethod
    def _add_playbook_stable(context):
        """Sổ tay tình huống vào NỀN CỐ ĐỊNH (cả hai làn, cả "Thử AI"). Cờ tắt/lỗi => không thêm."""
        try:
            context.update(sales_playbook.stable_context(_model_processor.has_prompt_override()))
        except Exception:
            logging.getLogger("kid-te.core").warning("playbook stable failed", exc_info=True)
        return context

    def _add_stock_facts(self, context, live, product, recommendations):
        """`ton_kho` (SP đang bàn) và `ton_kho_sp_dang_ghim` (nếu khác) — dữ kiện thật, không bịa số."""
        try:
            products = [item for item in live.get("products") or [] if isinstance(item, dict)]
            threshold = _model_processor.SETTINGS.get("nguong_sap_het", context_budget.LOW_STOCK_DEFAULT)
            current_id = clean((product or {}).get("product_id"), 160)
            alternatives = []
            for rec in recommendations or []:
                item = rec.get("live_product") if isinstance(rec, dict) else None
                if (isinstance(item, dict) and clean(item.get("product_id"), 160) != current_id
                        and (context_budget._stock_number(item) or 0) > 0):
                    alternatives.append(self._item_short_name(item))
            fact = context_budget.stock_fact(product, products, threshold, alternatives)
            if fact:
                context["ton_kho"] = fact
            pin_id = clean(live.get("pin_id"), 160)
            if pin_id and pin_id != current_id:
                pinned = next((item for item in products if clean(item.get("product_id"), 160) == pin_id), None)
                pinned_fact = context_budget.stock_fact(pinned, products, threshold)
                if pinned_fact:
                    context["ton_kho_sp_dang_ghim"] = pinned_fact
        except Exception:
            logging.getLogger("kid-te.core").warning("stock facts failed", exc_info=True)
        return context

    def _reply_context(self, comment, live, sel, memory_context, *, video=None, mark_used=True,
                       history=None, memory_prompt=None, persona_id="", customer_key=""):
        """Ngữ cảnh gửi model. Trả (context, price_signal). persona_id: thử với người trực khác (chỉ "Thử AI")."""
        learning = getattr(self, "learning", None)
        product, approved, intents = sel["product"], sel["approved"], sel["intents"]
        recommendations = sel["recommendations"]
        shop_id = (product or {}).get("product_id", "")
        question_type = self.workspace._question_type(comment)
        persona = learning.persona(persona_id) if learning is not None else None
        persona_id = (persona or {}).get("persona_id", "")
        shared_context = shared_shop_context(self, comment, shop_id, persona_id, mark_used=mark_used)
        approved_examples = shared_context.get('operator_approved_reply_examples', [])
        # Danh mục vào phần prompt CỐ ĐỊNH (DeepSeek cache theo tiền tố): bỏ
        # cờ đang ghim và tồn kho — hai thứ đổi liên tục — để phần này giữ
        # nguyên suốt phiên. Sản phẩm đang ghim đã có ở current_product/pin_id.
        compact_all_products = self._prompt_catalog(live)
        shown_approved, shown_relevant = approved, None
        # Chủ đề router HỢP với ý định của mã (không bao giờ bớt) — chỉ để chọn nhóm trường hồ sơ.
        shown_intents = {**intents, **(sel.get("router_intents") or {})}
        if context_budget.enabled("intent_fields"):
            try:
                shown_approved = context_budget.project_profile(approved, shown_intents, comment)
                shown_relevant = context_budget.trim_recommendations(
                    recommendations, shop_id, shown_intents, self._item_short_name)
            except Exception:
                logging.getLogger("kid-te.core").warning("intent fields failed", exc_info=True)
                shown_approved, shown_relevant = approved, None
        if shown_relevant is None:
            shown_relevant = [{
                **item,
                "live_product": public_product(
                    item.get("live_product"),
                    (item.get("live_product") or {}).get("product_id") == live["pin_id"],
                ),
            } for item in recommendations]
        context = {
            "price_policy": _price_processor.price_policy_text(),
            "price": price_context(product, approved),
            "current_product": public_product(
                product, (product or {}).get("product_id") == live["pin_id"],
            ),
            "approved_product_knowledge": shown_approved,
            # 12/09: khách hỏi "sốp mình ở đâu vậy e" và AI im lặng vì trong
            # ngữ cảnh không hề có địa chỉ. Đây là dữ liệu công khai trên
            # website công ty, không có lý do gì để thiếu.
            "thong_tin_shop": self.shop_info,
            # recommendations mang nguyên bản ghi SKU (có giá sale) nên phải
            # lọc y như current_product, nếu không giá sale vẫn lọt vào prompt.
            "relevant_approved_products": shown_relevant,
            "all_live_products": compact_all_products,
            "pinned_product_id": clean(live.get("pin_id"), 160),
            "customer_memory": memory_context if memory_prompt is None else memory_prompt,
            "operator_approved_reply_examples": approved_examples,
            "approved_example_policy": (
                "operator_approved_reply_examples là cách CHỦ SHOP từng trả lời câu tương "
                "tự: học Ý, KIẾN THỨC và GIỌNG trong đó, KHÔNG chép nguyên văn. Mỗi khách, "
                "mỗi lúc nói khác nhau — viết câu mới hợp đúng khách, đúng sản phẩm và "
                "đúng đoạn host đang nói. Dữ liệu sản phẩm, giá và liều hiện hành ở trên "
                "luôn có ưu tiên cao hơn ví dụ."
            ),
            "catalog_scope_warning": self._scope_warning(live),
        }
        # H2: tồn kho thật CHỈ ở phần theo lượt (đổi liên tục, không vào nền cố định để giữ cache).
        context["current_product"].pop("stock_num", None)
        self._add_stock_facts(context, live, product, recommendations)
        if learning is not None:
            learned = learning.relevant(
                comment, product_ids=shared_learning_product_ids(self.knowledge, sel.get("record"),
                    shop_id, clean(live.get("pin_id"), 160)), persona_id=persona_id,
            )
            if learned:
                context["kien_thuc_da_hoc"] = learned
                if mark_used:
                    learning.mark_used([item["id"] for item in learned])
            context.update(self._persona_context(learning, persona))
        recent = self._recent_sent_replies()
        if recent:
            context["cau_da_gui_gan_day"] = recent
            context["tranh_mo_dau"] = reply_variety.openings(recent, words=2)
        hint = self._reply_angle_hint(reply_angles.topics_from_intents(shown_intents), customer_key)
        if hint:
            context["goi_y_luot_nay"] = hint
        if history:
            context["lich_su_khach"] = history
        documents = getattr(self, "documents", None)
        if documents is not None:
            found = documents.search(comment, extra_terms=clean((product or {}).get("title"), 220))
            if found:
                context["tai_lieu_lien_quan"] = [{k: v for k, v in item.items() if k in {"tai_lieu", "loai", "doan"}}
                                                 for item in found]
                context["tai_lieu_policy"] = (
                    "tai_lieu_lien_quan là trích đoạn tài liệu CHỦ SHOP tải lên (công ty, sản phẩm, kiến thức "
                    "nông nghiệp). Dùng làm căn cứ để giải thích cho đúng; nói lại bằng lời tự nhiên, ngắn, không "
                    "chép nguyên văn, không nói 'theo tài liệu'. Giá, khuyến mãi, liều pha LUÔN theo dữ liệu sản "
                    "phẩm hiện hành ở trên (tài liệu có thể đã cũ). Đoạn nào không liên quan thì bỏ qua.")
        dose_profile = approved
        if intents["dose"]:
            dose_profile = self._profile_with_dose_rules(approved or {}, [
                clean(approved.get("san_pham_id"), 80),
                "tiktok:" + clean((product or {}).get("product_id"), 160),
                clean((product or {}).get("product_id"), 160)])
        dose_calculations = verified_dose_calculations(
            comment, dose_profile, dose_intent=intents["dose"],
        )
        if dose_calculations:
            context["verified_dose_calculations"] = dose_calculations
        price_signal = customer_price_signal(comment, product, memory_context)
        if price_signal:
            context["customer_price_signal"] = price_signal
        if isinstance(video, dict):
            try:
                position_s = round(max(0.0, float(video.get("position_s", 0))), 2)
            except (TypeError, ValueError):
                position_s = 0.0
            context["video_context"] = {
                "position_s": position_s,
                "transcript_excerpt": clean(video.get("transcript_excerpt"), 900),
                "current_topic": clean(video.get("current_topic"), 240),
                "scripted_product_id": clean(video.get("scripted_product_id"), 160),
                "sync_confidence": clean(video.get("sync_confidence"), 40),
                "reviewed_script_facts": [
                    clean(item, 260) for item in
                    (video.get("reviewed_script_facts") or [])[:6]
                    if clean(item, 260)
                ] if isinstance(video.get("reviewed_script_facts"), list) else [],
            }
        context.update(shared_context)
        self._add_playbook_stable(context)
        if context_budget.enabled("compact_catalog"):
            try:
                context_budget.drop_duplicate_policies(context, _model_processor.has_prompt_override())
            except Exception:
                logging.getLogger("kid-te.core").warning("policy dedupe failed", exc_info=True)
        return context, price_signal

    @classmethod
    def _item_short_name(cls, item):
        return cls._short_product_name(clean((item or {}).get("title"), 220))

    def _is_smalltalk_turn(self, comment, original_comment, live, sel, pending_question, turns, memory_context,
                           router_veto=False):
        """Làn xã giao: bảo thủ, lỗi => False (đường đầy đủ như cũ)."""
        if not context_budget.enabled("smalltalk_lane"):
            return False
        try:
            if pending_question or sel["learned_product"] or sel["missing"] or any(sel["intents"].values()):
                return False
            prior = (turns[0].get("conversation_state") if turns else None) or {}
            if isinstance(prior, dict) and prior.get("awaiting_fields"):
                return False
            if smalltalk.previous_turn_open(turns):
                return False
            product = sel["product"]
            if has_explicit_product_reference(original_comment, product):
                return False
            terms = smalltalk.catalog_terms([p.get("title") for p in live.get("products", []) if isinstance(p, dict)])
            if router_veto:   # router thấy câu hỏi/mua/phản đối...: chọn làn đầy đủ (giàu hơn)
                return False
            if not (smalltalk.is_small_talk(original_comment, terms) and smalltalk.is_small_talk(comment, terms)):
                return False
            if customer_price_signal(comment, product, memory_context):
                return False
            return True
        except Exception:
            logging.getLogger("kid-te.core").warning("smalltalk check failed", exc_info=True)
            return False

    def _lane_stable_context(self, live, learning, persona):
        """NỀN CỐ ĐỊNH của làn xã giao = đúng nền của làn đầy đủ (cùng tiền tố cache)."""
        context = {"price_policy": _price_processor.price_policy_text(),
                   "approved_example_policy": EXAMPLE_POLICY,
                   "catalog_scope_warning": self._scope_warning(live),
                   "thong_tin_shop": self.shop_info,
                   "all_live_products": self._prompt_catalog(live)}
        if learning is not None:
            context.update(self._persona_context(learning, persona))
        self._add_playbook_stable(context)
        if context_budget.enabled("compact_catalog"):
            try:
                context_budget.drop_duplicate_policies(context, _model_processor.has_prompt_override())
            except Exception:
                logging.getLogger("kid-te.core").warning("policy dedupe failed", exc_info=True)
        return context

    def _lane_examples(self, comment, persona_id=""):
        """1-2 ví dụ chủ shop duyệt hợp câu xã giao (chào/trêu); lỗi => []."""
        try:
            workspace = getattr(self, "workspace", None)
            if workspace is None:
                return []
            rows = workspace.reviewed_examples(comment, workspace._question_type(comment), "", limit=2,
                                               mark_used=True, persona_id=persona_id)
            return [{"question": row.get("question"), "approved_answer": row.get("approved_answer")}
                    for row in rows[:2]]
        except Exception:
            logging.getLogger("kid-te.core").warning("lane examples failed", exc_info=True)
            return []

    def _smalltalk_context(self, live, memory_context, *, video=None, comment="", turns=None, card=None,
                           reading=None, customer_key=""):
        """Ngữ cảnh làn xã giao.

        Cờ persona_all_lanes BẬT: cùng nền cố định như làn đầy đủ (giọng, quy tắc đã học, sổ tay, danh mục
        gọn, thông tin shop) — chỉ phần theo lượt là nhỏ: doc_y_khach, 2-3 lượt gần nhất, câu đã gửi gần đây,
        1-2 ví dụ chủ shop duyệt, video_context (the_khach/tinh_huong do handle thêm). Cắt DỮ LIỆU, không cắt TÍNH CÁCH.
        Cờ TẮT: bản tối giản cũ.
        """
        learning = getattr(self, "learning", None)
        persona = learning.persona() if learning is not None else None
        if context_budget.enabled("persona_all_lanes"):
            context = self._lane_stable_context(live, learning, persona)
            if reading:
                context["doc_y_khach"] = reading
            if not card:
                memory = context_budget.short_memory(memory_context)
                if memory:
                    context["customer_memory"] = memory
            recent_turns = customer_card.recent_turns_view(turns)
            if recent_turns:
                context["luot_gan_day"] = recent_turns
            examples = self._lane_examples(comment, (persona or {}).get("persona_id", ""))
            if examples:
                context["operator_approved_reply_examples"] = examples
        else:
            context = {"thong_tin_shop": context_budget.short_shop_info(self.shop_info)}
            if persona:
                style = self._persona_context(learning, persona).get("phong_cach_nguoi_truc")
                if style:
                    context["phong_cach_nguoi_truc"] = style
            memory = context_budget.short_memory(memory_context)
            if memory:
                context["customer_memory"] = memory
        recent = self._recent_sent_replies()
        if recent:
            context["cau_da_gui_gan_day"] = recent
            context["tranh_mo_dau"] = reply_variety.openings(recent, words=2)
        hint = self._reply_angle_hint(("smalltalk",), customer_key)
        if hint:
            context["goi_y_luot_nay"] = hint
        if isinstance(video, dict):
            try:
                position_s = round(max(0.0, float(video.get("position_s", 0))), 2)
            except (TypeError, ValueError):
                position_s = 0.0
            context["video_context"] = {
                "position_s": position_s,
                "transcript_excerpt": clean(video.get("transcript_excerpt"), 900),
                "current_topic": clean(video.get("current_topic"), 240),
                "scripted_product_id": clean(video.get("scripted_product_id"), 160),
                "sync_confidence": clean(video.get("sync_confidence"), 40),
                "reviewed_script_facts": [
                    clean(item, 260) for item in
                    (video.get("reviewed_script_facts") or [])[:6]
                    if clean(item, 260)
                ] if isinstance(video.get("reviewed_script_facts"), list) else [],
            }
        return context

    def handle(self, payload):
        """Một bình luận TikTok -> câu trả lời. Các bước (giữ nguyên thứ tự và logic cũ):
        lọc đầu vào -> chọn sản phẩm -> dựng ngữ cảnh -> gọi model + hậu kiểm -> ghi nhận.
        Mỗi bước trả về dict kết quả khi phải dừng sớm, None để đi tiếp; trạng thái lượt
        nằm trong ``t`` (SimpleNamespace) thay cho biến cục bộ của hàm cũ."""
        t = SimpleNamespace(payload=payload)
        for step in (self._handle_intake, self._handle_select, self._handle_context,
                     self._handle_model, self._handle_commit):
            result = step(t)
            if result is not None:
                return result
        raise AssertionError("handle: _handle_commit luôn trả kết quả")

    def _handle_intake(self, t):
        """Bước 1 — lọc đầu vào: bảo trì, bình luận hỏng, chủ phòng/echo, xoá ghi nhớ,
        hạn tuổi, trùng lặp, ghi nhận bình luận, nối câu hỏi đang chờ."""
        payload = t.payload
        blocked = self._maintenance_block()
        if blocked:
            return blocked
        recompose = int(payload.get('_recompose_count') or 0)
        comment = clean(payload.get("comment"), 500)
        comment_id = clean(payload.get("comment_id"), 160)
        user_id = clean(payload.get("user_id"), 160)
        sec_uid = clean(payload.get("sec_uid"), 240)
        unique_id = clean(payload.get("unique_id"), 160)
        nickname = clean(payload.get("nickname") or unique_id or "Khách", 160)
        # K5: "2", "5" có thể là câu trả lời cho câu hỏi lại vừa gửi — xét sau khi có lượt trước.
        short_reply = len(comment) == 1 and comment.isalnum()
        if (len(comment) < 2 and not short_reply) or not (user_id or sec_uid or unique_id):
            return {"ok": True, "should_send": False, "reason": "invalid_comment"}
        if (user_id in TIKTOK_HOST_USER_IDS or unique_id.casefold() in TIKTOK_HOST_UNIQUE_IDS
                or _is_room_host(payload.get("live_context"), user_id, sec_uid, unique_id)):
            self.reconcile_host_echo((payload.get("live_context") or {}).get("room_id")
                                     if isinstance(payload.get("live_context"), dict) else "", comment)
            return {"ok": True, "should_send": False, "reason": "host_comment"}
        if self._is_self_echo(comment):
            return {"ok": True, "should_send": False, "reason": "self_echo"}

        identity = {
            "platform_user_id": user_id, "sec_uid": sec_uid,
            "unique_id": unique_id, "display_name": nickname,
        }
        if self.memory.forget_if_requested(comment, **identity):
            return self._tracked_result({
                "ok": True, "should_send": True,
                "reply": random.choice(FORGET_REPLIES),
                "requires_ack": False,
            })
        live = self.catalog.sync(payload.get("live_context"))
        received_at = payload.get('received_at_ms', time.time() * 1000)
        if (not isinstance(received_at, (int, float)) or isinstance(received_at, bool)
                or not math.isfinite(received_at)):
            return {'ok': False, 'should_send': False, 'error': 'received_at_invalid'}
        if received_at > time.time() * 1000 + 5000:
            return {'ok': False, 'should_send': False, 'error': 'received_at_invalid'}
        if time.time() * 1000 - received_at >= 120000:
            return {'ok': True, 'should_send': False, 'reason': 'expired'}
        if not comment_id:
            event_time = clean(payload.get('created_at'), 80) or str(int(received_at / 1000))
            identity_key = user_id or sec_uid or unique_id
            comment_id = 'fallback:' + hashlib.sha256(json.dumps(
                [live.get('room_id', ''), identity_key, event_time, comment],
                ensure_ascii=False).encode('utf-8')).hexdigest()
        if not recompose and self._duplicate(f"{live.get('room_id', '')}:{comment_id}"):
            return {'ok': True, 'should_send': False, 'reason': 'duplicate'}
        ref = self.memory.record_comment(
            comment, comment_id=comment_id,
            data_source=clean(payload.get("data_source"), 40) or "unlabeled",
            room_id=live.get("room_id", ""),
            deduplicate=not recompose,
            **identity,
        )
        if ref.get('duplicate'):
            return {'ok': True, 'should_send': False, 'reason': 'duplicate'}
        if ref.get('opted_out'):
            return {'ok': True, 'should_send': False, 'reason': 'memory_disabled'}

        self._last_comment_at = now_iso()
        original_comment = comment
        turns = self.memory.recent_live_turns(ref.get('customer_key', ''), '',
            ref.get('interaction_id', ''), room_id=live.get('room_id', ''))
        if not recompose and customer_card.recent_duplicate(turns, original_comment):
            return self._tracked_result({"ok": True, "should_send": False, "reason": "duplicate_question"})
        if short_reply and not _awaiting_answer(turns):
            return {"ok": True, "should_send": False, "reason": "invalid_comment"}
        comment, pending_question = resolve_question(comment, turns)
        if pending_question:
            self.memory.note_resolved_question(ref.get('interaction_id', ''), comment)
        t.recompose, t.comment, t.comment_id, t.unique_id, t.nickname = (
            recompose, comment, comment_id, unique_id, nickname)
        t.live, t.received_at, t.ref, t.original_comment, t.turns, t.pending_question = (
            live, received_at, ref, original_comment, turns, pending_question)
        return None

    def _handle_select(self, t):
        """Bước 2 — chọn sản phẩm: chọn theo code, router, hỏi lại SKU, thiếu dữ liệu."""
        payload, comment, live, turns = t.payload, t.comment, t.live, t.turns
        original_comment, pending_question = t.original_comment, t.pending_question
        sel = self._select_product(comment, live, turns=turns)
        routed = self._tiktok_router(comment, original_comment, live, turns, sel, pending_question)
        selection_source = "code"
        if routed.get("product"):
            try:
                sel = self._select_product(comment, live, turns=turns, force_product=routed["product"])
                selection_source = "router"
            except Exception:
                logging.getLogger("kid-te.core").warning("router product selection failed", exc_info=True)
                routed.pop("product", None)
        if routed.get("topics"):
            sel["router_intents"] = router.topics_to_intents(routed["topics"])
        product, intents = sel["product"], sel["intents"]
        learned_product = sel["learned_product"]
        router_metrics = (router.metrics_from(routed.get("meta"), routed.get("result"),
                                              bool(routed.get("used")), selection_source)
                          if routed.get("meta") is not None else {})

        missing = sel["missing"]
        if routed.get("clarify_ids") is not None or (
                selection_source != "router" and self._tiktok_sku_ambiguous(comment, live, turns, sel)):
            try:
                if routed.get("clarify_ids") is not None:
                    router_metrics["selection_source"] = "router_clarify"
                clarified = self._tiktok_clarify_sku(payload, t.ref, live, turns, comment, original_comment,
                    t.nickname, t.unique_id, t.received_at, t.recompose,
                    candidate_ids=routed.get("clarify_ids"), extra_metrics=router_metrics)
            except Exception:
                logging.getLogger('kid-te.core').warning('tiktok clarify failed', exc_info=True)
                clarified = None
            if clarified is not None:
                return clarified
        promotion_clarification = missing == ["chuong_trinh"]
        if missing and not promotion_clarification:
            report = self.missing.record(payload, missing, product)
            return self._tracked_result({
                "ok": True, "should_send": False, "reason": "missing_data",
                "missing_fields": missing, "report_path": str(self.missing.path),
                "missing_count": self.missing.count, "report": report,
            })
        t.sel, t.routed, t.product, t.intents, t.learned_product = sel, routed, product, intents, learned_product
        t.router_metrics, t.promotion_clarification = router_metrics, promotion_clarification
        return None

    def _handle_context(self, t):
        """Bước 3 — dựng ngữ cảnh cho model (làn chuyện phiếm hoặc làn bán hàng)."""
        payload, comment, comment_id, live, turns = t.payload, t.comment, t.comment_id, t.live, t.turns
        ref, nickname, unique_id, sel, routed = t.ref, t.nickname, t.unique_id, t.sel, t.routed
        original_comment, pending_question, router_metrics = (
            t.original_comment, t.pending_question, t.router_metrics)
        memory_context = self.memory.context_for_reply(
            ref.get("customer_key", ""), ref.get("interaction_id", ""), nickname,
        )
        history = self._customer_history(ref.get("customer_key", ""), live, comment_id, comment)
        card = self._customer_card(ref)
        smalltalk_lane = self._is_smalltalk_turn(
            comment, original_comment, live, sel, pending_question, turns, memory_context,
            router_veto=bool(routed.get("veto_smalltalk")))
        if smalltalk_lane and routed.get("result") and router_metrics:
            router_metrics["router_used"] = True
        context = None
        if smalltalk_lane:
            try:
                context = self._smalltalk_context(
                    live, memory_context, video=payload.get("video_context"), comment=comment, turns=turns,
                    card=card, reading=router.reading(routed.get("result")),
                    customer_key=ref.get("customer_key", ""))
                price_signal = {}
            except Exception:
                logging.getLogger("kid-te.core").warning("smalltalk context failed", exc_info=True)
                smalltalk_lane, context = False, None
        if context is None:
            context, price_signal = self._reply_context(
                comment, live, sel, memory_context, video=payload.get("video_context"), mark_used=True,
                history=history, customer_key=ref.get("customer_key", ""),
                memory_prompt=self.memory.context_for_reply(
                    ref.get("customer_key", ""), ref.get("interaction_id", ""), nickname,
                    include_turns=False) if history else None,
            )
        record = sel["record"]
        if not smalltalk_lane and router.reading(routed.get("result")):
            context["doc_y_khach"] = router.reading(routed.get("result"))
        sales_playbook.add_card_and_hint(context, card, [original_comment, comment],
                                         (routed.get("result") or {}).get("acts") or (), turns=turns)
        general_hint = None if smalltalk_lane else general_knowledge_hint(sel.get("soft_gaps"))
        if general_hint:
            context["kien_thuc_chung"] = general_hint
        context['conversation'] = {
            'current_message': original_comment, 'resolved_question': comment,
            'pending_question': pending_question.get('comment', '') if pending_question else '',
            'rule': 'Trả lời đúng câu hỏi đang tiếp diễn. Câu khách vừa làm rõ sản phẩm không phải yêu cầu giới thiệu công dụng. Không hỏi lại thông tin khách đã cho.',
        }
        product_ref_note = None
        try:
            if not smalltalk_lane:
                catalog_rows = [{"sku": "tiktok:" + clean(p.get("product_id"), 160), "title": clean(p.get("title"), 220)}
                                for p in live.get("products", []) if isinstance(p, dict) and p.get("product_id")]
                prior_state = (turns[0].get("conversation_state") if turns else None) or {}
                pin_id = clean(live.get("pin_id"), 160)
                product_ref = product_reference(
                    "tiktok", catalog_rows, original_comment,
                    {"sku": "tiktok:" + pin_id} if pin_id else None, prior_state)
                if context_budget.enabled("intent_fields"):
                    # Điểm ứng viên nội bộ model không cần: chỉ ghi vào số liệu.
                    product_ref_note = {"family_id": product_ref.get("family_id", ""),
                                        "selected_sku": product_ref.get("selected_sku"),
                                        "candidates": [c.get("sku") for c in (product_ref.get("candidates") or [])[:3]]}
                else:
                    context["product_ref"] = product_ref
        except Exception:
            logging.getLogger("kid-te.core").warning("product_ref failed", exc_info=True)
        multi = payload.get('protocol_version') == 2
        if multi:
            context['delivery_protocol_version'] = 2
        tag = "@" + (unique_id or nickname)
        max_body = max(1, (150 if multi else _model_processor.max_chars()) - utf16_length(tag) - 1)
        t.started = time.monotonic()
        if t.promotion_clarification:
            context['promotion_status'] = 'Chưa có chương trình được duyệt cho SKU này. Phần khách hỏi quà/ưu đãi: nói cần kiểm tra quà tặng, không bịa quà; không hỏi lại loại hoặc số lượng đã rõ. Nếu câu khách còn hỏi ý khác (liều, công dụng, giá gốc) thì vẫn trả lời phần đó bình thường.'
        t.card, t.smalltalk_lane, t.context, t.price_signal, t.record = card, smalltalk_lane, context, price_signal, record
        t.product_ref_note, t.multi, t.tag, t.max_body = product_ref_note, multi, tag, max_body
        return None

    def _handle_model(self, t):
        """Bước 4 — gọi model (trong hạn tổng của lượt) + hậu kiểm, gom số liệu."""
        payload, comment, live, turns, ref, nickname = t.payload, t.comment, t.live, t.turns, t.ref, t.nickname
        sel, routed, product, intents, router_metrics = t.sel, t.routed, t.product, t.intents, t.router_metrics
        context, smalltalk_lane, card, started = t.context, t.smalltalk_lane, t.card, t.started
        model_calls = []          # C4: metrics từng lần gọi model trong lượt

        def ask_model(*args, **kwargs):
            answer = self.responder.reply(*args, **kwargs)
            model_calls.append((answer or {}).get("metrics") or {})
            return answer
        if not smalltalk_lane:
            context["_sampling"] = _sampling_kind(comment, intents, context)
        # C8: hạn tổng cho mọi lần gọi model của lượt (gồm thử lại) để không giữ hàng chờ 40–60 giây.
        with _model_processor.turn_budget(TURN_BUDGET_S):
            result, dose_gate, retried = self._tiktok_model_turn(
                ask_model, nickname, comment, context, sel, intents, t.max_body, t.price_signal, live, turns)
        if retried == "advisory":
            logging.getLogger("kid-te.core").warning(
                "TikTok reply advisory interaction_id=%s", ref.get('interaction_id', ''),
                extra={"event":"ai_reply_advisory"})
        retried = bool(retried)
        if any(str(r) == "deepseek_TurnDeadline" for r in result.get("missing_fields") or ()):
            logging.getLogger("kid-te.core").warning(
                "TikTok turn dropped: over %ss total model budget interaction_id=%s calls=%d",
                TURN_BUDGET_S, ref.get('interaction_id', ''), len(model_calls),
                extra={"event": "ai_turn_deadline"})
        metrics = _sum_model_metrics(result.get("metrics"), model_calls)
        metrics["total_ms"] = int((time.monotonic() - started) * 1000)
        metrics["intents"] = sorted(name for name, on in intents.items() if on)
        metrics["retried"] = retried
        metrics["knowledge_matched"] = bool(t.record)
        metrics["learned_items"] = len(context.get("kien_thuc_da_hoc") or [])
        metrics["learned_product"] = bool(t.learned_product)
        if smalltalk_lane:
            metrics["context_lane"] = "smalltalk"
        if card:
            metrics["customer_tier"] = card.get("quen_la", "")
        if context.get("tinh_huong_phu_hop"):
            metrics["playbook_hint"] = list(context["tinh_huong_phu_hop"])
        if (routed.get("topics") or router.reading(routed.get("result"))) and router_metrics:
            router_metrics["router_used"] = True
        metrics.update(router_metrics)
        if t.product_ref_note is not None:
            metrics["product_ref"] = t.product_ref_note
        metrics["product_id"] = clean((product or {}).get("product_id"), 160)
        metrics["product_is_pinned"] = (
            metrics["product_id"] == clean(live.get("pin_id"), 160)
        )
        if result.get("action") != "reply":
            reasons = result.get("missing_fields") or ["model_skipped"]
            if any(str(r).startswith(("deepseek_", "model_", "empty_model")) for r in reasons):
                report_problem("ai_reply_failed", f"AI không trả lời được: {', '.join(reasons)}",
                               severity="warning")
                self._note_ai_failure(reasons, metrics.get("http_status"))
            self.missing.record(payload, reasons, product)
            return self._tracked_result({
                "ok": True, "should_send": False, "reason": "model_skipped",
                "missing_fields": reasons, "report_path": str(self.missing.path),
                "missing_count": self.missing.count, "metrics": metrics,
            })
        schema = reply_schema.safe_normalize(result)
        if schema and schema["awaiting_fields"]:
            result = {**result, "awaiting_fields": schema["awaiting_fields"]}
        metrics = reply_schema.audit_metrics(metrics, schema)
        t.result, t.schema, t.metrics = result, schema, metrics
        return None

    def _handle_commit(self, t):
        """Bước 5 — ghi nhận: kiểm hạn/độ dài, lưu lượt trả lời, chuẩn bị gửi bền vững."""
        payload, live, ref, sel, product = t.payload, t.live, t.ref, t.sel, t.product
        result, schema, metrics, multi, tag = t.result, t.schema, t.metrics, t.multi, t.tag
        received_at, recompose, original_comment = t.received_at, t.recompose, t.original_comment
        reply = result["reply"]
        self._live_alerts().ai_succeeded()   # O3: một lượt thành công là hết báo động AI
        bodies = result.get('messages', [reply]) if multi else [reply]
        if time.time() * 1000 - received_at >= 120000:
            return self._tracked_result({'ok': True, 'should_send': False, 'reason': 'expired'})
        if (not isinstance(reply, str) or not isinstance(bodies, list) or not 1 <= len(bodies) <= 3
                or any(not isinstance(body, str) or not body.strip() or utf16_length(f'{tag} {body}') > 150 for body in bodies)):
            return self._tracked_result({'ok': True, 'should_send': False, 'reason': 'reply_invalid'})
        reply_id = uuid.uuid4().hex
        with self._lock:
            self._pending_reply_bodies[reply_id] = reply
        self.memory.record_reply(
            ref.get("interaction_id", ""), reply, complete=False,
            product=product or {}, turn_id=reply_id, metrics=metrics,
        )
        self.memory.note_conversation_state(ref.get('interaction_id', ''),
            conversation_state(result.get('awaiting_fields'),
                product_id=(product or {}).get('product_id', ''), observed_pin=live.get('pin_id', '')))
        durable_turn = None
        if live.get('room_id'):
            try:
                durable_turn = self.delivery_store.prepare(reply_id, 'tiktok', live['room_id'],
                    ref.get('interaction_id', ''), [f'{tag} {body}' for body in bodies],
                    expires_at=received_at / 1000 + 120,
                    fact_revision=self._reply_fact_revision(
                        'tiktok', live, product, sel.get('recommendations') or [], original_comment,
                        payload.get('video_context'), schema=schema),
                    recompose_count=recompose)
            except ValueError as exc:
                if str(exc) in {'delivery_interaction_missing', 'delivery_customer_deleted'}:
                    with self._lock:
                        self._pending_reply_bodies.pop(reply_id, None)
                    return self._tracked_result({'ok': True, 'should_send': False, 'reason': 'memory_disabled'})
                raise
        with self._lock:
            while len(self._pending_reply_bodies) > 500:
                self._pending_reply_bodies.pop(next(iter(self._pending_reply_bodies)))
        return self._tracked_result({
            "ok": True, "should_send": True, "reply": reply, "max_chars": _model_processor.max_chars(),
            "customer_key": ref.get("customer_key", ""),
            "interaction_id": ref.get("interaction_id", ""),
            "reply_id": reply_id, "metrics": metrics,
            **({'protocol_version': 2, 'messages': durable_turn['messages'],
                'turn_id': reply_id, 'room_id': live['room_id'], 'expires_at': durable_turn['expires_at']}
               if multi and durable_turn else {}),
        })





LOOPBACK_HOSTNAMES = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _loopback_host(header_value):
    """True chi khi Host tro dung ve may nay (da bo port, so khop tuyet doi)."""
    host = (header_value or "").strip()
    if not host:
        return False
    if host.startswith("["):                      # dang IPv6: [::1]:8793
        closing = host.find("]")
        if closing < 0:
            return False
        hostname, rest = host[:closing + 1], host[closing + 1:]
        if rest and not rest.startswith(":"):
            return False
    else:
        hostname, _, port = host.partition(":")
        if port and not port.isdigit():
            return False
    return hostname.casefold() in LOOPBACK_HOSTNAMES


import http_routes  # noqa: E402
http_routes.bind(globals())


class Handler(BaseHTTPRequestHandler):
    engine = None
    server_version = "KidTEAI/3.4"
    # O9/F13: hạn cho TỪNG lần đọc/ghi socket (không phải cả yêu cầu): kết nối treo/keep-alive
    # rảnh quá 30 s thì đóng, trả luồng. Tải video/upload theo khối vẫn chạy được vì mỗi khối
    # chỉ cần tiến triển trong 30 s; xử lý lâu phía server không bị tính.
    timeout = 30

    def log_message(self, fmt, *args):
        try:
            logging.getLogger("kid-te.http").info(fmt, *args)
        except (TypeError, ValueError, OSError):
            pass

    def local_tool_request(self):
        """Trang công cụ chạy cùng gốc với backend; extension thì không.

        Server chỉ lắng nghe 127.0.0.1, nhưng vẫn khoá Origin và Host để một
        trang web bất kỳ trong trình duyệt không gọi lén được vào đây.
        """
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        # So khop CHINH XAC hostname, khong dung startswith: "localhost.evil.com"
        # hay "127.0.0.1.attacker.net" deu co tien to hop le. Ke tan cong tro
        # DNS cua ten mien do ve 127.0.0.1 (DNS rebinding) thi trinh duyet gui
        # Host/Origin cua chinh ten mien do — va dieu kien cu khop ca hai.
        if not _loopback_host(host):
            return False
        return origin in ("", f"http://{host}")

    def loopback_read_request(self):
        """B5: route đọc dùng chung cho UI, tiện ích, OBS/plugin cục bộ.

        Bắt buộc Host loopback (chặn DNS rebinding); Origin được là rỗng
        (plugin/OBS/PowerShell), chính trang app, hoặc chrome-extension://.
        """
        host = self.headers.get("Host", "")
        if not _loopback_host(host):
            return False
        origin = self.headers.get("Origin", "")
        return origin in ("", f"http://{host}") or origin.startswith("chrome-extension://")

    @staticmethod
    def extension_platform(route, payload=None):
        """Sàn của một request tiện ích: /api/shopee/* là shopee; heartbeat và
        purge-ack theo trường platform; còn lại (reply, tiktok/queue, catalog/sync,
        observe) là tiktok."""
        if route.startswith("/api/shopee/"):
            return "shopee"
        if route in ("/api/heartbeat", "/api/privacy/purge-ack"):
            value = payload.get("platform", "tiktok") if isinstance(payload, dict) else "tiktok"
            return value if value in PLATFORMS else None
        return "tiktok"

    def extension_paired(self, platform, heartbeat=False):
        origin = self.headers.get("Origin", "")
        if not origin.startswith("chrome-extension://"):
            return True
        pairing = getattr(self.engine, "extension_pairing", None)
        if pairing is None or platform is None:
            return True
        return pairing.check(platform, origin, heartbeat=heartbeat)

    def security_headers(self):
        """B5: không cho nhúng iframe, không đoán MIME."""
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")

    def send_html(self, status, body):
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        self.end_headers()
        self.wfile.write(raw)

    def send_local_file(self, path, content_type="application/octet-stream"):
        path = Path(path)
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.security_headers()
        self.end_headers()
        self.wfile.write(raw)

    def send_cacheable_file(self, path, content_type):
        """Tài nguyên dẫn xuất bất biến theo khoá: cho trình duyệt giữ lại."""
        raw = Path(path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.security_headers()
        self.end_headers()
        self.wfile.write(raw)

    def cors(self):
        origin = self.headers.get("Origin", "")
        if origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")

    def send_video(self, path):
        path = Path(path)
        size = path.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get("Range", "")
        status = 200
        if range_header.startswith("bytes="):
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            if match.group(1):
                start = int(match.group(1))
                end = min(int(match.group(2) or size - 1), size - 1)
            elif match.group(2):
                length = min(int(match.group(2)), size)
                start = size - length
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError):
                    # Trình phát thường hủy range cũ ngay khi người dùng tua, hoặc dừng đọc
                    # khi tạm dừng (quá Handler.timeout). Bình thường, không phải lỗi backend.
                    self.close_connection = True
                    break
                remaining -= len(chunk)

    def _drain_request_body(self, size, cap=512 * 1024 * 1024):
        """Đọc bỏ thân yêu cầu quá lớn để phản hồi lỗi đến được trình duyệt."""
        remaining = min(int(size), cap)
        while remaining > 0:
            chunk = self.rfile.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
        if int(size) > cap:
            self.close_connection = True

    def send_json(self, status, data, *, cache_control=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if cache_control:
                self.send_header("Cache-Control", cache_control)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # A cancelled browser request cannot receive a second error response.
            # The operation may already be committed; preserve its receipt/state.
            self.close_connection = True

    def reject(self, status, error):
        """Từ chối một POST mà vẫn đọc hết body trước khi trả lời.

        Đóng kết nối khi thân yêu cầu còn nằm trong bộ đệm thì Windows reset
        socket, bên gọi nhận ConnectionAborted thay vì đọc được mã 403/404 —
        nhìn ra ngoài giống hệt server sập.
        """
        try:
            size = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            size = 0
        remaining = min(max(size, 0), 8_000_000)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)
        self.send_json(status, {"ok": False, "error": error})

    def do_OPTIONS(self):
        if not self.headers.get("Origin", "").startswith("chrome-extension://"):
            self.send_json(403, {"ok": False, "error": "extension_origin_required"})
            return
        self.send_json(204, {})

    def do_GET(self):
        from urllib.parse import parse_qs, unquote, urlsplit
        parsed = urlsplit(self.path)
        route = parsed.path
        handler = http_routes.match(http_routes.GET_ROUTES, self, route)
        if handler is not None:
            handler(self, route, parsed)
        else:
            self.send_json(404, {"ok": False, "error": "not_found"})

    def do_PUT(self):
        """Chỉ một đường: PUT /api/workspace/sales-playbook (sổ tay tình huống, cục bộ)."""
        from urllib.parse import urlsplit
        route = urlsplit(self.path).path
        if route != "/api/workspace/sales-playbook" or not self.local_tool_request():
            self.reject(403 if route.startswith("/api/workspace/") else 404,
                        "local_only" if route.startswith("/api/workspace/") else "not_found")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 200_000:
                self.reject(400, "invalid_body_size")
                return
            payload = request_json_object(self.rfile.read(size))
            self.send_json(200, self.engine.sales_playbook_save(payload))
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            report_problem("api_error", f"PUT {route}", exc)
            self.send_json(500, {"ok": False, "error": "internal_error"})

    def do_PATCH(self):
        from urllib.parse import unquote, urlsplit
        route = urlsplit(self.path).path
        if not route.startswith("/api/workspace/") or not self.local_tool_request():
            self.reject(403 if route.startswith("/api/workspace/") else 404,
                        "local_only" if route.startswith("/api/workspace/") else "not_found")
            return
        parts = [unquote(part) for part in route.split("/")[3:] if part]
        if len(parts) != 2:
            self.reject(404, "not_found")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 8_000_000:
                raise ValueError("invalid_body_size")
            payload = request_json_object(self.rfile.read(size))
            result = self.engine.workspace_patch(parts[0], parts[1], payload)
            self.send_json(200, result)
        except (ValueError, json.JSONDecodeError) as exc:
            status = 409 if str(exc) == "revision_conflict" else 400
            self.send_json(status, {"ok": False, "error": str(exc)})
        except Exception as exc:
            report_problem("api_error", f"PATCH {route}", exc)
            self.send_json(500, {"ok": False, "error": "internal_error"})

    def do_DELETE(self):
        from urllib.parse import unquote, urlsplit
        route = urlsplit(self.path).path
        if not route.startswith("/api/workspace/") or not self.local_tool_request():
            self.reject(403 if route.startswith("/api/workspace/") else 404,
                        "local_only" if route.startswith("/api/workspace/") else "not_found")
            return
        parts = [unquote(part) for part in route.split("/")[3:] if part]
        product_image = len(parts) == 3 and parts[0] == "products" and parts[2] == "image"
        if len(parts) != 2 and not product_image:
            self.reject(404, "not_found")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > 8_000_000:
                raise ValueError('invalid_body_size')
            payload = request_json_object(self.rfile.read(size)) if size else {}
            result = (self.engine.workspace_delete_product_image(parts[1]) if product_image else
                      self.engine.workspace_delete(parts[0], parts[1], payload))
            self.send_json(200, result)
        except (ValueError, json.JSONDecodeError) as exc:
            status = 409 if str(exc) == "revision_conflict" else 400
            self.send_json(status, {"ok": False, "error": str(exc)})
        except Exception as exc:
            report_problem("api_error", f"DELETE {route}", exc)
            self.send_json(500, {"ok": False, "error": "internal_error"})

    def do_POST(self):
        from urllib.parse import parse_qs, unquote, urlsplit
        route = urlsplit(self.path).path
        early = http_routes.match(http_routes.POST_EARLY_ROUTES, self, route)
        if early is not None:
            early(self, route)
            return
        flags = http_routes.post_route_flags(route)
        if not http_routes.post_guard(self, route, flags):
            return
        product_image_route = flags["product_image_route"]
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if route == "/api/video/upload/chunk":
                if size <= 0 or size > 16 * 1024 * 1024:
                    raise ValueError("invalid_chunk_size")
                query = parse_qs(urlsplit(self.path).query)
                job_id = (query.get("job_id") or [""])[0]
                offset = int((query.get("offset") or ["-1"])[0])
                try:
                    result = self.engine.video_analysis.append_chunk(
                        job_id, offset, self.rfile, size,
                    )
                except UploadOffsetMismatch as exc:
                    # Tra offset server da commit de client resync thay vi
                    # doan mo — retry mu la duong dan toi file nhan doi byte.
                    self.send_json(409, {"ok": False, "error": str(exc),
                                         "committed_offset": exc.committed_offset})
                    return
                except UploadPartCorrupt as exc:
                    self.send_json(409, {"ok": False, "error": str(exc),
                                         "actual_size": exc.actual_size})
                    return
                self.send_json(200, result)
                return
            maximum = 14_000_000 if product_image_route else 8_000_000
            if size <= 0 or size > maximum:
                raise ValueError("invalid_body_size")
            payload = request_json_object(self.rfile.read(size))
            if not self.extension_paired(self.extension_platform(route, payload),
                                         heartbeat=route == "/api/heartbeat"):
                self.send_json(403, {"ok": False, "error": "extension_not_paired"})
                return
            handler = http_routes.match(http_routes.POST_JSON_ROUTES, self, route, flags)
            result = handler(self, route, payload, flags)
            if result is http_routes.RESPONSE_SENT:
                return
            self.send_json(200, result)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(409 if str(exc) in {'revision_conflict', 'maintenance_required', 'backup_busy', 'maintenance'} else 400,
                           {"ok": False, "error": str(exc)})
        except Exception as exc:
            report_problem("api_error", f"POST {route}", exc)
            self.send_json(500, {"ok": False, "error": "internal_error"})
