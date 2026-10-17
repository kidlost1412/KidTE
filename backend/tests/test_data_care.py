"""Chăm sóc dữ liệu: quên khách trong snapshot/thùng rác (D4/D11), opt-out không giữ tên (D5),
V2 giữ nhiều bản (D8), dọn log/dữ liệu thô quá hạn (D10)."""
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import data_retention
from backup_service import BackupService
from backup_v2 import BackupV2
from customer_memory import CustomerMemory
from privacy import PrivacyService, purge_db_by_tombstones
from workspace import WorkspaceStore


# ------------------------------------------------------------------ D4 / D11
def _env(tmp_path):
    db = tmp_path / 'db.sqlite3'
    privacy = PrivacyService(db, tmp_path / 'machine' / 'privacy_key')
    memory = CustomerMemory(db, tmp_path / 'journal', privacy=privacy)
    ws = WorkspaceStore(SimpleNamespace(knowledge=SimpleNamespace(overrides_path=tmp_path / 'ov.json'),
                                        mapping_state=lambda: {'products': []}),
                        root=tmp_path / 'ws')
    ws.privacy = privacy
    return privacy, memory, ws


def _row(uid, text):
    return {'user_id': uid, 'nickname': 'Nick' + uid, 'comment': text, 'platform': 'tiktok'}


def _seed_history(ws, conv, uid):
    data = _row(uid, 'binh luan rieng cua ' + uid)
    snap = ws._snapshot('conversations', conv, data, 'seed')
    ws._record_change('conversations', conv, data, {**data, 'comment': 'sua'}, 'seed')
    with ws._db() as db:
        db.execute("INSERT INTO trash_items (trash_id,resource,record_id,deleted_at,expires_at,label,data_json,"
                   "snapshot_id) VALUES (?,?,?,?,?,?,?,?)",
                   ('t' + conv, 'conversations', conv, 'd', '9999', 'l', json.dumps(data), snap))
    return snap


def _counts(ws, conv):
    with ws._db() as db:
        return tuple(db.execute(f"SELECT COUNT(*) FROM {t} WHERE record_id=?", (conv,)).fetchone()[0]
                     for t in ('operator_snapshots', 'operator_changes', 'trash_items'))


def test_forget_removes_snapshots_changes_trash_of_that_customer_only(tmp_path):
    privacy, memory, ws = _env(tmp_path)
    try:
        _seed_history(ws, 'c7', '7')
        _seed_history(ws, 'c8', '8')
        key = memory.customer_key(platform_user_id='7')[0]
        privacy.forget(key, memory=memory, derived={'workspace': ws})
        assert _counts(ws, 'c7') == (0, 0, 0)
        assert _counts(ws, 'c8') == (1, 1, 1)
    finally:
        memory.close(); privacy.close()


def test_forget_keeps_unreadable_and_non_customer_rows(tmp_path):
    privacy, memory, ws = _env(tmp_path)
    try:
        ws._snapshot('products', 'p1', _row('7', 'khong phai khach'), 'seed')
        with ws._db() as db:
            db.execute("INSERT INTO operator_snapshots VALUES ('bad','conversations','cx','t','r','{not json','x',NULL)")
        key = memory.customer_key(platform_user_id='7')[0]
        privacy.forget(key, memory=memory, derived={'workspace': ws})
        with ws._db() as db:
            assert db.execute("SELECT COUNT(*) FROM operator_snapshots").fetchone()[0] == 2
    finally:
        memory.close(); privacy.close()


def test_permanent_purge_deletes_customer_snapshot(tmp_path):
    privacy, memory, ws = _env(tmp_path)
    try:
        snap = _seed_history(ws, 'c7', '7')
        ws.purge_now(['tc7'])
        with ws._db() as db:
            assert db.execute("SELECT COUNT(*) FROM operator_snapshots WHERE snapshot_id=?", (snap,)).fetchone()[0] == 0
            assert db.execute("SELECT data_json FROM trash_items WHERE trash_id='tc7'").fetchone()[0] == '{}'
    finally:
        memory.close(); privacy.close()


