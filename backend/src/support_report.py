"""Tạo gói hỗ trợ cục bộ đã che bí mật; không bao giờ tự gửi mạng."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from config import (DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, INSTALL_ROOT, IS_FROZEN, LOG_DIR,
                    PLUGINS_DIR, ROOT)
from diagnostics import run_setup_checks
from plugin_registry import discover_plugins
from structured_log import redact


REPORT_DIR = ROOT / "reports"
TAIL_LINES = 300
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
# SĐT Việt Nam liền hoặc có dấu cách/chấm/gạch: 0912345678, 0912 345 678,
# 0912.345.678, +84 912345678, 84912345678. Hai chữ số đầu phải dính tiền tố
# để số thực (0.123456789) và ngày giờ không bị che nhầm.
_PHONE = re.compile(r"(?<![\d.])(?:\+84[ .-]?|84|0)\d{2}(?:[ .-]?\d){6,8}(?!\d)")
_COOKIE = re.compile(r"(?i)\b(cookie|set-cookie|authorization)\b\s*[:=]\s*[^\r\n,;]+")
# Tên người dùng Windows trong đường dẫn (cả dạng JSON "C:\\Users\\ten").
_WIN_USER = re.compile(r"(?i)\b([A-Z]:(?:\\\\|\\|/)Users(?:\\\\|\\|/))([^\\/\r\n\"'<>|:*?]+)")


def redact_support(value):
    text = redact(value)
    text = _COOKIE.sub(lambda m: m.group(1) + "=[REDACTED]", text)
    text = _EMAIL.sub("[REDACTED_EMAIL]", text)
    text = _WIN_USER.sub(lambda m: m.group(1) + "<user>", text)
    return _PHONE.sub("[REDACTED_PHONE]", text)


def _app_root():
    selected = os.environ.get("KIDTE_APP_ROOT", "") or (str(INSTALL_ROOT) if IS_FROZEN else "")
    return Path(selected) if selected else None


def _tail(path, lines=TAIL_LINES):
    """Đuôi tệp văn bản (đọc tối đa 2 MB cuối) để báo cáo không phình."""
    with open(path, "rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - 2 * 1024 * 1024))
        raw = stream.read()
    text = raw.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:]) + "\n"


def _tail_files():
    """Tệp chỉ lấy phần đuôi: console.log (traceback chưa bắt), crash.log, launcher."""
    found = {}
    for name in ("console.log", "crash.log"):
        path = LOG_DIR / name
        if path.is_file():
            found[f"logs/{name}.tail.txt"] = path
    app_root = _app_root()
    if app_root is not None:
        for name in ("launcher-startup.log", "update-failure.json"):
            path = app_root / name
            if path.is_file():
                found[f"app/{name}" + (".tail.txt" if name.endswith(".log") else "")] = path
    return found


def _memory_mb():
    if os.name != "nt":
        return None
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return {"total_mb": status.ullTotalPhys // (1024 * 1024),
                "available_mb": status.ullAvailPhys // (1024 * 1024),
                "load_percent": status.dwMemoryLoad}
    except (AttributeError, OSError):
        return None


def _system_info():
    info = {"python": sys.version.split()[0], "platform": platform.platform(),
            "machine": platform.machine(), "cpu_count": os.cpu_count(), "frozen": IS_FROZEN}
    if os.name == "nt":
        try:
            version = sys.getwindowsversion()
            info["windows_build"] = f"{version.major}.{version.minor}.{version.build}"
            info["windows_release"] = platform.win32_edition() if hasattr(platform, "win32_edition") else ""
        except (AttributeError, OSError):
            pass
    info["memory"] = _memory_mb()
    return info


def _version_info():
    info = {}
    try:
        from version import app_version
        info["app_version"] = app_version()
    except Exception as exc:  # noqa: BLE001 - report must still be created
        info["app_version_error"] = type(exc).__name__
    app_root = _app_root()
    if app_root is not None:
        try:
            from component_store import ComponentStore
            store = ComponentStore(app_root)
            for label, mapping in (("active", store.load_active()), ("pending", store.load_pending())):
                info[f"components_{label}"] = {
                    key: (row.get("package") or {}).get("version")
                    for key, row in ((mapping or {}).get("components") or {}).items()}
        except Exception as exc:  # noqa: BLE001
            info["components_error"] = f"{type(exc).__name__}: {exc}"
    try:
        from config import EXTENSION_DIR
        manifest = Path(EXTENSION_DIR) / "manifest.json"
        if manifest.is_file():
            info["extension_version"] = json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except Exception as exc:  # noqa: BLE001
        info["extension_error"] = type(exc).__name__
    return info


def _source_files():
    candidates = []
    for pattern in ("core.jsonl*", "metrics/session-metrics.jsonl*"):
        candidates.extend(path for path in LOG_DIR.glob(pattern) if path.is_file())
    service_dir = ROOT / "runtime" / "services"
    candidates.extend(path for path in service_dir.glob("*.health.json") if path.is_file())
    return sorted(set(candidates), key=lambda path: str(path))


def _archive_name(path):
    """Dat ten noi bo on dinh, khong gia dinh moi file deu nam trong LOG_DIR."""
    path = Path(path)
    try:
        relative = path.relative_to(LOG_DIR)
        return "logs/" + str(relative).replace("\\", "/")
    except ValueError:
        service_dir = ROOT / "runtime" / "services"
        try:
            relative = path.relative_to(service_dir)
        except ValueError as exc:
            raise ValueError("support_source_outside_allowlist") from exc
        return "services/" + str(relative).replace("\\", "/")


def preview_support_report():
    files = [_archive_name(path) for path in _source_files()]
    files += sorted(_tail_files())
    files += ["generated/diagnostics.json", "generated/runtime.json",
              "generated/config-redacted.json", "generated/system.json", "manifest.json"]
    return {"ok": True, "files": files, "will_send": False,
            "excluded": [".env", "API key", "cookie", "database", "raw stream",
                         "nội dung bình luận", "backup"]}


def _generated_documents(runtime_state=None):
    manifests, errors = discover_plugins(PLUGINS_DIR)
    plugins = [{"id": item.plugin_id, "version": item.version, "kind": item.kind}
               for item in manifests.values()]
    return {
        "generated/diagnostics.json": run_setup_checks(include_audio=False),
        "generated/runtime.json": runtime_state or {},
        "generated/system.json": {**_system_info(), **_version_info()},
        "generated/config-redacted.json": {
            "deepseek_base_url": DEEPSEEK_BASE_URL,
            "deepseek_model": DEEPSEEK_MODEL,
            "api_key_configured": bool(__import__("config").DEEPSEEK_API_KEY),
            "plugins": plugins, "plugin_errors": errors,
        },
    }


def create_support_report(runtime_state=None, *, now=None):
    now = now or datetime.now(timezone.utc)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    destination = REPORT_DIR / f"kid-te-support-{now:%Y%m%dT%H%M%SZ}.zip"
    temporary = destination.with_suffix(".zip.tmp")
    entries = {}
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, document in _generated_documents(runtime_state).items():
            raw = redact_support(json.dumps(document, ensure_ascii=False, indent=2)).encode("utf-8")
            archive.writestr(name, raw)
            entries[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        for path in _source_files():
            name = _archive_name(path)
            raw = redact_support(path.read_text(encoding="utf-8", errors="replace")).encode("utf-8")
            archive.writestr(name, raw)
            entries[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        for name, path in _tail_files().items():
            try:
                text = _tail(path)
            except OSError as exc:
                text = f"[không đọc được: {type(exc).__name__}]\n"
            raw = redact_support(text).encode("utf-8")
            archive.writestr(name, raw)
            entries[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        manifest = {"schema_version": 1, "created_at": now.isoformat(),
                    "sent": False, "entries": entries}
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    temporary.replace(destination)
    return {"ok": True, "path": str(destination), "files": sorted(entries),
            "sent": False}
