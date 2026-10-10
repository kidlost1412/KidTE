import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from delivery_store import DeliveryStore, utf16_length
from customer_memory import CustomerMemory


def test_customer_forget_removes_delivery_payload_but_preserves_other_customer(tmp_path):
    path = tmp_path / 'memory.sqlite3'
    memory = CustomerMemory(path, tmp_path / 'journal')
    store = DeliveryStore(path)
    try:
        for user in ('77', '88'):
            ref = memory.record_comment('Giá?', comment_id=user, room_id='room',
                platform_user_id=user, display_name='Khách ' + user)
            if user == '77':
                deleted_interaction = ref['interaction_id']
            store.prepare('turn-' + user, 'tiktok', 'room', ref['interaction_id'], ['Tư vấn ' + user])
        assert memory.forget_if_requested('xóa dữ liệu của tôi',
            platform_user_id='77', display_name='Khách 77')
        assert store.get('turn-77') is None
        assert store.get('turn-88')['messages'][0]['content'] == 'Tư vấn 88'
        assert store._db.execute('SELECT COUNT(*) FROM reply_messages').fetchone()[0] == 1
        with pytest.raises(ValueError, match='delivery_interaction_missing'):
            store.prepare('late-turn', 'tiktok', 'room', deleted_interaction, ['Muộn'])
        blocked = memory.record_comment('Câu mới', room_id='room', platform_user_id='77')
        assert blocked['opted_out']
    finally:
        store.close()
        memory.close()


def test_dispatch_crash_never_reclaims_same_message(tmp_path):
    now = [1000.0]
    path = tmp_path / 'memory.sqlite3'
    store = DeliveryStore(path, clock=lambda: now[0])
    turn = store.prepare('turn', 'shopee', 'room', 'interaction', ['First', 'Second'])
    first, second = turn['messages']
    assert not store.claim('turn', second['message_id'], 'shopee', 'room')['ok']
    claim = store.claim('turn', first['message_id'], 'shopee', 'room')
    assert claim['ok']
    assert not store.claim('turn', first['message_id'], 'shopee', 'other')['ok']
    assert store.dispatched(first['message_id'], claim['claim_token'])
    store.close()
    now[0] += 31
    reopened = DeliveryStore(path, clock=lambda: now[0])
    try:
        result = reopened.claim('turn', first['message_id'], 'shopee', 'room')
        assert result == {'ok': False, 'error': 'delivery_not_ready', 'state': 'unknown'}
    finally:
        reopened.close()


def test_status_reconciles_dispatch_timeout_without_another_claim(tmp_path):
    now = [1000.0]
    store = DeliveryStore(tmp_path / 'db.sqlite3', clock=lambda: now[0])
    try:
        message = store.prepare('turn', 'tiktok', 'room', 'i', ['One'])['messages'][0]
        claim = store.claim('turn', message['message_id'], 'tiktok', 'room')
        assert store.dispatched(message['message_id'], claim['claim_token'])
        now[0] += 31
        assert store.reconcile('turn')['state'] == 'unknown'
        receipt = {'room_id': 'room', 'response_code': 0}
        assert store.confirm_message('turn', message['message_id'], 'tiktok', 'room', message['content_hash'], receipt)
        assert store.reconcile('turn')['state'] == 'sent'
    finally:
        store.close()


def test_v2_receipt_proves_exact_message_and_partial_turn(tmp_path):
    now = [1000.0]
    store = DeliveryStore(tmp_path / 'db.sqlite3', clock=lambda: now[0])
    try:
        turn = store.prepare('turn', 'tiktok', 'room', 'i', ['One', 'Two'])
        first, second = turn['messages']
        receipt = {'room_id': 'room', 'response_code': 0}
        args = ('turn', first['message_id'], 'tiktok', 'room', first['content_hash'], receipt)
        assert not store.confirm_message(*args)
        lease = store.claim('turn', first['message_id'], 'tiktok', 'room')
        assert store.dispatched(first['message_id'], lease['claim_token'])
        assert not store.confirm_message(*args[:-2], 'wrong-hash', receipt)
        for code in (None, False, '', 0.0, 4):
            assert not store.confirm_message(*args[:-1], {**receipt, 'response_code': code})
        assert store.confirm_message(*args)
        assert store.confirm_message(*args)
        assert store.get('turn')['state'] == 'partially_sent'
        other = store.prepare('other-turn', 'tiktok', 'room', 'other', ['Other'])['messages'][0]
        now[0] = 1003
        assert store.claim('other-turn', other['message_id'], 'tiktok', 'room')['error'] == 'delivery_previous_turn_pending'
        now[0] = 1060.0
        assert store.claim('turn', second['message_id'], 'tiktok', 'room')['error'] == 'delivery_expired'
        assert store.get('turn')['state'] == 'partially_sent'
    finally:
        store.close()


