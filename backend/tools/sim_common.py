"""Phần dùng chung cho token_budget_sim.py và context_ab.py (không gọi API thật)."""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
LIVE_SRC = Path(r"D:\KidTE\src")
CHARS_PER_TOKEN = 2.6


def scratch_dir(prefix):
    """Thư mục tạm tự xóa khi tiến trình thoát."""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


def tok(chars):
    return round(chars / CHARS_PER_TOKEN)


def resolve_root(value):
    """Nhận bản SAO của thư mục dữ liệu backend; từ chối bản live."""
    path = Path(value).expanduser().resolve()
    if not (path / "data" / "live_products_current.json").is_file() and (path / "live_products_current.json").is_file():
        path = path.parent
    if not (path / "data" / "live_products_current.json").is_file():
        raise SystemExit(f"{path}: không thấy data/live_products_current.json — cần một bản SAO dữ liệu backend")
    live = str(LIVE_SRC.resolve()).lower()
    if str(path).lower().startswith(live) or str(path / "data").lower().startswith(live):
        raise SystemExit("TỪ CHỐI: đây là dữ liệu live (D:\\KidTE\\src). Hãy copy ra thư mục khác rồi chạy lại.")
    return path


ROUTER_NEUTRAL = {"kind": "other", "product_ids": [], "topics": [], "clarify": False, "confidence": 0.5}


class FakeCompletions:
    """router_sink: nếu có, lời gọi router (nhận ra qua lời nhắc hệ thống) vào đây, KHÔNG vào sink chính."""

    def __init__(self, sink, reply="Dạ em gửi anh chị ạ.", usage=None, router_sink=None, router_answer=None):
        self.sink, self.reply, self.usage = sink, reply, usage or {}
        self.router_sink, self.router_answer = router_sink, router_answer or ROUTER_NEUTRAL

    def create(self, **inference):
        if self.router_sink is not None and _is_router_call(inference):
            self.router_sink.append(inference)
            msg = types.SimpleNamespace(content=json.dumps(self.router_answer, ensure_ascii=False))
            usage = types.SimpleNamespace(prompt_tokens=0, completion_tokens=0, prompt_cache_hit_tokens=0,
                                          prompt_cache_miss_tokens=0)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")],
                                         usage=usage)
        self.sink.append(inference)
        text = self.reply(inference) if callable(self.reply) else self.reply
        msg = types.SimpleNamespace(content=json.dumps(
            {"action": "reply", "reply": text, "messages": [text], "awaiting_fields": []},
            ensure_ascii=False), reasoning_content=None)
        usage = types.SimpleNamespace(prompt_tokens=self.usage.get("prompt_tokens", 0),
                                      completion_tokens=self.usage.get("completion_tokens", 0),
                                      prompt_cache_hit_tokens=self.usage.get("prompt_cache_hit_tokens", 0),
                                      prompt_cache_miss_tokens=0)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")], usage=usage)


def _is_router_call(inference):
    import router
    messages = inference.get("messages") or []
    return bool(messages) and messages[0].get("content") == router.SYSTEM_PROMPT


def fake_client(sink, reply="Dạ em gửi anh chị ạ.", usage=None, router_sink=None, router_answer=None):
    return types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=FakeCompletions(sink, reply, usage, router_sink, router_answer)))


def write_router_config(enabled, folder=None, **extra):
    """File router.json tạm; trả đường dẫn (gán vào router.CONFIG_PATH)."""
    folder = Path(folder or scratch_dir("routercfg-"))
    path = folder / ("router_on.json" if enabled else "router_off.json")
    path.write_text(json.dumps({"enabled": bool(enabled), **extra}), encoding="utf-8")
    return path


