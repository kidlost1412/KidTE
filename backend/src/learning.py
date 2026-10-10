"""Trí nhớ AI — kiến thức chủ shop dạy, phong cách từng người, vòng học lại.

Vấn đề cũ: mỗi lần chủ shop sửa một câu trả lời, hệ thống chỉ lưu CẶP
(câu hỏi, câu trả lời) làm "câu mẫu". Lần sau AI tìm câu mẫu gần giống bằng
trùng từ rồi chép gần như nguyên văn. Nó không hề HIỂU vì sao câu cũ sai:
"Win Gold 20 là mã nào" → sửa thành "là can đạm cá amino" thì AI chỉ nhớ
câu đó, không biết "Win Gold 20 = Đạm Cá Amino" để dùng cho mọi câu khác
(giá Win Gold 20? liều Win Gold 20? Win Gold 20 có can 1L không?).

Thiết kế mới tách bản sửa thành ba lớp, mỗi lớp dùng một cách:

1. KIẾN THỨC (``knowledge_items``) — câu khẳng định nhỏ, dùng lại được:
   - ``alias``: tên gọi khác → sản phẩm. Dùng TẤT ĐỊNH trước khi gọi model:
     khách nói "wingold 20" là chọn đúng SKU Đạm Cá, nạp đúng hồ sơ, giá, liều.
   - ``fact``: dữ kiện gắn với sản phẩm hoặc với shop.
   - ``rule``: cách xử lý một loại tình huống ("tên lạ giống tên sản phẩm thì
     đừng đoán là mã giảm giá").
   Mỗi mục có trạng thái suggested → active → retired, nguồn gốc (bản sửa nào),
   số lần được dùng. Chỉ mục ``active`` mới vào prompt.

2. PHONG CÁCH (``personas``) — mỗi người trực LIVE một giọng. Hướng dẫn
   phong cách được rút ra từ chính các câu người đó sửa (xưng hô, từ đệm, độ
   dài, cách chốt) và nằm trong phần prompt CỐ ĐỊNH (được DeepSeek cache).

3. CÂU MẪU (bảng cũ ``approved_reply_examples``) — giữ lại nhưng đổi vai: chỉ
   để học Ý và GIỌNG, prompt cấm chép nguyên văn.

Vòng học:
- Rút kiến thức (``distill``): ngay sau khi chủ shop dạy một câu, chạy nền —
  DeepSeek (bật suy luận) so câu AI đã nói với câu chủ shop sửa, giải thích AI
  sai ở đâu và đề xuất các mục kiến thức. Không có mạng/khoá thì dùng luật
  đơn giản (ghi chú dạng "X là Y").
- Học lại toàn bộ (``relearn``): gom mọi kiến thức + bản sửa, gộp trùng, phát
  hiện mâu thuẫn, viết lại hướng dẫn phong cách. Kết quả là một ĐỀ XUẤT có
  diff; áp dụng thì lưu bản trước để hoàn tác.
"""
from __future__ import annotations

import json
import queue
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from knowledge import clean, fold

KINDS = ("alias", "fact", "rule")
STATUSES = ("suggested", "active", "retired", "rejected")
MAX_TERM_CHARS = 60
MAX_STATEMENT_CHARS = 400
# Bao nhiêu mục được đưa vào MỘT lượt trả lời. Ít mà trúng hơn nhiều mà nhiễu.
MAX_FACTS_PER_TURN = 8
MAX_RULES = 12
MAX_STYLE_CHARS = 1600

# Từ quá phổ biến trong bình luận LIVE — không dùng để khớp kiến thức.
_COMMON = frozenset("""
a ah ak anh bac ban bao be ben bi biet binh bon bot cai can cay chai chi cho chu co con cua cung da dang
day de den deu di dinh do dong du dung duoc em gi gia gio giup ha hang hay het hoi hom hon hong khi khong
ko la lam lan lay len luon ma mai minh mot mua muon nao nay ne nen nha nhe nhieu nho noi nua o oi phai
phan phun qua ra rat roi sao san se shop so sp sua tai thang the thi thoi thu thuoc toi tren tu tuoi va
vay ve vi voi vo xem xin
""".split())


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def compact(value):
    """"Win Gold-20" → "wingold20": khớp được cả khi khách viết liền/tách."""
    return re.sub(r"[^a-z0-9]", "", fold(value))


def multiline(value, limit):
    """Như clean() nhưng giữ xuống dòng (hướng dẫn phong cách là danh sách gạch đầu dòng)."""
    lines = [clean(line, 400) for line in str(value or "").splitlines()]
    return "\n".join(line for line in lines if line)[:limit]


def informative_tokens(value):
    return {t for t in re.findall(r"[a-z0-9]+", fold(value))
            if len(t) >= 2 and t not in _COMMON}


def _term_ok(term):
    """Từ khoá đủ đặc trưng để tự động kích hoạt một mục kiến thức.

    "đạm" hay "20" một mình sẽ khớp nửa số bình luận. Yêu cầu: sau khi bỏ dấu
    và khoảng trắng dài ≥ 3 ký tự, không phải toàn chữ số, không phải từ thông dụng.
    """
    c = compact(term)
    # 3 ký tự (vd "cod") vẫn được nếu không phải từ thông dụng — khi đó chỉ
    # khớp theo ranh giới từ (xem _term_hit), không khớp dạng viết liền.
    return len(c) >= 3 and not c.isdigit() and fold(term).strip() not in _COMMON


def ambiguous_terms(terms, titles, product_id=""):
    """Từ khoá nằm trong tên của ≥ 2 sản phẩm đang bán thì KHÔNG phân biệt được.

    Ví dụ thật 23/09: "can 5l" được học thành tên gọi của Đạm Cá 5L, nhưng Humic
    và combo cũng là can 5 lít → khách hỏi "can 5l humic" sẽ bị gán nhầm.
    ``titles``: {product_id: tên + tiêu đề}. Chỉ đếm sản phẩm KHÁC ``product_id``
    cộng với chính nó: xuất hiện ở sản phẩm khác là đủ coi là mơ hồ.
    """
    compact_titles = {pid: compact(text) for pid, text in (titles or {}).items()}
    out = []
    for term in terms or []:
        c = compact(term)
        if not c:
            continue
        others = [pid for pid, text in compact_titles.items() if pid != product_id and c in text]
        if others:
            out.append(term)
    return out


def _clean_terms(values):
    out = []
    for value in values or []:
        term = clean(value, MAX_TERM_CHARS).strip(" .,;:!?\"'")
        if term and _term_ok(term) and compact(term) not in {compact(t) for t in out}:
            out.append(term)
    return out[:8]


