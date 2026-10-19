import update_launcher
from launcher_rev import REQUIRED_LAUNCHER_REV, launcher_status

URL = "https://github.com/kidlost1412/KidTE/releases/tag/v1.15.17-p4"


def test_direct_run_never_reports():
    assert launcher_status("1.15.17", environ={}) == {"launcher_outdated": False}
    assert launcher_status("1.15.17", environ={"KIDTE_LAUNCHER_REV": "0"}) == {"launcher_outdated": False}


def test_old_launcher_without_rev_reports():
    r = launcher_status("1.15.17", environ={"KIDTE_LAUNCH_NONCE": "n"})
    assert r["launcher_outdated"] is True
    assert r["launcher_rev"] == 0 and r["launcher_required_rev"] == REQUIRED_LAUNCHER_REV
    assert r["launcher_download_url"] == URL


def test_low_or_garbage_rev_reports():
    for value in ("1", "abc", ""):
        env = {"KIDTE_LAUNCH_NONCE": "n", "KIDTE_LAUNCHER_REV": value}
        assert launcher_status("1.15.17", environ=env)["launcher_outdated"] is True


def test_current_or_newer_rev_is_silent():
    for value in (str(REQUIRED_LAUNCHER_REV), str(REQUIRED_LAUNCHER_REV + 3)):
        env = {"KIDTE_LAUNCH_NONCE": "n", "KIDTE_LAUNCHER_REV": value}
        assert launcher_status("1.15.17", environ=env)["launcher_outdated"] is False


def test_launcher_in_repo_meets_requirement():
    assert update_launcher.LAUNCHER_REV >= REQUIRED_LAUNCHER_REV
