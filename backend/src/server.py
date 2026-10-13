from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import net as _net
_net.sanitize_env()

from config import (
    BACKUP_DIR, DATA_DIR,
    APPROVED_CATALOG_PATH, APPROVED_PROFILES_PATH, AUDIO_DEVICE_ID,
    DEEPSEEK_MODEL, PROCESSOR_SETTINGS_PATH, EXTENSION_DIR, KNOWLEDGE_PACKAGE_SOURCE, KNOWLEDGE_SEED_PATH,
    LIVE_CATALOG_PATH, LIVE_RAW_CATALOG_PATH, LOG_DIR, MACHINE_CATALOG_MAPPING_PATH,
    MEMORY_DB, MEMORY_JOURNAL_DIR, MEMORY_MAX_TURNS, MISSING_DIR, PLUGINS_DIR, ROOT,
    SHOP_INFO_PATH, SHOPEE_LIVE_MAPPING_PATH, TIKTOK_HOST_UNIQUE_IDS, TIKTOK_HOST_USER_IDS, WEB_DIR,
)
from customer_history import CustomerHistory
from captcha_bridge import CaptchaBridge
from documents import DocumentLibrary
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
import smalltalk
from live_sources import LiveSources, PLATFORMS, read_channel_health
from video_analysis import (
    UploadOffsetMismatch,
    UploadPartCorrupt,
    VideoAnalysisManager,
)
from workspace import WorkspaceStore
from learning import Learner, LearningStore, style_text as _persona_style_text
from ai_context import task_block as _task_block
from media_assets import ProductImageCache, VideoAssetManager
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
    def __init__(self):
        self._lock = threading.RLock()
        self.snapshot = {}
        try:
            self.snapshot = json.loads(LIVE_CATALOG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    def sync(self, snapshot):
        """Đồng bộ danh mục, nhưng KHÔNG BAO GIỜ để một payload rỗng xoá dữ liệu.

        Trước đây mỗi bình luận đều gọi hàm này; một snapshot lỗi là đủ để ghi
        đè danh mục thành 0 sản phẩm và AI mất sạch giá tới lần đồng bộ sau.
        """
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        # Sản phẩm chủ shop loại khỏi AI: bỏ ngay từ cửa, mọi đường trả lời đều không thấy.
        products = product_blocklist.filter_products(snapshot.get("products") or [])
        incoming_room = clean(snapshot.get("room_id"), 160)
        if not products:
            with self._lock:
                if self.snapshot.get("products") and (not incoming_room or self.snapshot.get("room_id") == incoming_room):
                    return dict(self.snapshot)
        compact = {
            "schema_version": 1,
            "synced_at": now_iso(),
            "room_id": incoming_room,
            "pin_id": clean(snapshot.get("pin_id"), 160),
            "product_count": len(products),
            "price_policy": (
                "Khi khách hỏi giá mở, mặc định dùng original_price. Giá khách tự báo "
                "có thể là giá cuối theo voucher/xu/tài khoản và không được tự động bác bỏ."
            ),
            "products": products,
        }
        raw = {
            "schema_version": 1,
            "synced_at": compact["synced_at"],
            "room_id": compact["room_id"],
            "raw_api_data": snapshot.get("raw_api_data") or {},
        }
        with self._lock:
            # Chỉ chạm đĩa khi nội dung thật sự đổi: trước đây mỗi bình luận
            # ghi lại ~22KB, cộng 125KB raw mỗi lần extension thử cài lại.
            changed = self._fingerprint(compact) != self._fingerprint(self.snapshot)
            self.snapshot = compact
            if changed:
                atomic_json(LIVE_CATALOG_PATH, compact)
            if raw["raw_api_data"]:
                atomic_json(LIVE_RAW_CATALOG_PATH, raw)
        return compact

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


class ReplyEngine:
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

    def close(self):
        jobs = getattr(self, '_job_store', None)
        if jobs is not None:
            jobs.close()
        if self._shopee_ai is not None:
            self._shopee_ai.close()
        learner = getattr(self, "learner", None)
        if learner:
            learner.close()
        self.delivery_store.close()
        self.comment_queue.close()
        if getattr(self, "privacy", None) is not None:
            self.privacy.close()
        self.memory.close()
        self.metrics.close()

    def shopee_ai(self):
        with self._lock:
            if self._shopee_ai is None:
                self._shopee_ai = ShopeeAiEngine(
                    self, getattr(self.memory, "db_path", MEMORY_DB),
                    MEMORY_JOURNAL_DIR / "shopee",
                    dose_rule_source=lambda sku: self._dose_rules_for(sku))
            return self._shopee_ai

    def _media_bindings(self):
        with self._lock:
            store = getattr(self, "_media_bindings_store", None)
            if store is None:
                from config import MEDIA_BINDINGS_DB
                from obs_source import MediaBindingStore
                store = self._media_bindings_store = MediaBindingStore(MEDIA_BINDINGS_DB)
            return store

    def _library_revision(self):
        try:
            return int(self._media_bindings().library_revision())
        except Exception:
            logging.getLogger("kid-te.core").warning("library_revision unavailable", exc_info=True)
            return 0

    def video_library_changed(self):
        """C04: every library change (upload/import/delete/publish) invalidates media identity."""
        try:
            return self._media_bindings().bump_library_revision()
        except Exception:
            logging.getLogger("kid-te.core").warning("library_revision bump failed", exc_info=True)
            return None

    def _dose_rules_for(self, sku):
        """D01/D05: read-only, short-lived read of the workspace dose_rules table."""
        import dose_rules
        with self.workspace._db() as db:
            return dose_rules.list_rules(db, sku)["rules"]

    def _profile_with_dose_rules(self, profile, skus):
        """Approved rules (never candidates) merged into the profile; unchanged when none."""
        import dose_rules
        try:
            merged = profile
            with self.workspace._db() as db:
                for sku in dict.fromkeys(s for s in skus if isinstance(s, str) and s.strip()):
                    merged = dose_rules.profile_with_approved_rules(db, merged, sku)
            return merged if merged != profile else profile
        except Exception:
            logging.getLogger("kid-te.core").warning("dose rules merge failed", exc_info=True)
            return profile

    def health(self):
        checker = getattr(self.responder, "check_balance", None)
        status = (checker() if callable(checker)
                  else {"ok": True, "service": "kid-te-ai-standalone"})
        # Mot server CU con song va chiem cong 8793 thi ban moi khong bind
        # duoc, tien ich van noi chuyen voi ban cu — danh muc cu, ma cu, ma
        # khong co dau hieu gi. Ba truong nay lam cai do lo ra ngay.
        try:
            approved = len(getattr(self.knowledge, "records", []) or [])
        except Exception:
            approved = -1
        status.update({
            "pid": os.getpid(),
            "started_at": self.started_at,
            "approved_records": approved,
            "plugins": {
                "loaded": sorted(getattr(self, "plugin_runtime", None).manifests)
                if getattr(self, "plugin_runtime", None) is not None else [],
                "errors": dict(getattr(self, "plugin_errors", {})),
            },
            "safe_mode": bool(getattr(self, "safe_mode", False)),
            "version": _app_version(),
            "capabilities": self.delivery_capabilities(),
        })
        return status

    @staticmethod
    def backend_queue_mode():
        """Công tắc chung: tiện ích lấy việc qua queue/next và kiểm dữ kiện lúc gửi.
        Bật/tắt bằng data/machine/queue_mode.json {"enabled": true}; không cần khởi động lại."""
        if os.environ.get('KIDTE_FACT_REVISION_CHECK') == '1':
            return True
        try:
            return json.loads((ROOT / 'data' / 'machine' / 'queue_mode.json').read_text('utf-8')).get('enabled') is True
        except (OSError, ValueError, AttributeError):
            return False

    @staticmethod
    def delivery_capabilities():
        return {'delivery_store_version': 2, 'message_claim': True,
                'multi_message_reply': True, 'comment_queue_api': True,
                'backend_queue_mode': ReplyEngine.backend_queue_mode()}

    def plugin_state(self):
        if self.plugin_runtime is None:
            return {"ok": True, "plugins": [], "errors": dict(self.plugin_errors)}
        result = self.plugin_runtime.snapshot()
        result["errors"] = dict(self.plugin_errors)
        return result

    def plugin_action(self, payload):
        if self.plugin_runtime is None:
            return {"ok": False, "error": "plugin_runtime_unavailable"}
        action = clean(payload.get("action"), 40)
        plugin_id = clean(payload.get("plugin_id"), 80)
        if action == "start":
            result = self.plugin_runtime.start(plugin_id)
        elif action == "stop":
            result = self.plugin_runtime.stop(plugin_id)
            if plugin_id == "dong-bo-video" and result.get("ok"):
                for platform in PLATFORMS:
                    self.set_video_position({"platform": platform, "available": False})
        elif action == "restart":
            result = self.plugin_runtime.restart(plugin_id)
        elif action == "safe_mode":
            result = self.plugin_runtime.set_safe_mode(payload.get("enabled") is True)
            self.safe_mode = self.plugin_runtime.safe_mode
        else:
            return {"ok": False, "error": "plugin_action_invalid"}
        if result.get("ok") and getattr(self, "workspace", None):
            self.workspace._touch("plugins", "dashboard")
        return result

    def dashboard_state(self):
        """Ảnh chụp vận hành cho UI, không bịa số khi nguồn chưa có."""
        mapping = self.mapping_state()
        products = mapping.get("products") or []
        mapped = sum(1 for item in products if item.get("profile_id"))
        backup = None
        try:
            from backup_policy import latest_backup_time
            from config import BACKUP_DIR
            # Timestamp lookup reads manifests only, never backup payloads.
            cached = getattr(self, "_backup_cache", None)
            if cached and time.monotonic() - cached[0] < 60:
                latest = cached[1]
            else:
                latest = latest_backup_time(BACKUP_DIR)
                self._backup_cache = (time.monotonic(), latest)
            if latest is not None:
                age = (datetime.now(timezone.utc) - latest.astimezone(timezone.utc)).total_seconds()
                backup = {"latest": latest.isoformat(), "overdue": age >= 12 * 3600}
        except Exception:
            backup = None
        health = self._video_health()
        video = {key: health.get(key) for key in (
            "state", "device", "device_id", "video_id", "margin",
            "position_s", "last_seen", "last_lost", "reason",
        )} if health else None
        extension = None
        heartbeat = self.extension_heartbeat
        if isinstance(heartbeat, dict):
            age = max(0.0, time.time() - float(heartbeat.get("received_at", 0)))
            extension = {**heartbeat, "age_s": round(age, 1), "connected": age <= 25}
        # Tong hop tren TOAN BO tap du lieu, khong phai tren mot trang 100 dong.
        # Ban cu lay list(..., limit=100) roi dem len(items): voi 234 hoi thoai
        # thi raw_stream_rows, business_session_count, ai_replied, manual_replied
        # va unanswered deu bi cat cung o 100.
        totals, review_jobs = {}, []
        session_error = ""
        try:
            totals = self.workspace.totals()
            review_jobs = [row for row in self.video_analysis_jobs().get("jobs", [])
                           if row.get("state") == "review"]
            active_video_ids = {row["id"] for row in self.workspace._videos()}
            if video and video.get("video_id") not in active_video_ids:
                video = {**video, "state": "unavailable", "reason": "video_no_longer_in_library",
                         "video_id": "", "position_s": None, "margin": None}
        except Exception as exc:
            # Khong nuot loi thanh so 0: UI phai phan biet "chua doc duoc"
            # voi "that su bang 0".
            session_error = f"{type(exc).__name__}: {exc}"[:200]
        return {
            "ok": True,
            "backend": {"state": "ready", "started_at": self.started_at},
            "captcha": self.captcha.status(),
            "ai": self.health(),
            "plugins": self.plugin_state(),
            "catalog": {
                "total": len(products), "mapped": mapped,
                "pending": len(products) - mapped,
                "unmapped": len(products) - mapped,
                "approved_profiles": len(mapping.get("profiles") or []),
                "synced_at": mapping.get("synced_at") or None,
            },
            "session": {
                "raw_stream_rows": totals.get("conversations"),
                "raw_file_rows": self.raw_stream.count,
                "business_session_count": totals.get("sessions"),
                "ai_replied": totals.get("ai_replied"),
                "manual_replied": totals.get("manual_replied"),
                "unanswered": totals.get("unanswered"),
                "todo": totals.get("todo"),
                "review": totals.get("review"),
                "missing_count": self.missing.count,
                "memory": self.memory.data_summary(),
                "metrics": self.metrics.snapshot(),
                "scope": "all_time",
                "counts_available": not session_error,
                "counts_error": session_error,
            },
            "backup": backup,
            "video": video,
            "live_channels": self.live_channels(),
            "video_review": {"pending_jobs": len(review_jobs),
                             "pending_events": sum((row.get("suggestions") or 0) for row in review_jobs)},
            "extension": extension,
            "recent_activity": self.workspace.recent_activity(),
            "learning": self._learning_summary(),
            "ai_queue": self._ai_queue_summary(),
            "restore_v2_enabled": self._restore_v2_flag(),
        }

    def _restore_v2_flag(self):
        try:
            from restore_coordinator import restore_v2_enabled
            return bool(restore_v2_enabled(DATA_DIR / 'machine' / 'restore_v2.json'))
        except Exception:
            return False

    def _ai_queue_summary(self, window_s=86400):
        """Additive: recent-window queue counts, one grouped SQL per store."""
        empty = lambda: {"pending": 0, "oldest_age_s": 0, "expired": 0, "unknown": 0}
        by_platform = {"tiktok": empty(), "shopee": empty()}
        try:
            rows, now = self.comment_queue.window_counts(window_s)
            for row in rows:
                bucket = by_platform.get(row["platform"])
                if bucket is None:
                    continue
                if row["state"] in ("queued", "generating", "ready", "sending", "partially_sent"):
                    bucket["pending"] += row["n"]
                    if row["state"] == "queued" and row["oldest"] is not None:
                        bucket["oldest_age_s"] = max(bucket["oldest_age_s"], round(max(0, now - row["oldest"])))
                elif row["state"] == "expired":
                    bucket["expired"] += row["n"]
                elif row["state"] == "unknown":
                    bucket["unknown"] += row["n"]
            stores = {"tiktok": self.delivery_store, "shopee": getattr(getattr(self, "_shopee_ai", None), "delivery_store", None)}
            for name, store in stores.items():
                if store is not None:
                    by_platform[name]["unknown"] += store.unknown_count(window_s)
        except Exception:
            pass
        total = {key: sum(b[key] for b in by_platform.values()) for key in ("pending", "expired", "unknown")}
        total["oldest_age_s"] = max(b["oldest_age_s"] for b in by_platform.values())
        return {**total, "by_platform": by_platform, "queue_mode": bool(self.backend_queue_mode())}

    @staticmethod
    def _video_health(max_age_s=30):
        """Trạng thái dịch vụ đồng bộ video. File cũ hơn 30 giây = dịch vụ đã dừng/chết:
        bản cũ vẫn đọc nó nên Tổng quan báo "Đang phát" mãi sau khi dịch vụ tắt."""
        path = ROOT / "runtime" / "services" / "dong-bo-video.health.json"
        try:
            if time.time() - path.stat().st_mtime > max_age_s:
                return {}
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _learning_summary(self):
        learning = getattr(self, "learning", None)
        if learning is None:
            return None
        try:
            stats = learning.stats()
            running = [r for r in learning.runs(limit=5) if r["status"] in {"queued", "running"}]
            return {"suggested": stats["by_status"]["suggested"], "active": stats["by_status"]["active"],
                    "used_total": stats["used_total"], "busy": bool(running),
                    "persona": (learning.persona() or {}).get("name", "")}
        except Exception:
            return None

    @staticmethod
    def backup_now(parts=None):
        from backup_policy import create_operational_backup
        return {"ok": True, **create_operational_backup(parts=parts)}

    @staticmethod
    def _backup_parts(payload):
        parts = payload.get("parts")
        if parts is None:
            return None
        if not isinstance(parts, list):
            raise ValueError("backup_parts_invalid")
        return [clean(item, 40) for item in parts]

    def derived_stores(self):
        return {"learning": getattr(self, "learning", None),
                "workspace": getattr(self, "workspace", None),
                "history": getattr(self, "customer_history", None)}

    def privacy_forget(self, payload):
        key = payload.get("customer_key") if isinstance(payload, dict) else None
        if not isinstance(key, str) or not key.strip() or len(key) > 240:
            raise ValueError("privacy_customer_key_invalid")
        return {"ok": True, **self.privacy.forget(key, memory=self.memory, derived=self.derived_stores())}

    def privacy_purge_ack(self, payload):
        if not isinstance(payload, dict) or payload.get("platform") not in PLATFORMS:
            raise ValueError("privacy_purge_ack_invalid")
        return {"ok": True, **self.privacy.ack_purges(payload.get("ids"), payload["platform"])}

    def privacy_request(self, request_id):
        found = self.privacy.request(request_id)
        if found is None:
            raise ValueError("privacy_request_not_found")
        return {"ok": True, **found}

    def _maintenance_block(self):
        maintenance = getattr(self, 'maintenance', None)
        return maintenance.blocked() if maintenance is not None else None

    def _ai_disabled_by_restore(self):
        maintenance = getattr(self, 'maintenance', None)
        return bool(maintenance is not None and maintenance.ai_disabled)

    def reenable_ai_after_restore(self, payload=None):
        self.maintenance.reenable()
        return {'ok': True, 'ai_disabled': False}

    def _restore_writers(self):
        """Registry of every writer that must be closed before the data dir is swapped."""
        from restore_coordinator import WriterRegistry
        registry = WriterRegistry()

        def closer(owner):
            def run():
                target = getattr(self, owner, None)
                if target is not None:
                    target.close()
            return run
        for name in ('memory', 'delivery_store', 'comment_queue', 'learning', 'privacy'):
            registry.register(name, closer(name))
        registry.register('workspace', lambda: getattr(getattr(self, 'workspace', None), 'close', lambda: None)())
        registry.register('captcha_uploads', lambda: getattr(getattr(self, 'captcha', None), 'close_uploads', lambda: None)())
        registry.register('media_bindings', lambda: setattr(self, '_media_bindings_store', None))
        registry.register('shopee_ai', lambda: (self._shopee_ai.close() if self._shopee_ai is not None else None,
                                                setattr(self, '_shopee_ai', None)))
        return registry

    def _restore_migrate(self, staged):
        """Init each store against the STAGED dir so schema migrations run before the swap."""
        import tempfile
        from comment_queue import CommentQueue
        from learning import LearningStore
        staged = Path(staged)
        memory_db = staged / MEMORY_DB.relative_to(DATA_DIR)
        if memory_db.is_file():
            with tempfile.TemporaryDirectory(prefix='kidte-migrate-') as journal:
                CustomerMemory(memory_db, journal, channel='restore-migrate').close()
            for factory in (DeliveryStore, CommentQueue):
                factory(memory_db).close()
        learning_db = staged / 'ai_learning.sqlite3'
        if learning_db.is_file():
            LearningStore(learning_db).close()

    def _restore_reinit(self):
        from comment_queue import CommentQueue
        from learning import LearningStore
        self.memory = CustomerMemory(MEMORY_DB, MEMORY_JOURNAL_DIR, channel="live-re-ai",
                                     max_prior_turns=MEMORY_MAX_TURNS)
        self.memory.privacy = PrivacyService(self.memory.db_path, ROOT / "data" / "machine" / "privacy_key")
        self.privacy = self.memory.privacy
        self.delivery_store = DeliveryStore(self.memory.db_path, privacy=self.privacy)
        self.comment_queue = CommentQueue(self.memory.db_path, privacy=self.privacy)
        self.learning = LearningStore(self.learning.db_path)
        self.learning.privacy = self.privacy
        self.learner.store = self.learning
        self.workspace = WorkspaceStore(self, root=self.workspace.root)
        self.workspace.privacy = self.privacy
        self.memory.derived_stores = self.derived_stores
        self.customer_history = CustomerHistory(getattr(self.raw_stream, "dir", LOG_DIR / "raw_stream"),
                                                memory=self.memory)
        self._media_bindings_store = None
        self._shopee_ai = None
        self._backup_cache = None

    def restore_backup_v2(self, payload):
        from backup import BackupError
        from backup_v2 import BackupV2
        from restore_coordinator import RestoreCoordinator, RestoreError, restore_v2_enabled
        if not restore_v2_enabled(DATA_DIR / 'machine' / 'restore_v2.json'):
            raise ValueError('restore_v2_disabled')
        backup_id = clean(payload.get('backup_id'), 120)
        password = payload.get('password')
        if not isinstance(password, str) or not password:
            raise ValueError('backup_password_required')
        coordinator = RestoreCoordinator(
            data_dir=DATA_DIR, store=BackupV2(ROOT / 'backups' / 'v2'), maintenance=self.maintenance,
            is_live=self._is_live_running, writers=self._restore_writers(),
            reinit=self._restore_reinit, migrate=self._restore_migrate,
            privacy_db=self.memory.db_path, privacy_key_path=ROOT / 'data' / 'machine' / 'privacy_key')
        try:
            return coordinator.restore(backup_id, password)
        except (RestoreError, BackupError) as exc:
            raise ValueError(str(exc)) from None

    def restore_backup(self, payload):
        if self._is_live_running():
            raise ValueError('maintenance_required')
        if payload.get('v2') is True:
            return self.restore_backup_v2(payload)
        from backup_policy import restore_operational_backup
        result = restore_operational_backup(clean(payload.get("backup_id"), 80),
                                            parts=self._backup_parts(payload))
        # Nạp lại mọi thứ đọc từ các file vừa được ghi đè.
        self.knowledge.reload()
        self.shop_info = load_shop_info()
        learning = getattr(self, "learning", None)
        if learning is not None:
            learning._bump()
        processors = getattr(self, "processors", None)
        if processors is not None:
            processors.store.reload()
            processors.apply_all()
        documents = getattr(self, "documents", None)
        if documents is not None:
            documents._invalidate()
        self.workspace._touch("all", "dashboard")
        self._backup_cache = None
        return result

    def backup_now_and_refresh(self, payload=None):
        result = self.backup_now(self._backup_parts(payload or {}))
        self._backup_cache = None
        return result

    def heartbeat(self, payload):
        platform = payload.get("platform", "tiktok")
        if platform not in PLATFORMS:
            raise ValueError("invalid_platform")
        allowed = {
            "live_running": payload.get("live_running") is True,
            "ai_enabled": payload.get("ai_enabled") is True and not self._ai_disabled_by_restore(),
            "ai_ready": payload.get("ai_ready") is True,
            "observer": payload.get("observer") is True,
            "reply_queue": max(0, min(int(payload.get("reply_queue", 0) or 0), 10000)),
            "observe_queue": max(0, min(int(payload.get("observe_queue", 0) or 0), 10000)),
            "send_paused_until_ms": max(0, int(payload.get("send_paused_until_ms", 0) or 0)),
            "send_pause_reason": clean(payload.get("send_pause_reason"), 160),
            "session_id": clean(payload.get("session_id"), 100),
            "host": _clean_host(payload.get("host")),
            "received_at": time.time(),
        }
        self.platform_heartbeats[platform] = allowed
        if platform == "tiktok":
            self.extension_heartbeat = allowed
        response = {"ok": True, "received_at": allowed["received_at"]}
        try:
            purge = self.privacy.pending_purges(platform)
        except Exception:
            purge = []
        if purge:
            response["privacy_purge"] = purge
        return response

    def delivery_operation(self, platform, operation, payload):
        if operation in {'claim', 'dispatched'}:
            blocked = self._maintenance_block()
            if blocked:
                return blocked
        if platform not in PLATFORMS or not isinstance(payload, dict):
            raise ValueError('delivery_request_invalid')
        keys = ('turn_id', 'room_id') if operation == 'status' else ('turn_id', 'room_id', 'message_id')
        if any(not isinstance(payload.get(key), str) or not payload[key] or len(payload[key]) > 160
               for key in keys):
            raise ValueError('delivery_identity_invalid')
        store = self.delivery_store if platform == 'tiktok' else self.shopee_ai().delivery_store
        turn = store.reconcile(payload['turn_id'])
        if not turn or (turn['platform'], turn['room_id']) != (platform, payload['room_id']):
            return {'ok': False, 'error': 'delivery_identity_mismatch'}
        if operation == 'status':
            return {'ok': True, 'turn': turn}
        if operation in {'claim', 'dispatched'}:
            heartbeat = self.platform_heartbeats.get(platform, {})
            if (not heartbeat.get('live_running') or not heartbeat.get('ai_enabled')
                    or not heartbeat.get('ai_ready') or time.time() - heartbeat.get('received_at', 0) > 30
                    or heartbeat.get('session_id') != payload['room_id']):
                return {'ok': False, 'error': 'delivery_control_inactive'}
            if operation == 'claim':
                # Tắt mặc định: lượt soạn lại chỉ được gửi khi tiện ích lấy việc qua queue/next;
                # ở đường cũ, kiểm tra này sẽ huỷ câu trả lời mà không có lượt thay thế.
                check = self.backend_queue_mode()
                stored = json.loads(turn['fact_revision']) if check and turn.get('fact_revision') else None
                facts = self._current_facts(platform, payload['room_id'], stored) if stored else None
                claim = store.claim(payload['turn_id'], payload['message_id'], platform,
                                    payload['room_id'], current_facts=facts)
                if claim.get('error') in {'revision_changed', 'revision_stale'}:
                    recomposed = claim['recompose'] and self._recompose(platform, turn)
                    if not recomposed:
                        store.cancel_turn(payload['turn_id'], 'revision_stale')
                        self.comment_queue.settle(payload['turn_id'], 'cancelled')
                        claim['error'] = 'revision_stale'
                return claim
            token = payload.get('claim_token')
            if not isinstance(token, str) or not token:
                raise ValueError('delivery_claim_token_invalid')
            if not any(m['message_id'] == payload['message_id'] for m in turn['messages']):
                return {'ok': False, 'error': 'delivery_message_not_found'}
            return {'ok': store.dispatched(payload['message_id'], token)}
        if operation == 'receipt':
            confirmed = store.confirm_message(payload['turn_id'], payload['message_id'], platform,
                payload['room_id'], payload.get('content_hash'), payload.get('receipt'))
            if confirmed:
                state = store.get(payload['turn_id'])['state']
                if state == 'sent':
                    memory = self.memory if platform == 'tiktok' else self.shopee_ai().memory
                    final_message = store.get(payload['turn_id'])['messages'][-1]
                    confirmed = memory.confirm_reply_sent(turn['interaction_id'], turn['turn_id'],
                        json.loads(final_message['receipt_json']), allow_previous_session=True)
                elif state == 'partially_sent':
                    memory = self.memory if platform == 'tiktok' else self.shopee_ai().memory
                    confirmed = memory.mark_reply_delivery(turn['interaction_id'], turn['turn_id'],
                        'partially_sent', allow_previous_session=True)
                if confirmed:
                    sent_times = [m['sent_at'] for m in store.get(payload['turn_id'])['messages'] if m['sent_at'] is not None]
                    self.comment_queue.settle(payload['turn_id'], state,
                        first_sent_at=min(sent_times) if sent_times else None)
            return {'ok': confirmed}
        if operation == 'failure':
            marked = store.mark_message_unknown(payload['turn_id'], payload['message_id'], platform,
                payload['room_id'], payload.get('content_hash'))
            if marked:
                state = store.get(payload['turn_id'])['state']
                memory = self.memory if platform == 'tiktok' else self.shopee_ai().memory
                memory.mark_reply_delivery(turn['interaction_id'], turn['turn_id'], state,
                    'message_delivery_unknown', allow_previous_session=True)
                sent_times = [m['sent_at'] for m in store.get(payload['turn_id'])['messages'] if m['sent_at'] is not None]
                self.comment_queue.settle(payload['turn_id'], state,
                    first_sent_at=min(sent_times) if sent_times else None)
            return {'ok': marked}
        raise ValueError('delivery_operation_invalid')

    def _enqueue_one(self, platform, room, comment):
        if not isinstance(comment, dict):
            raise ValueError('queue_payload_invalid')
        user = comment.get('user_id', '')
        sec_uid = comment.get('sec_uid', '') if platform == 'tiktok' else ''
        unique_id = comment.get('unique_id', '') if platform == 'tiktok' else ''
        if not any((user, sec_uid, unique_id)) or any(not isinstance(v, str) for v in (user, sec_uid, unique_id)):
            raise ValueError('queue_customer_invalid')
        if platform == 'tiktok' and not isinstance(comment.get('live_context', {}), dict):
            raise ValueError('queue_live_context_invalid')
        supplied_room = comment.get('session_id') if platform == 'shopee' else (comment.get('live_context') or {}).get('room_id')
        if supplied_room and supplied_room != room:
            raise ValueError('queue_room_mismatch')
        memory = self.memory if platform == 'tiktok' else self.shopee_ai().memory
        customer_key = memory.customer_key(platform_user_id=user, sec_uid=sec_uid,
            unique_id=unique_id, platform=platform)[0]
        return self.comment_queue.enqueue(platform, room, customer_key, comment)

    def queue_operation(self, platform, operation, payload):
        if operation in {'enqueue', 'next'}:
            blocked = self._maintenance_block()
            if blocked:
                return blocked
        room = payload.get('room_id')
        if not isinstance(room, str) or not room or len(room) > 160:
            raise ValueError('queue_room_invalid')
        if operation == 'status':
            return {'ok': True, **self.comment_queue.summary(platform)}
        if operation == 'cancel':
            comment_id = payload.get('comment_id')
            return {'ok': True, **self.comment_queue.cancel(platform, room, comment_id)}
        if operation == 'enqueue':
            batch = payload.get('comment_payloads')
            if batch is not None:
                # Lô nhiều comment: giữ đúng thứ tự FIFO, mỗi comment có kết quả riêng.
                if not isinstance(batch, list) or not batch or len(batch) > 50:
                    raise ValueError('queue_payload_invalid')
                results = []
                for item in batch:
                    try:
                        results.append({'ok': True, **self._enqueue_one(platform, room, item)})
                    except ValueError as error:
                        results.append({'ok': False, 'error': str(error)})
                return {'ok': True, 'results': results}
            return {'ok': True, **self._enqueue_one(platform, room, payload.get('comment_payload'))}
        if operation == 'next':
            heartbeat = self.platform_heartbeats.get(platform, {})
            if (not heartbeat.get('live_running') or not heartbeat.get('ai_enabled')
                    or not heartbeat.get('ai_ready') or time.time() - heartbeat.get('received_at', 0) > 30
                    or heartbeat.get('session_id') != room):
                return {'ok': False, 'error': 'delivery_control_inactive'}
            ready = self.comment_queue.ready(platform, room)
            if ready:
                turn_id = ready['result'].get('turn_id', ready['result'].get('reply_id', ''))
                store = self.delivery_store if platform == 'tiktok' else self.shopee_ai().delivery_store
                turn = store.reconcile(turn_id)
                if turn and turn['state'] != 'ready':
                    sent_times = [m['sent_at'] for m in turn['messages'] if m['sent_at'] is not None]
                    # Sending is recovered through message status/claim; a lost
                    # dispatch becomes unknown and must not call the model again.
                    if turn['state'] != 'sending':
                        self.comment_queue.settle(turn_id, turn['state'], first_sent_at=min(sent_times) if sent_times else None)
                    ready = self.comment_queue.ready(platform, room)
                if ready:
                    return {'ok': True, 'available': True, 'recovered': True, **ready}
            work = self.comment_queue.claim_next(platform, room)
            if work is None:
                return {'ok': True, 'available': False}
            try:
                comment = work['payload']
                if platform == 'shopee':
                    comment = {**comment, 'session_id': room}
                    result = self.shopee_ai().reply(comment)
                else:
                    comment = {**comment, 'live_context': {**(comment.get('live_context') or {}), 'room_id': room}}
                    result = self.handle(comment)
                accepted = self.comment_queue.finish_generation(work['queue_id'], work['worker_token'], result)
                if not accepted:
                    return {'ok': True, 'available': False, 'reason': 'queue_invalidated'}
                if time.time() >= work['expires_at']:
                    result = {'ok': True, 'should_send': False, 'reason': 'expired'}
                return {'ok': True, 'available': True, 'queue_id': work['queue_id'],
                        'result': result, 'source_comment': work['payload']}
            except Exception:
                self.comment_queue.finish_generation(work['queue_id'], work['worker_token'],
                    {'ok': False, 'should_send': False, 'reason': 'generation_failed'})
                raise
        raise ValueError('queue_operation_invalid')

    def queued_reply(self, platform, payload):
        blocked = self._maintenance_block()
        if blocked:
            return blocked
        if platform == 'tiktok' and not isinstance(payload.get('live_context'), dict):
            raise ValueError('queue_live_context_invalid')
        room = payload.get('session_id') if platform == 'shopee' else (payload.get('live_context') or {}).get('room_id')
        queued = self.queue_operation(platform, 'enqueue', {'room_id': room, 'comment_payload': payload})
        if queued.get('reason'):
            return {'ok': True, 'should_send': False, 'reason': queued['reason']}
        if queued.get('state') in {'expired', 'cancelled', 'sent', 'unknown'}:
            return {'ok': True, 'should_send': False, 'reason': queued['state']}
        selected = self.queue_operation(platform, 'next', {'room_id': room})
        if not selected.get('ok'):
            return selected
        # Comment không phải đầu hàng: chờ, không nhận nhầm lượt trả lời của comment khác.
        if not selected.get('available') or selected.get('queue_id') != queued.get('queue_id'):
            return {'ok': True, 'should_send': False, 'reason': 'queue_waiting',
                    'queue_id': queued.get('queue_id'), 'state': queued.get('state')}
        return {**selected['result'], 'source_comment': selected['source_comment']}

    def _network_check(self):
        probes = [_net.probe(host) for host in ("api.deepseek.com", "huggingface.co")]
        failed = [row for row in probes if not row.get("ok")]
        proxy = _net.proxy_info()
        lines = []
        for row in probes:
            if row.get("ok"):
                lines.append(f"{row['host']}: DNS + TLS đều qua")
            else:
                lines.append(f"{row['host']}: lỗi ở bước {'DNS' if row.get('step') == 'dns' else 'TLS/kết nối'}"
                             f" — {row.get('message', '')} [{row.get('detail', '')}]")
        lines.append("Proxy: " + (", ".join(f"{k}={v}" for k, v in proxy.items()) if proxy else "không cấu hình"))
        return not failed, "; ".join(lines)

    def diagnostics_state(self):
        from diagnostics import fix, run_setup_checks
        result = run_setup_checks(key_configured=getattr(self.responder, "client", None) is not None)
        checks = list(result.get("checks") or [])

        def add(check_id, label, ok, detail, impact, how):
            row = {"id": check_id, "label": label, "status": "ok" if ok else "warn",
                   "detail": detail, "impact": "" if ok else impact, "fixable": False}
            if not ok:
                row["fix"] = how
            checks.append(row)

        checker = getattr(self.responder, "check_balance", None)
        if callable(checker) and getattr(self.responder, "client", None) is not None:
            balance = checker()
            reason = balance.get("reason", "")
            add("deepseek-balance", "Số dư DeepSeek", bool(balance.get("ok")),
                "Còn tiền, sẵn sàng" if balance.get("ok") else balance.get("error", "Không kiểm tra được"),
                "AI không trả lời được khách",
                fix("Nạp thêm tiền vào tài khoản DeepSeek." if reason == "deepseek_insufficient_balance"
                    else "Kiểm tra mạng Internet của máy, rồi bấm Kiểm tra lại.",
                    "Mở trang nạp tiền" if reason == "deepseek_insufficient_balance" else "",
                    **({"open": "deepseek_topup"} if reason == "deepseek_insufficient_balance" else {})))
        try:
            net_ok, net_detail = self._network_check()
        except Exception as exc:
            net_ok, net_detail = False, f"Không kiểm tra được: {type(exc).__name__}"
        add("network", "Kết nối mạng", net_ok, net_detail,
            "Không kiểm tra được khóa DeepSeek, không tải được model giọng nói",
            fix("Xem dòng lỗi: DNS = mạng/DNS; TLS = diệt virus/proxy quét HTTPS hoặc Windows thiếu chứng chỉ gốc."
                " Mở trình duyệt vào https://api.deepseek.com một lần rồi bấm Kiểm tra lại."))
        plugins = {row.get("id"): row for row in self.plugin_state().get("plugins", [])}
        video_sync = plugins.get("dong-bo-video") or {}
        running = video_sync.get("state") == "dang_chay"
        add("runtime-video-sync", "Dịch vụ đồng bộ video", running,
            "Đang nghe âm thanh" if running else "Đang tắt",
            "Tiện ích không tự ghim sản phẩm theo lịch của video",
            fix("Bật dịch vụ trước khi phát video LIVE.", "Mở Trợ lý tự động", page="automation", sub=""))
        heartbeat = self.extension_heartbeat or {}
        age = time.time() - float(heartbeat.get("received_at") or 0)
        connected = age <= 25
        add("runtime-extension", "Tiện ích Chrome đang kết nối", connected,
            ("Đã kết nối; LIVE đang bật" if heartbeat.get("live_running") else
             "Đã kết nối; LIVE và AI đang tắt") if connected else "Chưa nhận được tín hiệu",
            "Không gửi được câu trả lời và không ghim được sản phẩm",
            fix("Mở Chrome → trang TikTok LIVE Manager. Chưa cài tiện ích thì bấm nút bên cạnh.",
                "Hướng dẫn cài tiện ích", page="system", sub="setup"))
        if running:
            health = self._video_health()
            healthy_audio = (health.get("state") in {"watching", "detected", "running"}
                             and bool(health.get("device_id")))
            add("runtime-audio-signal", "Âm thanh video đang phát", healthy_audio,
                "Đang nghe được âm thanh video" if healthy_audio else "Chưa nghe được",
                "Chưa xác định được video và vị trí đang phát",
                fix("Phát video, rồi vào Âm thanh bấm \"Nghe thử\" để chọn đúng nguồn.",
                    "Mở Âm thanh", page="system", sub="audio"))
        result["checks"] = checks
        result["counts"] = {status: sum(row.get("status") == status for row in checks)
                            for status in ("ok", "warn", "error")}
        result["ok"] = result["counts"]["error"] == 0
        return result

    @staticmethod
    def diagnostics_fix():
        from diagnostics import fix_safe_setup
        return fix_safe_setup()

    def support_report_preview(self):
        from support_report import preview_support_report
        return preview_support_report()

    def support_report_create(self):
        from support_report import create_support_report
        runtime = {"plugins": self.plugin_state(), "safe_mode": self.safe_mode,
                   "started_at": self.started_at}
        return create_support_report(runtime)

    def settings_state(self):
        lister = getattr(self.responder, "list_models", None)
        models, model_error = [], ""
        try:
            models = lister() if callable(lister) else [DEEPSEEK_MODEL]
        except Exception as exc:
            model_error = type(exc).__name__
        devices = []
        try:
            from audio_capture import is_forbidden_device, list_devices
            devices = [{"id": item["id"], "name": item["name"],
                        "loopback": bool(item.get("loopback")),
                        "disabled": is_forbidden_device(item),
                        "reason": "Dock USB bị cấm để tránh lỗi giữa LIVE"
                                  if is_forbidden_device(item) else ""}
                       for item in list_devices()]
        except Exception:
            devices = []
        return {"ok": True, "models": models,
                "selected_model": getattr(self.responder, "model", DEEPSEEK_MODEL),
                "model_labels": dict(getattr(self.responder, "model_labels", None) or {}),
                "model_error": model_error,
                "api_key_configured": getattr(self.responder, "client", None) is not None,
                "tiktok_user_ids": ",".join(sorted(TIKTOK_HOST_USER_IDS)),
                "tiktok_unique_ids": ",".join(sorted(TIKTOK_HOST_UNIQUE_IDS)),
                "audio_devices": devices,
                "selected_audio_device": getattr(self, "audio_device_id", AUDIO_DEVICE_ID),
                "knowledge_source": KNOWLEDGE_PACKAGE_SOURCE,
                "knowledge_active_file": "shared/product_knowledge.json",
                "knowledge_seed": self.knowledge_seed_status(),
                "version": _app_version(), "data_root": str(ROOT)}

    def save_settings(self, payload):
        model = clean(payload.get("model"), 120)
        api_key = str(payload.get("api_key") or "").strip()
        tiktok_user_ids = payload.get("tiktok_user_ids")
        tiktok_unique_ids = payload.get("tiktok_unique_ids")
        knowledge_source = payload.get("knowledge_source")
        audio_device_id = payload.get("audio_device_id")
        setter = getattr(self.responder, "set_model", None)
        configure_key = getattr(self.responder, "configure_api_key", None)
        result = {"ok": True, "restart_required": False}
        if audio_device_id is not None:
            audio_device_id = str(audio_device_id or "").strip()
            devices = self.settings_state().get("audio_devices", [])
            selected = next((item for item in devices if item.get("id") == audio_device_id), None)
            if not selected or selected.get("disabled") or not selected.get("loopback"):
                raise ValueError("audio_device_not_allowed")
            from config_settings import save_audio_device
            previous_device = self.audio_device_id
            previous_env = os.environ.get("KID_TE_AUDIO_DEVICE")
            saved = save_audio_device(audio_device_id)
            self.audio_device_id = audio_device_id
            os.environ["KID_TE_AUDIO_DEVICE"] = audio_device_id
            runtime = getattr(self, "plugin_runtime", None)
            if runtime:
                state = next((item for item in runtime.snapshot().get("plugins", [])
                              if item.get("id") == "dong-bo-video"), {})
                if state.get("state") == "dang_chay":
                    restarted = runtime.restart("dong-bo-video")
                    if not restarted.get("ok"):
                        # Không để một lựa chọn thiết bị lỗi làm dừng bộ đồng bộ
                        # đang chạy. Khôi phục cả file cấu hình, tiến trình và
                        # môi trường của tiến trình cha trước khi báo lỗi cho UI.
                        save_audio_device(previous_device)
                        self.audio_device_id = previous_device
                        if previous_env is None:
                            os.environ.pop("KID_TE_AUDIO_DEVICE", None)
                        else:
                            os.environ["KID_TE_AUDIO_DEVICE"] = previous_env
                        runtime.restart("dong-bo-video")
                        raise ValueError("audio_device_restart_failed")
            result.update(saved)
        if tiktok_user_ids is not None or tiktok_unique_ids is not None:
            from config_settings import save_tiktok_identity
            identity = save_tiktok_identity(tiktok_user_ids or "", tiktok_unique_ids or "")
            result.update(identity)
        if knowledge_source is not None:
            from config_settings import save_knowledge_source
            result.update(save_knowledge_source(knowledge_source))
        if api_key:
            if not callable(configure_key):
                return {"ok": False, "error": "model_provider_not_configurable"}
            try:
                configure_key(api_key)
            except Exception as exc:
                failure = _key_failure(exc)
                if failure is None:
                    raise
                return failure
            from config_settings import save_api_key
            result.update(save_api_key(api_key))
        if not model:
            return result
        if not callable(setter):
            return {"ok": False, "error": "model_provider_not_configurable"}
        selected = setter(model)
        from config_settings import save_model
        result.update(save_model(selected))
        return result

    def test_audio_device(self, payload):
        device_id = str(payload.get("audio_device_id") or "").strip()
        devices = self.settings_state().get("audio_devices", [])
        selected = next((item for item in devices if item.get("id") == device_id), None)
        if not selected or selected.get("disabled") or not selected.get("loopback"):
            raise ValueError("audio_device_not_allowed")
        from audio_capture import RollingCapture
        with RollingCapture(device_id, 1.0) as capture:
            time.sleep(1.25)
            samples = capture.latest()
            error = capture.error
        if error:
            return {"ok": True, "heard": False,
                    "message": "Không nghe được từ thiết bị này. Hãy thử thiết bị khác."}
        if samples is None:
            return {"ok": True, "heard": False,
                    "message": "Chưa nhận được âm thanh. Hãy phát video rồi thử lại."}
        level = float((samples * samples).mean() ** 0.5)
        heard = level >= 0.0001
        return {"ok": True, "heard": heard, "level": round(level, 6),
                "message": ("Đã nghe thấy âm thanh video từ thiết bị này."
                            if heard else
                            "Thiết bị hoạt động nhưng âm thanh đang im lặng.")}

    def open_extension_setup(self, platform="tiktok"):
        if platform not in PLATFORMS:
            raise ValueError("invalid_platform")
        from windows_integration import open_extension_setup
        return open_extension_setup(EXTENSION_DIR if platform == "tiktok" else Path(EXTENSION_DIR).parent / "extension-shopee")

    def _is_live_running(self):
        """Có phiên LIVE thật đang chạy không, theo heartbeat mới nhất của
        tiện ích — dùng chung cho mọi thao tác cấm làm giữa lúc đang bán
        (cập nhật gói kiến thức, cập nhật/rollback plugin)."""
        heartbeats = list(getattr(self, 'platform_heartbeats', {}).values())
        heartbeats.append(getattr(self, 'extension_heartbeat', None) or {})
        for heartbeat in heartbeats:
            if heartbeat.get('live_running') is not True:
                continue
            try:
                age = time.time() - float(heartbeat.get('received_at', 0) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(age) and 0 <= age <= 25:
                return True
        return False

    def update_knowledge_package(self, payload):
        """Nhập gói kiến thức chung. `che_do="xem_truoc"` chỉ xem, không ghi."""
        source = str(payload.get("source") or KNOWLEDGE_PACKAGE_SOURCE).strip()
        if payload.get("seed") is True:
            # Gói đi kèm bộ cài: đường dẫn do server quyết định, không tin client.
            source = str(KNOWLEDGE_SEED_PATH)
        import knowledge_package as goi_kt
        if str(payload.get("che_do") or "").strip() == "xem_truoc":
            return goi_kt.xem_truoc(goi_kt.read_source(source), APPROVED_CATALOG_PATH)
        quyet_dinh = payload.get("quyet_dinh_xung_dot")
        if quyet_dinh is not None and not isinstance(quyet_dinh, dict):
            raise ValueError("quyet_dinh_xung_dot phải là object")
        result = goi_kt.update_from_source(
            source, APPROVED_CATALOG_PATH, live_running=self._is_live_running(),
            quyet_dinh_xung_dot=quyet_dinh)
        self.knowledge.reload()
        return result

    def knowledge_seed_status(self):
        import knowledge_package as goi_kt
        try:
            return goi_kt.seed_status(KNOWLEDGE_SEED_PATH, APPROVED_CATALOG_PATH)
        except Exception as exc:       # trạng thái chỉ để hiển thị, không được làm hỏng Cài đặt
            return {"state": "no_seed", "seed_version": "", "installed_version": "",
                    "error": str(exc)}

    def ensure_seed_knowledge(self):
        """Máy trắng (chưa có shared/product_knowledge.json): cài gói chính thức đi kèm.

        Không ghi đè tệp đã có, không chạy giữa phiên LIVE. Gói đi kèm mới hơn
        gói đang cài chỉ hiện thông báo ở Cài đặt, không tự áp dụng.
        """
        import knowledge_package as goi_kt
        result = goi_kt.seed_first_run(KNOWLEDGE_SEED_PATH, APPROVED_CATALOG_PATH,
                                       live_running=self._is_live_running())
        if result.get("status") == "applied":
            self.knowledge.reload()
        return result

    def undo_knowledge_package(self, _payload=None):
        import knowledge_package as goi_kt
        if self._is_live_running():
            raise ValueError("không hoàn tác gói kiến thức trong phiên LIVE")
        result = goi_kt.hoan_tac(APPROVED_CATALOG_PATH)
        self.knowledge.reload()
        return result

    def export_knowledge_package(self, payload):
        import knowledge_package as goi_kt
        version = str((payload or {}).get("version") or "").strip()
        if not version:
            raise ValueError("thiếu version cho gói kiến thức")
        goi = goi_kt.xuat_goi(APPROVED_CATALOG_PATH, version, (payload or {}).get("publisher", ""))
        # Ghi ra file để mang sang máy khác (ô "Nguồn gói" ở máy kia đọc file này).
        safe = re.sub(r"[^0-9A-Za-z._-]+", "-", version).strip("-") or "goi"
        out_dir = Path(ROOT) / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"goi-kien-thuc-{safe}.json"
        atomic_json(path, goi)
        return {"ok": True, "path": str(path), "products": sum(
            len(n.get("san_pham") or []) for n in (goi.get("content") or {}).get("nhom_san_pham") or [])}

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
        sources = {}
        try:
            raw = json.loads(Path(LIVE_RAW_CATALOG_PATH).read_text(encoding="utf-8"))
            for product in ((raw.get("raw_api_data") or {}).get("products") or []):
                cover = product.get("cover") or {}
                urls = cover.get("url_list") or cover.get("thumb_url_list") or []
                if isinstance(urls, str):
                    urls = [urls]
                pid = str(product.get("product_id") or "")
                if pid and urls:
                    sources.setdefault(pid, []).extend(str(u) for u in urls[:2])
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
        if not pid or not str(record.get("featured_image_url") or "").startswith(("http://", "https://")):
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
        fetched = self.product_images.fetch(
            product_id, self._product_image_sources().get(product_id, []))
        if not fetched:
            raise ValueError("product_image_not_found")
        return fetched

    def video_catalog(self):
        """Danh mục cho Studio video: một chỗ có đủ ảnh, giá, giỏ, hồ sơ."""
        live = self.catalog.snapshot or {}
        by_id = {clean(item.get("product_id"), 160): item for item in live.get("products") or []}
        mapping = self.mapping_state()
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
                "synced_at": mapping.get("synced_at", ""), "products": products}

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

    def sync_catalog(self, snapshot):
        saved = self.catalog.sync(snapshot)
        self.workspace._touch("products", "catalog", "dashboard")
        return {"ok": True, "product_count": saved["product_count"], "synced_at": saved["synced_at"]}

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
            missing.append("san_pham_phu_hop")
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

    def _reply_context(self, comment, live, sel, memory_context, *, video=None, mark_used=True,
                       history=None, memory_prompt=None):
        """Ngữ cảnh gửi model. Trả (context, price_signal)."""
        learning = getattr(self, "learning", None)
        product, approved, intents = sel["product"], sel["approved"], sel["intents"]
        recommendations = sel["recommendations"]
        shop_id = (product or {}).get("product_id", "")
        question_type = self.workspace._question_type(comment)
        persona = learning.persona() if learning is not None else None
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
                shown_approved = context_budget.project_profile(approved, shown_intents)
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
                           reading=None):
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
        if len(comment) < 2 or not (user_id or sec_uid or unique_id):
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
                "reply": "Dạ, em đã xóa dữ liệu ghi nhớ của mình rồi ạ.",
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
        comment, pending_question = resolve_question(comment, turns)
        if pending_question:
            self.memory.note_resolved_question(ref.get('interaction_id', ''), comment)
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
                clarified = self._tiktok_clarify_sku(payload, ref, live, turns, comment, original_comment,
                    nickname, unique_id, received_at, recompose,
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
                    card=card, reading=router.reading(routed.get("result")))
                price_signal = {}
            except Exception:
                logging.getLogger("kid-te.core").warning("smalltalk context failed", exc_info=True)
                smalltalk_lane, context = False, None
        if context is None:
            context, price_signal = self._reply_context(
                comment, live, sel, memory_context, video=payload.get("video_context"), mark_used=True,
                history=history,
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
        started = time.monotonic()
        if promotion_clarification:
            context['promotion_status'] = 'Chưa có chương trình được duyệt cho SKU này. Ghi nhận đúng sản phẩm/số lượng khách đã nói, nói cần kiểm tra quà tặng; không hỏi lại loại hoặc số lượng đã rõ, không chuyển sang công dụng/giá, không bịa quà.'
        try:
            dose_gate = self._tiktok_dose_gate(nickname, comment, context, sel, intents, max_body)
        except Exception:
            logging.getLogger('kid-te.core').warning('tiktok dose gate failed', exc_info=True)
            dose_gate = None
        if dose_gate and dose_gate.get('handoff'):
            result = {'action': 'reply', 'reply': dose_selection.DOSE_HANDOFF, 'metrics': {}}
        else:
            result = self.responder.reply(nickname, comment, context, max_body)
            if dose_gate and dose_gate.get('rows') and result.get('action') == 'reply':
                if not dose_selection.reply_dose_numbers_ok(clean(result.get('reply'), 2000), dose_gate['rows']):
                    result = self.responder.reply(nickname, comment, context, max_body,
                        correction='Chỉ được nêu liều đúng bằng verified_dose_calculations; không tự tính hay nêu số liều khác.')
                    if (result.get('action') != 'reply'
                            or not dose_selection.reply_dose_numbers_ok(clean(result.get('reply'), 2000), dose_gate['rows'])):
                        result = {'action': 'reply', 'reply': dose_selection.DOSE_HANDOFF, 'metrics': {}}
        correction = (self._policy_violation(comment, result, price_signal)
                      or self._invented_price(result, live, sel["approved"], price_signal))
        if dose_gate and dose_gate.get('handoff'):
            correction = ''
        retried = False
        if correction:
            # Chỉ thử lại một lần, và chỉ ở những lượt hiếm/đắt giá này. Thà
            # tốn thêm một lượt gọi còn hơn bác con số khách đang nhìn thấy.
            retried = True
            drafted = result
            result = self.responder.reply(
                nickname, comment, context, max_body, correction=correction,
            )
            if result.get("action") != "reply":
                result = drafted
            if (self._policy_violation(comment, result, price_signal)
                    or self._invented_price(result, live, sel["approved"], price_signal)):
                logging.getLogger("kid-te.core").warning(
                    "TikTok reply advisory interaction_id=%s", ref.get('interaction_id', ''),
                    extra={"event":"ai_reply_advisory"})
        metrics = dict(result.get("metrics") or {})
        metrics["total_ms"] = int((time.monotonic() - started) * 1000)
        metrics["intents"] = sorted(name for name, on in intents.items() if on)
        metrics["retried"] = retried
        metrics["knowledge_matched"] = bool(record)
        metrics["learned_items"] = len(context.get("kien_thuc_da_hoc") or [])
        metrics["learned_product"] = bool(learned_product)
        if smalltalk_lane:
            metrics["context_lane"] = "smalltalk"
        if card:
            metrics["customer_tier"] = card.get("quen_la", "")
        if context.get("tinh_huong_phu_hop"):
            metrics["playbook_hint"] = list(context["tinh_huong_phu_hop"])
        if (routed.get("topics") or router.reading(routed.get("result"))) and router_metrics:
            router_metrics["router_used"] = True
        metrics.update(router_metrics)
        if product_ref_note is not None:
            metrics["product_ref"] = product_ref_note
        metrics["product_id"] = clean((product or {}).get("product_id"), 160)
        metrics["product_is_pinned"] = (
            metrics["product_id"] == clean(live.get("pin_id"), 160)
        )
        if result.get("action") != "reply":
            reasons = result.get("missing_fields") or ["model_skipped"]
            if any(str(r).startswith(("deepseek_", "model_", "empty_model")) for r in reasons):
                report_problem("ai_reply_failed", f"AI không trả lời được: {', '.join(reasons)}",
                               severity="warning")
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
        reply = result["reply"]
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

    def _tiktok_dose_gate(self, nickname, comment, context, sel, intents, max_body):
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

        def call(ask, correction):
            kwargs = {"system_override": system}
            if correction:
                kwargs["correction"] = correction
            return self.responder.reply(nickname, comment, ask, max_body, **kwargs)
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
                if isinstance(result, dict) and result.get("action") == "reply":
                    body = clean(result.get("reply"), 2000)
                    if (body and utf16_length(f"{tag} {body}") <= limit
                            and not _clarify_price_violation(self, body, {}, {})):
                        text = body
            except Exception:
                logging.getLogger("kid-te.core").warning("tiktok clarify model failed", exc_info=True)
            text = text or dose_selection.clarify_listing([p.get("title") for p in top], tag_len, template, limit)
            new_counts = {**counts, family_id: count + 1}
        if utf16_length(f"{tag} {text}") > limit:
            text = template
        reply_id = uuid.uuid4().hex
        with self._lock:
            self._pending_reply_bodies[reply_id] = text
        metrics = {"reply_source": "clarification", "clarify_count": new_counts.get(family_id, 0),
                   **(extra_metrics or {})}
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
            snapshot = self.catalog.snapshot or {}
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
            return learner.apply_relearn(clean(payload.get("run_id"), 40),
                                         include_styles=payload.get("include_styles", True) is not False)
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
        if action == "settings":
            return {"ok": True, "settings": learning.save_settings(payload)}
        if action == "try":
            return self.learning_try(payload)
        raise ValueError("learning_action_unknown")

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
        context, _signal = self._reply_context(comment, live, sel, "", mark_used=False)
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
        correction = (self._policy_violation(comment, result, _signal)
                      or self._invented_price(result, live, sel["approved"], _signal))
        if correction:
            drafted = result
            result = self.responder.reply("Khách", comment, context, max_body, correction=correction,
                                          system_override=try_system)
            if result.get("action") != "reply":
                result = drafted
            if (self._policy_violation(comment, result, _signal)
                    or self._invented_price(result, live, sel["approved"], _signal)):
                logging.getLogger("kid-te.core").warning(
                    "TikTok preview reply advisory", extra={"event":"ai_reply_advisory"})
        metrics = dict(result.get("metrics") or {})
        metrics["policy_retry"] = bool(correction)
        metrics["total_ms"] = int((time.monotonic() - started) * 1000)
        return {"ok": True, "reply": result.get("reply", "") if result.get("action") == "reply" else "",
                "reason": "" if result.get("action") == "reply" else "model_skipped",
                "missing_fields": result.get("missing_fields") or [], "metrics": metrics, "explain": explain}

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

    # "555k", "1.110k", "640.000đ", "1,2 triệu" — số tiền trong câu trả lời.
    _PRICE_TOKEN = re.compile(
        r"(?<![\d.,])(\d{1,3}(?:[.,]\d{3})+|\d{2,4})(?:\s?(k|nghin|ngan)(?![a-z])|\s?(d|vnd|dong)(?![a-z])"
        r"|(?=[.,]?\s|$))|(?<![\d.,])(\d{1,3}(?:[.,]\d)?)\s?(tr|trieu)(?![a-z])")

    @classmethod
    def _price_values(cls, text):
        values = set()
        folded = fold(text).replace("₫", "d")
        for m in cls._PRICE_TOKEN.finditer(folded):
            if m.group(4):
                values.add(int(round(float(m.group(4).replace(",", ".")) * 1_000_000)))
                continue
            digits, k, dong = m.group(1), m.group(2), m.group(3)
            has_sep = bool(re.search(r"[.,]\d{3}", digits))
            if not (k or dong or has_sep):
                continue                    # số trần (vd "20", "150") không phải giá
            number = int(re.sub(r"[.,]", "", digits))
            values.add(number * 1000 if k else number)
        return {v for v in values if v >= 10_000}

    def _invented_price(self, result, live, approved, price_signal):
        """Câu trả lời có số tiền KHÔNG phải giá của sản phẩm nào → tự tính/bịa.

        Thấy thật 23/09: "cho 2 can đạm cá 5 lít" → "hết 1.110k". Giá sau mã theo
        tài khoản khác nhau nên tổng tự nhân là sai trước mặt khách.
        """
        if result.get("action") != "reply" or not _price_processor.SETTINGS["chan_tu_tinh_gia"]:
            return ""
        said = self._price_values(result.get("reply") or "")
        if not said:
            return ""
        allowed = set()
        for item in live.get("products") or []:
            for key in ("original_price", "price"):
                allowed |= self._price_values(clean((item or {}).get(key), 80) + " d")
        allowed |= self._price_values(clean((approved or {}).get("gia_goc_duyet"), 80) + " d")
        allowed |= self._price_values(json.dumps(price_signal or {}, ensure_ascii=False))
        extra = sorted(said - allowed)
        if not extra:
            return ""
        return (f"Câu vừa soạn có số tiền {extra[0]:,}đ không phải giá gốc của sản phẩm nào (có vẻ tự "
                "nhân/cộng hoặc tự đoán). Chỉ nói đúng giá gốc của MỘT sản phẩm trong price.quotable_price; "
                "khách mua nhiều thì mời chọn số lượng trong giỏ, TikTok tự tính tổng theo mã của họ.").replace(",", ".")

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

    # ================================================================ /api/system/*
    def _system_jobs(self):
        with self._lock:
            store = getattr(self, '_job_store', None)
            if store is None:
                from maintenance import JobStore
                store = self._job_store = JobStore(DATA_DIR / 'system_jobs.sqlite3')
            return store

    def _backup_v2_service(self):
        from backup_service import BackupService
        from captcha_secret import _protect
        from config import SYSTEM_PROMPT_OVERRIDE_PATH
        def references():
            rows = []
            for path in sorted((ROOT / 'data' / 'videos').glob('*/timeline.json')):
                from backup import _strict_json_loads, _reject_links
                _reject_links(path, Path(path.anchor))
                timeline = _strict_json_loads(path.read_text(encoding='utf-8'))
                if not isinstance(timeline, dict):
                    raise ValueError('backup_video_timeline_invalid')
                if not timeline.get('source_file'):
                    continue
                video_id = path.parent.name
                try:
                    source = self.video_media_path(video_id)
                except ValueError:
                    # Preserve the expected missing source explicitly; do not
                    # silently call a backup complete after losing a video.
                    source = Path.home() / 'Downloads' / Path(str(timeline['source_file'])).name
                rows.append({'path': source, 'video_id': video_id,
                             'sha256': timeline.get('source_sha256')})
            return rows
        # Installed solver hash resolution remains a release-manifest task.
        # An empty hash is reported missing instead of claiming it was checked.
        additional = {}
        for relative, source in (('external-settings/processor-settings.json', PROCESSOR_SETTINGS_PATH),
                                 ('external-settings/system-prompt.txt', SYSTEM_PROMPT_OVERRIDE_PATH)):
            source = Path(source).absolute()
            if not source.is_relative_to(Path(ROOT).absolute()) and source.is_file():
                additional[relative] = source
        return BackupService(ROOT, app_version=_app_version(), schema_version=2,
            solver_hash='', reference_provider=references, privacy_db=getattr(self.memory, 'db_path', None),
            dpapi_decrypt=lambda data: _protect(data, decrypt=True), additional_sources=additional)

    def backup_v2_list(self):
        from backup_v2 import BackupV2
        store = BackupV2(ROOT / 'backups' / 'v2')
        items = []
        for path in store.manifests.glob('*.json'):
            try:
                # Listing a backup must never read its videos/models. Checksum
                # verification runs on explicit integrity/delete/restore paths.
                manifest = store._load(path.stem)
                verified = False
            except Exception:
                try:
                    manifest, verified = store._load(path.stem), False
                except Exception:
                    continue
            if not isinstance(manifest, dict) or manifest.get('scope') not in ('full', 'partial'):
                continue
            files = manifest.get('files') if isinstance(manifest.get('files'), list) else []
            items.append({'id': path.stem, 'kind': manifest['scope'],
                          'created_at': manifest.get('created_at', ''),
                          'size': sum(f.get('bytes', 0) for f in files if isinstance(f, dict)
                                      and isinstance(f.get('bytes'), int)),
                          'verified': verified})
        items.sort(key=lambda item: item['created_at'], reverse=True)
        return {'ok': True, 'items': items}

    def backup_v2_start(self, payload):
        from backup_crypto import _password_bytes
        password = payload.get('password')
        _password_bytes(password)
        full = payload.get('full', True)
        if not isinstance(full, bool):
            raise ValueError('backup_full_invalid')
        result = ReplyEngine._guarded_v2_create(self, password, full)
        return {'ok': True, 'job_id': result['job_id']}

    def _guarded_v2_create(self, password, full=True):
        _V2_WRITERS.begin_create()
        def run(progress):
            try:
                return self._backup_v2_service().create(password, progress=progress, full=full)
            finally:
                _V2_WRITERS.end_create()
        try:
            return self._system_jobs().start('backup_v2', run)
        except BaseException:
            _V2_WRITERS.end_create()
            raise

    @staticmethod
    def _v2_delete_ids(store, payload):
        if payload.get('all') is True:
            if payload.get('ids') is not None:
                raise ValueError('backup_ids_invalid')
            return sorted(path.stem for path in store.manifests.glob('*.json'))
        return payload.get('ids')

    def backup_v2_delete_preview(self, payload):
        from backup import BackupError
        from backup_v2 import BackupV2
        store = BackupV2(ROOT / 'backups' / 'v2')
        _V2_WRITERS.begin_delete()
        try:
            ids = self._v2_delete_ids(store, payload)
            preview = store.preview_delete(ids)
        except BackupError as exc:
            raise ValueError(str(exc)) from None
        finally:
            _V2_WRITERS.end_delete()
        return {'ok': True, 'token': preview['preview_token'],
                'reclaim_bytes': preview['reclaimable_bytes'], 'ids': preview['ids']}

    def backup_v2_delete(self, payload):
        from backup import BackupError
        from backup_v2 import BackupV2
        token = payload.get('token')
        if not isinstance(token, str) or not token:
            raise ValueError('backup_preview_changed')
        store = BackupV2(ROOT / 'backups' / 'v2')
        _V2_WRITERS.begin_delete()
        try:
            ids = self._v2_delete_ids(store, payload)
            return store.delete(ids, preview_token=token)
        except BackupError as exc:
            raise ValueError(str(exc)) from None
        finally:
            _V2_WRITERS.end_delete()

    def backup_indexeddb_save(self, payload):
        import json as _json
        from datetime import datetime, timezone
        data = payload.get('export', payload)
        if not isinstance(data, (dict, list)):
            raise ValueError('indexeddb_export_invalid')
        text = _json.dumps(data, ensure_ascii=False, allow_nan=False)
        if len(text.encode('utf-8')) > 6_000_000:
            raise ValueError('indexeddb_export_too_large')
        folder = ROOT / 'backups' / 'indexeddb'
        folder.mkdir(parents=True, exist_ok=True)
        name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json'
        temporary = folder / (name + '.tmp')
        temporary.write_text(text, encoding='utf-8')
        os.replace(temporary, folder / name)
        for old in sorted(folder.glob('*.json'))[:-5]:
            old.unlink(missing_ok=True)
        return {'ok': True, 'saved': name, 'bytes': len(text.encode('utf-8'))}

    def system_get(self, path, query):
        """Trang Trợ lý tự động và Cài đặt: chi tiết bộ xử lý, model chép lời, sao lưu."""
        parts = [part for part in path.split("/") if part]
        if len(parts) == 2 and parts[0] == 'jobs':
            return {'ok': True, **self._system_jobs().get(parts[1])}
        if len(parts) == 2 and parts[0] == "processors":
            return self.processors.detail(parts[1])
        if parts == ["whisper"]:
            import whisper_models
            return whisper_models.state(self._whisper_downloader())
        if parts == ["backup-parts"]:
            from backup_policy import parts_catalog
            return {"ok": True, "parts": parts_catalog()}
        if len(parts) == 2 and parts[0] == "backups":
            from backup_policy import backup_detail
            return backup_detail(parts[1])
        if parts == ["shop-info"]:
            return {"ok": True, "fields": SHOP_INFO_FIELDS, "values": load_shop_info()}
        if parts == ["documents"]:
            from documents import CATEGORIES
            return {"ok": True, "documents": self.documents.list(), "stats": self.documents.stats(),
                    "categories": CATEGORIES, "ai_ready": getattr(self.responder, "client", None) is not None}
        if len(parts) == 2 and parts[0] == "documents":
            return {"ok": True, "document": self.documents.detail(parts[1])}
        if parts == ["customer-history"]:
            # Đúng phần lịch sử AI sẽ đọc khi khách này bình luận ở phiên LIVE mới.
            live = dict(self.catalog.snapshot or {})
            live["room_id"] = ""
            return {"ok": True, "history": self._customer_history(clean(query.get("key"), 300), live, "", "")}
        if parts == ["version"]:
            from version import app_version
            return {"ok": True, "version": app_version()}
        raise ValueError("not_found")

    def _whisper_downloader(self):
        downloader = getattr(self, "_whisper_download", None)
        if downloader is None:
            from whisper_models import ModelDownloader
            downloader = self._whisper_download = ModelDownloader()
        return downloader

    def system_post(self, path, payload):
        parts = [part for part in path.split("/") if part]
        if parts == ['backup', 'v2', 'start']:
            from backup_crypto import _password_bytes
            password = payload.get('password')
            _password_bytes(password)
            # Password is captured only in the job closure, never persisted in
            # the job database, manifest, progress or result.
            return ReplyEngine._guarded_v2_create(self, password)
        if parts == ['backups', 'delete', 'preview']:
            from backup_delete import preview
            ids = payload.get('ids')
            if payload.get('all') is True:
                if ids is not None:
                    raise ValueError('backup_ids_invalid')
                ids = [path.name for path in BACKUP_DIR.iterdir()
                       if path.is_dir() and not path.name.startswith('.') and (path / 'manifest.json').is_file()]
            return preview(BACKUP_DIR, ids)
        if parts == ['backups', 'delete', 'recover']:
            from backup_delete import recover
            return self._system_jobs().start('backup_delete_recover', lambda progress: recover(BACKUP_DIR, progress))
        if parts == ['backups', 'delete']:
            from backup_delete import preview, execute
            checked = preview(BACKUP_DIR, payload.get('ids'))
            if payload.get('preview_token') != checked['preview_token']:
                raise ValueError('backup_preview_changed')
            return self._system_jobs().start('backup_delete', lambda progress:
                execute(BACKUP_DIR, checked['ids'], checked['preview_token'], progress))
        if parts == ['backup', 'start']:
            requested_parts = self._backup_parts(payload)
            return self._system_jobs().start('backup', lambda progress:
                self.backup_now_and_refresh({'parts': requested_parts}))
        if len(parts) == 3 and parts[0] == "processors":
            plugin_id, action = parts[1], parts[2]
            if action == "settings":
                return self.processors.save(plugin_id, payload.get("values") or {})
            if action == "reset":
                return self.processors.reset(plugin_id)
            if action == "try":
                return self.processors.try_it(plugin_id, payload)
            if action == "notes" and plugin_id == "model-deepseek":
                return self.processors.save_notes(payload.get("text"), bool(payload.get("archive_legacy")))
            if action == "legacy-dismiss" and plugin_id == "model-deepseek":
                return self.processors.dismiss_legacy_prompt()
        if len(parts) == 2 and parts[0] == "whisper":
            import whisper_models
            downloader = self._whisper_downloader()
            action, model_id = parts[1], clean(payload.get("model"), 40)
            if action == "download":
                downloader.start(model_id)
            elif action == "cancel":
                downloader.cancel()
            elif action == "select":
                whisper_models.select(model_id, clean(payload.get("custom_path"), 400))
            elif action == "delete":
                if (downloader.status().get("state") == "downloading"
                        and downloader.status().get("model") == model_id):
                    raise ValueError("download_in_progress")
                whisper_models.delete(model_id)
            elif action == "refresh":
                whisper_models.hardware(refresh=True)
            else:
                raise ValueError("not_found")
            return whisper_models.state(downloader)
        if parts == ["backup"]:
            return self.backup_now_and_refresh(payload)
        if len(parts) == 3 and parts[0] == "backups" and parts[2] == "restore":
            return self.restore_backup({**payload, "backup_id": parts[1]})
        if parts == ["open"]:
            return self.open_target(clean(payload.get("target"), 40))
        if parts == ["shop-info"]:
            return self.save_shop_info(payload.get("values") or {})
        if parts == ["documents", "search"]:
            return {"ok": True, "results": self.documents.search(clean(payload.get("comment"), 500))}
        if len(parts) == 3 and parts[0] == "documents":
            doc_id, action = parts[1], parts[2]
            if action == "update":
                return {"ok": True, "document": self.documents.update(doc_id, payload)}
            if action == "delete":
                return self.documents.delete(doc_id)
            if action == "distill":
                return {"ok": True, "job": self.documents.distill(doc_id, self.learner,
                                                                    self.learner._catalog_brief)}
        if parts == ["profiles", "create"]:
            return self.workspace.create_profile(payload)
        raise ValueError("not_found")

    def sales_playbook_state(self):
        return {"ok": True, "entries": sales_playbook.load(), "max_entries": sales_playbook.MAX_ENTRIES,
                "max_bytes": sales_playbook.MAX_BYTES}

    def sales_playbook_save(self, payload):
        """Ghi sổ tay tình huống (validate + nguyên tử). Sai dạng => ValueError (400)."""
        entries = sales_playbook.save(payload)
        return {"ok": True, "entries": entries, "max_entries": sales_playbook.MAX_ENTRIES,
                "max_bytes": sales_playbook.MAX_BYTES}

    def save_shop_info(self, values):
        clean_values = {key: clean(values.get(key), 1200) for key, _label, _hint in SHOP_INFO_FIELDS
                        if clean(values.get(key), 1200)}
        atomic_json(SHOP_INFO_PATH, clean_values)
        self.shop_info = load_shop_info()
        return {"ok": True, "fields": SHOP_INFO_FIELDS, "values": self.shop_info}

    # Chỉ mở những chỗ biết trước — không bao giờ mở đường dẫn/URL do trang gửi lên.
    OPEN_TARGETS = {
        "webview2": "https://developer.microsoft.com/microsoft-edge/webview2/#download",
        "deepseek_keys": "https://platform.deepseek.com/api_keys",
        "deepseek_topup": "https://platform.deepseek.com/top_up",
        "chrome_extensions": "chrome://extensions",
        "ffmpeg": "https://www.gyan.dev/ffmpeg/builds/",
        "sadcaptcha_dashboard": "https://www.sadcaptcha.com/dashboard",
        "telegram_botfather": "https://t.me/BotFather",
        "telegram_userinfobot": "https://t.me/userinfobot",
    }

    def open_target(self, target):
        from config import BACKUP_DIR, DATA_DIR, WHISPER_MODELS_DIR
        folders = {"data": DATA_DIR.parent, "backups": BACKUP_DIR, "logs": LOG_DIR,
                   "models": WHISPER_MODELS_DIR, "raw_stream": LOG_DIR / "raw_stream"}
        if target in folders:
            folder = Path(folders[target])
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))  # noqa: S606 — thư mục cố định của chính ứng dụng
            return {"ok": True, "opened": str(folder)}
        url = self.OPEN_TARGETS.get(target)
        if not url:
            raise ValueError("open_target_unknown")
        import webbrowser
        webbrowser.open(url)
        return {"ok": True, "opened": url}


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


