"""Tạo một bản sao lưu vận hành Kid TE (giống nút "Sao lưu ngay" trong app)."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from backup import BackupError  # noqa: E402
from backup_policy import create_operational_backup  # noqa: E402
from console_io import force_utf8_console  # noqa: E402


def main() -> int:
    force_utf8_console()
    try:
        result = create_operational_backup()
    except BackupError as exc:
        print(f"KHÔNG TẠO BACKUP: {exc}")
        return 1
    print(f"ĐÃ SAO LƯU: {result['backup_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
