"""Bộ định tuyến nhỏ chạy TRƯỚC khi dựng ngữ cảnh chính (gói B).

Một lời gọi model rất nhỏ (JSON, tắt thinking, ~120 token ra) phân loại câu khách:
loại câu, 0-3 sản phẩm trong danh mục, chủ đề, có cần hỏi lại không. Mã quyết định
chắc chắn (nhắc tên sản phẩm, ghim, câu hỏi đang chờ...) LUÔN thắng; router chỉ
lấp chỗ trống. Mọi lỗi/timeout/JSON hỏng => trả None => hành vi cũ y nguyên.

Cấu hình data/machine/router.json:
  {"enabled": false, "base_url": "", "model": "", "timeout_s": 4, "api_key_env": ""}
base_url/model rỗng = dùng lại client DeepSeek chính (tắt thinking). Điền base_url
để cắm máy chủ OpenAI-tương-thích cục bộ (Ollama/LM Studio) mà không sửa mã.
Không có file = TẮT.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from ai_context import system_with_brief
from config import DATA_DIR

CONFIG_PATH = DATA_DIR / "machine" / "router.json"
DEFAULT_CONFIG = {"enabled": False, "base_url": "", "model": "", "timeout_s": 4.0, "api_key_env": ""}
KINDS = ("smalltalk", "product", "purchase", "complaint", "other")   # suy ra từ acts (tiện dụng)
ACTS = ("greeting", "banter", "question", "purchase_signal", "objection", "complaint", "price_report", "thanks")
TONES = ("playful", "neutral", "serious", "upset")
_LIGHT_ACTS = frozenset({"greeting", "banter", "thanks"})
_KIND_TO_ACTS = {"smalltalk": ["banter"], "product": ["question"], "purchase": ["purchase_signal"],
                 "complaint": ["complaint"], "other": []}
TOPICS = ("price", "dose", "usage", "compat", "promotion", "ship", "agronomy", "stock")
MIN_CONFIDENCE = 0.7          # dưới ngưỡng này router không được đổi hành vi
MAX_TOKENS = 120
NAME_LIMIT = 40
_log = logging.getLogger("kid-te.core")
_cache = {"key": None, "config": dict(DEFAULT_CONFIG)}
_custom_clients = {}

SYSTEM_PROMPT = (
    "Bạn phân loại câu khách trong phiên LIVE bán nông sản. Chỉ trả một JSON: "
    '{"acts":[],"tone":"neutral","product_ids":[],"topics":[],"clarify":false,"confidence":0.0}. '
    "acts (nhiều nhãn được): greeting, banter (trêu/cười/khen), question (hỏi gì đó), purchase_signal (muốn mua/chốt), "
    "objection (nghi ngờ/chê/hỏi bán chịu/hỏi có phải AI), complaint, price_report (nói giá nơi khác), thanks. "
    "Ví dụ 'hello giá sao e' = greeting+question. tone: playful|neutral|serious|upset. "
    "product_ids: tối đa 3 id LẤY ĐÚNG từ DANH MỤC, chỉ khi câu nhắc tới sản phẩm đó; không chắc thì []. "
    "topics: tập con của price,dose,usage,compat,promotion,ship,agronomy,stock. "
    "clarify=true nếu câu hỏi cần biết sản phẩm/dung tích nhưng khớp nhiều sản phẩm hoặc không rõ. "
    "confidence 0..1. Không giải thích."
)

# Chủ đề -> khoá ý định của KnowledgeBase.intents (chỉ để chọn nhóm trường hồ sơ).
_TOPIC_INTENTS = {"price": "price", "promotion": "promotion", "dose": "dose",
                  "usage": "technical", "agronomy": "technical", "compat": "mixing"}


# ---------------------------------------------------------------------------
# Cấu hình
# ---------------------------------------------------------------------------
def load_config():
    """Cấu hình hiện hành; thiếu/hỏng file => TẮT."""
    path = Path(CONFIG_PATH)
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return dict(DEFAULT_CONFIG)
    if _cache["key"] == key:
        return dict(_cache["config"])
    config = dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            config["enabled"] = raw.get("enabled") is True
            for name in ("base_url", "model", "api_key_env"):
                if isinstance(raw.get(name), str):
                    config[name] = raw[name].strip()
            try:
                timeout = float(raw.get("timeout_s", DEFAULT_CONFIG["timeout_s"]))
                if timeout == timeout and 0.2 <= timeout <= 30:
                    config["timeout_s"] = timeout
            except (TypeError, ValueError):
                pass
    except Exception:  # noqa: BLE001
        _log.warning("router.json không đọc được; router tắt", exc_info=True)
        config = dict(DEFAULT_CONFIG)
    _cache.update(key=key, config=config)
    return dict(config)


def enabled():
    return bool(load_config()["enabled"])


def topics_to_intents(topics):
    """Chủ đề router -> {khoá ý định: True}; chỉ dùng để HỢP với ý định của mã."""
    return {_TOPIC_INTENTS[t]: True for t in topics or () if t in _TOPIC_INTENTS}


# ---------------------------------------------------------------------------
# Dựng lời nhắc và kiểm tra đầu ra
# ---------------------------------------------------------------------------
def _short(text, limit):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _catalog_rows(catalog_names):
    if isinstance(catalog_names, dict):
        pairs = catalog_names.items()
    else:
        pairs = []
        for item in catalog_names or []:
            if isinstance(item, dict):
                pairs.append((item.get("id", item.get("product_id")), item.get("name", item.get("title"))))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                pairs.append((item[0], item[1]))
    rows = {}
    for pid, name in pairs:
        pid = _short(pid, 80)
        if pid:
            rows[pid] = _short(name, NAME_LIMIT)
    return sorted(rows.items())


def build_messages(comment, catalog_rows, recent_turns, pinned, brief=False):
    """[system cố định, danh mục ổn định (cache), phần riêng lượt].

    brief=True: APP_BRIEF đứng đầu system (chỉ dùng cho DeepSeek: tiền tố cố định nên được cache, gần như
    không tốn thêm độ trễ). Máy chủ cục bộ nhỏ (custom) KHÔNG nhận brief để giữ lời gọi tí hon.
    """
    catalog = "DANH MỤC (id|tên):\n" + "\n".join(f"{pid}|{name}" for pid, name in catalog_rows)
    lines = []
    for turn in list(recent_turns or [])[:2]:
        if not isinstance(turn, dict):
            continue
        said, reply = _short(turn.get("comment"), 80), _short(turn.get("response"), 80)
        if said:
            lines.append("- Khách: " + said + (" | Shop: " + reply if reply else ""))
    pin = _short(pinned, 80)
    turn_part = ("Gần đây:\n" + "\n".join(lines) + "\n" if lines else "") \
        + ("Đang ghim: " + pin + "\n" if pin else "") + "Câu khách: " + _short(comment, 300)
    system = system_with_brief(SYSTEM_PROMPT) if brief else SYSTEM_PROMPT
    return [{"role": "system", "content": system},
            {"role": "system", "content": catalog},
            {"role": "user", "content": turn_part}]


def validate(raw, valid_ids):
    """Chuỗi JSON/dict -> dict chuẩn hoá hoặc None. Id lạ/chủ đề lạ bị bỏ."""
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("acts"), list):
        acts = [a for a in dict.fromkeys(data["acts"]) if isinstance(a, str) and a in ACTS]
    elif data.get("kind") in KINDS:      # định dạng cũ một nhãn
        acts = list(_KIND_TO_ACTS[data["kind"]])
    else:
        return None
    tone = data.get("tone") if data.get("tone") in TONES else "neutral"
    ids = []
    for pid in data.get("product_ids") if isinstance(data.get("product_ids"), list) else []:
        pid = str(pid).strip()
        if pid in valid_ids and pid not in ids:
            ids.append(pid)
    topics = []
    for topic in data.get("topics") if isinstance(data.get("topics"), list) else []:
        if isinstance(topic, str) and topic in TOPICS and topic not in topics:
            topics.append(topic)
    try:
        confidence = float(data.get("confidence", 0))
        confidence = min(1.0, max(0.0, confidence)) if confidence == confidence else 0.0
    except (TypeError, ValueError):
        confidence = 0.0
    pure = bool(acts) and set(acts) <= _LIGHT_ACTS
    if pure:
        kind = "smalltalk"
    elif "purchase_signal" in acts:
        kind = "purchase"
    elif "complaint" in acts:
        kind = "complaint"
    elif "question" in acts:
        kind = "product"
    else:
        kind = "other"
    return {"kind": kind, "acts": acts, "tone": tone, "pure_smalltalk": pure, "product_ids": ids[:3],
            "topics": topics, "clarify": data.get("clarify") is True, "confidence": confidence}


def _call_with_deadline(client_call, request, timeout):
    box = {}

    def run():
        try:
            box["response"] = client_call(request)
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True, name="router-call")
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError("router timeout")
    if "error" in box:
        raise box["error"]
    return box["response"]


def route(client_call, comment, *, catalog_names, recent_turns=None, pinned="", platform="",
          model="", timeout=4.0, custom=False, meta=None):
    """Gọi router. client_call(request_dict) -> phản hồi kiểu OpenAI. Lỗi bất kỳ => None.

    meta (dict, tuỳ chọn) được điền số đo: router_ms, tokens. Không chứa nội dung.
    """
    started = time.monotonic()
    meta = meta if meta is not None else {}
    try:
        rows = _catalog_rows(catalog_names)
        if not rows or not _short(comment, 10):
            return None
        request = {"model": model, "messages": build_messages(comment, rows, recent_turns, pinned, brief=not custom),
                   "max_tokens": MAX_TOKENS, "temperature": 0,
                   "response_format": {"type": "json_object"}, "timeout": timeout}
        if not custom:
            request["extra_body"] = {"thinking": {"type": "disabled"}}
        response = _call_with_deadline(client_call, request, timeout)
        usage = getattr(response, "usage", None)
        meta.update(router_prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                    router_cache_hit_tokens=int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0),
                    router_completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0))
        content = response.choices[0].message.content or ""
        result = validate(content, {pid for pid, _ in rows})
        if result:
            meta["router_kind"] = result["kind"]
        return result
    except Exception:  # noqa: BLE001 - router không bao giờ được chặn câu trả lời
        _log.info("router failed; fail open", exc_info=True)
        return None
    finally:
        meta["router_ms"] = int((time.monotonic() - started) * 1000)


# ---------------------------------------------------------------------------
# Nối với responder chính
# ---------------------------------------------------------------------------
def make_call(responder, config):
    """(client_call, model, custom) hoặc None nếu chưa có client."""
    if config.get("base_url"):
        key = (os.environ.get(config["api_key_env"], "") if config.get("api_key_env") else "") or "sk-local"
        cache_key = (config["base_url"], key)
        client = _custom_clients.get(cache_key)
        if client is None:
            from openai import OpenAI
            client = OpenAI(api_key=key, base_url=config["base_url"], timeout=config["timeout_s"], max_retries=0)
            _custom_clients[cache_key] = client
        model, custom = config.get("model") or getattr(responder, "model", ""), True
    else:
        client = getattr(responder, "client", None)
        model = config.get("model") or getattr(responder, "model", "")
        custom = False
    if client is None:
        return None
    return (lambda request: client.chat.completions.create(**request)), model, custom


def consult(responder, comment, *, catalog_names, recent_turns=None, pinned="", platform=""):
    """(kết quả|None, meta). Tắt/thiếu client/lỗi => (None, {}) mà không gọi mạng."""
    try:
        config = load_config()
        if not config["enabled"]:
            return None, {}
        built = make_call(responder, config)
        if built is None:
            return None, {}
        call, model, custom = built
        meta = {}
        result = route(call, comment, catalog_names=catalog_names, recent_turns=recent_turns, pinned=pinned,
                       platform=platform, model=model, timeout=config["timeout_s"], custom=custom, meta=meta)
        return result, meta
    except Exception:  # noqa: BLE001
        _log.info("router consult failed; fail open", exc_info=True)
        return None, {}


def metrics_from(meta, result, used, selection_source):
    """Số liệu để ghi vào metrics (không nội dung)."""
    out = {"router_used": bool(used), "selection_source": selection_source}
    for key in ("router_ms", "router_prompt_tokens", "router_cache_hit_tokens", "router_completion_tokens"):
        if key in (meta or {}):
            out[key] = meta[key]
    if result:
        out["router_kind"] = result["kind"]
        out["router_acts"] = list(result["acts"])
        out["router_tone"] = result["tone"]
    return out


def reading(result):
    """doc_y_khach: {acts, tone} rất gọn cho ngữ cảnh chính; None nếu router không chắc."""
    if not result or result["confidence"] < MIN_CONFIDENCE or not result["acts"]:
        return None
    return {"acts": list(result["acts"]), "tone": result["tone"]}
