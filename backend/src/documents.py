"""Tài liệu chủ shop tải lên để AI đọc: giới thiệu công ty, sản phẩm, kiến thức nông nghiệp.

Hai cách AI dùng tài liệu:

1. **Mỗi câu trả lời**: tìm vài đoạn liên quan nhất tới câu khách hỏi và đưa vào
   ngữ cảnh (``tai_lieu_lien_quan``). Không cần làm gì thêm sau khi tải lên.
2. **Rút kiến thức** (bấm nút): DeepSeek đọc toàn bộ và đề xuất các dữ kiện ngắn vào
   Trí nhớ AI để chủ shop duyệt — dữ kiện đã duyệt được dùng như điều chủ shop dạy.

Giá, khuyến mãi và liều pha KHÔNG lấy từ tài liệu: những thứ đó luôn theo hồ sơ sản
phẩm và giỏ hàng hiện hành (tài liệu có thể đã cũ).
"""
from __future__ import annotations

import html
import io
import json
import math
import re
import shutil
import threading
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from knowledge import clean, tokens

CATEGORIES = {"cong_ty": "Về công ty / shop", "san_pham": "Sản phẩm", "nong_nghiep": "Kiến thức nông nghiệp",
              "khac": "Khác"}
EXTENSIONS = {".txt", ".md", ".csv", ".docx", ".pdf", ".html", ".htm"}
MAX_BYTES = 30 * 1024 * 1024
MAX_TEXT = 2_000_000
CHUNK_CHARS = 700
MAX_RESULTS = 3
# Từ để hỏi / từ chung: trùng những từ này không có nghĩa là đoạn đó liên quan.
_STOP = frozenset("""gia bao nhieu la nay do kia co khong ko minh em anh chi oi a the sao nao voi cho hoi ship
ban shop nha ne vay duoc con hang mua dat tien ma va roi chua di toi can giup xin tu khi lam gi dung cai loai
thu xem coi biet noi hay nen moi cua cac nhung mot hai ba trong ngoai tren duoi rat nhieu it hon nhat cung
se da dang bi thi neu vi nen ra vao len xuong""".split())

