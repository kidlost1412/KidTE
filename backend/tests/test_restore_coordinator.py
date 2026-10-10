import json
import os
import sqlite3
from contextlib import closing

import pytest

from backup_v2 import BackupV2
from maintenance import MaintenanceMode
from privacy import PrivacyService
from restore_coordinator import (RestoreCoordinator, RestoreError, WriterRegistry,
                                 restore_v2_enabled)

META = {'app_version': 'f', 'schema_version': 2, 'solver_hash': 'f', 'coverage': ['customers']}


def make_db(path, keys):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE customers(customer_key TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE interactions(interaction_id INTEGER PRIMARY KEY, customer_key TEXT)')
        for key in keys:
            db.execute('INSERT INTO customers VALUES(?)', (key,))
            db.execute('INSERT INTO interactions(customer_key) VALUES(?)', (key,))
    return path


def keys(path):
    with closing(sqlite3.connect(path)) as db:
        return sorted(r[0] for r in db.execute('SELECT customer_key FROM customers'))


class Env:
    def __init__(self, tmp_path, live=False, **overrides):
        self.root = tmp_path
        self.data = tmp_path / 'data'
        self.data.mkdir()
        self.db = make_db(self.data / 'mem.sqlite3', ['tt:id:alice', 'tt:id:bob'])
        (self.data / 'marker.txt').write_text('old')
        self.privacy = PrivacyService(self.db, self.data / 'machine_key')
        self.store = BackupV2(tmp_path / 'backups')
        self.manifest = self.store.create(
            {'data/mem.sqlite3': {'source': self.db, 'kind': 'sqlite'}},
            metadata=META, privacy_db=self.db)
        self.log = []
        self.maintenance = MaintenanceMode()
        self.writers = WriterRegistry()
        self.writers.register('memory', lambda: self.log.append('closed:memory'))
        self.writers.register('queue', lambda: self.log.append('closed:queue'))
        self.writers.register('privacy', self.privacy.close)
        kwargs = dict(data_dir=self.data, store=self.store, maintenance=self.maintenance,
                      is_live=lambda: live, writers=self.writers,
                      reinit=lambda: self.log.append('reinit'),
                      privacy_db=self.db, privacy_key_path=self.data / 'machine_key')
        kwargs.update(overrides)
        self.coordinator = RestoreCoordinator(**kwargs)

    def restore(self):
        return self.coordinator.restore(self.manifest['id'], None)


def test_refuses_when_live(tmp_path):
    env = Env(tmp_path, live=True)
    with pytest.raises(RestoreError) as error:
        env.restore()
    assert error.value.code == 'maintenance_required'
    assert env.log == [] and not env.maintenance.active
    assert (env.data / 'marker.txt').read_text() == 'old'


def test_writers_closed_before_swap(tmp_path):
    swaps = []
    env = Env(tmp_path)
    real = os.replace
    def rename(src, dst):
        swaps.append(list(env.log))
        real(src, dst)
    env.coordinator.rename = rename
    env.restore()
    assert swaps and all('closed:memory' in seen and 'closed:queue' in seen for seen in swaps)
    assert env.coordinator.events.index('close') < env.coordinator.events.index('swap')


@pytest.mark.parametrize('fail_at', ['migrate', 'swap'])
def test_failure_rolls_back(tmp_path, fail_at):
    calls = []
    def migrate(staged):
        raise RuntimeError('boom')
    def rename(src, dst):
        calls.append(1)
        if len(calls) == 2:          # old dir already moved aside, new one fails to move in
            raise OSError('swap failed')
        os.replace(src, dst)
    overrides = {'migrate': migrate} if fail_at == 'migrate' else {'rename': rename}
    env = Env(tmp_path, **overrides)
    with pytest.raises(RestoreError):
        env.restore()
    assert (env.data / 'marker.txt').read_text() == 'old'
    assert keys(env.data / 'mem.sqlite3') == ['tt:id:alice', 'tt:id:bob']
    assert env.log[-1] == 'reinit' and 'rollback' in env.coordinator.events
    assert not env.maintenance.active
    assert env.maintenance.ai_disabled          # stays disabled after a failed restore too
    assert not any(p.name.endswith('.rollback-') for p in tmp_path.iterdir())


def test_reinit_called(tmp_path):
    env = Env(tmp_path)
    result = env.restore()
    assert env.log.count('reinit') == 1 and env.log.index('reinit') > env.log.index('closed:queue')
    assert os.path.isdir(result['rollback_dir'])           # old dir kept, never deleted
    assert (os.path.join(result['rollback_dir'], 'marker.txt')) and \
        (tmp_path / 'data' / 'mem.sqlite3').is_file()


def test_flag_default_off(tmp_path):
    flag = tmp_path / 'restore_v2.json'
    assert restore_v2_enabled(flag) is False
    flag.write_text('not json')
    assert restore_v2_enabled(flag) is False
    flag.write_text(json.dumps({'restore_v2_enabled': 'yes'}))
    assert restore_v2_enabled(flag) is False
    flag.write_text(json.dumps({'restore_v2_enabled': True}))
    assert restore_v2_enabled(flag) is True


def test_forgotten_customer_not_resurrected(tmp_path):
    env = Env(tmp_path)                       # backup still contains alice
    env.privacy.forget('tt:id:alice')         # forgotten afterwards
    env.privacy.close()
    env.restore()
    assert keys(env.data / 'mem.sqlite3') == ['tt:id:bob']
    with closing(sqlite3.connect(env.data / 'mem.sqlite3')) as db:
        assert db.execute('SELECT COUNT(*) FROM privacy_tombstones').fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM interactions WHERE customer_key='tt:id:alice'").fetchone()[0] == 0


def test_ai_disabled_after_restore(tmp_path):
    env = Env(tmp_path)
    env.restore()
    assert not env.maintenance.active
    assert env.maintenance.ai_disabled and env.maintenance.auto_pin_disabled
    assert env.maintenance.blocked() is None
    env.maintenance.reenable()
    assert not env.maintenance.ai_disabled


def test_maintenance_blocks_reply_paths():
    import server
    engine = server.ReplyEngine.__new__(server.ReplyEngine)
    engine.maintenance = MaintenanceMode()
    engine.maintenance.enter()
    assert engine.handle({'comment': 'xin chao'}) == {'ok': False, 'error': 'maintenance'}
    assert engine.queue_operation('tiktok', 'next', {'room_id': 'r'}) == {'ok': False, 'error': 'maintenance'}
    assert engine.delivery_operation('tiktok', 'claim', {}) == {'ok': False, 'error': 'maintenance'}


def test_server_v2_branch_refused_when_flag_off(tmp_path, monkeypatch):
    import server
    monkeypatch.setattr(server, 'DATA_DIR', tmp_path / 'data')
    engine = server.ReplyEngine.__new__(server.ReplyEngine)
    with pytest.raises(ValueError, match='restore_v2_disabled'):
        engine.restore_backup({'v2': True, 'backup_id': 'x', 'password': 'pw-123456'})