@pytest.mark.parametrize('platform,cooldown', [('tiktok', 2.5), ('shopee', 10)])
def test_platform_serializes_claims_and_obeys_success_cooldown(tmp_path, platform, cooldown):
    now = [1000.0]
    store = DeliveryStore(tmp_path / 'db.sqlite3', clock=lambda: now[0])
    try:
        first = store.prepare('a', platform, 'room', 'a', ['One'])['messages'][0]
        second = store.prepare('b', platform, 'room', 'b', ['Two'])['messages'][0]
        claim = store.claim('a', first['message_id'], platform, 'room')
        assert store.claim('b', second['message_id'], platform, 'room')['error'] == 'delivery_platform_busy'
        assert store.dispatched(first['message_id'], claim['claim_token'])
        receipt = {'room_id': 'room', 'response_code': 0, 'shopee_message_id': 's1'}
        assert store.confirm_message('a', first['message_id'], platform, 'room', first['content_hash'], receipt)
        now[0] += cooldown - 0.01
        assert store.claim('b', second['message_id'], platform, 'room')['error'] == 'delivery_cooldown'
        now[0] += 0.01
        assert store.claim('b', second['message_id'], platform, 'room')['ok']
    finally:
        store.close()


def test_lease_before_dispatch_can_recover_but_expired_turn_cannot(tmp_path):
    now = [1000.0]
    store = DeliveryStore(tmp_path/'db.sqlite3', clock=lambda: now[0])
    try:
        turn = store.prepare('turn', 'tiktok', 'room', 'interaction', ['One'])
        message_id = turn['messages'][0]['message_id']
        first = store.claim('turn', message_id, 'tiktok', 'room')
        now[0] += 31
        second = store.claim('turn', message_id, 'tiktok', 'room')
        assert second['ok'] and second['claim_token'] != first['claim_token']
        assert not store.dispatched(message_id, first['claim_token'])
        now[0] = 1120
        assert store.claim('turn', message_id, 'tiktok', 'room')['error'] == 'delivery_expired'
    finally:
        store.close()


def test_claim_near_deadline_cannot_dispatch_after_expiry(tmp_path):
    now = [1000.0]
    store = DeliveryStore(tmp_path / 'db.sqlite3', clock=lambda: now[0])
    try:
        message = store.prepare('turn', 'tiktok', 'room', 'i', ['One'])['messages'][0]
        now[0] = 1119
        claim = store.claim('turn', message['message_id'], 'tiktok', 'room')
        assert claim['claim_until'] == 1120
        now[0] = 1120
        assert not store.dispatched(message['message_id'], claim['claim_token'])
    finally:
        store.close()


def test_content_is_exact_and_utf16_limits_include_emoji(tmp_path):
    store = DeliveryStore(tmp_path/'db.sqlite3')
    try:
        content = '😀' * 75
        assert utf16_length(content) == 150
        turn = store.prepare('turn', 'tiktok', 'room', 'i', [content])
        assert turn['messages'][0]['content'] == content
        with pytest.raises(ValueError, match='delivery_content_invalid'):
            store.prepare('long', 'tiktok', 'room', 'i', [content + 'a'])
        with pytest.raises(ValueError, match='delivery_turn_conflict'):
            store.prepare('turn', 'tiktok', 'room', 'i', ['Changed'])
    finally:
        store.close()


def test_legacy_receipt_survives_restart_and_rejects_mismatch(tmp_path):
    path = tmp_path/'db.sqlite3'
    store = DeliveryStore(path)
    store.prepare('turn', 'shopee', '123', 'i', ['Hello'])
    assert store.record_legacy_delivery('turn', 'shopee', '123', {'shopee_message_id': 'm'}, 'sent')
    store.close()
    store = DeliveryStore(path)
    try:
        assert store.record_legacy_delivery('turn', 'shopee', '123', {'shopee_message_id': 'm'}, 'sent')
        assert not store.record_legacy_delivery('turn', 'shopee', '124', {'shopee_message_id': 'm'}, 'sent')
        assert not store.record_legacy_delivery('turn', 'shopee', '123', {'shopee_message_id': 'other'}, 'sent')
    finally:
        store.close()


def test_existing_database_has_verified_migration_snapshot(tmp_path):
    path = tmp_path/'memory.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE interactions (id TEXT)')
        db.execute("INSERT INTO interactions VALUES ('keep')")
    store = DeliveryStore(path)
    store.close()
    backup = tmp_path/'schema_backups'/'memory-before-delivery-v2.sqlite3'
    with sqlite3.connect(backup) as db:
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT id FROM interactions').fetchone()[0] == 'keep'


def test_two_database_connections_cannot_both_claim_a_message(tmp_path):
    path = tmp_path/'db.sqlite3'
    first, second = DeliveryStore(path), DeliveryStore(path)
    barrier = threading.Barrier(2)
    turn = first.prepare('turn', 'shopee', '123', 'i', ['Hello'])
    message_id = turn['messages'][0]['message_id']
    def claim(store):
        barrier.wait()
        return store.claim('turn', message_id, 'shopee', '123')
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, [first, second]))
        assert sum(result['ok'] for result in results) == 1
    finally:
        first.close()
        second.close()
