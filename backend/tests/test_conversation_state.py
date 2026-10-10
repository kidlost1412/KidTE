from customer_memory import CustomerMemory


def test_pending_fields_are_persisted_but_only_confirmed_questions_are_visible(tmp_path):
    path = tmp_path / 'memory.sqlite3'
    memory = CustomerMemory(path, tmp_path / 'journal', session_id='test-session')
    try:
        ref = memory.record_comment('giá bao nhiêu', comment_id='one', room_id='room-a',
                                    platform_user_id='77', display_name='Khách')
        memory.record_reply(ref['interaction_id'], 'Mình lấy dung tích nào?', complete=False, turn_id='turn')
        memory.note_conversation_state(ref['interaction_id'], {
            'awaiting_fields': ['variant', 'variant', 'invalid'],
            'selected_sku': 'sku-asked', 'observed_pin': 'different-pin'})
        assert memory.recent_live_turns(ref['customer_key'], '', '', room_id='room-a')[0]['conversation_state'] == {}
        with memory._lock, memory._db:
            memory._db.execute("UPDATE interactions SET delivery_status='sent' WHERE interaction_id=?", (ref['interaction_id'],))
        assert memory.recent_live_turns(ref['customer_key'], '', '', room_id='room-b') == []
    finally:
        memory.close()
    reopened = CustomerMemory(path, tmp_path / 'journal', session_id='test-session')
    try:
        state = reopened.recent_live_turns(ref['customer_key'], '', '', room_id='room-a')[0]['conversation_state']
        assert state == {'awaiting_fields': ['variant'], 'selected_sku': 'sku-asked', 'observed_pin': 'different-pin',
                         'product_family': '', 'candidate_skus': []}
        assert reopened.forget_if_requested('xóa dữ liệu của tôi', platform_user_id='77', display_name='Khách')
        reopened.note_conversation_state(ref['interaction_id'], state)
        assert reopened.recent_live_turns(ref['customer_key'], '', '', room_id='room-a') == []
    finally:
        reopened.close()
