import json
import sys
from datetime import datetime, timedelta, timezone


import backup_policy
NAME_PFX = chr(92) * 2 + "." + chr(92)
import display_info
from backup_policy import run_startup_auto_backup


def _api(monitors, restored):
    return {"enum": lambda: list(range(len(monitors))),
            "info": lambda h: monitors[h]["info"],
            "dpi": lambda h: monitors[h]["dpi"],
            "awareness": lambda: (lambda: restored.append(1))}


def test_probe_with_mocked_api_two_monitors():
    restored = []
    mons = [
        {"info": {"name": NAME_PFX + "DISPLAY1", "rect": (0, 0, 1920, 1080), "work": (0, 0, 1920, 1040), "primary": True}, "dpi": 144},
        {"info": {"name": NAME_PFX + "DISPLAY2", "rect": (1920, 0, 3840, 1080), "work": (1920, 0, 3840, 1080), "primary": False}, "dpi": 96},
    ]
    out = display_info.probe_displays(_api=_api(mons, restored))
    assert out[0]["name"] == "DISPLAY1" and out[0]["primary"] is True and out[0]["scale"] == 1.5
    assert out[0]["work_height"] == 1040 and out[1]["x"] == 1920 and out[1]["width"] == 1920
    assert restored == [1]
    assert display_info.parse_displays("DISPLAY1 1920x1080 dpi=144 primary")[0]["name"] == out[0]["name"]


def test_probe_failure_returns_empty():
    def boom():
        raise OSError("x")
    assert display_info.probe_displays(_api={"enum": boom, "awareness": lambda: (lambda: None)}) == []


def test_probe_non_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert display_info.probe_displays() == []


def test_real_probe_smoke_types_only():
    out = display_info.probe_displays()
    assert isinstance(out, list)
    if sys.platform.startswith("win"):
        for row in out:
            assert isinstance(row["width"], int) and isinstance(row["dpi"], int)


def test_diagnostics_has_displays_field(monkeypatch):
    import diagnostics
    monkeypatch.setattr(display_info, "probe_displays", lambda: [{"name": "D1"}])
    assert diagnostics.run_setup_checks(include_audio=False)["displays"] == [{"name": "D1"}]


class _Svc:
    def __init__(self, root, fail=False):
        self.root, self.fail, self.calls = root, fail, 0

    def create_auto(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("x")
        return {"backup_id": "b2", "scope": "partial"}


def _manifest(root, age_hours, name="m1"):
    folder = root / "backups" / "v2" / "manifests"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    (folder / f"{name}.json").write_text(json.dumps({"created_at": stamp.isoformat()}), encoding="utf-8")


def test_v2_runs_when_v1_recent_but_no_v2(tmp_path):
    svc = _Svc(tmp_path)
    r = run_startup_auto_backup({"created": False, "reason": "recent_backup"}, lambda: svc)
    assert r["created"] is True and svc.calls == 1


def test_v2_skipped_when_recent_v2_exists(tmp_path):
    _manifest(tmp_path, 2)
    svc = _Svc(tmp_path)
    assert run_startup_auto_backup({"created": False}, lambda: svc)["reason"] == "v1_not_due"
    assert svc.calls == 0


def test_v2_runs_when_v2_old(tmp_path):
    _manifest(tmp_path, 30)
    svc = _Svc(tmp_path)
    assert run_startup_auto_backup({"created": False}, lambda: svc)["created"] is True


def test_background_never_raises(tmp_path):
    svc = _Svc(tmp_path, fail=True)
    t = run_startup_auto_backup({"created": False}, lambda: svc, background=True)
    t.join(5)
    assert svc.calls == 1
    assert run_startup_auto_backup({}, lambda: 1 / 0)["created"] is False


def test_no_two_concurrent_creates(tmp_path):
    assert backup_policy._V2_STARTUP_LOCK.acquire(blocking=False)
    try:
        svc = _Svc(tmp_path)
        assert run_startup_auto_backup({"created": True}, lambda: svc)["reason"] == "v2_busy"
        assert svc.calls == 0
    finally:
        backup_policy._V2_STARTUP_LOCK.release()


def test_uses_guarded_create_via_bound_provider(tmp_path):
    class Eng:
        def __init__(self):
            self.guarded = []
            self.svc = _Svc(tmp_path)
            self.svc.auto_password = lambda: "pw"

        def _backup_v2_service(self):
            return self.svc

        def _guarded_v2_create(self, password, full=True):
            self.guarded.append((password, full))
            return {"job_id": "j1"}
    eng = Eng()
    r = run_startup_auto_backup({"created": False}, eng._backup_v2_service)
    assert r["job_id"] == "j1" and eng.guarded == [("pw", False)] and eng.svc.calls == 0
