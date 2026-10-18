"""A3: gia hạn catalog không đổi gói; nhắc sớm hạn catalog; A2: prune dừng khi thiếu catalog feed."""
import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging"))
import release_prune_plan as rp  # noqa: E402
import update_release  # noqa: E402
from test_update_manager import Transport, done, package, signed  # noqa: E402
from test_update_release import payload  # noqa: E402
from update_catalog import CatalogError, canonical_json, verify_catalog  # noqa: E402
from update_manager import UpdateManager, catalog_expiry_info  # noqa: E402
from component_store import ComponentStore  # noqa: E402

CHANNEL = "stable"  # payload() của test_update_release dùng kênh stable


def _z(moment):
    return moment.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def signer(tmp_path, monkeypatch):
    """Khoá TEST tự sinh (không đụng khoá thật); DPAPI được thay bằng bọc giả."""
    monkeypatch.setattr(update_release, "_dpapi", lambda raw, decrypt=False: raw[5:] if decrypt else b"DPAPI" + raw)
    private = tmp_path / "external" / "release.dpapi"
    public = tmp_path / "public.json"
    key_id = update_release.keygen(private, public)
    trusted = {key_id: json.loads(public.read_text(encoding="utf-8"))["public_key"]}
    return private, trusted


def _sign_file(tmp_path, private, trusted, data, name):
    src = tmp_path / (name + ".payload.json")
    src.write_text(json.dumps(data), encoding="utf-8")
    out = tmp_path / name
    update_release.sign_catalog(src, private, out, trusted_keys=trusted)
    return out


def _manual_sign(tmp_path, data, name):
    """Ký bằng khoá test riêng, cho phép dựng catalog đã hết hạn / sai nội dung. Trả (đường dẫn, trusted)."""
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_id = "testkey"
    envelope = {"schema_version": 2, "key_id": key_id, "signed": data,
                "signature": base64.b64encode(key.sign(canonical_json(data))).decode("ascii")}
    path = tmp_path / name
    path.write_bytes(canonical_json(envelope) + b"\n")
    return path, {key_id: base64.b64encode(pub).decode("ascii")}


def test_renew_keeps_content_bumps_sequence_and_expiry_and_signature_verifies(tmp_path, signer):
    private, trusted = signer
    data = payload()
    data["sequence"] = 30
    published = _sign_file(tmp_path, private, trusted, data, "published.json")
    out_dir = tmp_path / "renew"
    now = datetime.now(timezone.utc)
    info = update_release.renew_catalog(published, out_dir, channel=CHANNEL, trusted_keys=trusted, now=now)
    assert info["sequence"] == 31
    renewed_payload = json.loads((out_dir / "catalog-payload.json").read_text(encoding="utf-8"))
    assert renewed_payload["components"] == data["components"]
    assert renewed_payload["expires_at"] == _z(now + timedelta(days=90))
    assert renewed_payload["issued_at"] == _z(now)
    assert renewed_payload["expires_at"] > data["expires_at"]
    assert data["sequence"] == 30  # bản gốc không bị sửa

    renewed = out_dir / "kidte-catalog-v2.json"
    update_release.sign_catalog(out_dir / "catalog-payload.json", private, renewed, trusted_keys=trusted)
    signed_payload = verify_catalog(renewed.read_bytes(), trusted, channel=CHANNEL, minimum_sequence=31, now=now)
    assert signed_payload["sequence"] == 31
    with pytest.raises(CatalogError, match="catalog_rollback"):
        verify_catalog(renewed.read_bytes(), trusted, channel=CHANNEL, minimum_sequence=32, now=now)
    assert update_release.check_renewed_catalog(renewed, published, channel=CHANNEL, trusted_keys=trusted, now=now) == []


def test_renew_works_on_expired_published_catalog(tmp_path):
    now = datetime.now(timezone.utc)
    data = payload()
    data["sequence"] = 7
    data["issued_at"] = _z(now - timedelta(days=100))
    data["expires_at"] = _z(now - timedelta(days=10))
    published, trusted = _manual_sign(tmp_path, data, "published.json")
    info = update_release.renew_catalog(published, tmp_path / "out", channel=CHANNEL, trusted_keys=trusted)
    assert info["sequence"] == 8
    assert info["expires_at"] > _z(now + timedelta(days=80))


def test_renew_refuses_untrusted_published_catalog(tmp_path):
    published, _ = _manual_sign(tmp_path, payload(), "published.json")
    with pytest.raises(CatalogError, match="unknown_signing_key|invalid_signature"):
        update_release.renew_catalog(published, tmp_path / "out", channel=CHANNEL, trusted_keys={"other": "AAAA"})
    assert not (tmp_path / "out" / "catalog-payload.json").exists()