class LearningStore:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache = None          # (version, items) — đọc nhiều, ghi ít
        self._version = 0
        self.privacy = None         # optional PrivacyService, wired by the server
        self._create_schema()

    # ---------------------------------------------------------------- DB
    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.db_path), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def close(self):
        """Per-call connections: nothing stays open; wait out any in-flight write
        so the restore writer registry can treat this store as quiesced."""
        with self._lock:
            return None

    def _create_schema(self):
        with self._db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS knowledge_items (
                    item_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    product_id TEXT NOT NULL DEFAULT '', persona_id TEXT NOT NULL DEFAULT '',
                    terms_json TEXT NOT NULL DEFAULT '[]', statement TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'suggested', confidence REAL NOT NULL DEFAULT .5,
                    source TEXT NOT NULL DEFAULT 'manual', source_refs_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    created_by TEXT NOT NULL DEFAULT '', use_count INTEGER NOT NULL DEFAULT 0,
                    last_used_at TEXT, supersedes_json TEXT NOT NULL DEFAULT '[]',
                    run_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_items_status ON knowledge_items(status, kind);
                CREATE TABLE IF NOT EXISTS personas (
                    persona_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '', style_guide TEXT NOT NULL DEFAULT '',
                    style_updated_at TEXT, is_active INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS learning_runs (
                    run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, finished_at TEXT, ref_id TEXT NOT NULL DEFAULT '',
                    input_json TEXT NOT NULL DEFAULT '{}', output_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
                    usage_json TEXT NOT NULL DEFAULT '{}', trigger TEXT NOT NULL DEFAULT 'manual'
                );
                CREATE INDEX IF NOT EXISTS idx_runs_kind ON learning_runs(kind, created_at DESC);
                CREATE TABLE IF NOT EXISTS learning_meta (
                    meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL
                );
            """)
            # Provenance (F02): migration-safe columns; old rows keep '' = legacy_unattributed.
            for table in ("knowledge_items", "learning_runs"):
                have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
                if "customer_key_hmac" not in have:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN customer_key_hmac TEXT NOT NULL DEFAULT ''")
                if "legacy_excluded" not in have:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN legacy_excluded INTEGER NOT NULL DEFAULT 0")
            if not db.execute("SELECT 1 FROM personas LIMIT 1").fetchone():
                db.execute(
                    "INSERT INTO personas(persona_id,name,description,style_guide,is_active,created_at) "
                    "VALUES ('default','Giọng mặc định','Giọng host mặc định trong lời nhắc hệ thống.','',1,?)",
                    (_now(),))

    def _bump(self):
        with self._lock:
            self._version += 1
            self._cache = None

    @property
    def version(self):
        return self._version

    # ---------------------------------------------------------------- meta
    def meta(self, key, default=""):
        with self._db() as db:
            row = db.execute("SELECT meta_value FROM learning_meta WHERE meta_key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        with self._db() as db:
            db.execute("INSERT INTO learning_meta(meta_key,meta_value) VALUES (?,?) "
                       "ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value",
                       (key, str(value)))

    def settings(self):
        return {
            "auto_activate": self.meta("auto_activate", "0") == "1",
            "auto_relearn": self.meta("auto_relearn", "0") == "1",   # mặc định TẮT: học lại chỉ khi chủ shop bấm
            "learning_model": self.meta("learning_model", ""),
            "learning_effort": self.meta("learning_effort", "high"),
        }

    def save_settings(self, payload):
        if "auto_activate" in payload:
            self.set_meta("auto_activate", "1" if payload["auto_activate"] else "0")
        if "auto_relearn" in payload:
            self.set_meta("auto_relearn", "1" if payload["auto_relearn"] else "0")
        if "learning_model" in payload:
            model = clean(payload.get("learning_model"), 120)
            if any(ch in model for ch in "\r\n\0"):
                raise ValueError("learning_model_invalid")
            self.set_meta("learning_model", model)
        if "learning_effort" in payload:
            effort = clean(payload.get("learning_effort"), 10)
            if effort not in {"low", "high", "max"}:
                raise ValueError("learning_effort_invalid")
            self.set_meta("learning_effort", effort)
        return self.settings()

    # ---------------------------------------------------------------- items
    @staticmethod
    def _row(row):
        item = dict(row)
        item["terms"] = json.loads(item.pop("terms_json") or "[]")
        item["source_refs"] = json.loads(item.pop("source_refs_json") or "[]")
        item["supersedes"] = json.loads(item.pop("supersedes_json") or "[]")
        return item

    def items(self, status=None):
        with self._lock:
            if self._cache is None:
                with self._db() as db:
                    rows = db.execute("SELECT * FROM knowledge_items ORDER BY updated_at DESC").fetchall()
                self._cache = [self._row(r) for r in rows]
            items = self._cache
        if status:
            wanted = {status} if isinstance(status, str) else set(status)
            return [dict(i) for i in items if i["status"] in wanted and not i.get("legacy_excluded")]
        return [dict(i) for i in items]

    def item(self, item_id):
        return next((i for i in self.items() if i["item_id"] == item_id), None)

    def add_item(self, *, kind, statement, product_id="", persona_id="", terms=(),
                 status="suggested", confidence=.6, source="manual", source_refs=(),
                 created_by="", supersedes=(), run_id="", db=None, customer_key_hmac=""):
        kind = clean(kind, 20)
        if kind not in KINDS:
            raise ValueError("learning_kind_invalid")
        statement = clean(statement, MAX_STATEMENT_CHARS)
        if len(statement) < 4:
            raise ValueError("learning_statement_required")
        if status not in STATUSES:
            raise ValueError("learning_status_invalid")
        terms = _clean_terms(terms)
        if kind == "alias" and (not terms or not product_id):
            raise ValueError("alias_needs_terms_and_product")
        now = _now()
        item_id = uuid.uuid4().hex
        values = (item_id, kind, clean(product_id, 160), clean(persona_id, 80),
                  json.dumps(terms, ensure_ascii=False), statement, status,
                  max(0.0, min(1.0, float(confidence or 0))), clean(source, 20),
                  json.dumps(list(source_refs)[:20], ensure_ascii=False), now, now,
                  clean(created_by, 80), json.dumps(list(supersedes)[:20]), clean(run_id, 40),
                  clean(customer_key_hmac, 80))
        sql = ("INSERT INTO knowledge_items(item_id,kind,product_id,persona_id,terms_json,statement,status,"
               "confidence,source,source_refs_json,created_at,updated_at,created_by,supersedes_json,run_id,customer_key_hmac) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
        if db is not None:
            db.execute(sql, values)
        else:
            with self._db() as conn:
                conn.execute(sql, values)
        self._bump()
        return item_id

    def update_item(self, item_id, changes):
        current = self.item(item_id)
        if not current:
            raise ValueError("learning_item_not_found")
        fields, values = [], []
        if "statement" in changes:
            statement = clean(changes["statement"], MAX_STATEMENT_CHARS)
            if len(statement) < 4:
                raise ValueError("learning_statement_required")
            fields.append("statement=?"); values.append(statement)
        if "terms" in changes:
            fields.append("terms_json=?"); values.append(json.dumps(_clean_terms(changes["terms"]), ensure_ascii=False))
        if "product_id" in changes:
            fields.append("product_id=?"); values.append(clean(changes["product_id"], 160))
        if "kind" in changes:
            if changes["kind"] not in KINDS:
                raise ValueError("learning_kind_invalid")
            fields.append("kind=?"); values.append(changes["kind"])
        if "status" in changes:
            if changes["status"] not in STATUSES:
                raise ValueError("learning_status_invalid")
            fields.append("status=?"); values.append(changes["status"])
        if not fields:
            return current
        after = {**current, **{k: v for k, v in changes.items() if k in {"kind", "product_id", "status"}}}
        if "terms" in changes:
            after["terms"] = _clean_terms(changes["terms"])
        if after["kind"] == "alias" and (not after.get("terms") or not after.get("product_id")):
            raise ValueError("alias_needs_terms_and_product")
        fields.append("updated_at=?"); values.append(_now())
        with self._db() as db:
            db.execute(f"UPDATE knowledge_items SET {','.join(fields)} WHERE item_id=?", (*values, item_id))
        self._bump()
        return self.item(item_id)

    def set_status(self, item_ids, status):
        if status not in STATUSES:
            raise ValueError("learning_status_invalid")
        ids = [clean(i, 40) for i in item_ids or [] if clean(i, 40)]
        if not ids:
            return 0
        with self._db() as db:
            count = db.executemany("UPDATE knowledge_items SET status=?,updated_at=? WHERE item_id=?",
                                   [(status, _now(), i) for i in ids]).rowcount
        self._bump()
        return count

    def delete_items(self, item_ids):
        """Xoá HẲN (không phải tắt) — cho mục rác/sai không muốn giữ lịch sử."""
        ids = [clean(i, 40) for i in item_ids or [] if clean(i, 40)]
        if not ids:
            return 0
        with self._db() as db:
            count = db.executemany("DELETE FROM knowledge_items WHERE item_id=?", [(i,) for i in ids]).rowcount
        self._bump()
        return count

    def delete_runs(self, run_ids=None, *, finished_only=True):
        ids = [clean(i, 40) for i in run_ids or [] if clean(i, 40)]
        with self._db() as db:
            if ids:
                count = db.executemany("DELETE FROM learning_runs WHERE run_id=? AND status NOT IN ('queued','running')",
                                       [(i,) for i in ids]).rowcount
            else:
                count = db.execute("DELETE FROM learning_runs WHERE status IN ('error','discarded','reverted')"
                                   + ("" if finished_only else " OR status='applied'")).rowcount
        return count

    def mark_used(self, item_ids):
        ids = [i for i in item_ids if i]
        if not ids:
            return
        now = _now()
        try:
            with self._db() as db:
                db.executemany("UPDATE knowledge_items SET use_count=use_count+1,last_used_at=? WHERE item_id=?",
                               [(now, i) for i in ids])
        except sqlite3.Error:
            return            # đếm lượt dùng không được phép làm hỏng lượt trả lời
        with self._lock:
            for item in self._cache or []:
                if item["item_id"] in ids:
                    item["use_count"] += 1
                    item["last_used_at"] = now

    # ---------------------------------------------------------------- truy xuất
    @staticmethod
    def _term_hit(term, folded_padded, compact_comment):
        ft = fold(term).strip()
        if not ft:
            return False
        # Khớp theo ranh giới từ ("win gold 20") HOẶC dạng viết liền ("wingold20").
        if f" {ft} " in folded_padded:
            return True
        c = compact(term)
        return len(c) >= 5 and c in compact_comment

    def matches(self, comment, statuses=("active",)):
        """Các mục có từ khoá xuất hiện trong câu khách: [(item, term)]."""
        folded_padded = " " + " ".join(re.findall(r"[a-z0-9]+", fold(comment))) + " "
        compact_comment = compact(comment)
        hits = []
        for item in self.items(statuses):
            for term in item["terms"]:
                if self._term_hit(term, folded_padded, compact_comment):
                    hits.append((item, term))
                    break
        return hits

    def alias_products(self, comment, titles=None):
        """product_id theo thứ tự tin cậy mà câu khách gọi bằng tên khác.

        ``titles`` (tên các SKU đang bán) để bỏ qua từ khoá mơ hồ — trùng tên
        sản phẩm khác thì không được phép tự quyết sản phẩm.
        """
        hits = [(item, term) for item, term in self.matches(comment) if item["kind"] == "alias"
                and not (titles and ambiguous_terms([term], titles, item["product_id"]))]
        hits.sort(key=lambda pair: (len(compact(pair[1])), pair[0]["confidence"]), reverse=True)
        out = []
        for item, _term in hits:
            if item["product_id"] and item["product_id"] not in out:
                out.append(item["product_id"])
        return out

    def relevant(self, comment, product_ids=(), persona_id="", limit=MAX_FACTS_PER_TURN):
        """Kiến thức nên đưa vào lượt trả lời này (không gồm quy tắc chung).

        Thứ tự ưu tiên: (1) mục có từ khoá khách vừa nhắc; (2) dữ kiện của
        đúng sản phẩm đang được hỏi; (3) dữ kiện của shop trùng ý câu hỏi.
        """
        product_ids = [p for p in product_ids if p]
        wanted = informative_tokens(comment)
        chosen, seen = [], set()

        def take(item, why, score):
            if item["item_id"] in seen:
                return
            seen.add(item["item_id"])
            chosen.append((score, why, item))

        for item, term in self.matches(comment):
            if item["kind"] == "rule" and not item["product_id"]:
                continue                      # quy tắc chung đã nằm trong phần cố định
            take(item, f"khách nhắc “{term}”", 3 + item["confidence"])
        active = self.items("active")
        for item in active:
            if item["kind"] == "rule" and not item["product_id"]:
                continue
            if item["product_id"] and item["product_id"] in product_ids:
                overlap = len(wanted & informative_tokens(item["statement"] + " " + " ".join(item["terms"])))
                take(item, "đúng sản phẩm đang hỏi", 2 + min(1, overlap * .25) + item["confidence"] * .5)
            elif not item["product_id"] and wanted:
                tokens = informative_tokens(item["statement"] + " " + " ".join(item["terms"]))
                overlap = len(wanted & tokens)
                if overlap >= 2 or (overlap and overlap / max(1, len(wanted)) >= .5):
                    take(item, "trùng ý câu hỏi", 1 + overlap * .3)
        chosen.sort(key=lambda row: row[0], reverse=True)
        return [{"id": item["item_id"], "loai": item["kind"], "san_pham_id": item["product_id"],
                 "noi_dung": item["statement"], "ly_do_dua_vao": why}
                for _score, why, item in chosen[:max(0, int(limit))]]

    # Dữ kiện nào chủ shop dạy được thì đủ căn cứ cho loại câu hỏi đó.
    # KHÔNG có liều dùng/giá: hai thứ đó chỉ lấy từ nhãn/giỏ hàng.
    GATE_PATTERNS = {
        "pha_chung": r"\b(?:pha chung|tron|phun chung|ket hop|dung chung|tuong thich|khong pha)\b",
        "chuong_trinh": r"\b(?:khuyen mai|tang kem|qua tang|chuong trinh|uu dai|mua \d+ tang|freeship|giam)\b",
        "khuyen_mai_tang_kem": r"\b(?:tang kem|qua tang|mua \d+ tang|tang \d+)\b",
        "thoi_diem_sau_trong": r"\b(?:sau (?:khi )?trong|moi trong|cay con|ngay sau)\b",
    }

    def covers(self, missing_field, product_ids):
        """Có mục kiến thức ĐANG DÙNG của đúng sản phẩm nói về ``missing_field`` không."""
        pattern = self.GATE_PATTERNS.get(missing_field)
        wanted = {p for p in product_ids if p}
        if not pattern or not wanted:
            return []
        return [i for i in self.items("active") if i["kind"] == "fact" and i["product_id"] in wanted
                and re.search(pattern, fold(i["statement"] + " " + " ".join(i["terms"])))]

    def rules(self, persona_id=""):
        rules = [i for i in self.items("active") if i["kind"] == "rule" and not i["product_id"]
                 and (not i["persona_id"] or i["persona_id"] == persona_id)]
        rules.sort(key=lambda i: (i["confidence"], i["use_count"]), reverse=True)
        return [i["statement"] for i in rules[:MAX_RULES]]

    # ---------------------------------------------------------------- personas
    def personas(self):
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM personas ORDER BY created_at").fetchall()]

    def persona(self, persona_id=""):
        people = self.personas()
        if persona_id:
            found = next((p for p in people if p["persona_id"] == persona_id), None)
            if found:
                return found
        return next((p for p in people if p["is_active"]), people[0] if people else None)

    def save_persona(self, payload):
        persona_id = clean(payload.get("persona_id"), 80)
        name = clean(payload.get("name"), 80)
        description = clean(payload.get("description"), 600)
        style = payload.get("style_guide")
        with self._db() as db:
            if persona_id:
                row = db.execute("SELECT * FROM personas WHERE persona_id=?", (persona_id,)).fetchone()
                if not row:
                    raise ValueError("persona_not_found")
                db.execute("UPDATE personas SET name=?,description=? WHERE persona_id=?",
                           (name or row["name"], description if "description" in payload else row["description"], persona_id))
                if style is not None:
                    db.execute("UPDATE personas SET style_guide=?,style_updated_at=? WHERE persona_id=?",
                               (multiline(style, MAX_STYLE_CHARS), _now(), persona_id))
            else:
                if not name:
                    raise ValueError("persona_name_required")
                persona_id = uuid.uuid4().hex[:12]
                db.execute("INSERT INTO personas(persona_id,name,description,style_guide,is_active,created_at) "
                           "VALUES (?,?,?,?,0,?)", (persona_id, name, description,
                                                    multiline(style or "", MAX_STYLE_CHARS), _now()))
            if payload.get("activate"):
                db.execute("UPDATE personas SET is_active=CASE WHEN persona_id=? THEN 1 ELSE 0 END", (persona_id,))
        self._bump()
        return self.persona(persona_id)

    def delete_persona(self, persona_id):
        if persona_id == "default":
            raise ValueError("persona_default_locked")
        with self._db() as db:
            was_active = db.execute("SELECT is_active FROM personas WHERE persona_id=?", (persona_id,)).fetchone()
            db.execute("DELETE FROM personas WHERE persona_id=?", (persona_id,))
            if was_active and was_active[0]:
                db.execute("UPDATE personas SET is_active=1 WHERE persona_id='default'")
        self._bump()
        return {"ok": True}

    # ---------------------------------------------------------------- runs
    def hmac_for(self, customer_key):
        """Provenance tag for a customer key; '' when no privacy service is wired."""
        if not customer_key or self.privacy is None:
            return ""
        return self.privacy.key_hmac(customer_key)

    def create_run(self, kind, *, ref_id="", payload=None, trigger="manual", customer_key_hmac=""):
        run_id = uuid.uuid4().hex
        payload = dict(payload or {})
        # Never persist the plain key; keep only the HMAC provenance tag.
        plain = payload.pop("customer_key", "")
        tag = customer_key_hmac or payload.get("customer_key_hmac") or self.hmac_for(plain)
        if tag:
            payload["customer_key_hmac"] = tag
        with self._db() as db:
            db.execute("INSERT INTO learning_runs(run_id,kind,status,created_at,ref_id,input_json,trigger,"
                       "customer_key_hmac) VALUES (?,?,?,?,?,?,?,?)",
                       (run_id, kind, "queued", _now(), clean(ref_id, 160),
                        json.dumps(payload, ensure_ascii=False), trigger, clean(tag, 80)))
        return run_id

    # ------------------------------------------------------- privacy (F02)
    _LEGACY_SQL = "customer_key_hmac='' AND source!='manual'"

    def forget_customer(self, key_hmac, *, exclude_legacy=True, identifiers=()):
        """Delete rows attributed to ``key_hmac``; hide legacy rows that mention the customer."""
        from privacy import row_mentions
        with self._db() as db:
            items = db.execute("DELETE FROM knowledge_items WHERE customer_key_hmac=? AND customer_key_hmac!=''",
                               (key_hmac,)).rowcount
            runs = db.execute("DELETE FROM learning_runs WHERE customer_key_hmac=? AND customer_key_hmac!='' "
                              "AND status NOT IN ('queued','running')", (key_hmac,)).rowcount
            excluded_items = excluded_runs = 0
            if exclude_legacy and identifiers:
                for row in db.execute("SELECT * FROM knowledge_items WHERE " + self._LEGACY_SQL
                                      + " AND legacy_excluded=0").fetchall():
                    if row_mentions(row, identifiers):
                        excluded_items += db.execute("UPDATE knowledge_items SET legacy_excluded=1 WHERE item_id=?",
                                                     (row["item_id"],)).rowcount
                for row in db.execute("SELECT * FROM learning_runs WHERE kind='distill' AND "
                                      "customer_key_hmac='' AND legacy_excluded=0").fetchall():
                    if row_mentions(row, identifiers):
                        excluded_runs += db.execute("UPDATE learning_runs SET legacy_excluded=1 WHERE run_id=?",
                                                    (row["run_id"],)).rowcount
        self._bump()
        return {"deleted": items + runs, "excluded": excluded_items + excluded_runs}

    def update_run(self, run_id, **fields):
        allowed = {"status", "finished_at", "output_json", "error", "model", "usage_json"}
        sets = [(k, v) for k, v in fields.items() if k in allowed]
        if not sets:
            return
        with self._db() as db:
            db.execute(f"UPDATE learning_runs SET {','.join(k + '=?' for k, _ in sets)} WHERE run_id=?",
                       (*[v for _, v in sets], run_id))

    def run(self, run_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM learning_runs WHERE run_id=?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    @staticmethod
    def _run_row(row):
        out = dict(row)
        out["input"] = json.loads(out.pop("input_json") or "{}")
        out["output"] = json.loads(out.pop("output_json") or "{}")
        out["usage"] = json.loads(out.pop("usage_json") or "{}")
        return out

    def runs(self, kind=None, limit=20, ref_id=""):
        sql, args = "SELECT * FROM learning_runs", []
        where = ["legacy_excluded=0"]
        if kind:
            where.append("kind=?"); args.append(kind)
        if ref_id:
            where.append("ref_id=?"); args.append(ref_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with self._db() as db:
            return [self._run_row(r) for r in db.execute(sql, args).fetchall()]

    def stats(self):
        items = self.items()
        by = {s: sum(1 for i in items if i["status"] == s) for s in STATUSES}
        kinds = {k: sum(1 for i in items if i["status"] == "active" and i["kind"] == k) for k in KINDS}
        return {"by_status": by, "active_by_kind": kinds,
                "used_total": sum(i["use_count"] for i in items if i["status"] == "active"),
                "last_relearn_at": self.meta("last_relearn_at", "")}


# ====================================================================== LLM
DISTILL_SYSTEM = """Bạn là người huấn luyện trợ lý AI trả lời bình luận livestream bán vật tư nông nghiệp.
Chủ shop vừa SỬA một câu trả lời. Nhiệm vụ: hiểu vì sao câu cũ chưa đúng và rút ra KIẾN THỨC DÙNG LẠI
ĐƯỢC cho những câu hỏi KHÁC, không phải chép lại câu trả lời.

Quy tắc:
- Chỉ rút điều có căn cứ trong câu trả lời đúng hoặc ghi chú của chủ shop. Không bịa thêm dữ kiện.
- Mỗi mục là MỘT ý ngắn, tự đứng được, viết như ghi chú nội bộ (không viết như lời nói với khách).
- kind = "alias": khách gọi sản phẩm bằng tên khác/mã/biệt danh. terms = các cách viết khách có thể dùng
  (ít nhất 1, mỗi cái đủ đặc trưng, vd "win gold 20", "wingold 20"). product_id BẮT BUỘC lấy từ danh sách
  san_pham_dang_ban.
- kind = "fact": dữ kiện về một sản phẩm (product_id) hoặc về shop (product_id rỗng). terms = từ khoá khách hay
  dùng khi hỏi đúng ý này (có thể rỗng).
- kind = "rule": cách xử lý một LOẠI tình huống, áp dụng chung. Chỉ đề xuất khi thấy rõ lỗi tư duy lặp lại được.
  Viết dạng CÓ ĐIỀU KIỆN ("khi … thì nên …"), KHÔNG dùng "luôn/phải/mọi câu" — một bản sửa hiếm khi đủ để
  thành mệnh lệnh tuyệt đối (ngoại lệ: điều CẤM như lộ thông tin nội bộ).
- Xưng hô, từ đệm, độ dài, cách mở/chốt câu là GIỌNG → ghi vào style_notes, KHÔNG tạo fact/rule.
- terms của alias phải ĐẶC TRƯNG cho sản phẩm đó: không dùng quy cách chung ("can 5l", "1 lít", "chai") hay
  từ có trong tên sản phẩm khác. Tên gọi chỉ đúng "trong ngữ cảnh" thì là fact, không phải alias.
- KHÔNG đưa giá tiền, liều lượng, % khuyến mãi thành kiến thức — những thứ đó lấy từ dữ liệu sản phẩm hiện hành.
- confidence: 0..1, mức chắc chắn rằng chủ shop thật sự muốn AI nhớ điều này.
- style_notes: 0–3 nhận xét về GIỌNG của chủ shop thấy rõ trong câu sửa (xưng hô, từ đệm, độ dài, cách chốt).
- diagnosis: một câu, AI đã hiểu sai/thiếu ở đâu.
- existing_item_ids_to_retire: id các mục kiến thức hiện có bị câu sửa này chứng minh là sai (thường rỗng).

Trả đúng một JSON object:
{"diagnosis":"...","items":[{"kind":"alias|fact|rule","product_id":"","terms":["..."],"statement":"...","confidence":0.8}],
 "style_notes":["..."],"existing_item_ids_to_retire":[]}"""

RELEARN_SYSTEM = """Bạn là người biên tập "trí nhớ" của trợ lý AI trả lời bình luận livestream bán vật tư nông nghiệp.
Đầu vào: kiến thức AI đang nhớ (có id), các bản sửa của chủ shop (câu khách, câu AI đã nói, câu chủ shop sửa, ghi chú,
người sửa), danh sách sản phẩm đang bán, các phong cách người trực.

Hãy HỌC LẠI TOÀN BỘ:
1. Gộp các mục trùng/na ná thành một mục rõ nhất (liệt kê id gốc ở from_ids). Giữ nguyên mục đã tốt (from_ids=[id đó],
   statement giữ nguyên hoặc sửa câu cho rõ hơn).
2. Rút thêm kiến thức mới từ các bản sửa mà chưa có mục nào nắm được (from_ids rỗng).
3. Mục sai hoặc bị bản sửa MỚI HƠN phủ định → đưa id vào retire_ids.
4. Hai mục mâu thuẫn mà không đủ căn cứ để chọn → conflicts (giữ cả hai, giải thích ngắn để chủ shop quyết).
5. Với mỗi phong cách có bản sửa, viết lại style_guide: 4–8 gạch đầu dòng NGẮN mô tả cách người đó nói
   (xưng hô với khách, tự xưng, từ đệm hay dùng, độ dài, cách mở/chốt, điều tránh). Chỉ dựa vào câu họ đã sửa;
   không có bản sửa thì bỏ qua phong cách đó.
6. Quy tắc (rule) cùng chủ đề (chốt đơn, hỏi thêm cây trồng, nhắc ưu đãi…) GỘP thành một, tối đa 8 rule tất cả.
   Viết dạng có điều kiện "khi … thì nên …", không "luôn/phải" (trừ điều cấm). Mục nói về xưng hô/giọng/cách chốt
   → đưa nội dung vào style_guide rồi cho id vào retire_ids.
7. style_guide mô tả XU HƯỚNG của người đó ("hay…", "thường…"), không phải mệnh lệnh; ghi rõ biến tấu theo khách.
8. alias có terms là quy cách chung ("can 5l", "5 lít") hoặc trùng tên sản phẩm khác → đổi thành fact không terms.
Quy tắc như cũ: không bịa dữ kiện; không đưa giá/liều/% khuyến mãi vào kiến thức; alias phải có product_id có thật
trong danh sách; mỗi mục một ý; kind ∈ alias|fact|rule.

Trả đúng một JSON object:
{"summary":"1–2 câu tóm tắt đã học được gì",
 "items":[{"from_ids":["..."],"kind":"...","product_id":"","terms":["..."],"statement":"...","confidence":0.8}],
 "retire_ids":["..."],"conflicts":[{"ids":["...","..."],"explain":"..."}],
 "personas":[{"persona_id":"...","style_guide":"- ...\\n- ..."}]}"""


def _parse_json(text):
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Model đôi khi thêm chữ trước/sau: lấy đúng khối {...} ngoài cùng.
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


class Learner:
    """Chạy các lượt học trên một luồng nền (không chặn Hộp thư / trả lời)."""

    def __init__(self, store, *, responder_provider, catalog_provider, examples_provider=None,
                 activity_provider=None):
        self.store = store
        self._responder = responder_provider      # () -> DeepSeekResponder | None
        self._catalog = catalog_provider          # () -> [{product_id, bag_index, name, title}]
        self._examples = examples_provider or (lambda: [])
        self._activity = activity_provider or (lambda: None)   # () -> iso của bình luận gần nhất
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------ luồng nền
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="ai-learning", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self._queue.put(None)

    def _loop(self):
        last_auto_check = 0.0
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=30)
            except queue.Empty:
                job = None
            if job:
                kind, run_id = job
                try:
                    (self._run_distill if kind == "distill" else self._run_relearn)(run_id)
                except Exception as exc:        # một lượt hỏng không được giết luồng
                    from problems import report_problem
                    report_problem("learning_failed", f"Lượt {kind} {run_id} lỗi", exc)
                    self.store.update_run(run_id, status="error", finished_at=_now(),
                                          error=f"{type(exc).__name__}: {exc}"[:500])
            if time.monotonic() - last_auto_check > 600:
                last_auto_check = time.monotonic()
                try:
                    self.maybe_auto_relearn()
                except Exception:
                    pass

    def _enqueue(self, kind, run_id, sync=False):
        if sync:
            (self._run_distill if kind == "distill" else self._run_relearn)(run_id)
        else:
            self.start()
            self._queue.put((kind, run_id))

    # ------------------------------------------------------------ gọi model
    def _client(self):
        responder = self._responder() if callable(self._responder) else None
        client = getattr(responder, "client", None)
        if client is None:
            return None, ""
        settings = self.store.settings()
        model = settings["learning_model"] or getattr(responder, "model", "") or "deepseek-flash"
        return client, model

    def _call(self, system, user, *, timeout=150, max_tokens=8000):
        client, model = self._client()
        if client is None:
            raise RuntimeError("no_model_client")
        effort = self.store.settings()["learning_effort"]
        try:
            client = client.with_options(timeout=timeout)
        except Exception:
            pass
        started = time.monotonic()
        request = {"model": model, "messages": [{"role": "system", "content": system},
                                                {"role": "user", "content": user}],
                   "max_tokens": max_tokens, "response_format": {"type": "json_object"},
                   "reasoning_effort": effort,
                   "extra_body": {"thinking": {"type": "enabled"}}}
        try:
            response = client.chat.completions.create(**request)
        except Exception:
            # Một số model/tài khoản không nhận chế độ suy luận: thử lại không suy luận.
            request.pop("reasoning_effort", None)
            request["extra_body"] = {"thinking": {"type": "disabled"}}
            request["temperature"] = 0.2
            response = client.chat.completions.create(**request)
        usage = getattr(response, "usage", None)
        details = getattr(usage, "completion_tokens_details", None)
        meta = {"model": model, "ms": int((time.monotonic() - started) * 1000),
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                "reasoning_tokens": getattr(details, "reasoning_tokens", 0) or 0}
        choice = response.choices[0]
        content = choice.message.content or ""
        try:
            return _parse_json(content), meta
        except json.JSONDecodeError:
            if "reasoning_effort" not in request:
                raise ValueError(f"model_json_invalid (finish={getattr(choice, 'finish_reason', '')}, "
                                 f"{len(content)} ký tự)") from None
        # Suy luận ăn hết hạn mức (finish=length) hoặc JSON hỏng: thử lại một lần,
        # tắt suy luận — chậm/kém sâu hơn một chút nhưng chắc có kết quả.
        meta["retried_without_thinking"] = getattr(choice, "finish_reason", "")
        request.pop("reasoning_effort", None)
        request["extra_body"] = {"thinking": {"type": "disabled"}}
        request["temperature"] = 0.2
        response = client.chat.completions.create(**request)
        choice = response.choices[0]
        content = choice.message.content or ""
        usage = getattr(response, "usage", None)
        meta["ms"] = int((time.monotonic() - started) * 1000)
        meta["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
        try:
            return _parse_json(content), meta
        except json.JSONDecodeError:
            raise ValueError(f"model_json_invalid (finish={getattr(choice, 'finish_reason', '')}, "
                             f"{len(content)} ký tự)") from None

    def _catalog_brief(self):
        rows = []
        for p in self._catalog() or []:
            pid = clean(p.get("product_id"), 160)
            if pid:
                rows.append({"product_id": pid, "gio": p.get("bag_index") or 0,
                             "ten": clean(p.get("name") or p.get("short_name") or p.get("title"), 90),
                             "tieu_de": clean(p.get("title"), 160)})
        return rows

    def _valid_product(self, product_id, catalog_ids):
        pid = clean(product_id, 160)
        return pid if pid in catalog_ids else ""

    # ------------------------------------------------------------ rút kiến thức từ 1 bản sửa
    def submit_distill(self, payload, *, sync=False):
        run_id = self.store.create_run("distill", ref_id=payload.get("conversation_id", ""), payload=payload,
                                       trigger="correction")
        self._enqueue("distill", run_id, sync=sync)
        return run_id

    def _run_distill(self, run_id):
        run = self.store.run(run_id)
        payload = run["input"]
        self.store.update_run(run_id, status="running")
        catalog = self._catalog_brief()
        catalog_ids = {c["product_id"] for c in catalog}
        product_id = clean(payload.get("product_id"), 160)
        existing = [{"id": i["item_id"], "kind": i["kind"], "product_id": i["product_id"],
                     "statement": i["statement"], "terms": i["terms"]}
                    for i in self.store.items(("active", "suggested"))
                    if not product_id or i["product_id"] in {product_id, ""}][:40]
        result, meta, used_model = None, {}, True
        try:
            user = json.dumps({
                "cau_khach": payload.get("question"), "cau_khach_viet_lai": payload.get("corrected_question"),
                "cau_ai_da_tra_loi": payload.get("ai_reply"), "cau_tra_loi_dung_cua_chu_shop": payload.get("answer"),
                "ghi_chu_cua_chu_shop": payload.get("note"), "loai_cau_hoi": payload.get("question_type"),
                "san_pham_chu_shop_chon": next((c for c in catalog if c["product_id"] == product_id), None),
                "kien_thuc_hien_co": existing, "san_pham_dang_ban": catalog,
            }, ensure_ascii=False)
            result, meta = self._call(DISTILL_SYSTEM, user, timeout=90)
        except Exception as exc:
            used_model = False
            meta = {"fallback": type(exc).__name__}
            result = self.heuristic_distill(payload, catalog)
        titles = {c["product_id"]: f"{c['ten']} {c['tieu_de']}" for c in catalog}
        items = self._store_suggestions(result.get("items") or [], catalog_ids, payload, run_id,
                                        default_product=product_id, titles=titles)
        retire = [i for i in (result.get("existing_item_ids_to_retire") or []) if self.store.item(str(i))]
        output = {"diagnosis": clean(result.get("diagnosis"), 400), "item_ids": items,
                  "style_notes": [clean(s, 200) for s in (result.get("style_notes") or [])[:3] if clean(s, 200)],
                  "retire_suggestions": retire, "used_model": used_model}
        self.store.update_run(run_id, status="done", finished_at=_now(),
                              output_json=json.dumps(output, ensure_ascii=False),
                              model=meta.get("model", ""), usage_json=json.dumps(meta))
        return output

    def _store_suggestions(self, raw_items, catalog_ids, payload, run_id, default_product="", titles=None,
                           *, source="correction", max_items=6, allow_kinds=KINDS):
        auto = self.store.settings()["auto_activate"]
        known = {(i["kind"], i["product_id"], fold(i["statement"])) for i in self.store.items(("active", "suggested"))}
        known_alias = {(i["product_id"], compact(t)) for i in self.store.items(("active", "suggested"))
                       if i["kind"] == "alias" for t in i["terms"]}
        ids = []
        for raw in raw_items[:max_items]:
            if not isinstance(raw, dict):
                continue
            kind = clean(raw.get("kind"), 10)
            if kind not in KINDS or kind not in allow_kinds:
                continue
            product_id = self._valid_product(raw.get("product_id"), catalog_ids)
            if kind == "fact" and not product_id and raw.get("product_id"):
                product_id = default_product if default_product in catalog_ids else ""
            statement = clean(raw.get("statement"), MAX_STATEMENT_CHARS)
            terms = _clean_terms(raw.get("terms") or [])
            if kind == "alias":
                terms = [t for t in terms if (product_id, compact(t)) not in known_alias
                         and not ambiguous_terms([t], titles or {}, product_id)]
                if not product_id:
                    continue
                if not terms:
                    # Chỉ còn từ khoá chung ("can 5l") → giữ ý như một dữ kiện, không tự chọn SKU.
                    kind = "fact"
            if len(statement) < 4 or (kind, product_id, fold(statement)) in known:
                continue
            try:
                confidence = float(raw.get("confidence") or .6)
            except (TypeError, ValueError):
                confidence = .6
            ids.append(self.store.add_item(
                kind=kind, statement=statement, product_id=product_id, terms=terms,
                status="active" if auto and confidence >= .75 else "suggested",
                confidence=confidence, source=source,
                source_refs=[payload.get("conversation_id", "")], created_by=payload.get("persona_id", ""),
                run_id=run_id,
                customer_key_hmac=payload.get("customer_key_hmac") or self.store.hmac_for(payload.get("customer_key"))))
        return ids

    # Ghi chú dạng "X là Y" / "X = Y" / "X chính là Y": không cần model vẫn học được tên gọi khác.
    _ALIAS_NOTE = re.compile(r"^\s*(?P<a>[^,.;:\n]{3,40}?)\s+(?:chính là|tức là|là|=|->|→)\s+(?P<b>[^\n]{3,120})$", re.I)

    def heuristic_distill(self, payload, catalog=None):
        catalog = catalog if catalog is not None else self._catalog_brief()
        note = clean(payload.get("note"), 300)
        items = []
        m = self._ALIAS_NOTE.match(note or "")
        if m:
            alias, target = m.group("a").strip(), m.group("b").strip()
            target_tokens = informative_tokens(target)
            best, best_score = None, 0.0
            for c in catalog:
                tokens = informative_tokens(c["ten"] + " " + c["tieu_de"][:70])
                score = len(target_tokens & tokens) / max(1, len(target_tokens))
                if score > best_score:
                    best, best_score = c, score
            if best and best_score >= .5 and _term_ok(alias):
                items.append({"kind": "alias", "product_id": best["product_id"],
                              "terms": [alias, compact(alias)] if " " in alias else [alias],
                              "statement": f"“{alias}” là tên khách gọi {best['ten']}.", "confidence": .7})
        if note and not items and len(note) >= 8:
            items.append({"kind": "fact", "product_id": payload.get("product_id") or "",
                          "terms": [], "statement": note, "confidence": .5})
        return {"diagnosis": "", "items": items, "style_notes": []}

    # ------------------------------------------------------------ học lại toàn bộ
    def submit_relearn(self, *, trigger="manual", sync=False):
        running = [r for r in self.store.runs("relearn", limit=3) if r["status"] in {"queued", "running"}]
        if running:
            return running[0]["run_id"]
        run_id = self.store.create_run("relearn", trigger=trigger)
        self._enqueue("relearn", run_id, sync=sync)
        return run_id

    def maybe_auto_relearn(self):
        """Sau phiên LIVE (≥ 30 phút không có bình luận) và có ≥ 3 bản dạy mới."""
        if not self.store.settings()["auto_relearn"]:
            return None
        last_comment = self._activity()
        if last_comment:
            try:
                idle = time.time() - datetime.fromisoformat(str(last_comment)).timestamp()
            except ValueError:
                idle = 0
            if idle < 1800:
                return None
        # Mốc = lượt học lại gần nhất (kể cả lượt chưa duyệt). Trước đây chỉ lấy mốc
        # lúc chủ shop DUYỆT, nên đề xuất không ai duyệt khiến app tự gọi model mỗi 10 phút.
        relearns = self.store.runs("relearn", limit=20)
        if any(r["status"] in ("queued", "running", "done") for r in relearns):
            return None                      # đang chạy hoặc còn đề xuất chờ duyệt
        last_run = max((r["created_at"] for r in relearns), default="")
        if last_run:
            try:
                if time.time() - datetime.fromisoformat(last_run).timestamp() < 86400:
                    return None              # tự học lại tối đa một lần mỗi 24 giờ
            except ValueError:
                return None
        since = max(self.store.meta("last_relearn_at", ""), last_run)
        fresh = [r for r in self.store.runs("distill", limit=200) if r["created_at"] > since]
        if len(fresh) < 3:
            return None
        return self.submit_relearn(trigger="auto")

    def _run_relearn(self, run_id):
        self.store.update_run(run_id, status="running")
        catalog = self._catalog_brief()
        catalog_ids = {c["product_id"] for c in catalog}
        items = self.store.items(("active", "suggested"))
        # Bản sửa trước khi có "người trực" thuộc về giọng mặc định — nếu không,
        # model bỏ qua bước viết lại phong cách vì "không bản sửa nào gắn persona".
        examples = [{**ex, "persona_id": ex.get("persona_id") or "default"}
                    for ex in list(self._examples() or [])[:120]]
        personas = self.store.personas()
        user = json.dumps({
            "kien_thuc": [{"id": i["item_id"], "kind": i["kind"], "product_id": i["product_id"],
                           "terms": i["terms"], "statement": i["statement"], "status": i["status"],
                           "use_count": i["use_count"], "updated_at": i["updated_at"]} for i in items],
            "ban_sua": examples, "san_pham_dang_ban": catalog,
            "phong_cach": [{"persona_id": p["persona_id"], "name": p["name"], "description": p["description"],
                            "style_guide_hien_tai": p["style_guide"]} for p in personas],
        }, ensure_ascii=False)
        try:
            result, meta = self._call(RELEARN_SYSTEM, user, timeout=300, max_tokens=16000)
        except Exception as exc:
            self.store.update_run(run_id, status="error", finished_at=_now(),
                                  error=f"Không gọi được model: {type(exc).__name__} {str(exc)[:160]}")
            return None
        titles = {c["product_id"]: f"{c['ten']} {c['tieu_de']}" for c in catalog}
        proposal = self._proposal(result, items, catalog_ids, personas, titles)
        self.store.update_run(run_id, status="done", finished_at=_now(),
                              output_json=json.dumps(proposal, ensure_ascii=False),
                              model=meta.get("model", ""), usage_json=json.dumps(meta))
        return proposal

    def _proposal(self, result, items, catalog_ids, personas, titles=None):
        by_id = {i["item_id"]: i for i in items}
        out_items, touched = [], set()
        for raw in (result.get("items") or [])[:200]:
            if not isinstance(raw, dict):
                continue
            kind = clean(raw.get("kind"), 10)
            statement = clean(raw.get("statement"), MAX_STATEMENT_CHARS)
            if kind not in KINDS or len(statement) < 4:
                continue
            product_id = self._valid_product(raw.get("product_id"), catalog_ids)
            terms = _clean_terms(raw.get("terms") or [])
            if kind == "alias":
                terms = [t for t in terms if not ambiguous_terms([t], titles or {}, product_id)]
                if not product_id:
                    continue
                if not terms:
                    kind = "fact"
            from_ids = [str(i) for i in (raw.get("from_ids") or []) if str(i) in by_id][:20]
            touched.update(from_ids)
            if len(from_ids) == 1:
                old = by_id[from_ids[0]]
                same = (fold(old["statement"]) == fold(statement) and old["kind"] == kind
                        and old["product_id"] == product_id
                        and sorted(map(compact, old["terms"])) == sorted(map(compact, terms)))
                action = "keep" if same and old["status"] == "active" else "revise"
            else:
                action = "merge" if from_ids else "new"
            try:
                confidence = max(0.0, min(1.0, float(raw.get("confidence") or .7)))
            except (TypeError, ValueError):
                confidence = .7
            out_items.append({"action": action, "from_ids": from_ids, "kind": kind, "product_id": product_id,
                              "terms": terms, "statement": statement, "confidence": confidence})
        retire = [i for i in (result.get("retire_ids") or []) if i in by_id and i not in touched]
        conflicts = [{"ids": [i for i in (c.get("ids") or []) if i in by_id], "explain": clean(c.get("explain"), 300)}
                     for c in (result.get("conflicts") or []) if isinstance(c, dict)]
        persona_ids = {p["persona_id"]: p for p in personas}
        styles = []
        for raw in result.get("personas") or []:
            pid = str((raw or {}).get("persona_id") or "")
            guide = multiline((raw or {}).get("style_guide"), MAX_STYLE_CHARS)
            if pid in persona_ids and guide and guide != persona_ids[pid]["style_guide"]:
                styles.append({"persona_id": pid, "name": persona_ids[pid]["name"],
                               "before": persona_ids[pid]["style_guide"], "after": guide})
        untouched = [i for i in items if i["item_id"] not in touched and i["item_id"] not in retire]
        return {"summary": clean(result.get("summary"), 500), "items": out_items, "retire_ids": retire,
                "conflicts": [c for c in conflicts if len(c["ids"]) >= 2], "personas": styles,
                "untouched_ids": [i["item_id"] for i in untouched],
                "counts": {"keep": sum(1 for i in out_items if i["action"] == "keep"),
                           "revise": sum(1 for i in out_items if i["action"] == "revise"),
                           "merge": sum(1 for i in out_items if i["action"] == "merge"),
                           "new": sum(1 for i in out_items if i["action"] == "new"),
                           "retire": len(retire)}}

    def apply_relearn(self, run_id, *, include_styles=True):
        run = self.store.run(run_id)
        if not run or run["kind"] != "relearn":
            raise ValueError("learning_run_not_found")
        if run["status"] != "done":
            raise ValueError("learning_run_not_ready" if run["status"] != "applied" else "learning_run_already_applied")
        proposal = run["output"]
        before_items = {i["item_id"]: i["status"] for i in self.store.items()}
        before_styles = {p["persona_id"]: p["style_guide"] for p in self.store.personas()}
        created, retired = [], []
        for entry in proposal.get("items") or []:
            if entry["action"] == "keep":
                continue
            if entry["action"] == "revise" and entry["from_ids"]:
                old = self.store.item(entry["from_ids"][0])
                if old:
                    self.store.update_item(old["item_id"], {"statement": entry["statement"], "terms": entry["terms"],
                                                            "kind": entry["kind"], "product_id": entry["product_id"],
                                                            "status": "active"})
                    continue
            new_id = self.store.add_item(kind=entry["kind"], statement=entry["statement"],
                                         product_id=entry["product_id"], terms=entry["terms"], status="active",
                                         confidence=entry["confidence"], source="relearn",
                                         supersedes=entry["from_ids"], run_id=run_id)
            created.append(new_id)
            retired += entry["from_ids"]
        retired += proposal.get("retire_ids") or []
        self.store.set_status(sorted(set(retired)), "retired")
        if include_styles:
            for style in proposal.get("personas") or []:
                try:
                    self.store.save_persona({"persona_id": style["persona_id"], "style_guide": style["after"]})
                except ValueError:
                    pass
        undo = {"statuses": before_items, "styles": before_styles, "created": created}
        self.store.update_run(run_id, status="applied",
                              output_json=json.dumps({**proposal, "undo": undo}, ensure_ascii=False))
        self.store.set_meta("last_relearn_at", _now())
        return {"ok": True, "created": len(created), "retired": len(set(retired))}

    def undo_relearn(self, run_id):
        run = self.store.run(run_id)
        if not run or run["status"] != "applied":
            raise ValueError("learning_run_not_applied")
        undo = run["output"].get("undo") or {}
        statuses = undo.get("statuses") or {}
        for item_id in undo.get("created") or []:
            self.store.set_status([item_id], "retired")
        by_status = {}
        for item_id, status in statuses.items():
            by_status.setdefault(status, []).append(item_id)
        for status, ids in by_status.items():
            self.store.set_status(ids, status)
        for pid, guide in (undo.get("styles") or {}).items():
            try:
                self.store.save_persona({"persona_id": pid, "style_guide": guide})
            except ValueError:
                pass
        self.store.update_run(run_id, status="reverted")
        return {"ok": True}

    def discard_run(self, run_id):
        run = self.store.run(run_id)
        if not run:
            raise ValueError("learning_run_not_found")
        if run["status"] == "done":
            self.store.update_run(run_id, status="discarded")
        return {"ok": True}
