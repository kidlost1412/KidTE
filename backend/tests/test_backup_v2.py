import json
import sqlite3
from pathlib import Path

import pytest

from backup import BackupError
from backup_v2 import BackupV2

META = {'app_version':'fixture', 'schema_version':2, 'solver_hash':'fixture', 'coverage':['customers','external_video']}


def sources(tmp_path):
    source = tmp_path / 'external-video.mp4'
    source.write_bytes(b'immutable video fixture' * 10)
    db = tmp_path / 'customer.sqlite3'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE messages(text TEXT)')
        connection.execute("INSERT INTO messages VALUES('fixture')")
    return {'videos/external.mp4': {'source':source, 'kind':'file'},
            'data/customer.sqlite3': {'source':db, 'kind':'sqlite'}}


def test_shared_objects_sqlite_snapshot_and_encrypted_portable_staging(tmp_path):
    store = BackupV2(tmp_path / 'backups-v2')
    data = sources(tmp_path)
    first = store.create(data, metadata=META, full=True, secrets=b'fake secret', password='fixture password')
    second = store.create(data, metadata=META, full=True, secrets=b'fake secret', password='fixture password')
    assert first['complete'] and first['scope'] == 'full'
    assert first['id'] != second['id']
    first_rows = {row['path']:row for row in first['files']}
    second_rows = {row['path']:row for row in second['files']}
    assert first_rows['videos/external.mp4']['sha256'] == second_rows['videos/external.mp4']['sha256']
    assert first_rows['secrets.enc']['sha256'] != second_rows['secrets.enc']['sha256']
    target = tmp_path / 'restored-stage'
    store.stage_restore(first['id'], target, password='fixture password')
    with sqlite3.connect(target / 'data/customer.sqlite3') as db:
        assert db.execute('SELECT text FROM messages').fetchone()[0] == 'fixture'
    assert b'fake secret' not in (target / 'secrets.enc').read_bytes()
    assert (target / 'videos/external.mp4').read_bytes() == data['videos/external.mp4']['source'].read_bytes()


def test_missing_coverage_or_secrets_cannot_be_called_full(tmp_path):
    store = BackupV2(tmp_path / 'backups')
    manifest = store.create(sources(tmp_path), metadata=META, full=True, missing=['model unavailable'])
    assert not manifest['complete'] and manifest['scope'] == 'partial'
    assert manifest['missing'] == ['model unavailable']


def test_wrong_password_is_rejected_before_creating_staging(tmp_path):
    store = BackupV2(tmp_path / 'backups')
    manifest = store.create(sources(tmp_path), metadata=META, full=True, secrets=b'fixture', password='correct')
    target = tmp_path / 'staging'
    with pytest.raises(ValueError, match='authentication_failed'):
        store.stage_restore(manifest['id'], target, password='wrong')
    assert not target.exists()
    with pytest.raises(BackupError, match='password_required'):
        store.stage_restore(manifest['id'], target, password=None)
    assert not target.exists()


def test_missing_or_corrupted_object_is_rejected_before_staging(tmp_path):
    store = BackupV2(tmp_path / 'backups')
    manifest = store.create(sources(tmp_path), metadata=META)
    path = store._object_path(manifest['files'][0]['sha256'])
    path.write_bytes(b'bad')
    with pytest.raises(BackupError, match='corrupt_or_missing'):
        store.stage_restore(manifest['id'], tmp_path / 'stage', password='fixture')
    assert not (tmp_path / 'stage').exists()


def test_unsafe_manifest_cannot_write_outside_staging(tmp_path):
    store = BackupV2(tmp_path / 'backups')
    manifest = store.create(sources(tmp_path), metadata=META)
    manifest['files'][0]['path'] = '../outside'
    store._manifest_path(manifest['id']).write_text(json.dumps(manifest))
    with pytest.raises(BackupError):
        store.stage_restore(manifest['id'], tmp_path / 'stage', password='fixture')
    assert not (tmp_path / 'stage').exists()


def test_recursive_backup_and_missing_source_fail_without_publishing_manifest(tmp_path):
    store = BackupV2(tmp_path / 'backups')
    internal = store.root / 'inside'
    internal.write_bytes(b'fixture')
    for source in (internal, tmp_path / 'missing'):
        with pytest.raises(BackupError):
            store.create({'file': {'source':source,'kind':'file'}}, metadata=META)
    assert list(store.manifests.iterdir()) == []
    assert not list(store.root.glob('.staging-*'))


def test_delete_only_reclaims_objects_without_other_references(tmp_path):
    store = BackupV2(tmp_path / 'backups')
    data = sources(tmp_path)
    first = store.create(data, metadata=META, secrets=b'one', password='fixture')
    second = store.create(data, metadata=META, secrets=b'two', password='fixture')
    shared = next(row['sha256'] for row in first['files'] if row['path'] == 'videos/external.mp4')
    checked = store.preview_delete([first['id']])
    assert shared not in checked['object_ids']
    result = store.delete([first['id']], preview_token=checked['preview_token'])
    assert result['reclaimed_bytes'] == checked['reclaimable_bytes']
    assert store._object_path(shared).exists()
    store.verify(second['id'], password='fixture')
    checked = store.preview_delete([second['id']])
    assert shared in checked['object_ids']
    store.delete([second['id']], preview_token=checked['preview_token'])
    assert not store._object_path(shared).exists()
    assert not list(store.manifests.iterdir())


