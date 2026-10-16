"""A4/A5/A6: kiểm khóa ký và catalog trước khi phát hành."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging"))
import update_release
from test_update_release import payload

HEAD = "c" * 40
TAG = "v1.2.3-p4"


@pytest.fixture
def signer(tmp_path, monkeypatch):
    monkeypatch.setattr(update_release, "_dpapi", lambda raw, decrypt=False: raw[5:] if decrypt else b"DPAPI" + raw)
    private = tmp_path / "external" / "release.dpapi"
    public = tmp_path / "public.json"
    key_id = update_release.keygen(private, public)
    trusted = {key_id: json.loads(public.read_text(encoding="utf-8"))["public_key"]}
    return private, trusted


def _sign(tmp_path, private, trusted, data):
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(data), encoding="utf-8")
    out = tmp_path / "release" / "kidte-catalog-v2.json"
    out.parent.mkdir(exist_ok=True)
    update_release.sign_catalog(payload_path, private, out, trusted_keys=trusted)
    return out


def test_sign_refuses_key_not_in_trusted_keys(tmp_path, signer):
    private, _ = signer
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(payload()), encoding="utf-8")
    out = tmp_path / "signed.json"
    with pytest.raises(ValueError, match="signing_key_not_trusted"):
        update_release.sign_catalog(payload_path, private, out, trusted_keys={"0" * 16: "AAAA"})
    assert not out.exists()


def _release(tmp_path, private, trusted, *, url=None, commit=HEAD, archive=b"zip-bytes"):
    data = payload()
    component = data["components"][0]
    release = tmp_path / "release"
    release.mkdir(exist_ok=True)
    (release / "core-1.2.3.zip").write_bytes(archive)
    component["archive_sha256"] = hashlib.sha256(b"zip-bytes").hexdigest()
    component["url"] = url or f"https://github.com/kidlost1412/KidTE/releases/download/{TAG}/core-1.2.3.zip"
    component["source_commit"] = commit
    return _sign(tmp_path, private, trusted, data), release


def test_check_publish_accepts_good_release(tmp_path, signer):
    private, trusted = signer
    catalog, release = _release(tmp_path, private, trusted)
    assert update_release.check_release_catalog(catalog, release, HEAD, tag=TAG, trusted_keys=trusted,
                                                url_exists=lambda url: False) == []


def test_check_publish_rejects_untrusted_signature(tmp_path, signer):
    private, trusted = signer
    catalog, release = _release(tmp_path, private, trusted)
    problems = update_release.check_release_catalog(catalog, release, HEAD, tag=TAG,
                                                    trusted_keys={"0" * 16: "AAAA"})
    assert problems and "TRUSTED_KEYS" in problems[0]


def test_check_publish_rejects_wrong_commit_tag_hash_and_missing_asset(tmp_path, signer):
    private, trusted = signer
    catalog, release = _release(tmp_path, private, trusted, commit="d" * 40)
    problems = update_release.check_release_catalog(catalog, release, HEAD, tag=TAG, trusted_keys=trusted)
    assert any("source_commit" in p for p in problems)

    catalog, release = _release(tmp_path, private, trusted,
                                url="https://github.com/kidlost1412/KidTE/releases/download/v9.9.9-p4/core-1.2.3.zip")
    problems = update_release.check_release_catalog(catalog, release, HEAD, tag=TAG, trusted_keys=trusted)
    assert any("không trỏ đúng tag" in p for p in problems)

    catalog, release = _release(tmp_path, private, trusted, archive=b"other")
    problems = update_release.check_release_catalog(catalog, release, HEAD, tag=TAG, trusted_keys=trusted)
    assert any("sha256" in p for p in problems)


def test_check_publish_old_component_must_exist_on_github(tmp_path, signer):
    private, trusted = signer
    old_url = "https://github.com/kidlost1412/KidTE/releases/download/v1.0.0-p4/core-1.0.0.zip"
    catalog, release = _release(tmp_path, private, trusted, url=old_url, commit="e" * 40)
    seen = []
    ok = update_release.check_release_catalog(catalog, release, HEAD, tag=TAG, trusted_keys=trusted,
                                              url_exists=lambda url: seen.append(url) or True)
    assert ok == [] and seen == [old_url]          # thành phần cũ: không đòi commit == HEAD
    problems = update_release.check_release_catalog(catalog, release, HEAD, tag=TAG, trusted_keys=trusted,
                                                    url_exists=lambda url: False)
    assert any("không tồn tại trên GitHub" in p for p in problems)
