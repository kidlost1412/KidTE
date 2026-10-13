import os
import sys
from pathlib import Path

from dotenv import load_dotenv


IS_FROZEN = bool(getattr(sys, "frozen", False))
INSTALL_ROOT = Path(sys.executable).resolve().parent if IS_FROZEN else Path(__file__).resolve().parents[2]


def _app_package_root():
    """Directory holding Kid TE's own code, plugins, web UI and prompts.

    Split layout: the launcher verifies the active ``app`` package and passes it in
    ``KIDTE_APP_PACKAGE_DIR``. Without it (installer baseline, direct start) the app
    sits next to ``KidTE.exe``. Source runs use the checkout.
    """
    if not IS_FROZEN:
        return INSTALL_ROOT
    explicit = os.getenv("KIDTE_APP_PACKAGE_DIR", "").strip()
    return Path(explicit).resolve() if explicit else INSTALL_ROOT


APP_PACKAGE_ROOT = _app_package_root()
if IS_FROZEN:
    # Legacy monolithic builds froze the web UI and prompts into the bundle.
    RESOURCE_ROOT = (APP_PACKAGE_ROOT / "backend"
                     if (APP_PACKAGE_ROOT / "backend" / "src" / "app_entry.py").is_file()
                     else Path(sys._MEIPASS) / "backend")
else:
    RESOURCE_ROOT = Path(__file__).resolve().parent.parent


def _runtime_root():
    """Chọn nơi chứa dữ liệu thật, ưu tiên bố cục portable cạnh tool.

    Bản bàn giao trên ổ D đặt executable trong ``KidTE`` và dữ liệu trong
    ``DuLieu`` ở cùng thư mục cha. Dùng đường dẫn thật này thay vì đi vòng qua
    junction AppData: thao tác lock độc quyền trên Windows không ổn định khi
    file được tạo/xóa liên tục qua junction khác ổ đĩa.
    """
    override = (os.getenv("KID_TE_ROOT", "") or os.getenv("LIVE_RE_ROOT", "")).strip()
    if override:
        return Path(override).expanduser()
    if IS_FROZEN:
        app_root = os.getenv("KIDTE_APP_ROOT", "").strip()
        portable = (Path(app_root) if app_root else INSTALL_ROOT).parent / "DuLieu"
        if portable.is_dir():
            return portable
        # Lần đầu chưa có ``DuLieu`` thì TẠO tại thư mục gốc portable.
        #
        # Bản trước khi đổi tên từng lặng lẽ lui về ``%LOCALAPPDATA%\\LiveRe``
        # trong phiên nâng cấp: một lần chạy mà ``DuLieu`` chưa kịp tồn tại đã
        # ghi cả hai database, log và cấu hình vào AppData — dữ liệu thật nằm
        # ở nơi người vận hành không ngờ tới, không nằm trong thư mục bàn giao
        # nên không được backup, và lần chạy sau (khi ``DuLieu`` đã có) lại
        # đọc một kho hoàn toàn khác.
        try:
            portable.mkdir(parents=True, exist_ok=True)
            return portable
        except OSError as exc:
            # Không ghi được thì phải nói rõ, không đổi chỗ trong im lặng.
            du_phong = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "KidTE"
            print(f"[kid-te] KHÔNG TẠO ĐƯỢC THƯ MỤC DỮ LIỆU {portable}: {exc}\n"
                  f"[kid-te] Đang dùng tạm {du_phong}. Hãy cài vào thư mục có "
                  f"quyền ghi, hoặc đặt biến môi trường KID_TE_ROOT.", flush=True)
            return du_phong
    return RESOURCE_ROOT


ROOT = _runtime_root()
PROJECT_ROOT = APP_PACKAGE_ROOT
PLUGINS_DIR = PROJECT_ROOT / "plugins"
load_dotenv(ROOT / ".env")

DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()   # = V4.1 Flash
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# Tên model Gemini đổi theo thời gian: để chủ shop sửa trong .env nếu Google đổi tên.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
KNOWLEDGE_PACKAGE_SOURCE = os.getenv("KNOWLEDGE_PACKAGE_SOURCE", "").strip()
DEEPSEEK_DISABLE_THINKING = os.getenv("DEEPSEEK_DISABLE_THINKING", "1") == "1"
DEEPSEEK_REASONING_EFFORT = os.getenv(
    "DEEPSEEK_REASONING_EFFORT", "high"
).strip().lower()
AUDIO_DEVICE_ID = (os.getenv("KID_TE_AUDIO_DEVICE", "") or
                   os.getenv("LIVE_RE_AUDIO_DEVICE", "auto")).strip() or "auto"
CHAT_HOST = os.getenv("CHAT_HOST", "127.0.0.1").strip()
CHAT_PORT = int(os.getenv("CHAT_PORT", "8793"))
# TikTok cho bình luận tối đa 150 ký tự (tính cả "@tên_khách "). Bản cũ để 100
# nên 11,5% câu AI gửi bị cắt cụt giữa chừng ("...rồi đặt…").
CHAT_MAX_CHARS = int(os.getenv("CHAT_MAX_CHARS", "150"))
CHAT_MAX_TOKENS = int(os.getenv("CHAT_MAX_TOKENS", "2400"))
# 0.25 cũ làm câu lặp y khuôn ("…có hông rồi chốt liền nha" 10/182 câu). DeepSeek
# khuyên 1.3 cho hội thoại; 0.8 giữ tự nhiên mà vẫn bám dữ kiện trong ngữ cảnh.
CHAT_TEMPERATURE = float(os.getenv("CHAT_TEMPERATURE", "0.8"))
MEMORY_MAX_TURNS = int(os.getenv("MEMORY_MAX_TURNS", "5"))
RAW_STREAM_ENABLED = os.getenv("RAW_STREAM_ENABLED", "1") == "1"
TIKTOK_HOST_USER_IDS = {
    item.strip() for item in os.getenv("TIKTOK_HOST_USER_IDS", "").split(",")
    if item.strip()
}
TIKTOK_HOST_UNIQUE_IDS = {
    item.strip().casefold()
    for item in os.getenv("TIKTOK_HOST_UNIQUE_IDS", "").split(",")
    if item.strip()
}

DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
BACKUP_DIR = ROOT / "backups"
MEMORY_DB = DATA_DIR / "customer_memory.sqlite3"
OPERATOR_WORKSPACE_DB = DATA_DIR / "operator_workspace.sqlite3"
AI_LEARNING_DB = DATA_DIR / "ai_learning.sqlite3"
VIDEO_LIBRARY_DIR = DATA_DIR / "videos"
MEDIA_BINDINGS_DB = DATA_DIR / "media_bindings.sqlite3"
MEMORY_JOURNAL_DIR = LOG_DIR / "customer_sessions"
MISSING_DIR = LOG_DIR / "missing_answers"
RAW_STREAM_DIR = LOG_DIR / "raw_stream"
LIVE_CATALOG_PATH = DATA_DIR / "live_products_current.json"
LIVE_RAW_CATALOG_PATH = DATA_DIR / "live_products_raw_current.json"
SHARED_PRODUCT_KNOWLEDGE_PATH = DATA_DIR / "shared" / "product_knowledge.json"
MACHINE_CATALOG_MAPPING_PATH = DATA_DIR / "machine" / "catalog_mapping.json"
SHOPEE_LIVE_MAPPING_PATH = DATA_DIR / "machine" / "shopee_live_mappings.json"
APPROVED_CATALOG_PATH = SHARED_PRODUCT_KNOWLEDGE_PATH
APPROVED_PROFILES_PATH = DATA_DIR / "approved_live_profiles.json"
SHOP_INFO_PATH = DATA_DIR / "thong_tin_shop.json"
CATALOG_MAPPING_PATH = DATA_DIR / "company_catalog_mapping.json"
SYSTEM_PROMPT_PATH = RESOURCE_ROOT / "prompts" / "system_prompt.txt"
SALES_PLAYBOOK_PATH = DATA_DIR / "shared" / "sales_playbook.json"
KNOWLEDGE_SEED_PATH = RESOURCE_ROOT / "prompts" / "knowledge_seed.json"
SALES_PLAYBOOK_DEFAULT_PATH = RESOURCE_ROOT / "prompts" / "sales_playbook.default.json"
# Bản lời nhắc chủ shop tự sửa (Trợ lý tự động → DeepSeek). Không có thì dùng bản gốc.
SYSTEM_PROMPT_OVERRIDE_PATH = Path((os.getenv("KID_TE_PROMPT_OVERRIDE", "") or
                                    os.getenv("LIVE_RE_PROMPT_OVERRIDE", "")).strip()
                                   or DATA_DIR / "loi_nhac_he_thong.txt")
