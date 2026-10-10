import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def _module():
    path = Path(__file__).resolve().parents[2] / "plugins" / "dong-bo-video" / "video_sync.py"
    spec = importlib.util.spec_from_file_location("live_re_video_plugin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_video_health_is_atomic_and_preserves_previous_fields(tmp_path):
    video = _module()
    path = tmp_path / "runtime" / "health.json"
    video.write_health(path, state="running", device="Loopback A", video_id="live1")
    video.write_health(path, margin=0.1234, last_seen="now")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["state"] == "running"
    assert saved["device"] == "Loopback A"
    assert saved["video_id"] == "live1"
    assert saved["margin"] == 0.1234
    assert saved["last_seen"] == "now"
    assert "updated_at" in saved
    assert not path.with_suffix(".json.tmp").exists()


def test_relock_sticks_with_last_device_before_full_rescan(monkeypatch):
    """`[T9]` 'Mất thiết bị âm thanh -> bám lại -> mới dò toàn bộ': mất dấu
    lần đầu phải BÁM LẠI đúng thiết bị vừa dùng (không đánh thức cả dàn thiết
    bị USB); mất dấu lần THỨ HAI liên tiếp với cùng thiết bị mới được dò lại
    toàn bộ (device='auto')."""
    video = _module()
    calls = []

    def fake_run(args, references, last_device_id="", same_device_tries=0):
        calls.append({
            "device": args.device, "last_device_id": last_device_id,
            "same_device_tries": same_device_tries,
        })
        return None

    monkeypatch.setattr(video, "run", fake_run)
    args = SimpleNamespace(device="device-7")

    video.main_relock(args, references=[], last_device_id="device-7", same_device_tries=0)
    assert args.device == "device-7", "phai bam lai dung thiet bi cu, khong duoc doi sang auto"
    assert calls[-1] == {"device": "device-7", "last_device_id": "device-7", "same_device_tries": 1}

    video.main_relock(args, references=[], last_device_id="device-7", same_device_tries=1)
    assert args.device == "auto", "bam lai roi ma van mat dau -> phai do lai toan bo"
    assert calls[-1] == {"device": "auto", "last_device_id": "", "same_device_tries": 0}


def test_relock_with_no_previous_device_goes_straight_to_full_rescan(monkeypatch):
    """Chưa từng bám được thiết bị nào (`last_device_id` rỗng — vd mất dấu
    ngay từ lần khởi động đầu) thì không có gì để bám lại, phải dò toàn bộ
    ngay, không chờ thêm một lượt vô nghĩa."""
    video = _module()
    calls = []
    monkeypatch.setattr(video, "run", lambda *a, **k: calls.append((a, k)))
    args = SimpleNamespace(device="auto")

    video.main_relock(args, references=[], last_device_id="", same_device_tries=0)

    assert args.device == "auto"
