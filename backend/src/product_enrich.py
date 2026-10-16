"""“Bổ sung bằng Google”: Gemini tra web (Google Search grounding) để ĐỀ XUẤT các trường còn thiếu.

Quy tắc:
- Chỉ đề xuất trường đang TRỐNG trong hồ sơ; không bao giờ ghi đè trường đã có.
- Mỗi trường đề xuất phải có NGUỒN lấy từ ``groundingMetadata`` (URL + tiêu đề); không có nguồn -> bỏ.
- Không bao giờ chứa giá / khuyến mãi (bộ lọc ``_scrub`` của product_ingest).
- Kết quả chỉ là đề xuất: chủ shop chọn trường rồi bấm Duyệt (``apply``), lưu qua ``workspace.patch``
  nên có lịch sử và "Khôi phục bản trước" như sửa tay.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from ai_providers import AIProviderError, GeminiProvider
from product_ingest import (LIST_FIELDS, PAIRS_FIELD, ExtractionInvalid, _as_lines, _as_pairs, _clean_str,
                            _scrub, parse_json)

ENRICH_FIELDS = ("thanh_phan_dang_ky", "cong_dung", "doi_tuong_cay_trong", "huong_dan_su_dung", "luu_y")
FIELD_LABELS = {"thanh_phan_dang_ky": "Thành phần đăng ký", "cong_dung": "Công dụng",
                "doi_tuong_cay_trong": "Cây trồng / đối tượng", "huong_dan_su_dung": "Hướng dẫn sử dụng",
                "luu_y": "Lưu ý (gồm thời gian cách ly)"}
ENRICH_TIMEOUT = 90           # giây cho một lượt gọi Gemini có tra Google
MAX_SOURCES_PER_FIELD = 5
_NOT_FOUND = re.compile(r"^\s*(không\s*tìm\s*thấy|không\s*có\s*thông\s*tin|không\s*rõ|chưa\s*rõ|n/?a|null|none)\W*$",
                        re.I)


class EnrichError(ValueError):
    pass


def is_empty(value):
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return not value


def missing_fields(profile):
    return [key for key in ENRICH_FIELDS if is_empty((profile or {}).get(key))]


# ------------------------------------------------------------------ prompt
def build_system():
    return """Bạn là trợ lý tra cứu hồ sơ kỹ thuật vật tư nông nghiệp (phân bón, thuốc bảo vệ thực vật) cho một shop.
