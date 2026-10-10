"""Tạo gói hỗ trợ cục bộ đã che bí mật; không bao giờ tự gửi mạng."""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from config import DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, LOG_DIR, PLUGINS_DIR, ROOT
from diagnostics import run_setup_checks
from plugin_registry import discover_plugins
from structured_log import redact


REPORT_DIR = ROOT / "reports"
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?84|0)\d{8,10}(?!\d)")
_COOKIE = re.compile(r"(?i)\b(cookie|set-cookie|authorization)\b\s*[:=]\s*[^\r\n,;]+")


def redact_support(value):
    text = redact(value)
    text = _COOKIE.sub(lambda m: m.group(1) + "=[REDACTED]", text)
    text = _EMAIL.sub("[REDACTED_EMAIL]", text)
    return _PHONE.sub("[REDACTED_PHONE]", text)


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
    files += ["generated/diagnostics.json", "generated/runtime.json",
              "generated/config-redacted.json", "manifest.json"]
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
            raw = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
            archive.writestr(name, raw)
            entries[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        for path in _source_files():
            name = _archive_name(path)
            raw = redact_support(path.read_text(encoding="utf-8", errors="replace")).encode("utf-8")
            archive.writestr(name, raw)
            entries[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        manifest = {"schema_version": 1, "created_at": now.isoformat(),
                    "sent": False, "entries": entries}
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    temporary.replace(destination)
    return {"ok": True, "path": str(destination), "files": sorted(entries),
            "sent": False}
