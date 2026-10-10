"""Kiểm tra môi trường cho UI/CLI, không sửa máy hoặc lộ bí mật.

Mỗi mục chưa đạt đều kèm ``fix``: một câu hướng dẫn và (nếu làm được) một nút —
mở trang cài đặt liên quan, mở trang tải, hoặc để ứng dụng tự sửa.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import uuid
from pathlib import Path

from backup_policy import latest_backup_time
from catalog_layers import CatalogLayerError, read_json
from config import (
    APPROVED_CATALOG_PATH, APPROVED_PROFILES_PATH, BACKUP_DIR, DEEPSEEK_API_KEY,
    EXTENSION_DIR, IS_FROZEN, LIVE_CATALOG_PATH, LOG_DIR,
    MACHINE_CATALOG_MAPPING_PATH, PLUGINS_DIR, ROOT, find_tool,
)
from plugin_registry import discover_plugins

REINSTALL = "Cài lại Kid TE bằng bộ cài mới nhất (dữ liệu được giữ nguyên)." if IS_FROZEN else \
    "Chạy: backend\\.venv\\Scripts\\pip install -r backend\\requirements.txt"


def fix(text, label="", **action):
    """``action``: page/sub (mở trang trong app), open (mở trang/thư mục), api (tự sửa)."""
    return {"text": text, "label": label, **action}


def _check(check_id, label, status, detail, *, fixable=False, fix=None, impact=""):
    row = {"id": check_id, "label": label, "status": status,
           "detail": detail, "fixable": bool(fixable), "impact": impact}
    if fix and status != "ok":
        row["fix"] = fix
    return row


def _webview2_version():
    if os.name != "nt":
        return None
    try:
        import winreg
        roots = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
        paths = (r"SOFTWARE\Microsoft\EdgeUpdate\Clients",
                 r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients")
        for hive in roots:
            for path in paths:
                try:
                    with winreg.OpenKey(hive, path) as clients:
                        count = winreg.QueryInfoKey(clients)[0]
                        for index in range(count):
                            with winreg.OpenKey(clients, winreg.EnumKey(clients, index)) as item:
                                name = str(winreg.QueryValueEx(item, "name")[0])
                                if "webview2" in name.casefold():
                                    try:
                                        return str(winreg.QueryValueEx(item, "pv")[0])
                                    except OSError:
                                        return "đã cài"
                except OSError:
                    continue
    except (ImportError, OSError):
        return None
    return None


def _json_status(path, label, *, missing_fix=None):
    if not Path(path).exists():
        # Máy mới cài chưa có là BÌNH THƯỜNG — hướng dẫn tạo, không báo lỗi.
        return _check("json-" + Path(path).stem, label, "warn", "Chưa có (máy mới)",
                      impact="AI chưa có hồ sơ kỹ thuật để trả lời công dụng / liều dùng",
                      fix=missing_fix or fix("Tạo hồ sơ cho sản phẩm đang bán.", "Mở Hồ sơ sản phẩm",
                                             page="catalog", sub="profiles"))
    try:
        read_json(path)
        return _check("json-" + Path(path).stem, label, "ok", "Đọc được, không hỏng")
    except CatalogLayerError as exc:
        return _check("json-" + Path(path).stem, label, "error", str(exc),
                      impact="AI thiếu dữ liệu sản phẩm này khi trả lời",
                      fix=fix("File bị hỏng hoặc bị xoá. Khôi phục mục \"Hồ sơ sản phẩm\" từ một bản sao lưu.",
                              "Mở Sao lưu", page="system", sub="backups"))


def _writable_status(path):
    path = Path(path)
    auto = fix("Ứng dụng tự tạo lại thư mục này.", "Tự sửa", api="/api/diagnostics/fix")
    if not path.exists():
        return _check("write-" + path.name, f"Thư mục {path.name}", "warn",
                      "Chưa có thư mục", fixable=True, fix=auto)
    try:
        probe = path / f".write-check-{uuid.uuid4().hex}"
        probe.write_bytes(b"")
        probe.unlink()
        return _check("write-" + path.name, f"Thư mục {path.name}", "ok", str(path))
    except OSError as exc:
        return _check("write-" + path.name, f"Thư mục {path.name}", "error", type(exc).__name__,
                      fix=fix(f"Không ghi được vào {path}. Kiểm tra ổ đĩa còn chỗ trống, hoặc thư mục "
                              "đang bị phần mềm diệt virus / OneDrive khoá."))


def _disk_status():
    try:
        free = shutil.disk_usage(ROOT).free / 1024 ** 3
    except OSError:
        return _check("disk", "Chỗ trống ổ dữ liệu", "warn", "Không đọc được")
    status = "ok" if free >= 5 else "warn" if free >= 1 else "error"
    return _check("disk", "Chỗ trống ổ dữ liệu", status, f"Còn {free:.1f} GB trống ({ROOT.anchor})",
                  impact="Sao lưu, tải video và model chép lời có thể lỗi giữa chừng",
                  fix=fix("Xoá bớt video cũ trong Thư viện video, dọn Thùng rác, hoặc giải phóng ổ đĩa.",
                          "Mở Thùng rác", page="system", sub="trash"))


def run_setup_checks(*, include_audio=True, key_configured=None):
    """``key_configured``: trạng thái khoá ĐANG dùng (máy chủ biết); None = đọc .env lúc mở app."""
    checks = []
    has_key = bool(DEEPSEEK_API_KEY) if key_configured is None else bool(key_configured)
    for module, required, why in (
            ("openai", True, "gọi DeepSeek"), ("dotenv", True, "đọc cài đặt"),
            ("webview", True, "mở cửa sổ ứng dụng"), ("numpy", False, "đồng bộ video"),
            ("soundcard", False, "nghe âm thanh video"),
            ("faster_whisper", False, "chép lời video tải lên"), ("PIL", False, "ảnh sản phẩm")):
        found = importlib.util.find_spec(module) is not None
        checks.append(_check("module-" + module, f"Thành phần {module} ({why})",
                             "ok" if found else "error" if required else "warn",
                             "Đã cài" if found else "Chưa cài", fix=fix(REINSTALL)))
    version = _webview2_version()
    checks.append(_check("webview2", "Microsoft WebView2 (khung hiển thị ứng dụng)",
                         "ok" if version else "error", version or "Không tìm thấy",
                         impact="Ứng dụng không mở được cửa sổ riêng",
                         fix=fix("Tải và cài \"Evergreen Bootstrapper\" của Microsoft (miễn phí, 2 phút), "
                                 "rồi mở lại Kid TE.", "Mở trang tải WebView2", open="webview2")))
    extension_ok = bool(EXTENSION_DIR and (EXTENSION_DIR / "manifest.json").is_file())
    checks.append(_check("extension", "Thư mục tiện ích Chrome", "ok" if extension_ok else "error",
                         str(EXTENSION_DIR) if extension_ok else "Không tìm thấy manifest",
                         fix=fix(REINSTALL)))
    checks.append(_check("api-key", "Khoá DeepSeek", "ok" if has_key else "error",
                         "Đã cấu hình (giá trị được che)" if has_key else "Chưa cấu hình",
                         impact="AI không trả lời được khách",
                         fix=fix("Tạo khoá ở trang DeepSeek (platform.deepseek.com → API keys), rồi dán vào "
                                 "Cài đặt → Cài đặt.", "Nhập khoá", page="system", sub="setup")))
    ffmpeg = find_tool("ffmpeg")
    checks.append(_check("ffmpeg", "ffmpeg (xử lý video)", "ok" if ffmpeg else "warn",
                         ffmpeg or "Không tìm thấy",
                         impact="Studio thiếu hình xem trước/sóng âm; không phân tích được video tải lên",
                         fix=fix(REINSTALL if IS_FROZEN else
                                 "Tải bản \"release essentials\", giải nén ffmpeg.exe + ffprobe.exe vào thư mục "
                                 "tools\\ffmpeg của dự án.", "" if IS_FROZEN else "Mở trang tải ffmpeg",
                                 **({} if IS_FROZEN else {"open": "ffmpeg"}))))
    try:
        from whisper_models import active_model_path
        model = active_model_path()
    except Exception:
        model = None
    checks.append(_check("whisper", "Model chép lời video", "ok" if model else "warn",
                         model or "Chưa tải model nào",
                         impact="Không tự chép lời và gợi ý lịch ghim khi tải video lên",
                         fix=fix("Chọn model hợp với máy này và bấm Tải (một lần, cần mạng).",
                                 "Mở Chép lời", page="system", sub="whisper")))
    # Máy mới cài: kho hồ sơ dùng chung và hồ sơ LIVE cũ đều không có — chỉ kiểm
    # khi file tồn tại (hỏng mới là lỗi). Cái cần có là ánh xạ giỏ ↔ hồ sơ.
    for path, label in ((APPROVED_CATALOG_PATH, "Kho hồ sơ sản phẩm dùng chung"),
                        (APPROVED_PROFILES_PATH, "Hồ sơ LIVE đã duyệt")):
        if Path(path).exists():
            checks.append(_json_status(path, label))
    checks.append(_json_status(MACHINE_CATALOG_MAPPING_PATH, "Ánh xạ giỏ hàng ↔ hồ sơ",
                               missing_fix=fix("Tạo hồ sơ cho từng sản phẩm trong giỏ (nút Tạo hồ sơ ở trang "
                                               "Sản phẩm) — hồ sơ tạo từ sản phẩm được gắn sẵn.",
                                               "Mở Sản phẩm", page="catalog", sub="")))
    from config import SHOP_INFO_PATH
    checks.append(_check("shop-info", "Thông tin shop (địa chỉ, hotline)",
                         "ok" if Path(SHOP_INFO_PATH).is_file() else "warn",
                         "Đã nhập" if Path(SHOP_INFO_PATH).is_file() else "Chưa nhập",
                         impact="Khách hỏi shop ở đâu / số điện thoại thì AI không trả lời được",
                         fix=fix("Nhập tên, địa chỉ, hotline của shop.", "Nhập thông tin shop",
                                 page="system", sub="setup")))
    if LIVE_CATALOG_PATH.exists():
        checks.append(_json_status(LIVE_CATALOG_PATH, "Giỏ hàng LIVE gần nhất"))
    else:
        checks.append(_check("live-catalog", "Giỏ hàng LIVE gần nhất", "warn", "Chưa quét giỏ lần nào",
                             impact="AI chưa biết shop đang bán gì",
                             fix=fix("Mở TikTok LIVE Manager trong Chrome (có tiện ích Kid TE), bấm biểu tượng "
                                     "tiện ích → QUÉT GIỎ TRƯỚC LIVE.")))
    for path in (ROOT / "runtime", LOG_DIR, BACKUP_DIR):
        checks.append(_writable_status(path))
    checks.append(_disk_status())
    manifests, errors = discover_plugins(PLUGINS_DIR)
    checks.append(_check("plugins", "Bộ xử lý & dịch vụ", "ok" if not errors else "error",
                         f"{len(manifests)} bộ đã nạp" if not errors else "; ".join(
                             f"{key}: {value}" for key, value in sorted(errors.items())),
                         fix=fix(REINSTALL)))
    latest = latest_backup_time(BACKUP_DIR)
    checks.append(_check("backup", "Bản sao lưu đầy đủ gần nhất", "ok" if latest else "warn",
                         latest.isoformat() + ' · Chưa kiểm tra checksum trong lượt này' if latest else "Chưa có bản đầy đủ được ghi nhận",
                         fix=fix("Tạo một bản sao lưu ngay.", "Sao lưu ngay", api="/api/backup")))
    if include_audio:
        try:
            from audio_capture import is_forbidden_device, list_devices
            devices = list_devices()
            loopback = [item for item in devices if item.get("loopback") and not is_forbidden_device(item)]
            checks.append(_check("audio", "Thiết bị âm thanh", "ok" if loopback else "warn",
                                 f"{len(devices)} thiết bị; {len(loopback)} nguồn nghe được âm thanh máy",
                                 impact="Đồng bộ video không nghe được video đang phát",
                                 fix=fix("Cắm loa/tai nghe hoặc bật thiết bị phát âm thanh trong Windows, rồi "
                                         "chọn nguồn ở Cài đặt → Âm thanh.", "Mở Âm thanh",
                                         page="system", sub="audio")))
        except Exception as exc:
            checks.append(_check("audio", "Thiết bị âm thanh", "warn", type(exc).__name__,
                                 fix=fix(REINSTALL)))
    counts = {status: sum(1 for item in checks if item["status"] == status)
              for status in ("ok", "warn", "error")}
    try:
        from display_info import probe_displays
        displays = probe_displays()
    except Exception:
        displays = []
    return {"ok": counts["error"] == 0, "checks": checks, "counts": counts,
            "python": sys.version.split()[0], "displays": displays}


def fix_safe_setup():
    created = []
    for path in (ROOT / "runtime", LOG_DIR, BACKUP_DIR):
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created.append(str(path))
    return {"ok": True, "created": created, "checks": run_setup_checks(include_audio=False)}
