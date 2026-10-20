"""Lease "người gọi TikTok" giữa nhiều trình duyệt/profile (chỉ trong bộ nhớ)."""
import json
import threading

import pytest

import tiktok_lease
from tiktok_lease import TikTokCallerLease
from test_security_pairing_ssrf import GOOD, OTHER, app  # noqa: F401  (fixture HTTP)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def req(lease, action, profile, tab="7"):
    return lease.handle({"action": action, "profile_id": profile, "tab_id": tab})


def test_claim_refuse_renew_expire_release():
    clock = Clock()
    lease = TikTokCallerLease(ttl=30, clock=clock)
    assert req(lease, "claim", "chrome")["granted"] is True
    denied = req(lease, "claim", "coccoc", "9")
    assert denied["granted"] is False and denied["retry_after_ms"] == 30000
    clock.now += 20
    assert req(lease, "renew", "chrome")["granted"] is True          # gia hạn thêm 30s
    clock.now += 20
    assert req(lease, "claim", "coccoc", "9")["granted"] is False    # holder vẫn còn hạn
    clock.now += 11
    assert req(lease, "claim", "coccoc", "9")["granted"] is True     # hết hạn -> profile khác nhận
    assert req(lease, "claim", "chrome")["granted"] is False


def test_release_hands_over_immediately_and_only_by_holder():
    lease = TikTokCallerLease(ttl=30, clock=Clock())
    req(lease, "claim", "chrome")
    assert req(lease, "release", "coccoc", "9")["released"] is False
    assert req(lease, "claim", "coccoc", "9")["granted"] is False
    assert req(lease, "release", "chrome")["released"] is True
    assert req(lease, "claim", "coccoc", "9")["granted"] is True


def test_same_profile_other_tab_takes_over():
    lease = TikTokCallerLease(ttl=30, clock=Clock())
    req(lease, "claim", "chrome", "7")
    assert req(lease, "claim", "chrome", "8")["granted"] is True
    assert req(lease, "claim", "coccoc", "7")["granted"] is False


@pytest.mark.parametrize("payload", [
    {"action": "grab", "profile_id": "a", "tab_id": "1"},
    {"action": "claim", "profile_id": "", "tab_id": "1"},
    {"action": "claim", "profile_id": "a b", "tab_id": "1"},
    {"action": "claim", "profile_id": "a", "tab_id": ""},
    [],
])
def test_invalid_payload(payload):
    with pytest.raises(ValueError):
        TikTokCallerLease().handle(payload)


def test_thread_safe_single_winner():
    lease = TikTokCallerLease(ttl=30, clock=Clock())
    results = []
    barrier = threading.Barrier(16)

    def worker(i):
        barrier.wait()
        results.append(req(lease, "claim", f"p{i}")["granted"])
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1


def test_http_lease_route(app, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tiktok_lease, "LEASE", TikTokCallerLease(ttl=30))
    body = {"action": "claim", "profile_id": "chrome", "tab_id": "7"}
    status, _, data = app.call("POST", "/api/tiktok/lease", body, GOOD)
    assert status == 200 and json.loads(data)["granted"] is True
    status, _, data = app.call("POST", "/api/tiktok/lease", dict(body, profile_id="coccoc"), GOOD)
    assert status == 200 and json.loads(data)["granted"] is False
    # Không phải tiện ích -> 403; tiện ích chưa ghép bị chặn như các route TikTok khác.
    assert app.call("POST", "/api/tiktok/lease", body)[0] == 403
    app.call("POST", "/api/heartbeat", {"platform": "tiktok"}, GOOD)
    assert app.call("POST", "/api/tiktok/lease", body, OTHER)[0] == 403
    status, _, data = app.call("POST", "/api/tiktok/lease", dict(body, action="x"), GOOD)
    assert status == 400


# ---- 3.11.11: lease theo tài khoản TikTok ----
def acc(lease, action, profile, tab, owner="", live=False):
    return lease.handle({"action": action, "profile_id": profile, "tab_id": tab,
                         "owner_key": owner, "live": live})


def test_two_accounts_never_block_each_other():
    lease = TikTokCallerLease(ttl=30, clock=Clock())
    assert acc(lease, "claim", "chrome", "7", "uid:1", live=True)["granted"] is True
    assert acc(lease, "claim", "coccoc", "9", "uid:2")["granted"] is True       # B trước LIVE
    assert acc(lease, "claim", "coccoc", "9", "uid:2", live=True)["granted"] is True  # 2 LIVE cùng lúc
    assert acc(lease, "renew", "chrome", "7", "uid:1", live=True)["granted"] is True


def test_same_account_only_one_browser_and_live_wins():
    clock = Clock()
    lease = TikTokCallerLease(ttl=30, clock=clock)
    assert acc(lease, "claim", "coccoc", "9", "uid:1")["granted"] is True        # B chưa LIVE giữ
    taken = acc(lease, "claim", "chrome", "7", "uid:1", live=True)                # A LIVE thật
    assert taken["granted"] is True and taken["preempted"] is True
    denied = acc(lease, "claim", "coccoc", "9", "uid:1")
    assert denied["granted"] is False and denied["holder_live"] is True
    assert acc(lease, "claim", "coccoc", "9", "uid:1", live=True)["granted"] is False  # 2 LIVE: chỉ 1


def test_closed_browser_frees_account_lease_and_reopen_is_immediate():
    clock = Clock()
    lease = TikTokCallerLease(ttl=30, clock=clock)
    acc(lease, "claim", "chrome", "7", "uid:1", live=True)
    acc(lease, "claim", "coccoc", "9", "uid:2")
    assert acc(lease, "release", "chrome", "7")["released"] is True              # A đóng tab
    assert acc(lease, "claim", "chrome", "11", "uid:1", live=True)["granted"] is True  # mở lại: nhận ngay
    # B bị kill (không release): hết TTL thì khoá của B tự trống.
    clock.now += 31
    assert acc(lease, "claim", "edge", "3", "uid:2")["granted"] is True


def test_tab_moving_from_shared_key_to_account_key_drops_old_key():
    lease = TikTokCallerLease(ttl=30, clock=Clock())
    acc(lease, "claim", "chrome", "7")                     # chưa biết tài khoản: khoá chung
    acc(lease, "claim", "chrome", "7", "uid:1")            # biết rồi: chuyển sang uid:1
    assert acc(lease, "claim", "coccoc", "9")["granted"] is True   # khoá chung không còn bị giữ
