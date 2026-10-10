"""Thư viện video phát lại: mỗi video một thư mục dữ liệu riêng.

    data/videos/<video_id>/timeline.json    lịch ghim + ngữ cảnh kịch bản
    data/videos/<video_id>/fingerprint.npz  dấu vân tay âm thanh

Trước đây lịch nằm cứng trong `schedule.js` nên đổi video là phải sửa mã
nguồn. Ở đây lịch là dữ liệu, và vì mỗi video có vân tay riêng nên hệ thống
tự nhận ra ĐANG PHÁT VIDEO NÀO rồi dùng đúng lịch của video đó.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from config import DATA_DIR

VIDEO_DIR = DATA_DIR / "videos"
TIMELINE_NAME = "timeline.json"
FINGERPRINT_NAME = "fingerprint.npz"


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def validate_timeline(timeline):
    """Cùng bộ luật với hàm validate trong schedule.js.

    Một timeline sai không được phép lên live: người gọi giữ bản đang dùng và
    ghi lại lỗi, chứ không nạp bản mới.
    """
    errors = []
    if not isinstance(timeline, dict):
        return ["timeline không phải object"]
    if not str(timeline.get("video_id") or "").strip():
        errors.append("thiếu video_id")
    cycle = _number(timeline.get("cycle_ms"))
    if not cycle or cycle < 1000:
        errors.append("cycle_ms phải là số ms > 1000")
    products = timeline.get("products")
    if not isinstance(products, dict) or not products:
        errors.append("thiếu products")
        products = {}
    else:
        for key, item in products.items():
            if not isinstance(item, dict):
                errors.append(f"products.{key}: phải là object")
                continue
            if _number(item.get("bag_index")) is None:
                errors.append(f"products.{key}: bag_index sai")
            include = item.get("include_all")
            if not isinstance(include, list) or not include:
                errors.append(f"products.{key}: include_all phải là mảng không rỗng")
            if item.get("exclude_any") is not None and not isinstance(
                item.get("exclude_any"), list
            ):
                errors.append(f"products.{key}: exclude_any phải là mảng")
    events = timeline.get("events")
    if not isinstance(events, list) or not events:
        errors.append("thiếu events")
    else:
        previous = -1.0
        for index, event in enumerate(events):
            if not isinstance(event, dict):
                errors.append(f"events[{index}]: phải là object")
                continue
            at = _number(event.get("at_ms"))
            if at is None:
                errors.append(f"events[{index}]: at_ms sai")
            elif at <= previous:
                errors.append(f"events[{index}]: at_ms phải tăng dần")
            elif cycle and at >= cycle:
                errors.append(f"events[{index}]: at_ms vượt cycle_ms")
            else:
                previous = at
            key = event.get("product", None)
            if key is not None and key not in products:
                errors.append(f'events[{index}]: product "{key}" không có trong products')
            other = event.get("alternate_with")
            if other is not None:
                if other not in products:
                    errors.append(f'events[{index}]: alternate_with "{other}" không có')
                if not _number(event.get("alternate_every_ms")):
                    errors.append(f"events[{index}]: alternate_with cần alternate_every_ms")
        if events and _number(events[0].get("at_ms")) != 0:
            errors.append("events[0].at_ms phải là 0")
    contexts = timeline.get("script_contexts")
    if contexts is not None and not isinstance(contexts, list):
        errors.append("script_contexts phải là mảng")
    return errors


def load_timeline(path):
    path = Path(path)
    try:
        timeline = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"không đọc được: {type(exc).__name__}: {exc}"]
    return timeline, validate_timeline(timeline)


def list_videos(check_fingerprint=True):
    """Liệt kê mọi video có trong thư viện, kèm lỗi nếu có."""
    results = []
    if not VIDEO_DIR.exists():
        return results
    for folder in sorted(VIDEO_DIR.iterdir()):
        if not folder.is_dir():
            continue
        timeline_path = folder / TIMELINE_NAME
        if not timeline_path.exists():
            results.append({"video_id": folder.name, "timeline": None,
                            "errors": [f"thiếu {TIMELINE_NAME}"],
                            "fingerprint": None})
            continue
        timeline, errors = load_timeline(timeline_path)
        fingerprint = folder / FINGERPRINT_NAME
        if check_fingerprint and not fingerprint.exists():
            errors = list(errors) + [f"thiếu {FINGERPRINT_NAME}"]
        if timeline and str(timeline.get("video_id") or "") != folder.name:
            errors = list(errors) + [
                f'video_id "{timeline.get("video_id")}" khác tên thư mục "{folder.name}"'
            ]
        results.append({
            "video_id": folder.name,
            "timeline": timeline,
            "errors": errors,
            "fingerprint": str(fingerprint) if fingerprint.exists() else None,
        })
    return results


def load_references():
    """Nạp toàn bộ vân tay để so cùng lúc, biết đang phát video nào."""
    from video_fingerprint import load_reference
    references = []
    for item in list_videos():
        if not item["fingerprint"]:
            continue
        try:
            reference = load_reference(item["fingerprint"])
        except Exception as exc:
            references.append({"video_id": item["video_id"], "error":
                               f"{type(exc).__name__}: {exc}"})
            continue
        reference["video_id"] = item["video_id"]
        reference["title"] = (item["timeline"] or {}).get("title", item["video_id"])
        references.append(reference)
    return references
