"""F02: browser extension storage purge (pending list, heartbeat delivery, ack, expiry)."""
import json
import sqlite3
from types import SimpleNamespace

import server
from customer_memory import CustomerMemory
from privacy import PrivacyService

KEY = 'tiktok:id:nongdan9999'
NAME = 'Nguyen Van Nong Dan'


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def _env(tmp_path):
    clock = Clock()
    db = tmp_path / 'db.sqlite3'
    privacy = PrivacyService(db, tmp_path / 'machine' / 'privacy_key', clock=clock)
    memory = CustomerMemory(db, tmp_path / 'journal', privacy=privacy)
    return privacy, memory, clock, db


def _forget(privacy, memory):
    return privacy.forget(KEY, memory=memory, display_name=NAME, derived={})


def _engine(privacy):
    return SimpleNamespace(privacy=privacy, platform_heartbeats={}, extension_heartbeat=None,
                           _ai_disabled_by_restore=lambda: False)


def _beat(engine, platform):
    return server.ReplyEngine.heartbeat(engine, {'platform': platform, 'live_running': True})


def test_forget_creates_pending_step_and_partial_request(tmp_path):
    privacy, memory, _, _ = _env(tmp_path)
    result = _forget(privacy, memory)
    assert result['status'] == 'partial'
    step = [s for s in privacy.request(result['request_id'])['steps'] if s['step'] == 'browser_extension_storage'][0]
    assert step['status'] == 'pending'
    items = privacy.pending_purges('tiktok')
    assert len(items) == 1 and items[0]['platform'] == 'tiktok'
    assert 'nongdan9999' in items[0]['identifiers']
    assert 'nguyen van nong dan' in items[0]['identifiers']


def test_heartbeat_delivers_purge_per_platform_and_plain_heartbeat_unchanged(tmp_path):
    privacy, memory, _, _ = _env(tmp_path)
    engine = _engine(privacy)
    assert 'privacy_purge' not in _beat(engine, 'tiktok')
    _forget(privacy, memory)
    tiktok = _beat(engine, 'tiktok')
    shopee = _beat(engine, 'shopee')
    assert tiktok['ok'] and tiktok['privacy_purge'][0]['platform'] == 'tiktok'
    assert shopee['privacy_purge'][0]['platform'] == 'shopee'
    assert tiktok['privacy_purge'][0]['id'] == shopee['privacy_purge'][0]['id']


def test_ack_transitions_pending_to_deleted_and_complete(tmp_path):
    privacy, memory, _, _ = _env(tmp_path)
    result = _forget(privacy, memory)
    pid = privacy.pending_purges('tiktok')[0]['id']
    assert privacy.ack_purges([pid], 'tiktok') == {'acked': 1}
    assert privacy.pending_purges('tiktok') == []
    assert privacy.pending_purges('shopee')[0]['id'] == pid
    mid = privacy.request(result['request_id'])
    assert mid['status'] == 'partial'
    assert [s for s in mid['steps'] if s['step'] == 'browser_extension_storage'][0]['status'] == 'pending'
    # other derived stores are not wired in this fixture; mark them done so only the browser step decides
    steps = [dict(s, status='deleted') if s['step'] != 'browser_extension_storage' else s
             for s in mid['steps']]
    privacy._save(result['request_id'], 'partial', steps, finished=True)
    privacy.ack_purges([pid], 'shopee')
    done = privacy.request(result['request_id'])
    assert done['status'] == 'complete'
    assert [s for s in done['steps'] if s['step'] == 'browser_extension_storage'][0]['status'] == 'deleted'
    assert privacy.pending_purges('shopee') == []


def test_ack_via_engine_validates_platform_and_unknown_ids_ignored(tmp_path):
    privacy, memory, _, _ = _env(tmp_path)
    engine = _engine(privacy)
    engine.privacy = privacy
    _forget(privacy, memory)
    assert server.ReplyEngine.privacy_purge_ack(engine, {'ids': ['nope'], 'platform': 'tiktok'})['acked'] == 0
    for bad in ({'ids': 'x', 'platform': 'tiktok'}, {'ids': [], 'platform': 'bad'}, None):
        try:
            server.ReplyEngine.privacy_purge_ack(engine, bad)
        except ValueError as exc:
            assert 'privacy_purge_ack_invalid' in str(exc)
        else:
            raise AssertionError('expected ValueError')


def test_entries_expire_after_seven_days_and_request_stays_partial(tmp_path):
    privacy, memory, clock, db = _env(tmp_path)
    result = _forget(privacy, memory)
    clock.now += 7 * 86400 + 1
    assert privacy.pending_purges('tiktok') == []
    request = privacy.request(result['request_id'])
    assert request['status'] == 'partial'
    assert [s for s in request['steps'] if s['step'] == 'browser_extension_storage'][0]['status'] == 'expired'
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM privacy_browser_purge').fetchone()[0] == 0


def test_no_plain_names_persisted_after_ack_or_expiry(tmp_path):
    privacy, memory, clock, db = _env(tmp_path)
    _forget(privacy, memory)
    pid = privacy.pending_purges('tiktok')[0]['id']
    privacy.ack_purges([pid], 'tiktok')
    privacy.ack_purges([pid], 'shopee')
    # second customer expires instead of being acked
    privacy.forget('tiktok:id:other-person-77', memory=memory, display_name='Tran Thi Lan Anh', derived={})
    clock.now += 8 * 86400
    privacy.pending_purges('shopee')
    privacy.close()
    memory.close() if hasattr(memory, 'close') else None
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM privacy_browser_purge').fetchone()[0] == 0
        blob = json.dumps([list(r) for r in conn.execute('SELECT * FROM privacy_requests')]).lower()
        blob += json.dumps([list(r) for r in conn.execute('SELECT * FROM privacy_tombstones')]).lower()
    for plain in ('nongdan9999', 'nguyen van nong dan', 'other-person-77', 'tran thi lan anh'):
        assert plain not in blob


def test_restore_v2_enabled_flag_in_dashboard_payload(tmp_path, monkeypatch):
    machine = tmp_path / 'machine'
    machine.mkdir()
    monkeypatch.setattr(server, 'DATA_DIR', tmp_path)
    assert server.ReplyEngine._restore_v2_flag(None) is False
    (machine / 'restore_v2.json').write_text('{"restore_v2_enabled": true}', encoding='utf-8')
    assert server.ReplyEngine._restore_v2_flag(None) is True


def test_captcha_bridge_close_uploads_is_safe_noop():
    from captcha_bridge import CaptchaBridge
    bridge = CaptchaBridge.__new__(CaptchaBridge)
    bridge.close_uploads()
    closed = []
    bridge.uploads = SimpleNamespace(close=lambda: closed.append(1))
    bridge.close_uploads()
    assert closed == [1]
