"""Cập nhật gói kiến thức có version/hash, không chạm dữ liệu theo máy."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import urllib.request
from pathlib import Path


MAX_PACKAGE_BYTES = 16 * 1024 * 1024


def content_hash(content):
    raw = json.dumps(content, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_package(payload):
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("schema_version gói kiến thức không hỗ trợ")
    if not str(payload.get("version") or "").strip():
        raise ValueError("gói kiến thức thiếu version")
    content = payload.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("nhom_san_pham"), list):
        raise ValueError("content gói kiến thức không hợp lệ")
    expected = str(payload.get("content_sha256") or "").casefold()
    if len(expected) != 64 or content_hash(content) != expected:
        raise ValueError("hash gói kiến thức không khớp")
    return payload


def read_source(source):
    source = str(source or "").strip()
    if not source:
        raise ValueError("chưa cấu hình nguồn gói kiến thức")
    if source.startswith("https://"):
        with urllib.request.urlopen(source, timeout=20, context=__import__("net").ssl_context()) as response:
            raw = response.read(MAX_PACKAGE_BYTES + 1)
    elif "://" in source:
        raise ValueError("chỉ cho phép HTTPS hoặc đường dẫn file nội bộ")
    else:
        raw = Path(source).expanduser().read_bytes()
    if len(raw) > MAX_PACKAGE_BYTES:
        raise ValueError("gói kiến thức vượt giới hạn 16 MB")
    return raw


def _doc_goi(raw):
    try:
        payload = json.loads(bytes(raw).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("gói kiến thức không phải JSON hợp lệ") from exc
    return validate_package(payload)


def _noi_dung_hien_tai(target):
    try:
        hien_tai = json.loads(Path(target).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"nhom_san_pham": []}
    return hien_tai.get("content", hien_tai) or {"nhom_san_pham": []}


def xem_truoc(raw, target):
    """Gói sẽ đổi những gì — KHÔNG ghi bất cứ thứ gì lên đĩa."""
    from knowledge_diff import so_sanh
    payload = _doc_goi(raw)
    return {"ok": True, "version": str(payload["version"]),
            "content_sha256": payload["content_sha256"],
            "khac_biet": so_sanh(_noi_dung_hien_tai(target), payload["content"])}


def apply_package(raw, target, *, live_running=False, quyet_dinh_xung_dot=None):
    """Nhập gói vào hồ sơ tại máy.

    Bản cũ thay NGUYÊN tệp bằng nội dung gói. Điều đó xoá mọi trường mà gói
    không có — trong đó có ``legacy_profile_id``, đường nối tới hồ sơ kỹ thuật
    đã duyệt. Một gói thiếu trường đó sẽ tái tạo đúng sự cố F13. Nay gộp theo
    ``knowledge_diff``: trường riêng của máy được giữ, trường rỗng trong gói
    không xoá nội dung tại máy, và mục xung đột mặc định giữ bản tại máy.
    """
    if live_running:
        raise RuntimeError("không cập nhật gói kiến thức trong phiên LIVE")
    from knowledge_diff import gop
    payload = _doc_goi(raw)
    target = Path(target)
    goc = {}
    if target.is_file():
        try:
            goc = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            goc = {}
    ket = gop(_noi_dung_hien_tai(target), payload["content"], quyet_dinh_xung_dot)

    ra = dict(goc) if isinstance(goc, dict) else {}
    ra.update({
        "schema_version": 1,
        "published_at": payload.get("published_at") or ra.get("published_at"),
        "publisher": payload.get("publisher") or ra.get("publisher"),
        # Ghi lai may nay dang o phien ban goi chung nao.
        "version": str(payload["version"]),
        "content": ket["noi_dung"],
    })
    ra["content_sha256"] = content_hash(ket["noi_dung"])

    previous = target.parent / ".previous" / target.name
    temporary = target.with_suffix(target.suffix + ".incoming")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(ra, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    # Doc lai truoc khi thay: khong de mot lan ghi do dang thanh du lieu that.
    doc_lai = json.loads(temporary.read_text(encoding="utf-8"))
    if content_hash(doc_lai["content"]) != ra["content_sha256"]:
        temporary.unlink(missing_ok=True)
        raise ValueError("ghi gói kiến thức không toàn vẹn")
    if target.exists():
        previous.parent.mkdir(parents=True, exist_ok=True)
        previous_temp = previous.with_suffix(previous.suffix + ".tmp")
        shutil.copy2(target, previous_temp)
        previous_temp.replace(previous)
    temporary.replace(target)
    return {"ok": True, "version": str(payload["version"]),
            "content_sha256": ra["content_sha256"],
            "khac_biet": ket["khac_biet"], "da_doi": ket["da_doi"],
            "previous_available": previous.exists()}


def hoan_tac(target):
    """Khôi phục bản ngay trước lần nhập gần nhất."""
    target = Path(target)
    previous = target.parent / ".previous" / target.name
    if not previous.is_file():
        raise ValueError("không có bản trước lần nhập để hoàn tác")
    tam = target.with_suffix(target.suffix + ".undo")
    shutil.copy2(previous, tam)
    json.loads(tam.read_text(encoding="utf-8"))      # phải đọc được mới thay
    tam.replace(target)
    previous.unlink()
    return {"ok": True, "da_hoan_tac": True}


def xuat_goi(target, version, publisher=""):
    """Xuất hồ sơ tại máy thành gói chung: bỏ mọi trường riêng của máy."""
    from knowledge_diff import TRUONG_RIENG_CUA_MAY
    import copy as _copy
    noi_dung = _copy.deepcopy(_noi_dung_hien_tai(target))
    for nhom in noi_dung.get("nhom_san_pham", []) or []:
        for san_pham in nhom.get("san_pham", []) or []:
            for truong in TRUONG_RIENG_CUA_MAY:
                san_pham.pop(truong, None)
    goi = {"schema_version": 1, "version": str(version).strip(),
           "publisher": str(publisher or "").strip(),
           "content": noi_dung, "content_sha256": content_hash(noi_dung)}
    validate_package(goi)
    return goi


def update_from_source(source, target, *, live_running=False,
                       quyet_dinh_xung_dot=None):
    return apply_package(read_source(source), target, live_running=live_running,
                         quyet_dinh_xung_dot=quyet_dinh_xung_dot)


# ----------------------------------------------------------------------
# Gói hồ sơ chính thức đi kèm bộ cài (backend/prompts/knowledge_seed.json)
# ----------------------------------------------------------------------
_logger = logging.getLogger("kid-te.knowledge-seed")


def version_key(version):
    """'2026.09.13' -> (2026, 9, 13). So sánh theo số, không theo chữ."""
    return tuple(int(item) for item in re.findall(r"\d+", str(version or "")))


def installed_version(target):
    """Phiên bản gói đang cài. Tệp cũ chưa ghi version thì lấy ngày phát hành."""
    try:
        data = json.loads(Path(target).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("version") or str(data.get("published_at") or "")[:10]).strip()


def seed_status(seed_path, target):
    """Trạng thái gói đi kèm so với gói đang cài (chỉ đọc, không ghi gì).

    state: no_seed | missing (máy trắng) | newer (gói đi kèm mới hơn) | current.
    """
    seed_path, target = Path(seed_path), Path(target)
    try:
        seed = _doc_goi(seed_path.read_bytes())
    except (OSError, ValueError):
        return {"state": "no_seed", "seed_version": "", "installed_version": ""}
    seed_version = str(seed["version"])
    if not target.is_file():
        return {"state": "missing", "seed_version": seed_version, "installed_version": ""}
    installed = installed_version(target)
    try:
        same_content = json.loads(target.read_text(encoding="utf-8")).get("content_sha256") == seed["content_sha256"]
    except (OSError, json.JSONDecodeError, AttributeError):
        same_content = False
    newer = version_key(seed_version) > version_key(installed) and not same_content
    return {"state": "newer" if newer else "current", "seed_version": seed_version,
            "installed_version": installed}


def seed_first_run(seed_path, target, *, live_running=False):
    """Máy trắng: cài gói chính thức đi kèm. KHÔNG BAO GIỜ ghi đè tệp đã có.

    Đi qua ``apply_package`` (kiểm hash, ghi nguyên tử). Đang LIVE thì hoãn.
    """
    seed_path, target = Path(seed_path), Path(target)
    if target.exists():
        return {"ok": True, "status": "existing_untouched"}
    if not seed_path.is_file():
        return {"ok": True, "status": "no_seed"}
    if live_running:
        _logger.warning("knowledge_seed_deferred_live target=%s", target)
        return {"ok": True, "status": "deferred_live"}
    try:
        result = apply_package(seed_path.read_bytes(), target)
    except (OSError, ValueError, RuntimeError) as exc:
        _logger.error("knowledge_seed_failed: %s", exc)
        return {"ok": False, "status": "failed", "error": str(exc)}
    _logger.info("knowledge_seed_applied version=%s target=%s", result["version"], target)
    return {"ok": True, "status": "applied", "version": result["version"],
            "content_sha256": result["content_sha256"]}
