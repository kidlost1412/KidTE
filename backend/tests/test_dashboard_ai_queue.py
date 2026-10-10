import time
from comment_queue import CommentQueue
from delivery_store import DeliveryStore
import server


def _fake(tmp_path, now):
    eng = server.ReplyEngine.__new__(server.ReplyEngine)
    db = tmp_path / "m.sqlite3"
    eng.comment_queue = CommentQueue(db, clock=lambda: now)
    eng.delivery_store = DeliveryStore(db, clock=lambda: now)
    eng._shopee_ai = None
    return eng


def test_dashboard_ai_queue_shape(tmp_path, monkeypatch):
    now = time.time()
    eng = _fake(tmp_path, now)
    monkeypatch.setattr(server.ReplyEngine, "backend_queue_mode", staticmethod(lambda: True))
    q = eng.comment_queue
    ins = "INSERT INTO reply_comment_queue(queue_id,platform,room_id,comment_id,customer_key,received_at,expires_at,payload_json,state) VALUES(?,?,?,?,?,?,?,?,?)"
    rows = [("a", "tiktok", "r", "1", "k", now - 100, now + 50, "{}", "queued"),
            ("b", "tiktok", "r", "2", "k", now - 10, now + 50, "{}", "expired"),
            ("c", "shopee", "r", "3", "k", now - 20, now + 50, "{}", "unknown"),
            ("d", "tiktok", "r", "4", "k", now - 90000, now - 1, "{}", "queued")]
    for r in rows:
        q.db.execute(ins, r)
    q.db.commit()
    out = eng._ai_queue_summary()
    assert out["queue_mode"] is True
    assert out["pending"] == 1 and out["expired"] == 1 and out["unknown"] == 1
    assert 99 <= out["by_platform"]["tiktok"]["oldest_age_s"] <= 101
    assert out["by_platform"]["shopee"]["unknown"] == 1


def test_dashboard_ai_queue_counts_delivery_unknown(tmp_path):
    now = time.time()
    eng = _fake(tmp_path, now)
    d = eng.delivery_store._db
    d.execute("INSERT INTO reply_turns(turn_id,platform,room_id,interaction_id,created_at,expires_at) VALUES('t','tiktok','r','i',?,?)", (now, now + 60))
    d.execute("INSERT INTO reply_messages(message_id,turn_id,ordinal,content,content_hash,state) VALUES('m','t',0,'x','h','unknown')")
    d.commit()
    assert eng._ai_queue_summary()["by_platform"]["tiktok"]["unknown"] == 1
