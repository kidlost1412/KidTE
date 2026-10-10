import json
import sqlite3

from backup_v2 import BackupV2
from privacy import PrivacyService, read_tombstones

META = {'app_version': 'f', 'schema_version': 2, 'solver_hash': 'f', 'coverage': ['customers']}


def make_db(path, keys):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE customers(customer_key TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE interactions(interaction_id INTEGER PRIMARY KEY, customer_key TEXT)')
        db.execute('CREATE TABLE customer_facts(customer_key TEXT, k TEXT)')
        for k in keys:
            db.execute('INSERT INTO customers VALUES(?)', (k,))
            db.execute('INSERT INTO interactions(customer_key) VALUES(?)', (k,))
            db.execute('INSERT INTO customer_facts VALUES(?,?)', (k, 'x'))
    return path


def setup(tmp_path):
    db = make_db(tmp_path / 'mem.sqlite3', ['tt:id:alice', 'tt:id:bob'])
    svc = PrivacyService(db, tmp_path / 'privacy.key')
    return db, svc


def src(db):
    return {'data/mem.sqlite3': {'source': db, 'kind': 'sqlite'}}


def keys(path):
    with sqlite3.connect(path) as d:
        return sorted(r[0] for r in d.execute('SELECT customer_key FROM customers'))


def test_manifest_contains_only_hmac(tmp_path):
    db, svc = setup(tmp_path)
    svc.forget('tt:id:alice')
    svc.close()
    store = BackupV2(tmp_path / 'b')
    m = store.create(src(db), metadata=META, privacy_db=db)
    blob = json.dumps(store._load(m['id']))
    assert 'tt:id:alice' not in blob and 'alice' not in blob
    assert len(m['privacy_tombstones']) == 1 and len(m['privacy_tombstones'][0]['key_hmac']) == 64


def test_restore_does_not_resurrect_forgotten(tmp_path):
    db, svc = setup(tmp_path)
    store = BackupV2(tmp_path / 'b')
    m = store.create(src(db), metadata=META, privacy_db=db)   # backup still has alice
    svc.forget('tt:id:alice')                                  # forgotten after backup
    svc.close()
    out = tmp_path / 'stage'
    store.stage_restore(m['id'], out, password=None, current_privacy_db=db,
                        privacy_key_path=tmp_path / 'privacy.key')
    staged = out / 'data/mem.sqlite3'
    assert keys(staged) == ['tt:id:bob']
    with sqlite3.connect(staged) as d:
        assert d.execute("SELECT COUNT(*) FROM interactions WHERE customer_key='tt:id:alice'").fetchone()[0] == 0
        assert d.execute("SELECT COUNT(*) FROM customer_facts WHERE customer_key='tt:id:alice'").fetchone()[0] == 0


def test_tombstones_union_after_restore(tmp_path):
    db, svc = setup(tmp_path)
    svc.forget('tt:id:bob')
    store = BackupV2(tmp_path / 'b')
    m = store.create(src(db), metadata=META, privacy_db=db)   # backup tombstone: bob
    svc.forget('tt:id:alice')                                  # current tombstone: alice
    current = read_tombstones(db)
    svc.close()
    out = tmp_path / 'stage'
    store.stage_restore(m['id'], out, password=None, current_tombstones=current,
                        privacy_key_path=tmp_path / 'privacy.key')
    staged = {t['key_hmac'] for t in read_tombstones(out / 'data/mem.sqlite3')}
    assert staged == {t['key_hmac'] for t in current} and len(staged) == 2
    assert keys(out / 'data/mem.sqlite3') == []