def test_workspace_connection_uses_secure_delete(tmp_path):
    privacy, memory, ws = _env(tmp_path)
    try:
        with ws._db() as db:
            assert db.execute("PRAGMA secure_delete").fetchone()[0] == 1
    finally:
        memory.close(); privacy.close()


# ------------------------------------------------------------------ D5
def _opt_outs(db_path):
    with sqlite3.connect(db_path) as con:
        return con.execute("SELECT customer_key,display_name FROM memory_opt_outs").fetchall()


def test_opt_out_with_privacy_keeps_no_raw_key_or_name(tmp_path):
    privacy, memory, ws = _env(tmp_path)
    try:
        assert memory.forget_if_requested('xóa dữ liệu của tôi', platform_user_id='5', display_name='Ten That')
        assert _opt_outs(tmp_path / 'db.sqlite3') == []
        assert privacy.is_forgotten(memory.customer_key(platform_user_id='5')[0])
        assert memory.record_comment('hi', platform_user_id='5')['opted_out']
    finally:
        memory.close(); privacy.close()


def test_opt_out_without_privacy_stores_key_without_name_and_scrubs_old_names(tmp_path):
    db = tmp_path / 'm.sqlite3'
    memory = CustomerMemory(db, tmp_path / 'j')
    try:
        assert memory.forget_if_requested('xóa dữ liệu của tôi', platform_user_id='5', display_name='Ten That')
        rows = _opt_outs(db)
        assert len(rows) == 1 and rows[0][1] == ''
        memory._db.execute("UPDATE memory_opt_outs SET display_name='Ten That'")
        memory._db.commit()
    finally:
        memory.close()
    memory = CustomerMemory(db, tmp_path / 'j')
    try:
        assert [r[1] for r in _opt_outs(db)] == ['']
    finally:
        memory.close()


def test_purge_by_tombstones_drops_matching_opt_out_rows(tmp_path):
    db = tmp_path / 'm.sqlite3'
    memory = CustomerMemory(db, tmp_path / 'j')
    try:
        memory.forget_if_requested('xóa dữ liệu của tôi', platform_user_id='5')
        memory.forget_if_requested('xóa dữ liệu của tôi', platform_user_id='6')
        key5 = memory.customer_key(platform_user_id='5')[0]
    finally:
        memory.close()
    secret = b'k' * 32
    from privacy import hmac_for_key
    purge_db_by_tombstones(db, [{'key_hmac': hmac_for_key(secret, key5), 'created_at': 1.0}], secret)
    assert [r[0] for r in _opt_outs(db)] == [memory.customer_key(platform_user_id='6')[0]]


# ------------------------------------------------------------------ D8
def _service(tmp_path, **kw):
    root = tmp_path / 'runtime'
    (root / 'data').mkdir(parents=True)
    (root / 'data' / 'fixture.json').write_text('{"fixture":true}')
    (root / '.env').write_text('API_KEY=fixture secret\n')
    return BackupService(root, app_version='fixture', schema_version=2, solver_hash='',
                         reference_provider=lambda: [], dpapi_decrypt=lambda value: value, **kw)


def test_v2_keeps_several_backups_by_default(tmp_path):
    backup = _service(tmp_path)
    assert backup.keep == 7
    ids = [backup.create('fixture password')['backup_id'] for _ in range(3)]
    store = BackupV2(backup.root / 'backups' / 'v2')
    assert sorted(p.stem for p in store.manifests.glob('*.json')) == sorted(ids)
    for backup_id in ids:
        store.verify(backup_id, password='fixture password')


def test_v2_rotation_drops_only_oldest_beyond_keep(tmp_path):
    backup = _service(tmp_path, keep=2)
    ids = [backup.create('fixture password')['backup_id'] for _ in range(3)]
    store = BackupV2(backup.root / 'backups' / 'v2')
    left = sorted(p.stem for p in store.manifests.glob('*.json'))
    assert len(left) == 2 and set(left) <= set(ids) and ids[-1] in left
    store.verify(ids[-1], password='fixture password')


@pytest.mark.parametrize('bad', [0, -1, True, None, 'x'])
def test_invalid_keep_falls_back_to_default(tmp_path, bad):
    assert _service(tmp_path, keep=bad).keep == 7


