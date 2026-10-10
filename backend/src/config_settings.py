"""Cập nhật cấu hình cho phép theo danh sách trắng mà không đọc key ra UI."""
from __future__ import annotations

from pathlib import Path

from config import ROOT


def _save_values(values: dict[str, str], env_path=None, *, remove_keys=()):
    path = Path(env_path or ROOT / ".env")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    remaining = dict(values)
    output = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in remove_keys:
            continue
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in remaining.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    temporary.replace(path)


def save_model(model: str, env_path=None):
    model = str(model or "").strip()
    if not model or any(ch in model for ch in "\r\n=\0"):
        raise ValueError("Tên model không hợp lệ")
    _save_values({"DEEPSEEK_MODEL": model}, env_path)
    return {"ok": True, "model": model, "restart_required": False}


def save_api_key(api_key: str, env_path=None):
    api_key = str(api_key or "").strip()
    if len(api_key) < 8 or any(ch in api_key for ch in "\r\n\0"):
        raise ValueError("Khóa API không hợp lệ")
    _save_values({"DEEPSEEK_API_KEY": api_key}, env_path)
    return {"ok": True, "api_key_configured": True, "restart_required": False}


def save_audio_device(device_id: str, env_path=None):
    device_id = str(device_id or "").strip()
    if not device_id or any(ch in device_id for ch in "\r\n\0"):
        raise ValueError("Thiết bị âm thanh không hợp lệ")
    _save_values({"KID_TE_AUDIO_DEVICE": device_id}, env_path,
                 remove_keys={"LIVE_RE_AUDIO_DEVICE"})
    return {"ok": True, "audio_device_id": device_id,
            "restart_required": False}


def save_tiktok_identity(user_ids: str, unique_ids: str, env_path=None):
    values = {}
    for key, raw in (("TIKTOK_HOST_USER_IDS", user_ids),
                     ("TIKTOK_HOST_UNIQUE_IDS", unique_ids)):
        normalized = ",".join(part.strip() for part in str(raw or "").split(",") if part.strip())
        if any(ch in normalized for ch in "\r\n=\0"):
            raise ValueError("Định danh TikTok không hợp lệ")
        values[key] = normalized
    _save_values(values, env_path)
    return {"ok": True, "tiktok_identity_saved": True, "restart_required": True}


def save_knowledge_source(source: str, env_path=None):
    source = str(source or "").strip()
    if any(ch in source for ch in "\r\n\0") or ("://" in source and not source.startswith("https://")):
        raise ValueError("Nguồn kiến thức chỉ nhận HTTPS hoặc đường dẫn file nội bộ")
    _save_values({"KNOWLEDGE_PACKAGE_SOURCE": source}, env_path)
    return {"ok": True, "knowledge_source_saved": True, "restart_required": True}