def test_check_renewed_rejects_changed_components_wrong_sequence_and_untrusted_key(tmp_path, signer):
    private, trusted = signer
    data = payload()
    data["sequence"] = 5
    published = _sign_file(tmp_path, private, trusted, data, "published.json")

    def renewed_variant(mutate, name):
        variant = json.loads(json.dumps(data))
        variant["sequence"] = 6
        variant["issued_at"] = _z(datetime.now(timezone.utc))
        variant["expires_at"] = _z(datetime.now(timezone.utc) + timedelta(days=90))
        mutate(variant)
        return _sign_file(tmp_path, private, trusted, variant, name)

    good = renewed_variant(lambda v: None, "good.json")
    assert update_release.check_renewed_catalog(good, published, channel=CHANNEL, trusted_keys=trusted) == []

    tampered = renewed_variant(lambda v: v["components"][0].update(version="9.9.9"), "tampered.json")
    problems = update_release.check_renewed_catalog(tampered, published, channel=CHANNEL, trusted_keys=trusted)
    assert any("thanh phan" in p for p in problems)

    skipped = renewed_variant(lambda v: v.update(sequence=9), "skipped.json")
    assert any("Sequence" in p for p in update_release.check_renewed_catalog(skipped, published, channel=CHANNEL, trusted_keys=trusted))

    same_seq = renewed_variant(lambda v: v.update(sequence=5), "same.json")
    assert any("minimum_sequence" in p for p in update_release.check_renewed_catalog(same_seq, published, channel=CHANNEL, trusted_keys=trusted))

    assert update_release.check_renewed_catalog(good, published, channel=CHANNEL, trusted_keys={"x": "AAAA"})


def test_catalog_expiry_info_warns_under_21_days_and_never_blocks():
    now = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
    soon = catalog_expiry_info("2026-11-01T12:00:00Z", now=now)
    assert soon["catalog_expiring_soon"] is True and soon["catalog_days_left"] == 15
    edge = catalog_expiry_info("2026-11-07T12:00:00Z", now=now)  # đúng 21 ngày: chưa nhắc
    assert edge["catalog_days_left"] == 21 and edge["catalog_expiring_soon"] is False
    far = catalog_expiry_info("2027-01-13T12:00:00Z", now=now)
    assert far["catalog_expiring_soon"] is False
    assert catalog_expiry_info(None)["catalog_expiring_soon"] is False
    assert catalog_expiry_info("khong-phai-ngay")["catalog_days_left"] is None


def test_update_state_exposes_expiry_flag_after_check(tmp_path):
    archive, core = package(tmp_path)
    raw, keys = signed([core])  # hạn 1 ngày -> dưới 21 ngày
    store = ComponentStore(tmp_path / "App")
    manager = UpdateManager(store.app_root, transport=Transport(raw, {core["url"]: archive}), trusted_keys=keys)
    assert manager.state()["catalog_expiring_soon"] is False and manager.state()["catalog_days_left"] is None
    manager.action("check", {})
    state = done(manager)
    assert state["catalog_expiring_soon"] is True
    assert state["catalog_days_left"] in (0, 1)
    assert state["catalog_expires_at"]


def _feed_catalog(tag):
    return {"signed": {"components": [{"url": f"https://github.com/x/releases/download/{tag}/a.zip"}]}}


def _release(rid, tag):
    return {"id": rid, "tag_name": tag, "name": tag, "assets": []}


def test_prune_plan_stops_when_a_feed_catalog_is_missing_or_unreadable():
    releases = [_release(1, "updates-beta-p4"), _release(2, "v1.0.0-p4")]
    with pytest.raises(rp.PlanError):
        rp.check_feeds(releases, {})
    with pytest.raises(rp.PlanError):
        rp.check_feeds(releases, {"updates-beta-p4": "not-a-catalog"})
    with pytest.raises(rp.PlanError):
        rp.check_feeds(releases, {"updates-beta-p4": {"signed": {"components": []}}})
    rp.check_feeds(releases, {"updates-beta-p4": _feed_catalog("v1.0.0-p4")})


def test_prune_cli_exits_3_without_deleting_when_feed_catalog_not_given(tmp_path, capsys):
    releases = tmp_path / "releases.json"
    releases.write_text(json.dumps([_release(1, "updates-beta-p4"), _release(2, "v0.0.1-p4")]), encoding="utf-8")
    other = tmp_path / "other.json"
    other.write_text(json.dumps(_feed_catalog("v0.0.1-p4")), encoding="utf-8")
    plan_out = tmp_path / "plan.json"
    code = rp.main(["--releases", str(releases), "--catalog", str(other), "--plan-out", str(plan_out)])
    assert code == 3
    assert not plan_out.exists()
    assert "updates-beta-p4" in capsys.readouterr().err


def test_prune_cli_keeps_tags_referenced_by_feed_catalogs(tmp_path):
    releases = tmp_path / "releases.json"
    releases.write_text(json.dumps([_release(1, "updates-beta-p4"), _release(2, "runtime-1.0.0"), _release(3, "old-tag")]),
                        encoding="utf-8")
    feed = tmp_path / "feed.json"
    feed.write_text(json.dumps(_feed_catalog("runtime-1.0.0")), encoding="utf-8")
    plan_out = tmp_path / "plan.json"
    assert rp.main(["--releases", str(releases), "--feed", f"updates-beta-p4={feed}", "--plan-out", str(plan_out)]) == 0
    decisions = {row["tag"]: row["decision"] for row in json.loads(plan_out.read_text(encoding="utf-8"))}
    assert decisions == {"updates-beta-p4": "KEEP", "runtime-1.0.0": "KEEP", "old-tag": "DELETE"}
