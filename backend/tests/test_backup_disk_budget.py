import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backup import BackupError
import backup_policy
from backup_v2 import BackupV2
from backup_io import account_io, io_limit
from server import ReplyEngine

META = {'app_version':'fixture','schema_version':2,'solver_hash':'fixture','coverage':['video']}


def test_large_unchanged_source_reused_across_restart_without_payload_read(tmp_path, monkeypatch):
    source = tmp_path / 'video.mp4'
    source.write_bytes(b'v' * (2 * 1024 * 1024))
    root = tmp_path / 'backups'
    first = BackupV2(root).create({'video.mp4':{'source':source,'kind':'file'}}, metadata=META)
    store = BackupV2(root)
    original_open = Path.open
    def guarded(path, *args, **kwargs):
        if path == source or path == store._object_path(first['files'][0]['sha256']):
            pytest.fail('unchanged video/CAS payload must not be read again')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded)
    second = store.create({'video.mp4':{'source':source,'kind':'file'}}, metadata=META)
    assert first['files'] == second['files']
    assert store.prune()['deleted_ids'] == []


def test_source_change_and_object_corruption_invalidate_reuse(tmp_path):
    source = tmp_path / 'model.bin'
    source.write_bytes(b'a' * (1024 * 1024))
    store = BackupV2(tmp_path / 'backups')
    first = store.create({'model.bin':{'source':source,'kind':'file'}}, metadata=META)
    source.write_bytes(b'b' * (1024 * 1024))
    second = store.create({'model.bin':{'source':source,'kind':'file'}}, metadata=META)
    assert first['files'][0]['sha256'] != second['files'][0]['sha256']
    obj = store._object_path(second['files'][0]['sha256'])
    obj.write_bytes(b'c' * (1024 * 1024))
    with pytest.raises(BackupError, match='corrupt'):
        store.create({'model.bin':{'source':source,'kind':'file'}}, metadata=META)
    with pytest.raises(BackupError, match='corrupt'):
        store.verify(second['id'])


def test_latest_timestamp_does_not_verify_payloads_but_explicit_verification_does(tmp_path, monkeypatch):
    for index in range(20):
        directory = tmp_path / str(index)
        directory.mkdir()
        (directory/'manifest.json').write_text(json.dumps({'schema_version':1,'scope':'full',
            'created_at':datetime(2026,10,1,index,tzinfo=timezone.utc).isoformat(),'files':[{'path':'fixture'}]}))
    calls = []
    monkeypatch.setattr(backup_policy, 'verify_backup', lambda path: calls.append(path))
    assert backup_policy.latest_backup_time(tmp_path).hour == 19
    assert not calls
    assert backup_policy.latest_backup_time(tmp_path, verify=True).hour == 19
    assert len(calls) == 20


def test_io_budget_counts_reads_and_writes_and_does_not_leak_to_foreground(monkeypatch):
    now = [0.0]
    delays = []
    monkeypatch.setattr('backup_io.time.monotonic', lambda: now[0])
    def sleep(delay):
        delays.append(delay)
        now[0] += delay
    monkeypatch.setattr('backup_io.time.sleep', sleep)
    with io_limit(1024):
        account_io(2048)
        account_io(1024)
    assert delays == [2.0,1.0]
    account_io(1_000_000)
    assert len(delays) == 2


def test_startup_deferral_happens_before_service_creation(tmp_path, monkeypatch):
    calls = []
    class Event:
        def wait(self, seconds): calls.append(('wait',seconds))
    monkeypatch.setattr(backup_policy.threading, 'Event', Event)
    # Run the background target synchronously to test ordering without sleeping.
    class Thread:
        def __init__(self, *, target, **kwargs): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(backup_policy.threading, 'Thread', Thread)
    def provider():
        calls.append(('provider',None))
        raise RuntimeError('fixture')
    backup_policy.run_startup_auto_backup({}, provider, background=True, startup_delay_s=60)
    assert calls == [('wait',60),('provider',None)]


def test_cancelled_startup_delay_never_constructs_backup_service():
    class Cancelled:
        def wait(self, seconds): return True
    done = backup_policy.run_startup_auto_backup({}, lambda: pytest.fail('cancelled app must not start a backup'),
        background=True, startup_delay_s=60, cancel_event=Cancelled())
    done.join(1)
    assert not done.is_alive()


def test_backup_listing_never_reads_cas_payload_or_claims_checksum_verified(tmp_path, monkeypatch):
    import server
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    source = tmp_path / 'video.mp4'
    source.write_bytes(b'x' * (1024 * 1024))
    store = BackupV2(tmp_path / 'backups' / 'v2')
    store.create({'video.mp4':{'source':source,'kind':'file'}}, metadata=META)
    monkeypatch.setattr(BackupV2, 'verify', lambda *args, **kwargs: pytest.fail('listing must not verify payloads'))
    listing = ReplyEngine.backup_v2_list(None)
    assert len(listing['items']) == 1
    assert listing['items'][0]['verified'] is False


def test_delete_never_reads_large_object_and_handles_missing_object(tmp_path, monkeypatch):
    source = tmp_path / 'video.mp4'
    source.write_bytes(b'x' * (1024 * 1024))
    store = BackupV2(tmp_path / 'backups')
    manifest = store.create({'video.mp4': {'source': source, 'kind': 'file'}}, metadata=META)
    obj = store._object_path(manifest['files'][0]['sha256'])
    original_open = Path.open
    def guarded_open(path, *args, **kwargs):
        if path == obj:
            pytest.fail('deletion must not read video payload')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded_open)
    preview = store.preview_delete([manifest['id']])
    obj.unlink()
    store.delete([manifest['id']], preview_token=preview['preview_token'])
    assert not list(store.manifests.glob('*.json'))


def test_busy_store_returns_actionable_error(tmp_path):
    from backup import BackupError
    store = BackupV2(tmp_path / 'backups')
    with store._write():
        with pytest.raises(BackupError, match='backup_busy'):
            with BackupV2(store.root)._write():
                pytest.fail('must not acquire another writer')
