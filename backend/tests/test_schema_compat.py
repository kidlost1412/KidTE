"""T06: schema audit, migration idempotence, old-schema upgrade, old extension payloads."""
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tools"))

import schema_audit  # noqa: E402
from customer_memory import CustomerMemory  # noqa: E402
from delivery_store import DeliveryStore  # noqa: E402
from shopee_ai import ShopeeAiEngine  # noqa: E402


def columns(path, table):
    with closing(sqlite3.connect(path)) as db:
        return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def test_schema_matches_baseline():
    baseline = json.loads(schema_audit.DEFAULT_BASELINE.read_text(encoding="utf-8"))
    assert schema_audit.diff(baseline, schema_audit.collect()) == []


def test_migration_idempotent(tmp_path):
    once = schema_audit.init_all(tmp_path / "a", times=1)
    twice = schema_audit.init_all(tmp_path / "b", times=2)
    assert once == twice
    assert set(once) >= {"customer_memory", "delivery_store", "comment_queue", "workspace", "learning",
                         "privacy", "captcha_upload", "media_binding"}


def _old_customer_db(path):
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE sessions (session_id TEXT PRIMARY KEY, channel TEXT NOT NULL DEFAULT '',
                room_id TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, ended_at TEXT);
            CREATE TABLE customers (customer_key TEXT PRIMARY KEY, platform_user_id TEXT NOT NULL DEFAULT '',
                sec_uid TEXT NOT NULL DEFAULT '', unique_id TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '', aliases_json TEXT NOT NULL DEFAULT '[]',
                identity_strength INTEGER NOT NULL DEFAULT 0, first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL, last_session_id TEXT NOT NULL DEFAULT '');
            CREATE TABLE interactions (interaction_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                happened_at TEXT NOT NULL, customer_key TEXT NOT NULL, event_type TEXT NOT NULL,
                comment TEXT NOT NULL DEFAULT '', response TEXT NOT NULL DEFAULT '',
                response_complete INTEGER, product_id TEXT NOT NULL DEFAULT '',
                product_title TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE customer_facts (customer_key TEXT NOT NULL, fact_key TEXT NOT NULL,
                fact_value TEXT NOT NULL, source_comment TEXT NOT NULL, source_at TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0, updated_at TEXT NOT NULL,
                PRIMARY KEY (customer_key, fact_key));
            CREATE TABLE memory_opt_outs (customer_key TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '', opted_out_at TEXT NOT NULL);
            INSERT INTO customers VALUES ('tiktok:id:1','1','','','Mai','[]',3,'t','t','s0');
            INSERT INTO interactions (interaction_id,session_id,happened_at,customer_key,event_type,
                comment,response,response_complete) VALUES
                ('i1','s0','t','tiktok:id:1','comment','hi','legacy reply',1),
                ('i2','s0','t','tiktok:id:1','comment','hi2','partial',0),
                ('i3','s0','t','tiktok:id:1','comment','hi3','',NULL);
            INSERT INTO customer_facts VALUES ('tiktok:id:1','cay','sau rieng','c','t',1.0,'t');
        """)


def test_legacy_unverified_flagged(tmp_path):
    path = tmp_path / "old.sqlite3"
    _old_customer_db(path)
    mem = CustomerMemory(path, tmp_path / "j")
    try:
        rows = dict(mem._db.execute("SELECT interaction_id, delivery_status FROM interactions").fetchall())
    finally:
        mem.close()
    assert rows == {"i1": "legacy_unverified", "i2": "generated", "i3": "none"}


def test_old_customer_schema_gets_columns_and_keeps_rows(tmp_path):
    path = tmp_path / "old.sqlite3"
    _old_customer_db(path)
    assert "source_interaction_id" not in columns(path, "customer_facts")
    for _ in range(2):
        CustomerMemory(path, tmp_path / "j").close()
    assert {"source_interaction_id"} <= columns(path, "customer_facts")
    assert {"runtime_comment_id", "runtime_turn_id", "last_segment_id", "delivery_status"} \
        <= columns(path, "interactions")
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT fact_value FROM customer_facts WHERE fact_key='cay'").fetchone()[0] == "sau rieng"
        assert db.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 3
        assert db.execute("SELECT display_name FROM customers").fetchone()[0] == "Mai"


def test_old_reply_turns_gets_columns_and_keeps_rows(tmp_path):
    path = tmp_path / "d.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE reply_turns (turn_id TEXT PRIMARY KEY, platform TEXT NOT NULL,
                room_id TEXT NOT NULL, interaction_id TEXT NOT NULL, created_at REAL NOT NULL,
                expires_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'ready',
                source_dependent INTEGER NOT NULL DEFAULT 1);
            INSERT INTO reply_turns VALUES ('t1','tiktok','r1','i1',1.0,2.0,'ready',1);
        """)
    for _ in range(2):
        DeliveryStore(path).close()
    assert {"fact_revision", "recompose_count", "cancel_reason"} <= columns(path, "reply_turns")
    with closing(sqlite3.connect(path)) as db:
        row = db.execute("SELECT turn_id, state, fact_revision, recompose_count FROM reply_turns").fetchone()
    assert row == ("t1", "ready", "", 0)