PROCESSOR_SETTINGS_PATH = Path((os.getenv("KID_TE_PROCESSOR_SETTINGS", "") or
                                os.getenv("LIVE_RE_PROCESSOR_SETTINGS", "")).strip()
                               or DATA_DIR / "cai_dat_bo_xu_ly.json")
WHISPER_MODELS_DIR = ROOT / "models"
LOCAL_PRODUCT_OVERRIDES_PATH = DATA_DIR / "local_product_overrides.json"
DOCUMENTS_DIR = DATA_DIR / "tai_lieu"
WEB_DIR = RESOURCE_ROOT / "src" / "web"


def _find_extension_dir():
    """Tìm thư mục tiện ích Chrome mà không đóng cứng tên thư mục.

    Dự án có thể chạy ở cấu trúc nguồn cũ hoặc cấu trúc bàn giao mới, nên chỉ
    dò theo dấu hiệu chắc
    chắn: có manifest.json và schedule.js.
    """
    override = (os.getenv("KID_TE_EXTENSION_DIR", "") or
                os.getenv("LIVE_RE_EXTENSION_DIR", "")).strip()
    candidates = []
    if override:
        candidates.append(Path(override))
    if IS_FROZEN:
        # Chrome keeps unpacked extensions at a stable path through core updates.
        app_root = os.getenv("KIDTE_APP_ROOT", "").strip()
        if app_root:
            candidates.append(Path(app_root) / "extension")
        # Ban dong goi dat extension canh KidTE.exe. ROOT lai la AppData de
        # giu du lieu, nen tim theo ROOT se bao sai "khong co manifest".
        candidates.append(INSTALL_ROOT / "extension")
    candidates += [
        ROOT.parent / "extension",
        ROOT / "extension",
    ]
    for folder in candidates:
        if (folder / "manifest.json").exists() and (folder / "schedule.js").exists():
            return folder
    return None


EXTENSION_DIR = _find_extension_dir()


def find_tool(name):
    """Find the selected media-tools package, then use legacy/source fallbacks.

    Máy mới thường không có ffmpeg; không có nó thì Studio thiếu poster/dải hình
    /sóng âm và không phân tích được video tải lên.
    """
    import shutil
    if name not in {"ffmpeg", "ffprobe"}:
        raise ValueError("unsupported_media_tool")

    extension = ".exe" if IS_FROZEN or os.name == "nt" else ""
    filename = name + extension
    if IS_FROZEN:
        # A selected package is authoritative. component_path raises if its
        # active record or installed files are invalid; never hide that by
        # falling through to a stale copy or a PATH installation.
        from component_runtime import component_path
        selected = component_path("media-tools", f"tools/{filename}")
        if selected is not None:
            candidate = Path(selected)
            if not candidate.is_file():
                raise RuntimeError(f"selected_media_tool_missing:{name}")
            return str(candidate)

    for folder in (INSTALL_ROOT / "tools", PROJECT_ROOT / "tools" / "ffmpeg"):
        candidate = folder / filename
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)