class Handler(BaseHTTPRequestHandler):
    engine = None
    server_version = "KidTEAI/3.4"

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

    def send_html(self, status, body):
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
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
        self.end_headers()
        self.wfile.write(raw)

    def send_cacheable_file(self, path, content_type):
        """Tài nguyên dẫn xuất bất biến theo khoá: cho trình duyệt giữ lại."""
        raw = Path(path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "private, max-age=86400")
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
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    # Trình phát thường hủy range cũ ngay khi người dùng tua.
                    # Đây là hành vi bình thường, không phải lỗi backend.
                    break
                remaining -= len(chunk)

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
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
        if route == "/api/update-ready":
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            self.send_json(200, {"ok": True, "pid": os.getpid(),
                                 "launcher_nonce": os.environ.get("KIDTE_LAUNCH_NONCE", ""),
                                 "launcher_generation": os.environ.get("KIDTE_LAUNCH_GENERATION", ""),
                                 "version": _app_version(), "data_root": str(ROOT)})
        elif route == "/api/updates":
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            manager = getattr(self.engine, "updates", None)
            self.send_json(200, manager.state() if manager is not None else
                           {"ok": True, "enabled": False, "available": [], "installed": {},
                            "error": "update_source_mode_disabled"})
        elif route == "/health":
            self.send_json(200, self.engine.health())
        elif route in ("/", "/app", "/app/"):
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            page = WEB_DIR / "operator.html"
            try:
                self.send_html(200, versioned_operator_page(page.read_text(encoding="utf-8"), _app_version()))
            except OSError:
                self.send_json(500, {"ok": False, "error": "app_page_missing"})
        elif route.startswith("/assets/"):
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            relative = route[len("/assets/"):].lstrip("/")
            try:
                target = (WEB_DIR / relative).resolve()
                if not target.is_relative_to(WEB_DIR.resolve()) or not target.is_file():
                    self.send_json(404, {"ok": False, "error": "not_found"})
                    return
                mime = (
                    "application/javascript" if target.suffix == ".js"
                    else "text/css" if target.suffix == ".css"
                    else "text/html" if target.suffix == ".html"
                    else "image/png" if target.suffix == ".png"
                    else "image/svg+xml" if target.suffix == ".svg"
                    else "application/octet-stream"
                )
                self.send_local_file(target, mime)
            except (ValueError, OSError):
                self.send_json(404, {"ok": False, "error": "not_found"})
        elif route.startswith("/api/learning"):
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            try:
                if route == "/api/learning":
                    self.send_json(200, self.engine.learning_state())
                elif route == "/api/learning/runs":
                    ref = (parse_qs(parsed.query).get("ref") or [""])[0]
                    self.send_json(200, {"ok": True, "runs": [
                        self.engine.learning_run(r["run_id"])["run"]
                        for r in self.engine.learning.runs("distill", limit=5, ref_id=ref)] if ref else []})
                elif route.startswith("/api/learning/runs/"):
                    self.send_json(200, self.engine.learning_run(unquote(route.rsplit("/", 1)[-1])))
                else:
                    self.send_json(404, {"ok": False, "error": "not_found"})
            except ValueError as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
        elif route == "/api/backup/v2/list":
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            try:
                self.send_json(200, self.engine.backup_v2_list())
            except Exception as exc:
                report_problem("api_error", f"GET {route}", exc)
                self.send_json(500, {"ok": False, "error": "internal_error"})
        elif route.startswith("/api/system/"):
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            try:
                self.send_json(200, self.engine.system_get(
                    route[len("/api/system/"):], {k: v[0] for k, v in parse_qs(parsed.query).items()}))
            except ValueError as exc:
                self.send_json(409 if str(exc) in {'maintenance_required', 'maintenance_job_busy', 'backup_preview_changed'}
                               else 404 if str(exc).endswith("not_found") else 400,
                               {"ok": False, "error": str(exc)})
            except Exception as exc:
                report_problem("api_error", f"GET {route}", exc)
                self.send_json(500, {"ok": False, "error": "internal_error"})
        elif route == "/api/trash":
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            self.send_json(200, self.engine.workspace_trash({
                key: values[0] for key, values in parse_qs(parsed.query).items()
            }))
        elif route == "/api/workspace/sales-playbook":
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            self.send_json(200, self.engine.sales_playbook_state())
        elif route == "/api/workspace/dose-rules":
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            import dose_rules
            with self.engine.workspace._db() as db:
                self.send_json(200, dose_rules.list_rules(
                    db, (parse_qs(parsed.query).get("sku") or [""])[0] or None))
        elif route.startswith("/api/workspace/"):
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            parts = [unquote(part) for part in route.split("/")[3:] if part]
            try:
                if len(parts) == 1:
                    result = self.engine.workspace_list(parts[0], {
                        key: values[0] for key, values in parse_qs(parsed.query).items()
                    })
                elif len(parts) == 2:
                    result = self.engine.workspace_detail(parts[0], parts[1])
                else:
                    raise ValueError("workspace_route_not_found")
                self.send_json(200, result)
            except ValueError as exc:
                self.send_json(404 if "not_found" in str(exc) else 400,
                               {"ok": False, "error": str(exc)})
        elif route.startswith("/api/privacy/requests/"):
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            try:
                self.send_json(200, self.engine.privacy_request(unquote(route.rsplit("/", 1)[1])))
            except ValueError as exc:
                self.send_json(404, {"ok": False, "error": str(exc)})
        elif route == "/api/dashboard":
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            self.send_json(200, self.engine.dashboard_state())
        elif route == "/api/captcha":
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            self.send_json(200, self.engine.captcha.status())
        elif self.path == "/api/support/preview":
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            self.send_json(200, self.engine.support_report_preview())
        elif self.path == "/api/settings":
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            self.send_json(200, self.engine.settings_state())
        elif route.startswith("/api/logs"):
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            from log_viewer import recent_logs
            query = parse_qs(parsed.query)
            self.send_json(200, recent_logs(
                LOG_DIR, component=(query.get("component") or [""])[0],
                severity=(query.get("severity") or [""])[0],
            ))
        elif route.startswith("/api/product/image/"):
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            try:
                product_id = unquote(route.rsplit("/", 1)[-1])
                path = self.engine.workspace.product_image_path(product_id)
                mime = "image/png" if path.suffix.lower() == ".png" else "image/webp" if path.suffix.lower() == ".webp" else "image/jpeg"
                self.send_local_file(path, mime)
            except ValueError as exc:
                self.send_json(404, {"ok": False, "error": str(exc)})
        elif route.startswith("/api/video/media/"):
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            try:
                self.send_video(self.engine.video_media_path(unquote(route.rsplit("/", 1)[-1])))
            except ValueError as exc:
                self.send_json(404, {"ok": False, "error": str(exc)})
        elif route.startswith("/api/video/job-media/"):
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            try:
                self.send_video(self.engine.video_job_media_path(unquote(route.rsplit("/", 1)[-1])))
            except ValueError as exc:
                self.send_json(404, {"ok": False, "error": str(exc)})
        elif route == "/api/video/catalog":
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            self.send_json(200, self.engine.video_catalog())
        elif route == "/api/video/assets":
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            query = parse_qs(parsed.query)
            self.send_json(200, self.engine.video_assets_state(
                (query.get("video_id") or [""])[0], (query.get("job_id") or [""])[0]))
        elif route.startswith("/api/video/asset/"):
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            parts = [unquote(part) for part in route.split("/")[4:] if part]
            try:
                if len(parts) != 2:
                    raise ValueError("asset_not_found")
                path = self.engine.video_assets.file(parts[0], parts[1])
                mime = "application/json" if path.suffix == ".json" else "image/jpeg"
                self.send_cacheable_file(path, mime)
            except ValueError as exc:
                self.send_json(404, {"ok": False, "error": str(exc)})
        elif route.startswith("/api/profile/thumb/"):
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            try:
                path = self.engine.profile_thumb_path(unquote(route.rsplit("/", 1)[-1]))
                self.send_cacheable_file(path, "image/jpeg")
            except ValueError as exc:
                self.send_json(404, {"ok": False, "error": str(exc)})
        elif route.startswith("/api/product/thumb/"):
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            try:
                path = self.engine.product_thumb_path(unquote(route.rsplit("/", 1)[-1]))
                suffix = path.suffix.lower()
                mime = ("image/webp" if suffix == ".webp" else
                        "image/png" if suffix == ".png" else "image/jpeg")
                self.send_cacheable_file(path, mime)
            except ValueError as exc:
                self.send_json(404, {"ok": False, "error": str(exc)})
        elif self.path == "/api/mapping":
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            self.send_json(200, self.engine.mapping_state())
        elif route == "/api/video/position":
            platform = (parse_qs(parsed.query).get("platform") or ["tiktok"])[0]
            if platform not in PLATFORMS:
                self.send_json(400, {"ok": False, "error": "invalid_platform"})
            else:
                self.send_json(200, self.engine.current_video_position(platform))
        elif route == "/api/live/sources":
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            self.send_json(200, self.engine.live_sources_state())
        elif self.path == "/api/video/timelines":
            self.send_json(200, self.engine.video_timelines())
        elif self.path == "/api/shopee/mappings":
            if not self.local_tool_request() and not self.headers.get("Origin", "").startswith("chrome-extension://"):
                self.send_json(403, {"ok": False, "error": "extension_origin_required"})
                return
            self.send_json(200, self.engine.shopee_mapping_state())
        elif self.path == "/api/shopee/ai/status":
            if not self.local_tool_request() and not self.headers.get("Origin", "").startswith("chrome-extension://"):
                self.send_json(403, {"ok": False, "error": "extension_origin_required"})
                return
            checker = getattr(self.engine.responder, "check_balance", None)
            status = checker() if callable(checker) else {"ok": False, "error": "deepseek_unavailable"}
            status = {**status, 'capabilities': ReplyEngine.delivery_capabilities()}
            self.send_json(200, status)
        elif route.startswith("/api/video/analysis/status"):
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            query = parse_qs(parsed.query)
            job_id = (query.get("job_id") or [""])[0]
            include = (query.get("include_draft") or [""])[0] == "1"
            self.send_json(200, self.engine.video_analysis_status(job_id, include))
        elif self.path == "/api/plugins":
            if not self.local_tool_request():
                self.send_json(403, {"ok": False, "error": "local_only"})
                return
            self.send_json(200, self.engine.plugin_state())
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
                raise ValueError("invalid_body_size")
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
        if route == "/api/updates/restart":
            if not self.local_tool_request():
                self.reject(403, "local_only")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 0 or size > 10_000:
                    raise ValueError("invalid_body_size")
                payload = request_json_object(self.rfile.read(size)) if size else {}
                manager = getattr(self.engine, "updates", None)
                restart = getattr(self.engine, "request_restart", None)
                if manager is None:
                    raise ValueError("update_source_mode_disabled")
                from update_restart import evaluate_restart, launcher_supports_restart
                verdict = evaluate_restart(
                    has_pending=bool(manager.state().get("restart_required")),
                    live=bool(self.engine._is_live_running()), payload=payload,
                    supported=callable(restart) and launcher_supports_restart())
                self.send_json(verdict["status"], verdict["body"])
                if verdict["go"]:
                    # Reply first, then let the core exit so the launcher restarts it.
                    threading.Timer(0.4, restart).start()
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                report_problem("api_error", f"POST {route}", exc)
                self.send_json(500, {"ok": False, "error": "internal_error"})
            return
        if route.startswith("/api/updates/"):
            if not self.local_tool_request():
                self.reject(403, "local_only")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 0 or size > 10_000:
                    raise ValueError("invalid_body_size")
                payload = request_json_object(self.rfile.read(size)) if size else {}
                manager = getattr(self.engine, "updates", None)
                if manager is None:
                    raise ValueError("update_source_mode_disabled")
                result = manager.action(route[len("/api/updates/"):], payload)
                self.send_json(202 if result.get("accepted") is True else 200, result)
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json(409 if str(exc) in {"update_job_busy", "update_restart_required"}
                               else 404 if str(exc).endswith("not_found") else 400,
                               {"ok": False, "error": str(exc)})
            except Exception as exc:
                report_problem("api_error", f"POST {route}", exc)
                self.send_json(500, {"ok": False, "error": "internal_error"})
            return
        if route in {"/api/captcha/settings", "/api/captcha/telegram",
                     "/api/captcha/telegram/test", "/api/captcha/selftest",
                     "/api/captcha/start", "/api/captcha/stop"}:
            if not self.local_tool_request():
                self.reject(403, "local_only")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 0 or size > 2048:
                    raise ValueError("invalid_body_size")
                payload = request_json_object(self.rfile.read(size)) if size else {}
                if route == "/api/captcha/settings":
                    result = self.engine.captcha.save_key(str(payload.get("api_key", "")))
                elif route == "/api/captcha/telegram":
                    result = self.engine.captcha.save_telegram(
                        str(payload.get("bot_token", "")), str(payload.get("chat_id", "")))
                elif route == "/api/captcha/telegram/test":
                    result = self.engine.captcha.test_telegram()
                elif route == "/api/captcha/selftest":
                    result = self.engine.captcha.selftest()
                elif route == "/api/captcha/start":
                    result = self.engine.captcha.start()
                else:
                    result = self.engine.captcha.stop()
                self.send_json(200 if result.get("ok") else 409, result)
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                report_problem("api_error", f"POST {route}", exc)
                self.send_json(500, {"ok": False, "error": "internal_error"})
            return
        if route == "/api/system/documents/upload":
            if not self.local_tool_request():
                self.reject(403, "local_only")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > 30 * 1024 * 1024:
                    raise ValueError("File quá lớn (tối đa 30 MB)")
                query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
                meta = self.engine.documents.add(unquote(query.get("filename", "")), self.rfile.read(size),
                                                 category=query.get("category", "khac"),
                                                 title=unquote(query.get("title", "")))
                self.send_json(200, {"ok": True, "document": meta})
            except ValueError as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                report_problem("api_error", f"POST {route}", exc)
                self.send_json(500, {"ok": False, "error": "internal_error"})
            return
        if route.startswith("/api/system/"):
            if not self.local_tool_request():
                self.reject(403, "local_only")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 0 or size > 2_000_000:
                    raise ValueError("invalid_body_size")
                payload = request_json_object(self.rfile.read(size)) if size else {}
                result = self.engine.system_post(route[len("/api/system/"):], payload)
                self.send_json(202 if result.get('accepted') is True else 200, result)
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json(409 if str(exc) in {'maintenance_required', 'maintenance_job_busy', 'backup_preview_changed'}
                               else 404 if str(exc).endswith("not_found") else 400,
                               {"ok": False, "error": str(exc)})
            except Exception as exc:
                report_problem("api_error", f"POST {route}", exc)
                self.send_json(500, {"ok": False, "error": "internal_error"})
            return
        if route.startswith("/api/learning/"):
            if not self.local_tool_request():
                self.reject(403, "local_only")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 0 or size > 2_000_000:
                    raise ValueError("invalid_body_size")
                payload = request_json_object(self.rfile.read(size)) if size else {}
                self.send_json(200, self.engine.learning_post(route[len("/api/learning/"):], payload))
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                report_problem("api_error", f"POST {route}", exc)
                self.send_json(500, {"ok": False, "error": "internal_error"})
            return
        correction_undo = (route.startswith("/api/workspace/corrections/") and
                           route.endswith("/undo"))
        dose_rules_route = (route in ("/api/workspace/dose-rules/import",
                                      "/api/workspace/dose-rules/import-label") or (
            route.startswith("/api/workspace/dose-rules/") and route.endswith("/review")))
        workspace_route = route == "/api/workspace/corrections" or correction_undo or dose_rules_route or (
            route.startswith("/api/workspace/") and
            (route.endswith("/rollback") or route.endswith("/restore"))
        )
        trash_route = route in {"/api/trash/empty-expired", "/api/trash/purge"}
        video_draft_route = (route.startswith("/api/workspace/videos/") and
                             route.endswith("/draft"))
        dose_test_route = (route.startswith("/api/workspace/profiles/") and
                           route.endswith("/dose-test"))
        duplicate_route = (route.startswith("/api/workspace/videos/") and
                           route.endswith("/duplicates"))
        conversation_example_route = (route.startswith("/api/workspace/conversations/") and
                                      (route.endswith("/approve-example") or route.endswith("/disable-example")))
        video_job_draft_route = (route.startswith("/api/workspace/video-jobs/") and
                                 route.endswith("/draft"))
        video_job_publish_route = (route.startswith("/api/workspace/video-jobs/") and
                                   route.endswith("/publish"))
        product_image_route = (route.startswith("/api/workspace/products/") and
                               route.endswith("/image"))
        local_video_routes = {
            "/api/video/upload/start", "/api/video/upload/chunk",
            "/api/video/upload/resume",
            "/api/video/upload/finish",
            "/api/video/analysis/resume",
            "/api/video/assets/retry",
        }
        if route in (("/api/mapping", "/api/video/position", "/api/plugins/action", "/api/backup",
                         "/api/diagnostics/fix", "/api/support/create", "/api/extension/setup",
                         "/api/mapping/undo", "/api/focus", "/api/shutdown", "/api/live/sources", "/api/live/reidentify",
                         "/api/mapping/auto-match", "/api/profiles/bulk-draft", "/api/profiles/approve",
                         "/api/knowledge/update", "/api/knowledge/undo",
                         "/api/knowledge/export",
                         "/api/audio/test", "/api/backup/restore", "/api/privacy/forget",
                         "/api/backup/v2", "/api/backup/v2/delete/preview", "/api/backup/v2/delete", "/api/backup/restore/reenable-ai", "/api/backup/indexeddb") + tuple(local_video_routes)) or workspace_route or trash_route or video_draft_route or dose_test_route or duplicate_route or conversation_example_route or video_job_draft_route or video_job_publish_route or product_image_route:
            # Các route cấu hình/UI chỉ nhận same-origin local.
            if not self.local_tool_request():
                self.reject(403, "local_only")
                return
        elif route == "/api/settings":
            if not self.local_tool_request():
                self.reject(403, "local_only")
                return
        elif not self.headers.get("Origin", "").startswith("chrome-extension://"):
            self.reject(403, "extension_origin_required")
            return
        delivery_routes = {f'/api/{prefix}/{operation}': (platform, operation)
                           for platform, prefix in (('tiktok', 'reply'), ('shopee', 'shopee/reply'))
                           for operation in ('claim', 'status', 'dispatched', 'receipt', 'failure')}
        # Tiện ích gọi /api/{tiktok,shopee}/queue/*; giữ thêm đường cũ dưới /reply.
        queue_routes = {f'/api/{prefix}/queue/{operation}': (platform, operation)
                        for platform, prefix in (('tiktok', 'reply'), ('shopee', 'shopee/reply'),
                                                 ('tiktok', 'tiktok'), ('shopee', 'shopee'))
                        for operation in ('enqueue', 'next', 'status', 'cancel')}
        if route not in {"/api/reply", "/api/reply/ack", "/api/reply/delivery", "/api/catalog/sync",
                             *delivery_routes,
                             *queue_routes,
                             "/api/shopee/mappings", "/api/shopee/sync", "/api/shopee/reply", "/api/shopee/delivery",
                             "/api/observe", "/api/mapping", "/api/mapping/undo",
                             "/api/mapping/auto-match", "/api/profiles/bulk-draft", "/api/profiles/approve",
                             "/api/video/position", "/api/plugins/action", "/api/backup",
                         "/api/heartbeat", "/api/diagnostics/fix", "/api/support/create",
                         "/api/extension/setup", "/api/knowledge/update",
                         "/api/knowledge/undo", "/api/knowledge/export",
                         "/api/focus", "/api/shutdown", "/api/live/sources", "/api/live/reidentify",
                         "/api/audio/test", "/api/backup/restore", "/api/privacy/forget", "/api/privacy/purge-ack",
                         "/api/backup/v2", "/api/backup/v2/delete/preview", "/api/backup/v2/delete", "/api/backup/restore/reenable-ai", "/api/backup/indexeddb",
                         *local_video_routes} and not workspace_route and not trash_route and not video_draft_route and not dose_test_route and not duplicate_route and not conversation_example_route and not video_job_draft_route and not video_job_publish_route and not product_image_route:
            if route != "/api/settings":
                self.reject(404, "not_found")
                return
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
            if route in delivery_routes:
                result = self.engine.delivery_operation(*delivery_routes[route], payload)
            elif route in queue_routes:
                result = self.engine.queue_operation(*queue_routes[route], payload)
            elif route == "/api/workspace/corrections":
                result = self.engine.workspace_correct(payload)
            elif correction_undo:
                parts = [unquote(part) for part in route.split("/") if part]
                if len(parts) != 5 or parts[:3] != ["api", "workspace", "corrections"]:
                    raise ValueError("workspace_route_invalid")
                result = self.engine.workspace_undo_correction(parts[3])
            elif route == "/api/trash/empty-expired":
                result = self.engine.workspace_purge_expired()
            elif route == "/api/trash/purge":
                result = self.engine.workspace.purge_now(payload.get("trash_ids") or [],
                                                         everything=payload.get("all") is True)
            elif video_draft_route:
                parts = [unquote(part) for part in route.split("/")[3:] if part]
                if len(parts) != 3 or parts[2] != "draft":
                    raise ValueError("workspace_route_invalid")
                result = self.engine.workspace_save_video_draft(parts[1], payload)
            elif dose_test_route:
                parts = [unquote(part) for part in route.split("/")[3:] if part]
                if len(parts) != 3 or parts[2] != "dose-test":
                    raise ValueError("workspace_route_invalid")
                result = self.engine.workspace_dose_test(parts[1], payload)
            elif dose_rules_route:
                import dose_rules
                parts = [unquote(part) for part in route.split("/")[3:] if part]
                try:
                    with self.engine.workspace._db() as db:
                        if parts == ["dose-rules", "import"]:
                            result = dose_rules.import_rules(db, payload.get("rules"))
                        elif parts == ["dose-rules", "import-label"]:
                            import label_parser
                            sku = clean(payload.get("sku"), 160)
                            text = payload.get("text")
                            if not sku or not isinstance(text, str) or not text.strip() or len(text) > 20000:
                                raise ValueError("dose_label_invalid")
                            result = label_parser.import_label(
                                db, text, sku, source_doc=clean(payload.get("source_doc"), 200))
                        elif len(parts) == 3 and parts[2] == "review":
                            result = dose_rules.review_rule(
                                db, parts[1], payload.get("decision"))
                        else:
                            raise ValueError("workspace_route_invalid")
                except dose_rules.DoseRuleConflict as exc:
                    self.send_json(409, {"ok": False, "error": str(exc)})
                    return
            elif duplicate_route:
                parts = [unquote(part) for part in route.split("/")[3:] if part]
                if len(parts) != 3 or parts[2] != "duplicates":
                    raise ValueError("workspace_route_invalid")
                result = self.engine.workspace_resolve_video_duplicate(parts[1], payload)
            elif conversation_example_route:
                parts = [unquote(part) for part in route.split("/")[3:] if part]
                if len(parts) != 3:
                    raise ValueError("workspace_route_invalid")
                result = (self.engine.workspace_approve_reply_example(parts[1], payload)
                          if parts[2] == "approve-example" else
                          self.engine.workspace_disable_reply_example(parts[1], payload))
            elif video_job_draft_route:
                parts = [unquote(part) for part in route.split("/")[3:] if part]
                if len(parts) != 3:
                    raise ValueError("workspace_route_invalid")
                result = self.engine.workspace_save_video_job_draft(parts[1], payload)
            elif video_job_publish_route:
                parts = [unquote(part) for part in route.split("/")[3:] if part]
                if len(parts) != 3:
                    raise ValueError("workspace_route_invalid")
                result = self.engine.video_analysis_publish({**payload, "job_id": parts[1]})
            elif product_image_route:
                parts = [unquote(part) for part in route.split("/")[3:] if part]
                if len(parts) != 3:
                    raise ValueError("workspace_route_invalid")
                result = self.engine.workspace_save_product_image(parts[1], payload)
            elif workspace_route:
                parts = [unquote(part) for part in route.split("/")[3:] if part]
                if len(parts) != 3 or parts[2] not in {"rollback", "restore"}:
                    raise ValueError("workspace_route_invalid")
                result = (self.engine.workspace_rollback(parts[0], parts[1])
                          if parts[2] == "rollback" else
                          self.engine.workspace_restore(parts[0], parts[1]))
            elif route == "/api/video/upload/start":
                result = self.engine.video_upload_start(payload)
            elif route == "/api/video/upload/resume":
                result = self.engine.video_upload_resume(payload)
            elif route == "/api/video/upload/finish":
                result = self.engine.video_upload_finish(payload)
            elif route == "/api/video/analysis/resume":
                result = self.engine.video_analysis_resume(payload)
            elif route == "/api/video/assets/retry":
                result = self.engine.video_assets_state(
                    payload.get("video_id", ""), payload.get("job_id", ""), retry=True)
            elif route == "/api/mapping":
                result = self.engine.save_mapping(payload)
            elif route == "/api/mapping/undo":
                result = self.engine.undo_mapping()
            elif route == "/api/mapping/auto-match":
                result = self.engine.auto_match_mapping(payload)
            elif route == "/api/profiles/bulk-draft":
                result = self.engine.bulk_draft_profiles(payload)
            elif route == "/api/profiles/approve":
                result = self.engine.approve_profiles(payload)
            elif route == "/api/backup/restore":
                result = self.engine.restore_backup(payload)
            elif route == "/api/backup":
                result = self.engine.backup_now_and_refresh(payload)
            elif route == "/api/backup/v2":
                result = self.engine.backup_v2_start(payload)
            elif route == "/api/backup/restore/reenable-ai":
                result = self.engine.reenable_ai_after_restore(payload)
            elif route == "/api/backup/v2/delete/preview":
                result = self.engine.backup_v2_delete_preview(payload)
            elif route == "/api/backup/v2/delete":
                result = self.engine.backup_v2_delete(payload)
            elif route == "/api/backup/indexeddb":
                result = self.engine.backup_indexeddb_save(payload)
            elif route == "/api/privacy/forget":
                result = self.engine.privacy_forget(payload)
            elif route == "/api/privacy/purge-ack":
                result = self.engine.privacy_purge_ack(payload)
            elif route == "/api/heartbeat":
                result = self.engine.heartbeat(payload)
            elif route == "/api/diagnostics/fix":
                result = self.engine.diagnostics_fix()
            elif route == "/api/support/create":
                result = self.engine.support_report_create()
            elif route == "/api/settings":
                result = self.engine.save_settings(payload)
            elif route == "/api/audio/test":
                result = self.engine.test_audio_device(payload)
            elif route == "/api/extension/setup":
                result = self.engine.open_extension_setup(payload.get("platform", "tiktok"))
            elif route == "/api/focus":
                from windows_integration import focus_current_process_window
                result = {"ok": focus_current_process_window()}
            elif route == "/api/shutdown":
                shutdown = getattr(self.engine, "request_shutdown", None)
                result = {"ok": callable(shutdown)}
                if callable(shutdown):
                    shutdown()
            elif route == "/api/knowledge/update":
                result = self.engine.update_knowledge_package(payload)
            elif route == "/api/knowledge/undo":
                result = self.engine.undo_knowledge_package(payload)
            elif route == "/api/knowledge/export":
                result = self.engine.export_knowledge_package(payload)
            elif route == "/api/plugins/action":
                result = self.engine.plugin_action(payload)
            elif route == "/api/video/position":
                result = self.engine.set_video_position(payload)
            elif route == "/api/shopee/mappings":
                result = self.engine.save_shopee_mappings(payload)
            elif route == "/api/shopee/sync":
                result = self.engine.sync_shopee_catalog(payload)
            elif route == "/api/live/sources":
                result = self.engine.save_live_sources(payload)
            elif route == '/api/live/reidentify':
                result = self.engine.live_sources.request_reidentify(payload.get('platform'))
                self.engine.set_video_position({'platform': payload.get('platform'), 'available': False})
            elif route == "/api/shopee/reply":
                result = (self.engine.queued_reply('shopee', payload) if payload.get('protocol_version') == 2
                          else self.engine.shopee_ai().reply(payload))
            elif route == "/api/shopee/delivery":
                result = self.engine.shopee_ai().delivery(payload)
            elif route.endswith("/observe"):
                result = self.engine.observe(payload)
            elif route.endswith("catalog/sync"):
                result = self.engine.sync_catalog(payload.get("live_context", payload))
            elif route.endswith("reply/ack"):
                result = self.engine.acknowledge_reply(payload)
            elif route.endswith("reply/delivery"):
                result = self.engine.update_reply_delivery(payload)
            else:
                result = (self.engine.queued_reply('tiktok', payload) if payload.get('protocol_version') == 2
                          else self.engine.handle(payload))
            self.send_json(200, result)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(409 if str(exc) in {'revision_conflict', 'maintenance_required', 'backup_busy', 'maintenance'} else 400,
                           {"ok": False, "error": str(exc)})
        except Exception as exc:
            report_problem("api_error", f"POST {route}", exc)
            self.send_json(500, {"ok": False, "error": "internal_error"})
