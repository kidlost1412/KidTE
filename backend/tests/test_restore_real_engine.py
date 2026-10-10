"""F06: restore v2 through a REAL ReplyEngine on a tmp KID_TE_ROOT (never real data)."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import server
from customer_memory import CustomerMemory
from knowledge import KnowledgeBase

PASSWORD = 'correct horse battery'
FORGET_KEY = 'tiktok:id:forgotten'


class Responder:
    def reply(self, nickname, comment, context, max_chars):
        return {'action': 'reply', 'reply': 'Dạ, câu trả lời đủ dữ kiện ạ.'}


def _count(path, table, where='1=1', args=()):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(f'SELECT COUNT(*) FROM {table} WHERE {where}', args).fetchone()[0]


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / 'KidTE'
    data = root / 'data'
    (data / 'shared').mkdir(parents=True)
    (data / 'machine').mkdir()
    for name, value in (('ROOT', root), ('DATA_DIR', data), ('MEMORY_DB', data / 'customer_memory.sqlite3'),
                        ('LOG_DIR', root / 'logs'), ('MEMORY_JOURNAL_DIR', root / 'logs' / 'customer_sessions'),
                        ('LIVE_CATALOG_PATH', data / 'live_products_current.json'),
                        ('BACKUP_DIR', root / 'backups')):
        monkeypatch.setattr(server, name, value)
    import workspace
    monkeypatch.setattr(workspace, 'MEMORY_DB', data / 'customer_memory.sqlite3')
    kb = data / 'shared' / 'product_knowledge.json'
    kb.write_text(json.dumps({'nhom_san_pham': [{'san_pham': [{
        'san_pham_id': 'SP-03', 'ten_san_pham': 'Kích Rễ Giâm Cành TC01', 'approved_for_live': True,
        'shop_product_ids': ['p1'], 'huong_dan_su_dung': 'pha 10ml cho bình 16 lít',
        'cong_dung': 'Hỗ trợ kích rễ'}]}]}, ensure_ascii=False), encoding='utf-8')
    prof = data / 'shared' / 'profiles.json'
    prof.write_text('{"products":{}}', encoding='utf-8')
    store = server.LiveCatalogStore()
    store.snapshot = {}
    store.sync = lambda s: setattr(store, 'snapshot', {
        'room_id': s.get('room_id', ''), 'pin_id': s.get('pin_id', ''), 'products': s.get('products', []),
        'product_count': len(s.get('products', [])), 'synced_at': 'now'}) or store.snapshot
    reporter = server.MissingReporter()
    reporter.path = tmp_path / 'missing.jsonl'
    memory = CustomerMemory(data / 'customer_memory.sqlite3', root / 'logs' / 'customer_sessions',
                            channel='live-re-ai')
    engine = server.ReplyEngine(responder=Responder(), memory=memory, knowledge=KnowledgeBase(kb, prof),
                                catalog_store=store, missing_reporter=reporter)
    assert engine.workspace.root == root
    for key, text in (('alice', 'Kích rễ TC01 dùng sao'), ('bob', 'giá bao nhiêu'), ('forgotten', 'xin chào')):
        engine.memory.record_comment(text, comment_id='c-' + key, platform_user_id=key, display_name=key)
    with closing(sqlite3.connect(engine.workspace.db_path)) as db, db:
        db.execute("INSERT INTO workspace_meta(meta_key,meta_value) VALUES('probe','before')")
    yield engine, root
    engine.close()


def _enable_flag(root):
    (root / 'data' / 'machine' / 'restore_v2.json').write_text(
        json.dumps({'restore_v2_enabled': True}), encoding='utf-8')


def _make_backup(engine):
    result = engine._backup_v2_service().create(PASSWORD, full=True)
    return result['backup_id']


def _mutate(engine):
    engine.memory.record_comment('sau backup', comment_id='c-late', platform_user_id='late', display_name='late')
    with closing(sqlite3.connect(engine.workspace.db_path)) as db, db:
        db.execute("UPDATE workspace_meta SET meta_value='after' WHERE meta_key='probe'")


def _reply(engine, n):
    payload = {'comment': 'TC01 dùng sao', 'comment_id': 'rc%d' % n, 'user_id': 'u%d' % n,
               'unique_id': 'useru%d' % n, 'nickname': 'Khách', 'live_context': {
                   'room_id': 'room-1', 'pin_id': 'p1', 'products': [{
                       'product_id': 'p1', 'title': 'Kích Rễ Giâm Cành TC01',
                       'original_price': '120.000₫', 'sale_price': '90.000₫'}]}}
    return engine.handle(payload)


def test_flag_off_by_default(env):
    engine, root = env
    with pytest.raises(ValueError, match='restore_v2_disabled'):
        engine.restore_backup_v2({'backup_id': 'x', 'password': PASSWORD})


def test_real_restore_reverts_data_keeps_tombstone(env):
    engine, root = env
    _enable_flag(root)
    memdb = root / 'data' / 'customer_memory.sqlite3'
    backup_id = _make_backup(engine)
    # Forget AFTER the backup: the backup still contains this customer.
    engine.privacy_forget({'customer_key': FORGET_KEY})
    assert _count(memdb, 'customers', 'customer_key=?', (FORGET_KEY,)) == 0
    _mutate(engine)
    assert _count(memdb, 'customers', 'customer_key=?', ('tiktok:id:late',)) == 1

    result = engine.restore_backup_v2({'backup_id': backup_id, 'password': PASSWORD})
    assert result['ok'] and result['ai_enabled'] is False
    assert Path(result['rollback_dir']).is_dir()
    memdb = root / 'data' / 'customer_memory.sqlite3'
    assert _count(memdb, 'customers', 'customer_key=?', ('tiktok:id:late',)) == 0       # reverted
    assert _count(memdb, 'customers', 'customer_key=?', ('tiktok:id:alice',)) == 1
    assert _count(memdb, 'customers', 'customer_key=?', (FORGET_KEY,)) == 0               # not resurrected
    assert _count(memdb, 'interactions', 'customer_key=?', (FORGET_KEY,)) == 0
    assert engine.privacy.is_forgotten(FORGET_KEY)                                        # tombstone kept
    assert _count(memdb, 'privacy_tombstones') >= 1
    with closing(sqlite3.connect(engine.workspace.db_path)) as db:
        assert db.execute("SELECT meta_value FROM workspace_meta WHERE meta_key='probe'").fetchone()[0] == 'before'

    # AI disabled until reenable-ai; stores reopened and usable.
    assert engine._ai_disabled_by_restore() is True
    engine.memory.record_comment('mới', comment_id='c-new', platform_user_id='new', display_name='new')
    assert _count(memdb, 'customers', 'customer_key=?', ('tiktok:id:new',)) == 1
    assert engine.workspace.closed is False
    assert engine.reenable_ai_after_restore({})['ai_disabled'] is False
    reply = _reply(engine, 1)
    assert reply['ok'] is True and reply['should_send'] is True and 'câu trả lời' in reply['reply']


def test_failure_injection_restores_old_dir(env, monkeypatch):
    engine, root = env
    _enable_flag(root)
    memdb = root / 'data' / 'customer_memory.sqlite3'
    backup_id = _make_backup(engine)
    _mutate(engine)

    def boom(self, staged):
        raise RuntimeError('injected')
    monkeypatch.setattr(server.ReplyEngine, '_restore_migrate', boom)
    with pytest.raises(ValueError, match='restore_failed'):
        engine.restore_backup_v2({'backup_id': backup_id, 'password': PASSWORD})
    # Old data intact (mutation still there), stores reopened and usable.
    assert _count(memdb, 'customers', 'customer_key=?', ('tiktok:id:late',)) == 1
    assert (root / 'data').is_dir() and not list(root.glob('data.failed-*'))
    engine.memory.record_comment('x', comment_id='c-x', platform_user_id='x', display_name='x')
    assert _count(memdb, 'customers', 'customer_key=?', ('tiktok:id:x',)) == 1
    assert engine.maintenance.active is False
    engine.reenable_ai_after_restore({})
    assert _reply(engine, 2)['ok'] is True


def test_failure_after_swap_rolls_back(env, monkeypatch):
    engine, root = env
    _enable_flag(root)
    memdb = root / 'data' / 'customer_memory.sqlite3'
    backup_id = _make_backup(engine)
    _mutate(engine)
    real = server.ReplyEngine._restore_reinit
    calls = []

    def flaky(self):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('injected reinit failure')
        return real(self)
    monkeypatch.setattr(server.ReplyEngine, '_restore_reinit', flaky)
    with pytest.raises(ValueError, match='restore_failed'):
        engine.restore_backup_v2({'backup_id': backup_id, 'password': PASSWORD})
    assert len(calls) == 2
    assert _count(memdb, 'customers', 'customer_key=?', ('tiktok:id:late',)) == 1   # old dir back
    assert list(root.glob('data.failed-*'))
    engine.memory.record_comment('y', comment_id='c-y', platform_user_id='y', display_name='y')
    assert _count(memdb, 'customers', 'customer_key=?', ('tiktok:id:y',)) == 1