# ------------------------------------------------------------------ D10
NOW = time.time()


def _age(path, days):
    stamp = NOW - days * 86400
    os.utime(path, (stamp, stamp))


def _stream_name(days):
    return 'stream_' + (datetime.fromtimestamp(NOW) - timedelta(days=days)).strftime('%Y%m%d') + '.jsonl'


def _make(tmp_path):
    root = tmp_path / 'root'
    logs = root / 'logs'
    for sub in ('raw_stream', 'customer_sessions/shopee', 'missing_answers'):
        (logs / sub).mkdir(parents=True)
    files = {}

    def add(key, rel, days):
        path = logs / rel
        path.write_text('x', encoding='utf-8')
        _age(path, days)
        files[key] = path

    add('raw_old', 'raw_stream/' + _stream_name(120), 120)
    add('raw_new', 'raw_stream/' + _stream_name(10), 10)
    add('raw_today', 'raw_stream/' + _stream_name(0), 0)
    add('raw_old_mtime_new', 'raw_stream/' + _stream_name(200), 1)      # mtime mới -> giữ
    add('raw_badname', 'raw_stream/stream_notadate.jsonl', 300)
    add('journal_old', 'customer_sessions/customers_1.jsonl', 100)
    add('journal_shopee_old', 'customer_sessions/shopee/customers_2.jsonl', 100)
    add('journal_new', 'customer_sessions/customers_3.jsonl', 5)
    add('missing_old', 'missing_answers/missing_1.jsonl', 45)
    add('missing_new', 'missing_answers/missing_2.jsonl', 5)
    add('rot_old', 'http.jsonl.2', 45)
    add('rot_new', 'http.jsonl.1', 5)
    add('active_old', 'http.jsonl', 400)                                   # tệp đang ghi: không bao giờ xoá
    add('other_old', 'console.log', 400)
    (root / 'data').mkdir()
    db = root / 'data' / 'customer_memory.sqlite3'
    db.write_text('db')
    _age(db, 999)
    files['db'] = db
    return root, files


def test_plan_and_run_delete_only_expired_raw_and_logs(tmp_path):
    root, f = _make(tmp_path)
    result = data_retention.run_once(root, raw_days=90, log_days=30, now=NOW, pause_s=0)
    assert result['deleted'] == 5 and result['failed'] == 0
    for gone in ('raw_old', 'journal_old', 'journal_shopee_old', 'missing_old', 'rot_old'):
        assert not f[gone].exists(), gone
    for kept in ('raw_new', 'raw_today', 'raw_old_mtime_new', 'raw_badname', 'journal_new', 'missing_new',
                 'rot_new', 'active_old', 'other_old', 'db'):
        assert f[kept].exists(), kept


def test_zero_days_disables_a_group(tmp_path):
    root, f = _make(tmp_path)
    data_retention.run_once(root, raw_days=0, log_days=30, now=NOW, pause_s=0)
    assert f['raw_old'].exists() and f['journal_old'].exists()
    assert not f['missing_old'].exists() and not f['rot_old'].exists()


def test_no_logs_dir_is_noop(tmp_path):
    assert data_retention.run_once(tmp_path / 'nothing', now=NOW, pause_s=0)['deleted'] == 0


def test_symlink_and_outside_paths_are_never_deleted(tmp_path):
    root, f = _make(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    victim = outside / 'stream_20200101.jsonl'
    victim.write_text('secret')
    _age(victim, 999)
    link = root / 'logs' / 'raw_stream' / 'stream_20200102.jsonl'
    try:
        link.symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip('symlink không tạo được trên máy này')
    data_retention.run_once(root, raw_days=90, log_days=30, now=NOW, pause_s=0)
    assert victim.exists()


def test_directory_symlink_outside_logs_is_ignored(tmp_path):
    root, f = _make(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    victim = outside / 'stream_20200101.jsonl'
    victim.write_text('secret')
    _age(victim, 999)
    (root / 'logs' / 'raw_stream').rename(root / 'logs' / 'raw_stream_real')
    import subprocess
    made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(root / 'logs' / 'raw_stream'), str(outside)],
                          capture_output=True) if os.name == 'nt' else None
    if made is None or made.returncode != 0:
        try:
            (root / 'logs' / 'raw_stream').symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip('symlink/junction không tạo được trên máy này')
    data_retention.run_once(root, raw_days=90, log_days=30, now=NOW, pause_s=0)
    assert victim.exists()


