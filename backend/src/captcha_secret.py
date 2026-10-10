"""Windows-user protected SadCaptcha key shared with the separate solver process."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_byte))]


def _protect(payload: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("captcha_key_windows_only")
    source = ctypes.create_string_buffer(payload)
    incoming = _Blob(len(payload), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    outgoing = _Blob()
    crypt32 = ctypes.windll.crypt32
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    if not operation(ctypes.byref(incoming), None, None, None, None, 0,
                     ctypes.byref(outgoing)):
        raise OSError(ctypes.get_last_error(), "Windows DPAPI failed")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(ctypes.cast(outgoing.data, ctypes.c_void_p))


def key_path(data_dir: Path) -> Path:
    return Path(data_dir) / "sadcaptcha-key.dpapi"


def has_key(data_dir: Path) -> bool:
    path = key_path(data_dir)
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def save_key(data_dir: Path, value: str) -> None:
    value = value.strip()
    if not value or len(value) > 512 or any(ch.isspace() for ch in value):
        raise ValueError("captcha_key_invalid")
    path = key_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    protected = _protect(value.encode("utf-8"), decrypt=False)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(protected)
    temporary.replace(path)


def load_key(data_dir: Path) -> str:
    return _protect(key_path(data_dir).read_bytes(), decrypt=True).decode("utf-8")


def save_telegram(data_dir: Path, token: str, chat_id: str) -> None:
    token, chat_id = token.strip(), chat_id.strip()
    if not token or len(token) > 256 or any(ch.isspace() for ch in token):
        raise ValueError("captcha_telegram_token_invalid")
    if not chat_id.lstrip("-").isdigit() or len(chat_id) > 25:
        raise ValueError("captcha_telegram_chat_invalid")
    # Telegram bot IDs are the numeric prefix of a bot token. A bot cannot
    # send a message to itself, even though that ID passes the numeric check.
    if chat_id == token.split(":", 1)[0]:
        raise ValueError("captcha_telegram_chat_is_bot")
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    secret = directory / "telegram-token.dpapi"
    temporary = secret.with_suffix(".tmp")
    temporary.write_bytes(_protect(token.encode("utf-8"), decrypt=False))
    temporary.replace(secret)
    public = directory / "telegram.json"
    temporary = public.with_suffix(".tmp")
    temporary.write_text(json.dumps({"chat_id": chat_id}), encoding="utf-8")
    temporary.replace(public)


def has_telegram(data_dir: Path) -> bool:
    directory = Path(data_dir)
    return (directory / "telegram-token.dpapi").is_file() and (directory / "telegram.json").is_file()


def load_telegram(data_dir: Path) -> tuple[str, str]:
    directory = Path(data_dir)
    token = _protect((directory / "telegram-token.dpapi").read_bytes(), decrypt=True).decode("utf-8")
    chat_id = str(json.loads((directory / "telegram.json").read_text(encoding="utf-8"))["chat_id"])
    return token, chat_id