DISTILL_SYSTEM = """Bạn đọc một đoạn tài liệu của shop nông nghiệp (giới thiệu công ty, sản phẩm, hoặc kiến
thức canh tác) và rút ra những DỮ KIỆN NGẮN mà người bán cần để trả lời khách trên LIVE.

Trả về JSON: {"items": [{"kind": "fact", "product_id": "", "statement": "...", "terms": [], "confidence": 0.7}]}
- Tối đa 12 mục, mỗi mục MỘT ý, ≤ 220 ký tự, tự đọc hiểu được (không "như trên", "sản phẩm này").
- product_id: chỉ điền khi dữ kiện rõ ràng nói về đúng một sản phẩm trong danh sách san_pham_dang_ban; không chắc thì để "".
- KHÔNG ghi giá, khuyến mãi, mã giảm giá, số lượng tồn kho (những thứ này đổi liên tục).
- Liều pha chỉ ghi khi tài liệu nói rõ con số cho đúng sản phẩm, chép đúng con số.
- Bỏ qua câu quảng cáo chung chung ("chất lượng hàng đầu", "uy tín").
- Không trùng với kien_thuc_hien_co. Không bịa thêm ngoài tài liệu."""


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------------ đọc file
def _decode(data):
    for encoding in ("utf-8-sig", "utf-16", "cp1258", "latin-1"):
        try:
            text = data.decode(encoding)
            if encoding == "utf-16" and not data[:2] in (b"\xff\xfe", b"\xfe\xff"):
                continue
            return text
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _docx_text(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    paragraphs = []
    for block in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        block = re.sub(r"<w:tab/>", "\t", block)
        pieces = re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", block, re.S)
        line = html.unescape("".join(pieces)).strip()
        if line:
            paragraphs.append(line)
    return "\n".join(paragraphs)


def _pdf_text(data):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("Máy thiếu thành phần đọc PDF — cài lại ứng dụng") from exc
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            continue
    text = "\n".join(pages)
    if len(text.strip()) < 20:
        raise ValueError("PDF này là ảnh chụp (không có chữ để đọc). Hãy tải bản Word/PDF có chữ, "
                         "hoặc chép nội dung vào file .txt")
    return text


def extract_text(filename, data):
    suffix = Path(filename).suffix.lower()
    if suffix not in EXTENSIONS:
        raise ValueError("Chỉ nhận file .txt, .md, .csv, .docx, .pdf, .html")
    if suffix == ".docx":
        try:
            text = _docx_text(data)
        except (zipfile.BadZipFile, KeyError) as exc:
            raise ValueError("File Word bị hỏng hoặc là định dạng .doc cũ — lưu lại thành .docx") from exc
    elif suffix == ".pdf":
        text = _pdf_text(data)
    else:
        text = _decode(data)
        if suffix in {".html", ".htm"}:
            text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
            text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", text)
            text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    if len(text) < 20:
        raise ValueError("Không đọc được chữ nào trong file này")
    return text[:MAX_TEXT]


def chunk_text(text, size=CHUNK_CHARS):
    """Gộp đoạn văn thành khúc ~700 ký tự, không cắt giữa đoạn nếu tránh được."""
    chunks, current = [], ""
    for paragraph in re.split(r"\n+", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        while len(paragraph) > size * 1.5:          # đoạn quá dài: cắt ở hết câu
            cut = paragraph.rfind(". ", 0, size)
            cut = cut + 1 if cut > size * .4 else size
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        if current and len(current) + len(paragraph) + 1 > size:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n{paragraph}".strip()
    if current:
        chunks.append(current)
    return chunks


# ------------------------------------------------------------------ thư viện
class DocumentLibrary:
    def __init__(self, root, tokenizer=None):
        self.root = Path(root)
        self.tokenize = tokenizer or tokens
        self._lock = threading.RLock()
        self._index = None                   # [(doc_id, title, category, chunk, token_set)]
        self._df = {}
        self._jobs = {}

    def _meta_path(self, doc_id):
        return self.root / doc_id / "meta.json"

    def _read_meta(self, doc_id):
        try:
            return json.loads(self._meta_path(doc_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_meta(self, meta):
        path = self._meta_path(meta["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _doc_id(self, doc_id):
        doc_id = clean(doc_id, 40)
        if not re.fullmatch(r"[0-9a-f]{12}", doc_id) or not self._meta_path(doc_id).is_file():
            raise ValueError("document_not_found")
        return doc_id

    def list(self):
        rows = []
        for path in sorted(self.root.glob("*/meta.json")):
            meta = self._read_meta(path.parent.name)
            if meta:
                meta["category_label"] = CATEGORIES.get(meta.get("category"), "Khác")
                job = self._jobs.get(meta["id"])
                if job:
                    meta["distill"] = job
                rows.append(meta)
        rows.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        return rows

    def add(self, filename, data, *, category="khac", title=""):
        if not data:
            raise ValueError("File rỗng")
        if len(data) > MAX_BYTES:
            raise ValueError("File quá lớn (tối đa 30 MB)")
        filename = Path(clean(filename, 200) or "tai-lieu.txt").name
        text = extract_text(filename, data)
        doc_id = uuid.uuid4().hex[:12]
        folder = self.root / doc_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ("original" + Path(filename).suffix.lower())).write_bytes(data)
        (folder / "text.txt").write_text(text, encoding="utf-8")
        chunks = chunk_text(text)
        meta = {"id": doc_id, "title": clean(title, 160) or Path(filename).stem, "filename": filename,
                "category": category if category in CATEGORIES else "khac", "enabled": True,
                "chars": len(text), "chunks": len(chunks), "bytes": len(data),
                "created_at": _now(), "learned_items": 0, "learned_at": ""}
        self._write_meta(meta)
        self._invalidate()
        return meta

    def detail(self, doc_id):
        doc_id = self._doc_id(doc_id)
        meta = self._read_meta(doc_id)
        text = (self.root / doc_id / "text.txt").read_text(encoding="utf-8")
        return {**meta, "category_label": CATEGORIES.get(meta.get("category"), "Khác"),
                "text": text[:30000], "truncated": len(text) > 30000, "distill": self._jobs.get(doc_id)}

    def update(self, doc_id, changes):
        doc_id = self._doc_id(doc_id)
        with self._lock:
            meta = self._read_meta(doc_id)
            if "title" in changes and clean(changes["title"], 160):
                meta["title"] = clean(changes["title"], 160)
            if changes.get("category") in CATEGORIES:
                meta["category"] = changes["category"]
            if "enabled" in changes:
                meta["enabled"] = bool(changes["enabled"])
            self._write_meta(meta)
            self._invalidate()
        return meta

    def delete(self, doc_id):
        doc_id = self._doc_id(doc_id)
        with self._lock:
            shutil.rmtree(self.root / doc_id)
            self._invalidate()
        return {"ok": True}

    # ------------------------------------------------------------ tìm đoạn liên quan
    def _invalidate(self):
        with self._lock:
            self._index = None

    def _build(self):
        index, df = [], {}
        for meta in self.list():
            if not meta.get("enabled", True):
                continue
            try:
                text = (self.root / meta["id"] / "text.txt").read_text(encoding="utf-8")
            except OSError:
                continue
            for chunk in chunk_text(text):
                words = {t for t in self.tokenize(chunk) if t not in _STOP and not t.isdigit()}
                index.append((meta["id"], meta["title"], meta.get("category", "khac"), chunk, words))
                for word in words:
                    df[word] = df.get(word, 0) + 1
        self._index, self._df = index, df

    def search(self, comment, *, extra_terms="", limit=MAX_RESULTS):
        """Đoạn tài liệu liên quan nhất tới câu khách (và tên sản phẩm đang nói tới)."""
        with self._lock:
            if self._index is None:
                self._build()
            index, df = self._index, self._df
        if not index:
            return []
        wanted = {t for t in self.tokenize(comment) if t not in _STOP and not t.isdigit()}
        product_words = {t for t in self.tokenize(extra_terms) if t not in _STOP and not t.isdigit()}
        if not wanted:
            return []
        total = len(index)
        scored = []
        for doc_id, title, category, chunk, words in index:
            hits = wanted & words
            if not hits:
                continue
            score = sum(math.log(1 + total / df.get(t, 1)) for t in hits)
            score += .5 * sum(math.log(1 + total / df.get(t, 1)) for t in product_words & words)
            # Cần ít nhất hai chữ có nghĩa trùng, hoặc một chữ rất hiếm (tên riêng, tên bệnh).
            if len(hits) >= 2 or any(df.get(t, 1) <= max(1, total // 50) and len(t) >= 4 for t in hits):
                scored.append((score, doc_id, title, category, chunk))
        scored.sort(reverse=True)
        out, seen = [], set()
        for score, doc_id, title, category, chunk in scored:
            if len(out) >= limit or score < 2.5:
                break
            if chunk in seen:
                continue
            seen.add(chunk)
            out.append({"tai_lieu": title, "loai": CATEGORIES.get(category, "Khác"), "doan": chunk,
                        "diem": round(score, 2), "doc_id": doc_id})
        return out

    def stats(self):
        docs = self.list()
        return {"documents": len(docs), "enabled": sum(1 for d in docs if d.get("enabled", True)),
                "chunks": sum(d.get("chunks", 0) for d in docs if d.get("enabled", True))}

    # ------------------------------------------------------------ rút kiến thức
    def distill(self, doc_id, learner, catalog_brief):
        doc_id = self._doc_id(doc_id)
        if (self._jobs.get(doc_id) or {}).get("state") == "running":
            raise ValueError("Tài liệu này đang được đọc")
        self._jobs[doc_id] = {"state": "running", "done": 0, "total": 0, "items": 0, "error": ""}
        threading.Thread(target=self._distill, args=(doc_id, learner, catalog_brief), daemon=True,
                         name=f"doc-distill-{doc_id}").start()
        return self._jobs[doc_id]

    def _distill(self, doc_id, learner, catalog_brief):
        job = self._jobs[doc_id]
        try:
            text = (self.root / doc_id / "text.txt").read_text(encoding="utf-8")
            meta = self._read_meta(doc_id)
            chunks = chunk_text(text, 9000)[:12]          # tối đa ~100.000 ký tự mỗi lần đọc
            job["total"] = len(chunks)
            catalog = catalog_brief()
            catalog_ids = {c["product_id"] for c in catalog}
            titles = {c["product_id"]: f"{c['ten']} {c['tieu_de']}" for c in catalog}
            created = 0
            for part in chunks:
                existing = [i["statement"] for i in learner.store.items(("active", "suggested"))][:60]
                user = json.dumps({"tai_lieu": meta["title"], "loai": CATEGORIES.get(meta.get("category")),
                                   "doan_tai_lieu": part, "san_pham_dang_ban": catalog,
                                   "kien_thuc_hien_co": existing}, ensure_ascii=False)
                result, _meta = learner._call(DISTILL_SYSTEM, user, timeout=150)
                ids = learner._store_suggestions(
                    result.get("items") or [], catalog_ids, {"conversation_id": f"tai-lieu:{doc_id}"}, "",
                    titles=titles, source="document", max_items=12, allow_kinds=("fact",))
                created += len(ids)
                job["done"] += 1
                job["items"] = created
            meta = self._read_meta(doc_id) or meta
            meta["learned_items"] = int(meta.get("learned_items") or 0) + created
            meta["learned_at"] = _now()
            self._write_meta(meta)
            job["state"] = "done"
        except Exception as exc:
            from problems import report_problem
            report_problem("document_distill_failed", f"Rút kiến thức từ tài liệu {doc_id} lỗi", exc,
                           severity="warning")
            job["state"] = "error"
            job["error"] = ("Chưa có khoá DeepSeek" if "no_model_client" in str(exc)
                            else f"{type(exc).__name__}: {exc}"[:300])
