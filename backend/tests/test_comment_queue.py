import pytest
from concurrent.futures import ThreadPoolExecutor
from comment_queue import CommentQueue
from customer_memory import CustomerMemory


def test_fifo_is_durable_and_retry_never_refreshes_age(tmp_path):
    now = [1000.0]
    path = tmp_path / 'db.sqlite3'
    queue = CommentQueue(path, clock=lambda: now[0])
    a = queue.enqueue('tiktok', 'room', 'a', {'comment_id': '1', 'comment': 'Giá?', 'received_at_ms': 1000000})
    queue.enqueue('tiktok', 'room', 'b', {'comment_id': '2', 'comment': 'Liều?', 'received_at_ms': 1000000})
    queue.close()
    queue = CommentQueue(path, clock=lambda: now[0])
    try:
        now[0] += 10
        again = queue.enqueue('tiktok', 'room', 'a', {'comment_id': '1', 'received_at_ms': now[0] * 1000})
        assert again == a
        first = queue.claim_next('tiktok', 'room')
        assert first['payload']['comment_id'] == '1'
        assert queue.claim_next('tiktok', 'room') is None
        assert queue.finish_generation(first['queue_id'], first['worker_token'],
            {'should_send': True, 'reply_id': 'turn', 'reply': 'Tin'})
        assert queue.claim_next('tiktok', 'room') is None
        assert queue.ready('tiktok', 'room')['result']['reply_id'] == 'turn'
        queue.settle('turn', 'sent')
        assert queue.ready('tiktok', 'room') is None
        assert queue.claim_next('tiktok', 'room')['payload']['comment_id'] == '2'
    finally:
        queue.close()


@pytest.mark.parametrize('age,expected', [(119, True), (120, False), (121, False)])
def test_deadline_before_first_generation(tmp_path, age, expected):
    queue = CommentQueue(tmp_path / 'db.sqlite3', clock=lambda: 1000.0)
    try:
        queue.enqueue('shopee', 'room', 'a', {'comment_id': '1', 'received_at_ms': (1000-age)*1000})
        assert (queue.claim_next('shopee', 'room') is not None) == expected
    finally:
        queue.close()


def test_room_change_and_crashed_generation_do_not_replay(tmp_path):
    now = [1000.0]
    queue = CommentQueue(tmp_path / 'db.sqlite3', clock=lambda: now[0])
    try:
        queue.enqueue('tiktok', 'old', 'a', {'comment_id': '1'})
        first = queue.claim_next('tiktok', 'old', lease_s=10)
        now[0] += 11
        assert queue.claim_next('tiktok', 'old') is None
        assert queue.summary('tiktok')['states'] == {'unknown': 1}
        queue.enqueue('tiktok', 'new', 'a', {'comment_id': '1'})
        second = queue.claim_next('tiktok', 'new')
        assert second['queue_id'] != first['queue_id']
        assert not queue.finish_generation(first['queue_id'], first['worker_token'], {'should_send': True})
    finally:
        queue.close()


def test_forget_cancels_queue_and_blocks_reinsert(tmp_path):
    path = tmp_path / 'db.sqlite3'
    memory = CustomerMemory(path, tmp_path / 'journal')
    queue = CommentQueue(path)
    try:
        key = memory.customer_key(platform_user_id='77')[0]
        queue.enqueue('tiktok', 'room', key, {'comment_id': '1', 'comment': 'Giá?'})
        work = queue.claim_next('tiktok', 'room')
        assert memory.forget_if_requested('xóa dữ liệu của tôi', platform_user_id='77')
        assert not queue.finish_generation(work['queue_id'], work['worker_token'], {'should_send': True})
        assert queue.enqueue('tiktok', 'room', key, {'comment_id': '2'})['reason'] == 'memory_disabled'
        assert queue.db.execute('SELECT COUNT(*) FROM reply_comment_queue').fetchone()[0] == 0
    finally:
        queue.close()
        memory.close()


def test_two_connections_cannot_generate_two_turns_on_same_platform(tmp_path):
    path = tmp_path / 'db.sqlite3'
    first, second = CommentQueue(path), CommentQueue(path)
    try:
        for comment_id in ('1', '2'):
            first.enqueue('tiktok', 'room', 'a', {'comment_id': comment_id})
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda queue: queue.claim_next('tiktok', 'room'), (first, second)))
        assert sum(claim is not None for claim in claims) == 1
        first.enqueue('shopee', 'shop', 'b', {'comment_id': '3'})
        assert second.claim_next('shopee', 'shop') is not None
    finally:
        first.close()
        second.close()


