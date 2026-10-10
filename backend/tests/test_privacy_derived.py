import json
import sqlite3
from types import SimpleNamespace

from customer_history import CustomerHistory
from customer_memory import CustomerMemory
from learning import LearningStore
from privacy import PrivacyService
from workspace import WorkspaceStore


def _env(tmp_path, **kw):
    db = tmp_path / 'db.sqlite3'
    privacy = PrivacyService(db, tmp_path / 'machine' / 'privacy_key', **kw)
    memory = CustomerMemory(db, tmp_path / 'journal', privacy=privacy)
    learning = LearningStore(tmp_path / 'learn.sqlite3')
    learning.privacy = privacy
    ws = WorkspaceStore(SimpleNamespace(knowledge=SimpleNamespace(overrides_path=tmp_path / 'ov.json'),
                                        mapping_state=lambda: {'products': []}),
                        root=tmp_path / 'ws')
    ws.privacy = privacy
    (tmp_path / 'stream').mkdir()
    history = CustomerHistory(tmp_path / 'stream', memory)
    return privacy, memory, learning, ws, history


def _add_example(ws, conv, hmac, question='gia bao nhieu'):
    with ws._db() as db:
        db.execute("INSERT INTO approved_reply_examples (example_id,conversation_id,created_at,updated_at,"
                   "question,normalized_question,question_type,approved_answer,customer_key_hmac) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   ('e' + conv, conv, 't', 't', question, question, 'price', 'ans', hmac))
        db.execute("INSERT INTO corrections (correction_id,resource,record_id,created_at,reason,changes_json,"
                   "active,customer_key_hmac) VALUES (?,?,?,?,?,?,1,?)",
                   ('c' + conv, 'conversations', conv, 't', 'r', '{}', hmac))


def _forget(privacy, memory, learning, ws, history, key):
    return privacy.forget(key, memory=memory, derived={'learning': learning, 'workspace': ws, 'history': history})


def test_learning_rows_deleted(tmp_path):
    privacy, memory, learning, ws, history = _env(tmp_path)
    try:
        key = memory.customer_key(platform_user_id='7')[0]
        other = memory.customer_key(platform_user_id='8')[0]
        mine = learning.add_item(kind='fact', statement='fact of seven', source='correction',
                                 customer_key_hmac=learning.hmac_for(key))
        theirs = learning.add_item(kind='fact', statement='fact of eight', source='correction',
                                   customer_key_hmac=learning.hmac_for(other))
        run = learning.create_run('distill', payload={'customer_key': key, 'question': 'q'})
        raw = sqlite3.connect(learning.db_path).execute('SELECT input_json FROM learning_runs').fetchone()[0]
        assert key not in raw
        learning.update_run(run, status='done')
        _add_example(ws, 'conv1', privacy.key_hmac(key))
        _add_example(ws, 'conv2', privacy.key_hmac(other))
        res = _forget(privacy, memory, learning, ws, history, key)
        ids = {i['item_id'] for i in learning.items()}
        assert mine not in ids and theirs in ids
        assert learning.runs() == []
        with ws._db() as db:
            assert [r[0] for r in db.execute('SELECT conversation_id FROM approved_reply_examples')] == ['conv2']
            assert db.execute("SELECT COUNT(*) FROM corrections WHERE record_id='conv1'").fetchone()[0] == 0
        steps = {s['step']: s for s in privacy.request(res['request_id'])['steps']}
        assert steps['learning']['status'] == 'deleted' and steps['learning']['deleted'] == 2
        assert steps['corrections_examples']['deleted'] == 2
    finally:
        memory.close(); privacy.close()


def test_legacy_rows_excluded(tmp_path):
    # Policy change (W1): only legacy rows that mention the forgotten customer are hidden.
    privacy, memory, learning, ws, history = _env(tmp_path)
    try:
        legacy = learning.add_item(kind='fact', statement='legacy fact of SecretUser7', source='correction', status='active')
        manual = learning.add_item(kind='fact', statement='manual fact SecretUser7', source='manual', status='active')
        _add_example(ws, 'old', '', question='gia bao nhieu SecretUser7')
        assert ws.reviewed_examples('gia bao nhieu')
        key = memory.customer_key(platform_user_id='SecretUser7')[0]
        res = _forget(privacy, memory, learning, ws, history, key)
        active = {i['item_id'] for i in learning.items('active')}
        assert legacy not in active and manual in active
        assert ws.reviewed_examples('gia bao nhieu') == []
        assert ws.learning_examples() == []
        steps = {s['step']: s for s in privacy.request(res['request_id'])['steps']}
        assert steps['learning']['status'] == 'excluded'
        assert steps['corrections_examples']['status'] == 'excluded'
        assert privacy.request(res['request_id'])['status'] == 'partial'
    finally:
        memory.close(); privacy.close()


def test_legacy_kept_when_policy_off(tmp_path):
    privacy, memory, learning, ws, history = _env(tmp_path, exclude_legacy_learning_on_forget=False)
    try:
        legacy = learning.add_item(kind='fact', statement='legacy fact', source='correction', status='active')
        _forget(privacy, memory, learning, ws, history, 'tiktok:id:7')
        assert legacy in {i['item_id'] for i in learning.items('active')}
    finally:
        memory.close(); privacy.close()


def test_report_not_clean_when_unsupported(tmp_path):
    privacy, memory, learning, ws, history = _env(tmp_path)
    try:
        res = _forget(privacy, memory, learning, ws, history, 'tiktok:id:7')
        found = privacy.request(res['request_id'])
        by = {s['step']: s['status'] for s in found['steps']}
        assert by['browser_extension_storage'] == 'unsupported'
        assert by['raw_stream_logs'] == 'excluded'
        assert found['status'] == 'partial'
        res2 = privacy.forget('tiktok:id:9', memory=memory, derived={})
        by2 = {s['step']: s['status'] for s in privacy.request(res2['request_id'])['steps']}
        assert by2['learning'] == 'unsupported'
    finally:
        memory.close(); privacy.close()


def test_journal_purged_and_no_plain_key(tmp_path):
    privacy, memory, learning, ws, history = _env(tmp_path)
    try:
        key = memory.customer_key(platform_user_id='SecretUser1')[0]
        memory.record_comment('hello', platform_user_id='SecretUser1')
        memory.record_comment('hello', platform_user_id='Keep2')
        memory._append_journal('interaction', customer_key=key, platform_user_id='SecretUser1', display_name='Zed')
        memory._append_journal('interaction', customer_key='tiktok:id:keep2', platform_user_id='Keep2')
        _forget(privacy, memory, learning, ws, history, key)
        memory._append_journal('probe')  # live handle still usable after rewrite
        text = ''.join(p.read_text(encoding='utf-8') for p in (tmp_path / 'journal').glob('customers_*.jsonl'))
        assert 'SecretUser1' not in text and 'Keep2' in text and 'probe' in text
    finally:
        memory.close(); privacy.close()


def test_history_index_purged_and_blocked(tmp_path):
    privacy, memory, learning, ws, history = _env(tmp_path)
    try:
        key = memory.customer_key(platform_user_id='55', platform='tiktok')[0]
        row = {'ts': '2026-10-01T10:00:00+07:00', 'room_id': 'r1', 'user_id': '55', 'nickname': 'A',
               'text': 'xin chao shop', 'platform': 'tiktok', 'msg_id': 'm1'}
        (tmp_path / 'stream' / 'stream_1.jsonl').write_text(json.dumps(row) + '\n', encoding='utf-8')
        assert history.for_customer(key, current_room='r2')
        res = _forget(privacy, memory, learning, ws, history, key)
        assert history.for_customer(key, current_room='r2') == {}
        assert privacy.request(res['request_id'])['status'] == 'partial'
    finally:
        memory.close(); privacy.close()