def boot_engine(root, flags_file=None, real_key=False):
    """Nạp server trên bản sao dữ liệu (KID_TE_ROOT) và trả (server_module, engine)."""
    os.environ["KID_TE_ROOT"] = str(root)
    if real_key:
        # Khóa thật lấy từ .env của bản sao; không để khóa giả chặn trước load_dotenv.
        if os.environ.get("DEEPSEEK_API_KEY") == "sk-sim-not-real":
            del os.environ["DEEPSEEK_API_KEY"]
    else:
        os.environ.setdefault("DEEPSEEK_API_KEY", "sk-sim-not-real")
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    import server
    import context_budget
    from config import ROOT
    if Path(ROOT).resolve() != Path(root).resolve():
        raise SystemExit(f"KID_TE_ROOT không có hiệu lực ({ROOT}); chạy trong tiến trình mới.")
    if flags_file is not None:
        context_budget.FLAGS_PATH = Path(flags_file)
    engine = server.ReplyEngine()
    return server, context_budget, engine


def write_flags(on, folder=None, c_on=None):
    """Cờ Gói A theo `on`; cờ Gói C (persona_all_lanes/customer_card/playbook) theo `c_on` (mặc định = `on`)."""
    c_on = on if c_on is None else c_on
    folder = Path(folder or scratch_dir("ctxflags-"))
    path = folder / ("flags_on.json" if on and c_on else "flags_off.json" if not on and not c_on else "flags_mixed.json")
    path.write_text(json.dumps({"smalltalk_lane": bool(on), "intent_fields": bool(on),
                                "compact_catalog": bool(on), "persona_all_lanes": bool(c_on),
                                "customer_card": bool(c_on), "playbook": bool(c_on)}), encoding="utf-8")
    return path


def live_context_from(root):
    catalog = json.loads((Path(root) / "data" / "live_products_current.json").read_text(encoding="utf-8"))
    return {"room_id": "sim-room", "pin_id": catalog.get("pin_id", ""), "products": catalog["products"]}


def top_customer(root):
    """Khách có nhiều lượt bình luận nhất trong bản sao (đọc-chỉ)."""
    db = sqlite3.connect(f"file:{Path(root) / 'data' / 'customer_memory.sqlite3'}?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT c.platform_user_id, c.unique_id, c.display_name, COUNT(*) n FROM interactions i "
            "JOIN customers c USING(customer_key) WHERE i.event_type='comment' AND c.platform_user_id!='' "
            "GROUP BY c.customer_key ORDER BY n DESC LIMIT 1").fetchone()
    finally:
        db.close()
    if not row:
        return {"user_id": "sim-regular-0001", "unique_id": "khach_quen_sim", "nickname": "Khách Quen"}
    return {"user_id": row[0], "unique_id": row[1] or "", "nickname": row[2] or "Khách"}


def frequent_comments(root, limit=40):
    """Bình luận (khác nhau) được trả lời nhiều nhất trong customer_memory."""
    db = sqlite3.connect(f"file:{Path(root) / 'data' / 'customer_memory.sqlite3'}?mode=ro", uri=True)
    try:
        rows = db.execute(
            "SELECT comment, COUNT(*) n FROM interactions WHERE event_type='comment' "
            "AND response IS NOT NULL AND response!='' AND length(comment)>=2 "
            "GROUP BY comment ORDER BY n DESC, comment LIMIT ?", (int(limit),)).fetchall()
    finally:
        db.close()
    return [r[0] for r in rows]


def message_parts(messages):
    """Tách các message thành {tên_phần: số ký tự} như token_sim gốc."""
    parts = {}
    if not messages:
        return parts
    parts["system_prompt"] = len(messages[0]["content"])
    for message in messages[1:]:
        content = message["content"]
        head, _, rest = content.partition("\n")
        try:
            obj, end = json.JSONDecoder().raw_decode(rest)
        except Exception:  # noqa: BLE001
            parts["other_" + message["role"]] = len(content)
            continue
        prefix = "stable." if message["role"] == "system" else "turn."
        for key, value in obj.items():
            parts[prefix + key] = len(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) + len(key) + 4
        parts[prefix + "_wrapper"] = len(head) + len(rest[end:])
    return parts


def common_prefix_chars(left, right):
    a = "\x1e".join(f"{m['role']}:{m['content']}" for m in left)
    b = "\x1e".join(f"{m['role']}:{m['content']}" for m in right)
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i, len(b)
