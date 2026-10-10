import pytest

from backup import BackupError
from backup_service import BackupService
from backup_v2 import BackupV2, references_coverage


def make(tmp_path, refs=()):
    root = tmp_path / 'runtime'
    (root / 'data').mkdir(parents=True)
    (root / 'data' / 'fixture.json').write_text('{"fixture":true}')
    return BackupService(root, app_version='f', schema_version=2, solver_hash='',
                         reference_provider=lambda: list(refs), dpapi_decrypt=lambda v: v)


FAKE_P = lambda b: b'ENC:' + b[::-1]
FAKE_U = lambda b: b[4:][::-1]


def test_auto_uses_v2_partial(tmp_path):
    svc = make(tmp_path)
    result = svc.create_auto(protect=FAKE_P, unprotect=FAKE_U)
    assert result['schema_version'] == 2 and result['scope'] == 'partial'
    assert result['coverage_warnings'] == []
    pwd = svc.auto_password(protect=FAKE_P, unprotect=FAKE_U)
    assert BackupV2(svc.root / 'backups' / 'v2').verify(result['backup_id'], password=pwd)


def test_dpapi_password_roundtrip_mocked(tmp_path):
    svc = make(tmp_path)
    first = svc.auto_password(protect=FAKE_P, unprotect=FAKE_U)
    raw = svc.auto_password_path().read_bytes()
    assert first.encode() not in raw and raw.startswith(b'ENC:')
    assert svc.auto_password(protect=FAKE_P, unprotect=FAKE_U) == first
    svc.auto_password_path().write_bytes(b'garbage')
    with pytest.raises(BackupError):
        svc.auto_password(protect=FAKE_P, unprotect=lambda b: (_ for _ in ()).throw(OSError()))


def test_coverage_missing_reference_flagged():
    manifest = {'files': [{'path': 'data/a.json'}, {'path': 'secrets.enc'}, {'path': 'ghost.bin'}]}
    inv = {'sources': {'data/a.json': {}}, 'external_references': [{'path': 'external/x/v.mp4'}]}
    warns = references_coverage(manifest, inv)
    assert 'manifest_path_not_in_inventory/ghost.bin' in warns
    assert 'reference_not_in_backup/external/x/v.mp4' in warns
    assert references_coverage(None, inv)[0].startswith('coverage_check_failed')