def test_partial_turn_holds_fifo_for_sixty_seconds_after_first_send(tmp_path):
    now = [1000.0]
    queue = CommentQueue(tmp_path / 'db.sqlite3', clock=lambda: now[0])
    try:
        queue.enqueue('tiktok', 'room', 'a', {'comment_id': '1'})
        first = queue.claim_next('tiktok', 'room')
        queue.finish_generation(first['queue_id'], first['worker_token'], {'should_send': True, 'reply_id': 'turn'})
        now[0] = 1119
        queue.settle('turn', 'partially_sent', first_sent_at=now[0])
        queue.enqueue('tiktok', 'room', 'b', {'comment_id': '2'})
        now[0] = 1121
        assert queue.ready('tiktok', 'room')['result']['reply_id'] == 'turn'
        assert queue.claim_next('tiktok', 'room') is None
        now[0] = 1179
        assert queue.ready('tiktok', 'room') is None
        assert queue.claim_next('tiktok', 'room')['payload']['comment_id'] == '2'
    finally:
        queue.close()


def test_fallback_id_uses_event_time_without_merging_later_question(tmp_path):
    now = [1000.0]
    queue = CommentQueue(tmp_path / 'db.sqlite3', clock=lambda: now[0])
    try:
        event = {'comment': 'Giá?', 'created_at': '2026-10-02T00:00:00Z'}
        first = queue.enqueue('tiktok', 'room', 'a', event)
        now[0] += 1
        assert queue.enqueue('tiktok', 'room', 'a', event) == first
        later = queue.enqueue('tiktok', 'room', 'a', {**event, 'created_at': '2026-10-02T00:00:01Z'})
        assert later['queue_id'] != first['queue_id']
    finally:
        queue.close()


def test_old_room_inflight_queue_does_not_block_new_room_or_claim_success(tmp_path):
    queue = CommentQueue(tmp_path / 'db.sqlite3')
    try:
        old = queue.enqueue('tiktok', 'old', 'a', {'comment_id': '1'})
        with queue.db:
            queue.db.execute("UPDATE reply_comment_queue SET state='sending',turn_id='old-turn' WHERE queue_id=?",
                             (old['queue_id'],))
        new = queue.enqueue('tiktok', 'new', 'b', {'comment_id': '2'})
        assert queue.claim_next('tiktok', 'new')['queue_id'] == new['queue_id']
        row = queue.db.execute('SELECT state FROM reply_comment_queue WHERE queue_id=?', (old['queue_id'],)).fetchone()
        assert row['state'] == 'unknown'
    finally:
        queue.close()


def test_duplicate_enqueue_keeps_received_at_and_state(tmp_path):
    now = [1000.0]
    queue = CommentQueue(tmp_path / 'db.sqlite3', clock=lambda: now[0])
    try:
        first = queue.enqueue('tiktok', 'room', 'a', {'comment_id': '1', 'received_at_ms': 990000})
        now[0] += 5
        again = queue.enqueue('tiktok', 'room', 'a', {'comment_id': '1', 'received_at_ms': now[0] * 1000})
        assert again == first and again['expires_at'] == 990 + 120
    finally:
        queue.close()


def test_reenqueue_with_old_received_at_is_expired(tmp_path):
    queue = CommentQueue(tmp_path / 'db.sqlite3', clock=lambda: 1000.0)
    try:
        row = queue.enqueue('tiktok', 'room', 'a', {'comment_id': 'old', 'received_at_ms': (1000 - 121) * 1000})
        assert row['state'] == 'expired'
        assert queue.claim_next('tiktok', 'room') is None
    finally:
        queue.close()


def test_interleaved_customers_are_strictly_fifo(tmp_path):
    queue = CommentQueue(tmp_path / 'db.sqlite3', clock=lambda: 1000.0)
    try:
        for cid, customer in (('1', 'a'), ('2', 'b'), ('3', 'a'), ('4', 'b')):
            queue.enqueue('tiktok', 'room', customer, {'comment_id': cid, 'received_at_ms': 1000000})
        order = []
        for _ in range(4):
            work = queue.claim_next('tiktok', 'room')
            order.append(work['payload']['comment_id'])
            queue.finish_generation(work['queue_id'], work['worker_token'],
                {'should_send': True, 'reply_id': 't' + order[-1]})
            queue.settle('t' + order[-1], 'sent')
        assert order == ['1', '2', '3', '4']
    finally:
        queue.close()


def test_cancel_only_affects_queued_items_and_unblocks_fifo(tmp_path):
    queue = CommentQueue(tmp_path / 'db.sqlite3')
    try:
        queue.enqueue('tiktok', 'room', 'a', {'comment_id': '1'})
        queue.enqueue('tiktok', 'room', 'b', {'comment_id': '2'})
        assert queue.cancel('tiktok', 'room', '1') == {'cancelled': True}
        assert queue.cancel('tiktok', 'room', '1') == {'cancelled': False, 'state': 'cancelled'}
        assert queue.cancel('tiktok', 'room', 'none') == {'cancelled': False, 'state': 'missing'}
        claimed = queue.claim_next('tiktok', 'room')
        assert claimed['payload']['comment_id'] == '2'
        # Generating item is never cancelled.
        assert queue.cancel('tiktok', 'room', '2') == {'cancelled': False, 'state': 'generating'}
        with pytest.raises(ValueError):
            queue.cancel('tiktok', '', '2')
    finally:
        queue.close()