def test_new_manifest_invalidates_delete_preview(tmp_path):
    store = BackupV2(tmp_path / 'backups')
    data = sources(tmp_path)
    first = store.create(data, metadata=META)
    checked = store.preview_delete([first['id']])
    store.create(data, metadata=META)
    with pytest.raises(BackupError, match='preview_changed'):
        store.delete([first['id']], preview_token=checked['preview_token'])
    assert store._manifest_path(first['id']).exists()


def test_unreadable_retained_manifest_blocks_object_collection(tmp_path):
    store = BackupV2(tmp_path / 'backups')
    first = store.create(sources(tmp_path), metadata=META)
    (store.manifests / 'bad.json').write_text('broken')
    with pytest.raises(BackupError, match='unreadable'):
        store.preview_delete([first['id']])
    assert store._manifest_path(first['id']).exists()


def test_interrupted_delete_blocks_new_mutations_instead_of_guessing(tmp_path, monkeypatch):
    store = BackupV2(tmp_path / 'backups')
    data = sources(tmp_path)
    first = store.create(data, metadata=META)
    checked = store.preview_delete([first['id']])
    original = __import__('os').replace
    def failed_move(source, destination):
        if Path(destination).name.startswith('.deleted-'):
            raise PermissionError('fixture locked')
        return original(source, destination)
    monkeypatch.setattr('backup_v2.os.replace', failed_move)
    with pytest.raises(PermissionError):
        store.delete([first['id']], preview_token=checked['preview_token'])
    assert store._manifest_path(first['id']).exists()
    with pytest.raises(BackupError, match='recovery_required'):
        store.create(data, metadata=META)
    monkeypatch.undo()
    assert store.recover_deletions()['recovered_deleted_ids'] == [first['id']]
    assert not store._manifest_path(first['id']).exists()
    assert store.recover_deletions()['recovered_deleted_ids'] == []


def test_recovery_refuses_manifest_changed_after_interruption(tmp_path, monkeypatch):
    store = BackupV2(tmp_path / 'backups')
    first = store.create(sources(tmp_path), metadata=META)
    checked = store.preview_delete([first['id']])
    monkeypatch.setattr('backup_v2.os.replace', lambda *args: (_ for _ in ()).throw(PermissionError('fixture')))
    with pytest.raises(PermissionError):
        store.delete([first['id']], preview_token=checked['preview_token'])
    monkeypatch.undo()
    store._manifest_path(first['id']).write_text('changed')
    with pytest.raises(BackupError, match='journal_conflict'):
        store.recover_deletions()
    assert store._manifest_path(first['id']).read_text() == 'changed'


def test_retention_keeps_full_and_partial_separately_and_only_after_verification(tmp_path):
    store = BackupV2(tmp_path / 'backups')
    data = sources(tmp_path)
    full = store.create(data, metadata=META, full=True, secrets=b'fixture', password='fixture')
    partial = [store.create(data, metadata=META) for _ in range(3)]
    result = store.prune(keep=2)
    assert result['deleted_ids'] == [partial[0]['id']]
    assert store.verify(full['id'])['scope'] == 'full'
    assert store.verify(partial[-1]['id'])['scope'] == 'partial'
    (store.manifests / 'broken.json').write_text('bad')
    with pytest.raises(BackupError):
        store.prune(keep=1)
    assert store._manifest_path(partial[1]['id']).exists()


@pytest.mark.parametrize('change', ['secret_kind', 'missing_secret', 'complete_type', 'byte_type', 'scope_mismatch'])
def test_manifest_cannot_bypass_secret_authentication_or_claim_false_completeness(tmp_path, change):
    store = BackupV2(tmp_path / 'backups')
    manifest = store.create(sources(tmp_path), metadata=META, full=True, secrets=b'fixture', password='correct')
    if change == 'secret_kind':
        next(row for row in manifest['files'] if row['kind'] == 'secrets')['kind'] = 'file'
    elif change == 'missing_secret':
        manifest['files'] = [row for row in manifest['files'] if row['kind'] != 'secrets']
    elif change == 'complete_type':
        manifest['complete'] = 'true'
    elif change == 'byte_type':
        manifest['files'][0]['bytes'] = True
    else:
        manifest['scope'] = 'partial'
    store._manifest_path(manifest['id']).write_text(json.dumps(manifest))
    with pytest.raises(BackupError):
        store.stage_restore(manifest['id'], tmp_path / 'stage', password='wrong')
    assert not (tmp_path / 'stage').exists()