def test_old_workspace_schema_gets_columns_and_keeps_rows(tmp_path):
    from workspace import WorkspaceStore
    db_path = tmp_path / "data" / "operator_workspace.sqlite3"
    db_path.parent.mkdir()
    with closing(sqlite3.connect(db_path)) as db:
        db.executescript("""
            CREATE TABLE operator_changes (change_id TEXT PRIMARY KEY, resource TEXT NOT NULL,
                record_id TEXT NOT NULL, changed_at TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                before_json TEXT NOT NULL, after_json TEXT NOT NULL, before_revision TEXT NOT NULL,
                after_revision TEXT NOT NULL, action TEXT NOT NULL DEFAULT 'update');
            CREATE TABLE trash_items (trash_id TEXT PRIMARY KEY, resource TEXT NOT NULL,
                record_id TEXT NOT NULL, deleted_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                label TEXT NOT NULL, data_json TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                restored_at TEXT, purged_at TEXT);
            INSERT INTO operator_changes VALUES ('c1','r','1','t','','{}','{}','a','b','update');
            INSERT INTO trash_items VALUES ('x','r','1','t','t','l','{}','s',NULL,NULL);
        """)
    engine = SimpleNamespace(knowledge=SimpleNamespace(overrides_path=tmp_path / "ov.json"))
    for _ in range(2):
        WorkspaceStore(engine, root=tmp_path)
    assert {"patch_json", "actor"} <= columns(db_path, "operator_changes")
    assert "archived_path" in columns(db_path, "trash_items")
    assert "persona_id" in columns(db_path, "approved_reply_examples")
    with closing(sqlite3.connect(db_path)) as db:
        assert db.execute("SELECT change_id, patch_json, actor FROM operator_changes").fetchone() == \
            ("c1", "[]", "local_operator")
        assert db.execute("SELECT COUNT(*) FROM trash_items").fetchone()[0] == 1


# ---- old extension payloads (no protocol_version, no received_at_ms, unknown fields) ----
class FakeResponder:
    def reply(self, nickname, comment, context, max_chars, **kwargs):
        return {"action": "reply", "reply": "Dạ dùng theo hướng dẫn trên nhãn nha", "metrics": {}}


class FakeKnowledge:
    def match(self, title, comment=""):
        return {"ten_san_pham": title} if title == "Kích Rễ TC01" else None

    def context(self, record):
        return {"ten_san_pham": record["ten_san_pham"]}


def test_old_extension_payload_accepted_shopee(tmp_path):
    catalog = {"session_id": "123", "updated_at": time.time(), "products": [{
        "shop_id": "10", "product_id": "20", "title": "Kích Rễ TC01",
        "price_data": {"currency": "VND", "price": "80000", "price_before_discount": "80000"}}]}
    owner = SimpleNamespace(responder=FakeResponder(), knowledge=FakeKnowledge(),
        shopee_mapping_state=lambda: {"catalog": catalog, "videos": {"v": {
            "mappings": {"tc01": {"shop_id": "10", "product_id": "20"}}}}})
    engine = ShopeeAiEngine(owner, tmp_path / "m.sqlite3", tmp_path / "j")
    try:
        old = {"session_id": "123", "video_id": "v", "comment_id": "c1", "user_id": "77",
               "nickname": "Mai", "comment": "TC01 dùng sao?", "shop_id": "10", "product_id": "20",
               "title": "Kích Rễ TC01"}
        result = engine.reply(dict(old))
        assert result["ok"] is True and result["should_send"] is True
        assert "protocol_version" not in result and result.get("reply")
        # unknown future fields are ignored
        result = engine.reply({**old, "comment_id": "c2", "future_field": {"x": 1}})
        assert result["ok"] is True and result["should_send"] is True
    finally:
        engine.close()


def test_old_extension_payload_accepted_tiktok_protocol1(tmp_path, monkeypatch):
    import server as server_module
    from knowledge import KnowledgeBase
    from server import LiveCatalogStore, MissingReporter, ReplyEngine
    monkeypatch.setattr(server_module, "LIVE_CATALOG_PATH", tmp_path / "live.json")
    kb = tmp_path / "kb.json"
    kb.write_text(json.dumps({"nhom_san_pham": [{"san_pham": [{
        "san_pham_id": "SP-03", "ten_san_pham": "Kích Rễ Giâm Cành TC01", "approved_for_live": True,
        "shop_product_ids": ["p1"], "huong_dan_su_dung": "pha 10ml cho bình 16 lít",
        "cong_dung": "Hỗ trợ kích rễ"}]}]}, ensure_ascii=False), encoding="utf-8")
    prof = tmp_path / "p.json"
    prof.write_text('{"products":{}}', encoding="utf-8")
    store = LiveCatalogStore()
    store.snapshot = {}

    def sync(snapshot):
        store.snapshot = {"room_id": snapshot.get("room_id", ""), "pin_id": snapshot.get("pin_id", ""),
                          "products": snapshot.get("products", []),
                          "product_count": len(snapshot.get("products", [])), "synced_at": "now"}
        return store.snapshot
    store.sync = sync
    reporter = MissingReporter()
    reporter.path = tmp_path / "missing.jsonl"
    responder = FakeResponder()
    responder.reply = lambda nickname, comment, context, max_chars: {
        "action": "reply", "reply": "Dạ, câu trả lời đủ dữ kiện ạ."}
    engine = ReplyEngine(responder=responder,
        memory=CustomerMemory(tmp_path / "mem.sqlite3", tmp_path / "jr", session_id="s1"),
        knowledge=KnowledgeBase(kb, prof), catalog_store=store, missing_reporter=reporter)
    try:
        payload = {"comment": "TC01 dùng sao", "comment_id": "c1", "user_id": "u1", "unique_id": "useru1",
                   "nickname": "Khách", "unknown_new_field": True, "live_context": {
                       "room_id": "room-1", "pin_id": "p1", "products": [{
                           "product_id": "p1", "title": "Kích Rễ Giâm Cành TC01",
                           "original_price": "120.000₫", "sale_price": "90.000₫"}]}}
        result = engine.handle(payload)
        assert result["ok"] is True and result["should_send"] is True
        assert result.get("protocol_version") != 2
    finally:
        engine.close()
