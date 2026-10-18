import json
import threading
from types import SimpleNamespace

import pytest

from backup_service import BackupService
from backup_v2 import BackupV2
from maintenance import JobStore
from server import ReplyEngine


def service(tmp_path):
    root = tmp_path / 'runtime'
    (root / 'data').mkdir(parents=True)
    (root / 'data' / 'fixture.json').write_text('{"fixture":true}')
    (root / '.env').write_text('API_KEY=fixture secret\n')
    return BackupService(root, app_version='fixture', schema_version=2, solver_hash='',
                         reference_provider=lambda: [], dpapi_decrypt=lambda value: value)


def test_service_runs_in_durable_job_without_persisting_password_or_plain_secret(tmp_path):
    backup = service(tmp_path)
    jobs = JobStore(tmp_path / 'jobs.sqlite3')
    done = threading.Event()
    def operation(progress):
        try:
            return backup.create('fixture password', progress=progress)
        finally:
            done.set()
    try:
        accepted = jobs.start('backup_v2', operation)
        assert done.wait(10)
        for thread in jobs._threads:
            thread.join(2)
        job = jobs.get(accepted['job_id'])
        assert job['state'] == 'completed'
        result = job['result']
        assert result['created'] and not result['complete'] and result['scope'] == 'partial'
        assert 'browser_indexeddb_recovery_not_exported' in result['missing']
        assert 'cross_store_snapshot_not_coordinated' in result['missing']
        assert 'solver_version_unverified' in result['missing']
        serialized = json.dumps(job)
        assert 'fixture password' not in serialized and 'fixture secret' not in serialized
        store = BackupV2(backup.root / 'backups' / 'v2')
        assert store.verify(result['backup_id'], password='fixture password')['scope'] == 'partial'
        assert b'fixture password' not in (tmp_path / 'jobs.sqlite3').read_bytes()
    finally:
        jobs.close()


def test_retention_error_keeps_published_backup_id_visible(tmp_path, monkeypatch):
    backup = service(tmp_path)
    monkeypatch.setattr(BackupV2, 'prune', lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('secret fixture')))
    result = backup.create('fixture password')
    assert result['created'] and result['backup_id']
    assert result['retention'] == {'ok':False,'error':'OSError'}


def test_manual_backup_keeps_recent_backups_and_rotates_beyond_keep(tmp_path):
    backup = service(tmp_path)
    backup.keep = 1
    first = backup.create('fixture password')
    second = backup.create('fixture password')
    store = BackupV2(backup.root / 'backups' / 'v2')
    assert [p.stem for p in store.manifests.glob('*.json')] == [second['backup_id']]
    assert first['backup_id'] in second['retention']['deleted_ids']
    store.verify(second['backup_id'], password='fixture password')


def test_failed_new_backup_preserves_previous(tmp_path, monkeypatch):
    backup = service(tmp_path)
    first = backup.create('fixture password')
    monkeypatch.setattr(BackupV2, 'create', lambda *a, **k: (_ for _ in ()).throw(OSError('fixture disk full')))
    with pytest.raises(OSError):
        backup.create('fixture password')
    store = BackupV2(backup.root / 'backups' / 'v2')
    assert store.verify(first['backup_id'], password='fixture password')


@pytest.mark.parametrize('password', [None, '', 123, 'x' * 4097, '\ud800'])
def test_start_validates_password_before_creating_job(tmp_path, password):
    engine = ReplyEngine.__new__(ReplyEngine)
    engine._system_jobs = lambda: pytest.fail('invalid request must not create a job')
    with pytest.raises(ValueError, match='password_required'):
        engine.system_post('backup/v2/start', {'password':password})


def test_start_only_captures_password_in_operation_closure(tmp_path):
    engine = ReplyEngine.__new__(ReplyEngine)
    captured = []
    engine._system_jobs = lambda: SimpleNamespace(start=lambda kind, operation: captured.append((kind,operation)) or {'accepted':True})
    engine._backup_v2_service = lambda: SimpleNamespace(create=lambda password, **kwargs: {'called':password=='fixture'})
    assert engine.system_post('backup/v2/start', {'password':'fixture'})['accepted']
    assert captured[0][0] == 'backup_v2'
    assert captured[0][1](lambda _:None) == {'called':True}