Hãy DÙNG CÔNG CỤ TÌM KIẾM GOOGLE để tìm nhãn sản phẩm, trang nhà sản xuất, tài liệu đăng ký/danh mục thuốc được phép sử dụng.
Quy tắc bắt buộc:
- Chỉ ghi thông tin có trong kết quả tìm kiếm đúng SẢN PHẨM này (đúng tên, đúng nhà sản xuất nếu có). Không đoán, không dùng hiểu biết chung.
- Không tìm thấy thì ghi đúng chuỗi "không tìm thấy".
- TUYỆT ĐỐI không ghi giá, khuyến mãi, quà tặng, mã giảm giá, nơi bán.
- Chép đúng con số liều lượng, hàm lượng, đơn vị như nguồn.
- Trả về MỘT đối tượng JSON duy nhất (không giải thích ngoài JSON), viết tiếng Việt."""


def build_user_text(profile, fields):
    known = {k: profile.get(k) for k in ("ten_san_pham", "nha_san_xuat", "quy_cach_dong_goi", "phan_khuc")
             if not is_empty(profile.get(k))}
    schema = {
        "thanh_phan_dang_ky": '[{"ten": "tên hoạt chất", "ham_luong": "hàm lượng + đơn vị"}]',
        "cong_dung": '["công dụng ngắn, mỗi ý một dòng"]',
        "doi_tuong_cay_trong": '["tên cây trồng / đối tượng phòng trừ"]',
        "huong_dan_su_dung": '["liều pha, lượng nước, thời điểm phun/bón — mỗi ý một dòng"]',
        "luu_y": '"lưu ý an toàn, bảo quản (chuỗi)"',
        "thoi_gian_cach_ly": '"số ngày cách ly trước thu hoạch nếu là thuốc BVTV, không có thì \\"không tìm thấy\\""',
    }
    wanted = list(fields) + (["thoi_gian_cach_ly"] if "luu_y" in fields else [])
    lines = "\n".join(f'  "{k}": {schema[k]}' for k in wanted)
    return (f"Sản phẩm cần tra: {json.dumps(known, ensure_ascii=False)}\n\n"
            f"Tìm trên Google và điền các trường sau (trường nào không có nguồn thì ghi \"không tìm thấy\"):\n"
            f"{{\n{lines}\n}}\nChỉ trả JSON đúng các khoá trên.")


# ------------------------------------------------------------------ nguồn từ groundingMetadata
def _chunks(meta):
    out = []
    for chunk in (meta or {}).get("groundingChunks") or []:
        web = (chunk or {}).get("web") or (chunk or {}).get("retrievedContext") or {}
        uri = str(web.get("uri") or "").strip()
        out.append({"url": uri, "title": _clean_str(web.get("title") or uri, 200)}
                   if uri.startswith(("http://", "https://")) else None)
    return out


def _support_positions(raw, meta):
    """Mỗi groundingSupport -> (vị trí ký tự trong ``raw``, danh sách chỉ số chunk)."""
    data = raw.encode("utf-8")
    out = []
    for support in (meta or {}).get("groundingSupports") or []:
        segment = (support or {}).get("segment") or {}
        indices = [i for i in support.get("groundingChunkIndices") or [] if isinstance(i, int)]
        if not indices:
            continue
        text = str(segment.get("text") or "")
        spots = []
        if text:
            start = raw.find(text)
            while start >= 0:
                spots.append((start, start + len(text)))
                start = raw.find(text, start + 1)
        if not spots:
            try:   # chỉ số của Google tính theo byte UTF-8
                start = len(data[:int(segment.get("startIndex") or 0)].decode("utf-8", "ignore"))
                end = len(data[:int(segment.get("endIndex") or 0)].decode("utf-8", "ignore"))
                if end > start:
                    spots.append((start, end))
            except (TypeError, ValueError):
                pass
        for spot in spots:
            out.append((spot, indices))
    return out


def _field_spans(raw, keys):
    """Vị trí [bắt đầu, kết thúc) của từng khoá trong chuỗi JSON thô (theo thứ tự xuất hiện)."""
    found = []
    for key in keys:
        match = re.search(r'"%s"\s*:' % re.escape(key), raw)
        if match:
            found.append((match.start(), key))
    found.sort()
    spans = {}
    for index, (start, key) in enumerate(found):
        end = found[index + 1][0] if index + 1 < len(found) else len(raw)
        spans[key] = (start, end)
    return spans


def sources_by_field(raw, meta, keys):
    chunks = _chunks(meta)
    spans = _field_spans(raw, keys)
    result = {key: [] for key in keys}
    for (start, end), indices in _support_positions(raw, meta):
        for key, (lo, hi) in spans.items():
            if start < hi and end > lo:
                for i in indices:
                    if 0 <= i < len(chunks) and chunks[i] and chunks[i] not in result[key]:
                        result[key].append(chunks[i])
    return {k: v[:MAX_SOURCES_PER_FIELD] for k, v in result.items()}


# ------------------------------------------------------------------ chuẩn hoá đề xuất
def _normalize(key, value):
    if isinstance(value, str) and _NOT_FOUND.match(value):
        return None
    try:
        if key in LIST_FIELDS:
            value = [line for line in _as_lines(value) if not _NOT_FOUND.match(line)]
        elif key == PAIRS_FIELD:
            value = {k: v for k, v in _as_pairs(value).items() if not _NOT_FOUND.match(k)}
        else:
            if isinstance(value, list):
                value = "; ".join(str(v) for v in value)
            value = _clean_str(value, 4000) if value is not None else None
    except ExtractionInvalid:
        return None
    value, _ = _scrub(key, value)
    return None if is_empty(value) else value


def build_proposal(profile, raw, meta, fields=None):
    """Chữ JSON + groundingMetadata -> {"fields": {trường: {old, value, sources}}, "dropped": {...}}.

    ``fields``: các trường được hỏi (mặc định: trường đang trống). Trường đã có giá trị luôn bị bỏ.
    """
    profile = profile or {}
    fields = [k for k in (fields or missing_fields(profile)) if k in ENRICH_FIELDS and is_empty(profile.get(k))]
    try:
        data = parse_json(raw)
    except ExtractionInvalid as exc:
        raise EnrichError("Gemini trả về kết quả không đúng dạng. Thử lại sau.") from exc
    keys = list(fields) + (["thoi_gian_cach_ly"] if "luu_y" in fields else [])
    sources = sources_by_field(str(raw), meta, keys)
    out, dropped = {}, {}
    for key in fields:
        value = _normalize(key, data.get(key))
        src = list(sources.get(key) or [])
        if key == "luu_y":
            cach_ly = _normalize("luu_y", data.get("thoi_gian_cach_ly"))
            cach_ly_src = sources.get("thoi_gian_cach_ly") or []
            if cach_ly and cach_ly_src:
                line = (cach_ly if "cách ly" in cach_ly.lower() else "Thời gian cách ly: " + cach_ly).rstrip(".") + "."
                if value and src:
                    value = value.rstrip(".") + ". " + line
                    src = (src + [s for s in cach_ly_src if s not in src])[:MAX_SOURCES_PER_FIELD]
                else:
                    value, src = line, list(cach_ly_src)
        if value is None:
            dropped[key] = "không tìm thấy"
            continue
        if not src:
            dropped[key] = "không có nguồn"
            continue
        out[key] = {"label": FIELD_LABELS[key], "old": profile.get(key), "value": value, "sources": src}
    return {"fields": out, "dropped": dropped,
            "queries": [str(q) for q in (meta or {}).get("webSearchQueries") or []][:10]}


# ------------------------------------------------------------------ dịch vụ
class ProductEnrich:
    def __init__(self, engine, gemini, clock=time.time, spawn=None):
        self.engine = engine
        self.gemini = gemini
        self._clock = clock
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self._lock = threading.Lock()
        self._jobs = {}
        self._batch = None

    # -- lưu đề xuất (để mở lại sau khi tắt trang)
    def _dir(self):
        return Path(self.engine.workspace.root) / "data" / "enrich_proposals"

    def _path(self, profile_id):
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(profile_id))[:120]
        return self._dir() / f"{safe}.json"

    def _save(self, profile_id, proposal):
        folder = self._dir()
        folder.mkdir(parents=True, exist_ok=True)
        tmp = self._path(profile_id).with_suffix(".tmp")
        tmp.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path(profile_id))

    def _load(self, profile_id):
        try:
            return json.loads(self._path(profile_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _profile(self, profile_id):
        try:
            return self.engine.workspace.detail("profiles", str(profile_id))["data"]
        except (KeyError, ValueError, StopIteration) as exc:
            raise EnrichError("Không tìm thấy hồ sơ này.") from exc

    def _provider(self):
        gemini = self.gemini
        if not (gemini and gemini.configured):
            raise EnrichError("Chưa có khóa Gemini. Vào Cài đặt, dán khóa vào ô “Key Gemini (đọc ảnh/PDF)” "
                              "rồi bấm lại “Bổ sung bằng Google”.")
        provider = GeminiProvider(gemini.api_key, getattr(gemini, "explicit_model", ""), timeout=ENRICH_TIMEOUT,
                                  opener=getattr(gemini, "_opener", None), clock=self._clock)
        provider._candidates = list(getattr(gemini, "_candidates", []) or [])
        provider._detected_at = getattr(gemini, "_detected_at", 0.0)
        return provider

    # -- một hồ sơ (đồng bộ)
    def run_one(self, profile_id):
        profile = self._profile(profile_id)
        fields = missing_fields(profile)
        if not fields:
            proposal = {"profile_id": profile_id, "fields": {}, "dropped": {}, "queries": [],
                        "message": "Hồ sơ đã đủ các trường, không cần bổ sung."}
        else:
            provider = self._provider()
            try:
                raw, meta = provider.generate_grounded(build_system(), build_user_text(profile, fields))
            except AIProviderError as exc:
                raise EnrichError(str(exc)) from exc
            proposal = {"profile_id": profile_id, **build_proposal(profile, raw, meta, fields),
                        "model": provider.last_model}
            if not proposal["fields"]:
                proposal["message"] = "Google không tìm thấy thông tin có nguồn rõ ràng cho các trường còn thiếu."
        proposal["created_at"] = self._clock()
        self._save(profile_id, proposal)
        return proposal

    # -- chạy nền, có trạng thái
    def start(self, profile_id):
        profile_id = str(profile_id)
        self._profile(profile_id)
        self._provider()                            # báo thiếu khoá ngay, không cần chờ nền
        with self._lock:
            job = self._jobs.get(profile_id)
            if job and job["state"] == "running":
                return dict(job)
            job = {"profile_id": profile_id, "state": "running", "step": "Đang tra Google…",
                   "started_at": self._clock(), "error": ""}
            self._jobs[profile_id] = job

        def work():
            try:
                self.run_one(profile_id)
                update = {"state": "done", "step": "Xong"}
            except Exception as exc:                # lỗi luôn hiện ra dưới dạng câu dễ hiểu
                message = str(exc) if isinstance(exc, (EnrichError, AIProviderError)) else \
                    "Không bổ sung được (lỗi bất ngờ). Thử lại sau."
                update = {"state": "error", "step": "Lỗi", "error": message}
            with self._lock:
                job.update(update)
        snapshot = dict(job)
        self._spawn(work)
        return snapshot

    def status(self, profile_id):
        profile_id = str(profile_id)
        with self._lock:
            job = dict(self._jobs.get(profile_id) or {})
        if job.get("state") == "running" and self._clock() - job["started_at"] > ENRICH_TIMEOUT * 3:
            job.update({"state": "error", "error": "Tra cứu quá lâu, đã dừng chờ. Thử lại sau."})
        proposal = self._load(profile_id) if job.get("state") != "running" else None
        return {"ok": True, "profile_id": profile_id, "job": job or None, "proposal": proposal}

    # -- duyệt
    def apply(self, profile_id, fields):
        profile_id = str(profile_id)
        if not isinstance(fields, list) or not fields:
            raise EnrichError("Chưa chọn trường nào để duyệt.")
        proposal = self._load(profile_id)
        if not proposal or not proposal.get("fields"):
            raise EnrichError("Không còn đề xuất cho hồ sơ này. Bấm “Bổ sung bằng Google” lại.")
        profile = self._profile(profile_id)
        changes, skipped = {}, []
        for key in fields:
            item = proposal["fields"].get(str(key))
            if not item or key not in ENRICH_FIELDS:
                continue
            if not is_empty(profile.get(key)):
                skipped.append(key)                 # chủ shop đã tự điền trong lúc chờ: giữ nguyên
                continue
            value = _normalize(key, item.get("value"))
            if value is not None:
                changes[key] = value
        if not changes:
            raise EnrichError("Các trường đã chọn đều đã có nội dung, không ghi đè.")
        hosts = sorted({re.sub(r"^https?://([^/]+).*$", r"\1", s.get("title") or s.get("url") or "")
                        for key in changes for s in proposal["fields"][key].get("sources") or []})
        reason = ("Bổ sung bằng Google (chủ shop duyệt): " + ", ".join(FIELD_LABELS[k] for k in changes)
                  + (" · nguồn: " + ", ".join(hosts)[:300] if hosts else ""))
        result = self.engine.workspace.patch("profiles", profile_id, {"changes": changes, "reason": reason})
        remaining = {k: v for k, v in proposal["fields"].items() if k not in changes and k not in skipped}
        proposal["fields"] = remaining
        self._save(profile_id, proposal)
        return {"ok": True, "id": profile_id, "applied": sorted(changes), "skipped": skipped,
                "undo_available": bool(result.get("undo_available")), "revision": result.get("revision")}

    # -- hàng loạt (tuần tự, có huỷ)
    def start_batch(self, profile_ids=None):
        self._provider()
        with self._lock:
            if self._batch and self._batch["state"] == "running":
                return self.batch_status()
        rows = []
        if profile_ids:
            for pid in profile_ids:
                try:
                    rows.append((str(pid), self._profile(pid)))
                except EnrichError:
                    continue
        else:
            rows = [(str(p.get("id") or p.get("san_pham_id")), p) for p in self.engine.workspace._profiles()]
        todo = [(pid, len(missing_fields(p)), p.get("ten_san_pham") or pid) for pid, p in rows if missing_fields(p)]
        todo.sort(key=lambda row: -row[1])            # hồ sơ thiếu nhiều trường làm trước
        batch = {"state": "running", "total": len(todo), "done": 0, "current": "", "cancel": False,
                 "results": [], "started_at": self._clock()}
        with self._lock:
            self._batch = batch
        if not todo:
            batch["state"] = "done"
            return self.batch_status()

        def work():
            for pid, _count, name in todo:
                if batch["cancel"]:
                    break
                with self._lock:
                    batch["current"] = name
                try:
                    proposal = self.run_one(pid)
                    row = {"id": pid, "name": name, "ok": True, "fields": sorted(proposal.get("fields") or {})}
                except Exception as exc:
                    row = {"id": pid, "name": name, "ok": False,
                           "error": str(exc) if isinstance(exc, (EnrichError, AIProviderError)) else "Lỗi bất ngờ"}
                with self._lock:
                    batch["results"].append(row)
                    batch["done"] += 1
                if not row["ok"] and ("hết lượt" in row.get("error", "").lower() or "khóa" in row.get("error", "")):
                    break                                # hết hạn mức / khoá hỏng: dừng, khỏi tốn lượt
            with self._lock:
                batch["state"] = "cancelled" if batch["cancel"] else "done"
                batch["current"] = ""
        self._spawn(work)
        return self.batch_status()

    def cancel_batch(self):
        with self._lock:
            if self._batch and self._batch["state"] == "running":
                self._batch["cancel"] = True
        return self.batch_status()

    def batch_status(self):
        with self._lock:
            batch = dict(self._batch or {"state": "idle", "total": 0, "done": 0, "results": []})
            batch["results"] = list(batch.get("results") or [])
        return {"ok": True, **batch}