def test_delete_error_is_counted_and_others_continue(tmp_path, monkeypatch):
    root, f = _make(tmp_path)
    real_unlink = Path.unlink

    def flaky(self, *a, **k):
        if self.name == f['raw_old'].name:
            raise PermissionError('locked')
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, 'unlink', flaky)
    result = data_retention.run_once(root, raw_days=90, log_days=30, now=NOW, pause_s=0)
    assert result['failed'] == 1 and result['deleted'] == 4
    assert f['raw_old'].exists()


def test_stat_error_means_keep(tmp_path, monkeypatch):
    root, f = _make(tmp_path)
    real_stat = Path.stat

    def bad(self, *a, **k):
        if self.name == f['raw_old'].name:
            raise OSError('av lock')
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, 'stat', bad)
    plan = data_retention.plan(root, raw_days=90, log_days=30, now=NOW)
    assert f['raw_old'] not in [p['path'] for p in plan]


def test_batches_release_lock_each_time_and_stop_when_busy(tmp_path):
    root, f = _make(tmp_path)
    events = []
    result = data_retention.run_once(root, raw_days=90, log_days=30, now=NOW, pause_s=0, batch_size=2,
                                     acquire=lambda: events.append('a') or True,
                                     release=lambda: events.append('r'))
    assert result['deleted'] == 5
    assert events == ['a', 'r'] * 3
    root, f = _make(tmp_path / 'again')
    result = data_retention.run_once(root, raw_days=90, log_days=30, now=NOW, pause_s=0,
                                     acquire=lambda: False)
    assert result['stopped'] and result['deleted'] == 0 and f['raw_old'].exists()


def test_policy_from_env(monkeypatch):
    monkeypatch.delenv('KIDTE_RETENTION_RAW_DAYS', raising=False)
    monkeypatch.delenv('KIDTE_RETENTION_LOG_DAYS', raising=False)
    assert data_retention.policy_from_env() == {'raw_days': 90, 'log_days': 30}
    monkeypatch.setenv('KIDTE_RETENTION_RAW_DAYS', '2')
    monkeypatch.setenv('KIDTE_RETENTION_LOG_DAYS', 'abc')
    assert data_retention.policy_from_env() == {'raw_days': 7, 'log_days': 30}
    monkeypatch.setenv('KIDTE_RETENTION_RAW_DAYS', '0')
    monkeypatch.setenv('KIDTE_RETENTION_LOG_DAYS', '60')
    assert data_retention.policy_from_env() == {'raw_days': 0, 'log_days': 60}


def test_scheduler_defers_when_live_maintenance_or_backup_running(tmp_path):
    import threading
    root, f = _make(tmp_path)
    state = {'live': True, 'maint': False}
    lock = threading.RLock()
    sched = data_retention.RetentionScheduler(root, live_provider=lambda: state['live'],
                                              maintenance_provider=lambda: state['maint'],
                                              backup_lock=lock, policy={'raw_days': 90, 'log_days': 30}, pause_s=0)
    assert sched.run_once(now=NOW)['deferred'] and f['raw_old'].exists()
    state.update(live=False, maint=True)
    assert sched.run_once(now=NOW)['deferred'] and f['raw_old'].exists()
    state['maint'] = False
    holder = threading.Thread(target=lock.acquire)
    holder.start(); holder.join()          # khoá đang bị luồng khác giữ (đang sao lưu)
    assert sched.run_once(now=NOW)['deferred'] and f['raw_old'].exists()


def test_scheduler_runs_when_idle(tmp_path):
    import threading
    root, f = _make(tmp_path)
    sched = data_retention.RetentionScheduler(root, live_provider=lambda: False, backup_lock=threading.RLock(),
                                              policy={'raw_days': 90, 'log_days': 30}, pause_s=0)
    result = sched.run_once(now=NOW)
    assert result['deleted'] == 5 and not result['deferred'] and not f['raw_old'].exists()
