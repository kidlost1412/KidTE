"""Pure CAPTCHA health logic: parse the solver log tail and derive one state.

The solver is a separate program. Nothing here claims TikTok accepted an answer;
a vanished puzzle is reported as "not confirmed".
"""
from __future__ import annotations

import re
from datetime import datetime

LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| (\w+)\s*\| [^|]*\| (.*)$")
RES_RE = re.compile(r"\((\d+x\d+)\)")
HEARTBEAT_LIMIT_S = 120
SOLVING_WINDOW_S = 60
ERROR_NOTE_TTL_S = 30 * 60   # a one-off error stays only as a small note this long
REPEAT_ERRORS = 3            # this many errors with no success in between = ongoing
REPEAT_WINDOW_S = 10 * 60
HTTP_RE = re.compile(r"HTTP (\d{3})")

LABELS = {
    "not_installed": "Chưa cài bộ giải",
    "no_key": "Chưa có key SadCaptcha",
    "key_rejected": "Key SadCaptcha bị từ chối (sai hoặc hết tiền)",
    "start_failed": "Không khởi động được",
    "not_running": "Chưa chạy",
    "unresponsive": "Bộ giải không phản hồi",
    "manual_required": "Cần giải tay",
    "solving": "Đang giải",
    "service_error": "Lỗi dịch vụ giải",
    "ready": "Sẵn sàng",
}
TONES = {"ready": "ok", "solving": "info", "not_running": "warn", "service_error": "warn",
         "unresponsive": "danger", "manual_required": "danger", "start_failed": "danger",
         "key_rejected": "danger", "no_key": "warn", "not_installed": "warn"}
START_REASONS = {
    "captcha_admin_declined": "bạn đã từ chối quyền Administrator (UAC)",
    "captcha_other_solver_running": "đang có bộ giải khác chạy",
    "captcha_solver_missing": "không tìm thấy chương trình bộ giải",
    "captcha_solver_package_invalid": "tệp bộ giải không khớp gói đã ký (bị sửa hoặc hỏng) nên không chạy bằng quyền Administrator; vào Cài đặt → Cập nhật để cài lại gói CAPTCHA",
    "captcha_key_missing": "chưa có key SadCaptcha",
    "captcha_key_rejected": "key SadCaptcha bị từ chối",
    "captcha_app_closing": "Kid TE đang đóng",
    "captcha_start_failed": "Windows không mở được tiến trình bộ giải",
}


def explain_error(raw):
    """Plain Vietnamese meaning of a solver/SadCaptcha error line."""
    found = HTTP_RE.search(raw or "")
    code = found.group(1) if found else ""
    if code in ("401", "403"):
        return f"SadCaptcha từ chối ({code}): key sai hoặc hết hạn"
    if code == "400":
        return "SadCaptcha từ chối ảnh (400): ảnh gửi lên không hợp lệ (thường do nhận nhầm vùng), không phải lỗi key"
    if code:
        return f"SadCaptcha báo lỗi {code}"
    return (raw or "")[:80]


EMPTY = {k: None for k in (
    "last_heartbeat_at", "last_scan_resolution", "last_detect_at", "last_detect_type",
    "last_api_ok_at", "last_api_error", "last_api_error_at", "last_drag_at", "last_gone_at",
    "last_stuck_at", "manual_required_at", "last_telegram_ok_at", "last_line_at", "stopped_at")}


def parse_log(text: str, today: str | None = None) -> dict:
    """Tolerant parser: lines that do not match the log format are ignored."""
    out = dict(EMPTY, attempts_today=0, api_error_times=[])
    for raw in (text or "").splitlines():
        match = LINE_RE.match(raw.rstrip("\r"))
        if not match:
            continue
        at, _, msg = match.groups()
        out["last_line_at"] = at
        if "scanning screen" in msg:
            out["last_heartbeat_at"] = at
            found = RES_RE.search(msg)
            if found:
                out["last_scan_resolution"] = found.group(1)
        elif "captcha detected" in msg:
            out["last_detect_at"] = at
            out["last_detect_type"] = "shapes" if "shapes" in msg else "puzzle"
            if today and at.startswith(today):
                out["attempts_today"] += 1
        elif "got response for" in msg:
            out["last_api_ok_at"] = at
            out["api_error_times"] = []
        elif "answer unavailable" in msg or "unexpected exception" in msg:
            detail = msg.split("unavailable:", 1)[-1] if "unavailable" in msg else msg
            out["last_api_error"] = detail.split("; retrying")[0].strip()[:200]
            out["last_api_error_at"] = at
            out["api_error_times"] = (out["api_error_times"] + [at])[-20:]
        elif "dragged puzzle slider" in msg or "clicked confirm button" in msg:
            out["last_drag_at"] = at
        elif "no longer visible after drag" in msg:
            out["last_gone_at"] = at
        elif "still visible after drag" in msg:
            out["last_stuck_at"] = at
        elif "solver stopped" in msg:
            out["stopped_at"] = at
        if "CAPTCHA_MANUAL_REQUIRED" in msg:
            out["manual_required_at"] = at
    return out


