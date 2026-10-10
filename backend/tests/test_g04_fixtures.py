import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from display_info import parse_displays
from log_viewer import recent_logs
from structured_log import configure_logger

FIX = Path(__file__).parent / "fixtures" / "powershell"


def test_rotation_at_size_limit(tmp_path):
    log = configure_logger("g04rot", tmp_path, max_bytes=600, backup_count=2, console=False)
    for i in range(40):
        log.info("x" * 60 + str(i))
    for h in log.handlers:
        h.flush()
    files = sorted(p.name for p in tmp_path.glob("g04rot.jsonl*"))
    assert files == ["g04rot.jsonl", "g04rot.jsonl.1", "g04rot.jsonl.2"]
    assert all(p.stat().st_size <= 900 for p in tmp_path.glob("g04rot.jsonl*"))
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)


def test_partial_last_line_tolerated(tmp_path):
    good = json.dumps({"timestamp": "2026-01-01T00:00:00", "severity": "error", "message": "ok", "event": "e"})
    (tmp_path / "svc.jsonl").write_text(good + "\n" + good[:25], encoding="utf-8")
    rows = recent_logs(tmp_path)["rows"]
    assert len(rows) == 1 and rows[0]["message"] == "ok"


def test_multi_monitor_parse():
    d = parse_displays((FIX / "displays_multi.txt").read_text(encoding="utf-8"))
    assert [x["width"] for x in d] == [1920, 2560]
    assert d[0]["primary"] and not d[1]["primary"]
    assert d[1]["scale"] == 1.25


def test_dpi_150_parse():
    d = parse_displays((FIX / "displays_150.txt").read_text(encoding="utf-8-sig"))
    assert d[0]["scale"] == 1.5 and d[0]["dpi"] == 144


def test_orphan_process_cleanup():
    import subprocess
    from process_cleanup import stop_process_tree
    p = subprocess.Popen([sys.executable, "-c",
                          "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);time.sleep(60)"])
    import time; time.sleep(1)
    stop_process_tree(p, timeout_s=3)
    assert p.poll() is not None
