import sqlite3

from comment_queue import CommentQueue
from customer_memory import CustomerMemory
from privacy import PrivacyService


def _setup(tmp_path):
    db = tmp_path / 'db.sqlite3'
    privacy = PrivacyService(db, tmp_path / 'machine' / 'privacy_key')
    memory = CustomerMemory(db, tmp_path / 'journal', privacy=privacy)
    queue = CommentQueue(db, privacy=privacy)
    return db, privacy, memory, queue


def test_tombstone_blocks_ai_immediately(tmp_path):
    db, privacy, memory, queue = _setup(tmp_path)
    try:
        key = memory.customer_key(platform_user_id='9')[0]
        queue.enqueue('tiktok', 'room', key, {'comment_id': '1', 'comment': 'Giá?'})
        # tombstone only (no purge): claims and memory writes must stop at once
        privacy.forget(key)
        assert privacy.is_forgotten(key)
        assert queue.claim_next('tiktok', 'room') is None
        assert queue.enqueue('tiktok', 'room', key, {'comment_id': '2'})['reason'] == 'memory_disabled'
        assert memory.record_comment('hi', platform_user_id='9')['opted_out']
        assert memory.context_for_customer(key) == '' if hasattr(memory, 'context_for_customer') else True
    finally:
        queue.close(); memory.close(); privacy.close()


def test_queue_purged(tmp_path):
    db, privacy, memory, queue = _setup(tmp_path)
    try:
        key = memory.customer_key(platform_user_id='5')[0]
        queue.enqueue('shopee', 'room', key, {'comment_id': '1', 'comment': 'Giá?'})
        assert memory.forget_if_requested('xóa dữ liệu của tôi', platform_user_id='5')
        assert queue.db.execute('SELECT COUNT(*) FROM reply_comment_queue').fetchone()[0] == 0
        result = privacy.forget(key, memory=memory)
        found = privacy.request(result['request_id'])
        assert found['status'] == 'done'
        assert [s['step'] for s in found['steps']] == ['tombstone', 'customer_db', 'comment_queue']
    finally:
        queue.close(); memory.close(); privacy.close()


def test_no_plain_key_stored(tmp_path):
    db, privacy, memory, queue = _setup(tmp_path)
    try:
        key = 'tiktok:uid:SecretCustomer123'
        privacy.forget(key)
        con = sqlite3.connect(db)
        dump = ' '.join(str(r) for t in ('privacy_tombstones', 'privacy_requests')
                        for r in con.execute(f'SELECT * FROM {t}'))
        con.close()
        assert 'SecretCustomer123'.casefold() not in dump.casefold()
        assert privacy.key_hmac(key) in dump
        assert len((tmp_path / 'machine' / 'privacy_key').read_bytes()) == 32
    finally:
        queue.close(); memory.close(); privacy.close()


def test_record_comment_after_forget_ignored(tmp_path):
    db, privacy, memory, queue = _setup(tmp_path)
    try:
        assert memory.forget_if_requested('xóa dữ liệu của tôi', platform_user_id='3')
        # even if the legacy opt-out row is lost, the tombstone still blocks
        memory._db.execute('DELETE FROM memory_opt_outs'); memory._db.commit()
        ref = memory.record_comment('Giá bao nhiêu', platform_user_id='3')
        assert ref['opted_out'] and ref['interaction_id'] == ''
        assert memory._db.execute('SELECT COUNT(*) FROM interactions').fetchone()[0] == 0
    finally:
        queue.close(); memory.close(); privacy.close()