def parse_monitor(text: str) -> str | None:
    last = None
    for line in (text or "").splitlines():
        if ("delivery OK" in line or "delivered" in line) and re.match(r"\d{4}-\d{2}-\d{2}T", line):
            last = line[:19].replace("T", " ")
    return last


def _ts(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S") if value else None
    except ValueError:
        return None


def _newer(a, b):
    """True when a exists and is later than b (b may be missing)."""
    ta, tb = _ts(a), _ts(b)
    return ta is not None and (tb is None or ta > tb)


def result_text(info: dict) -> str | None:
    """Plain description of the last attempt, never claiming TikTok accepted it."""
    drag = info.get("last_drag_at")
    if not drag:
        return None
    gone = info.get("last_gone_at")
    if gone and not _newer(drag, gone):
        return f"đã kéo, CAPTCHA biến mất lúc {gone[11:16]} (TikTok chưa xác nhận)"
    if _newer(info.get("last_stuck_at"), drag):
        return f"đã kéo lúc {drag[11:16]}, CAPTCHA vẫn còn"
    return f"đã kéo lúc {drag[11:16]}, chưa thấy kết quả"


def derive(*, available, configured, key_rejected, running, start_error, info,
           now: datetime, started_at: datetime | None = None) -> dict:
    """Return {state, label, tone, detail} using the documented priority."""
    state, detail = "ready", ""
    reference = _ts(info.get("last_line_at")) or started_at
    lag = (now - reference).total_seconds() if reference else None
    if not available:
        state = "not_installed"
    elif not configured:
        state = "no_key"
    elif key_rejected:
        state = "key_rejected"
    elif not running and start_error:
        state, detail = "start_failed", start_error
    elif not running:
        state = "not_running"
    elif lag is not None and lag > HEARTBEAT_LIMIT_S:
        state = "unresponsive"
        detail = f"không có nhật ký mới trong {int(lag)} giây"
    elif _newer(info.get("manual_required_at"), info.get("last_gone_at")):
        state = "manual_required"
    else:
        detect = _ts(info.get("last_detect_at"))
        ended = max([t for t in (_ts(info.get(k)) for k in (
            "last_gone_at", "last_stuck_at", "last_api_error_at", "manual_required_at")) if t],
            default=None)
        if (detect and 0 <= (now - detect).total_seconds() <= SOLVING_WINDOW_S
                and (ended is None or ended < detect)):
            state = "solving"
        elif _newer(info.get("last_api_error_at"), info.get("last_api_ok_at")):
            raw = info.get("last_api_error") or ""
            recent = [t for t in (_ts(x) for x in info.get("api_error_times") or [])
                      if t and 0 <= (now - t).total_seconds() <= REPEAT_WINDOW_S]
            auth = HTTP_RE.search(raw) and HTTP_RE.search(raw).group(1) in ("401", "403")
            if (auth or len(recent) >= REPEAT_ERRORS) and recent:
                state, detail = "service_error", explain_error(raw)
    label = LABELS[state]
    if detail and state in {"start_failed", "service_error"}:
        label += ": " + detail
    # Small secondary line: the latest error, only while it is recent and nothing succeeded after it.
    note = ""
    err_at = _ts(info.get("last_api_error_at"))
    if (state not in {"service_error"} and err_at and info.get("last_api_error")
            and _newer(info.get("last_api_error_at"), info.get("last_api_ok_at"))
            and 0 <= (now - err_at).total_seconds() <= ERROR_NOTE_TTL_S):
        note = f"Lần trước: {explain_error(info['last_api_error'])} lúc {info['last_api_error_at'][11:16]}"
    return {"state": state, "label": label, "tone": TONES[state], "detail": detail, "note": note}
