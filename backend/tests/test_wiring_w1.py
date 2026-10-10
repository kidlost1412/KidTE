"""W1: features that existed and were tested in isolation are reached from the runtime."""
import importlib.util
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import urllib.request

import pytest

import app_entry
import backup_policy
import dose_rules
import reply_schema
import server as server_module
from customer_memory import CustomerMemory
from knowledge import KnowledgeBase
from privacy import PrivacyService
from server import Handler, LiveCatalogStore, MissingReporter, ReplyEngine


class Responder:
    def __init__(self, extra=None):
        self.calls, self.extra = [], extra or {}

    def reply(self, nickname, comment, context, max_chars, **kwargs):
        self.calls.append(context)
        if context.get("ask") == "select_dose_rule":  # D05: model picks an approved rule_id
            return {"action": "reply", "reply": "", "rule_id": context["dose_rule_options"][0]["rule_id"],
                    "params": {"volume": 16, "unit": "l"}}
        return {"action": "reply", "reply": "Dạ câu trả lời đủ dữ kiện ạ.", **self.extra}


def _live():
    return {"room_id": "room-1", "pin_id": "p1", "products": [{
        "product_id": "p1", "title": "Kích Rễ Giâm Cành TC01", "original_price": "120.000₫",
        "sale_price": "90.000₫", "skus": [{"sku_id": "s1", "title": "Chai 100ml", "original_price": "120.000₫"}]}]}


def _payload(text, cid="c1", uid="u1", nick="Nguyễn Thị Hoa"):
    return {"comment": text, "comment_id": cid, "user_id": uid, "unique_id": f"user{uid}",
            "nickname": nick, "live_context": _live()}


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "LIVE_CATALOG_PATH", tmp_path / "live.json")
    catalog = LiveCatalogStore()
    catalog.sync = lambda snap: {"room_id": snap.get("room_id", ""), "pin_id": snap.get("pin_id", ""),
                                 "products": snap.get("products", []), "product_count": 1, "synced_at": "now"}
    reporter = MissingReporter()
    reporter.path = tmp_path / "missing.jsonl"
    memory = CustomerMemory(tmp_path / "memory.sqlite3", tmp_path / "journals", session_id="s1")
    data = {"nhom_san_pham": [{"san_pham": [{
        "san_pham_id": "SP-03", "ten_san_pham": "Kích Rễ Giâm Cành TC01", "approved_for_live": True,
        "shop_product_ids": ["p1"], "listing_aliases": "TC01",
        "huong_dan_su_dung": "Cây mới trồng: pha 10ml cho bình 16 lít", "cong_dung": "Hỗ trợ kích rễ"}]}]}
    (tmp_path / "k.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "p.json").write_text('{"products":{}}', encoding="utf-8")
    eng = ReplyEngine(responder=Responder(), memory=memory,
                      knowledge=KnowledgeBase(tmp_path / "k.json", tmp_path / "p.json"),
                      catalog_store=catalog, missing_reporter=reporter)
    eng._media_bindings_store = None
    from obs_source import MediaBindingStore
    eng._media_bindings_store = MediaBindingStore(tmp_path / "bindings.sqlite3")
    yield eng
    eng.close()


RULE = {"sku": "SP-03", "amount": 33, "unit": "ml", "per_volume": 16, "source_doc": "nhan.pdf"}


def _import(engine, rule, approve):
    with engine.workspace._db() as db:
        ids = dose_rules.import_rules(db, [rule])["rule_ids"]
        if approve:
            dose_rules.review_rule(db, ids[0], "approve")
    return ids[0]


# ---------------------------------------------------------------- item 1 (D01/D05)
def test_shopee_engine_reads_dose_rules_from_workspace_db(engine):
    _import(engine, {**RULE, "sku": "shopee:20"}, approve=True)
    source = engine.shopee_ai().dose_rule_source
    assert source is not None
    assert [r["status"] for r in source("shopee:20")] == ["approved"]
    assert source("shopee:other") == []


def test_tiktok_dose_context_uses_approved_rules_never_candidates(engine):
    engine.handle(_payload("TC01 pha bình 16 lít bao nhiêu", cid="a"))
    base = json.dumps(engine.responder.calls[-1].get("verified_dose_calculations"), ensure_ascii=False)
    assert "10" in base and "33" not in base
    _import(engine, {**RULE, "amount": 77}, approve=False)
    engine.handle(_payload("TC01 pha bình 16 lít bao nhiêu", cid="b"))
    assert "77" not in json.dumps(engine.responder.calls[-1].get("verified_dose_calculations"))
    _import(engine, RULE, approve=True)
    engine.handle(_payload("TC01 pha bình 16 lít bao nhiêu", cid="c"))
    got = json.dumps(engine.responder.calls[-1].get("verified_dose_calculations"), ensure_ascii=False)
    assert "33" in got and "77" not in got


def test_workspace_dose_test_applies_approved_rules(engine):
    profile = {"san_pham_id": "SP-03", "huong_dan_su_dung": ["Pha 10ml cho bình 16 lít nước"]}
    engine.workspace.detail = lambda resource, record_id: {"data": profile}
    _import(engine, {**RULE, "amount": 77}, approve=False)
    plain = engine.workspace_dose_test("SP-03", {"liters": 16})
    assert "77" not in json.dumps(plain, ensure_ascii=False)
    _import(engine, RULE, approve=True)
    result = engine.workspace_dose_test("SP-03", {"liters": 16})
    assert "33" in json.dumps(result, ensure_ascii=False) and "77" not in json.dumps(result)


# ---------------------------------------------------------------- item 2 (D04)
class _Eng:
    def __init__(self, ws):
        self.workspace = ws


def _post(port, path, body, origin=""):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Origin": origin})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_import_label_route_is_local_only_and_all_candidates(engine):
    Handler.engine = engine
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, args=(0.1,), daemon=True).start()
    port = srv.server_address[1]
    try:
        body = {"sku": "SP-09", "text": "Pha 10ml cho 16 lít; hoặc 20ml cho 16 lít", "source_doc": "nhan.jpg"}
        assert _post(port, "/api/workspace/dose-rules/import-label", body, origin="chrome-extension://x")[0] == 403
        status, result = _post(port, "/api/workspace/dose-rules/import-label", body)
        assert status == 200 and result["needs_review"] is True and result["imported"] >= 2
        with engine.workspace._db() as db:
            rules = dose_rules.list_rules(db, "SP-09")["rules"]
        assert rules and {r["status"] for r in rules} == {"candidate"}
        assert _post(port, "/api/workspace/dose-rules/import-label", {"sku": "", "text": "x"})[0] == 400
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- item 3 (A01/A06)
def test_tiktok_context_has_product_ref_and_old_keys(engine):
    result = engine.handle(_payload("TC01 giá bao nhiêu"))
    assert result["should_send"]
    ctx = engine.responder.calls[-1]
    # Package A: điểm ứng viên nội bộ không còn gửi cho model (cờ intent_fields; cờ tắt thì
    # vẫn gửi - xem test_context_budget_flags.py). Các khoá cũ khác giữ nguyên.
    assert "product_ref" not in ctx
    assert "current_product" in ctx and "price" in ctx


def test_product_ref_failure_never_blocks_reply(engine, monkeypatch):
    monkeypatch.setattr(server_module, "product_reference", mock.Mock(side_effect=RuntimeError("boom")))
    assert engine.handle(_payload("TC01 giá bao nhiêu", cid="x"))["should_send"] is True
    assert "product_ref" not in engine.responder.calls[-1]


def test_reply_schema_is_used_and_malformed_output_still_replies(engine, monkeypatch):
    spy = mock.Mock(wraps=reply_schema.normalize)
    monkeypatch.setattr(server_module.reply_schema, "normalize", spy)
    engine.responder.extra = {"awaiting_fields": ["quantity", 5], "evidence": "garbage"}
    assert engine.handle(_payload("TC01 giá bao nhiêu", cid="s1"))["should_send"] is True
    spy.assert_called()
    monkeypatch.setattr(server_module.reply_schema, "normalize", mock.Mock(side_effect=RuntimeError))
    assert engine.handle(_payload("TC01 giá bao nhiêu", cid="s2"))["should_send"] is True


# ---------------------------------------------------------------- item 4 (F02)
def test_privacy_is_wired_into_learning_workspace_and_forget_path(engine):
    assert engine.learning.privacy is engine.privacy
    assert engine.workspace.privacy is engine.privacy
    assert callable(engine.memory.derived_stores)
    assert set(engine.derived_stores()) == {"learning", "workspace", "history"}
    tag = engine.learning.hmac_for("tiktok:id:u1")
    assert tag and tag == engine.privacy.key_hmac("tiktok:id:u1")


def test_forget_comment_excludes_only_legacy_rows_mentioning_customer(engine):
    mine = engine.learning.add_item(kind="fact", statement="Khách Nguyễn Thị Hoa hay mua TC01",
                                    source="correction", status="active")
    ids = engine.learning.add_item(kind="fact", statement="user note about userU1 dùng nhiều",
                                   source="correction", status="active")
    other = engine.learning.add_item(kind="fact", statement="Khách Trần Văn Nam hỏi giá",
                                     source="correction", status="active")
    engine.handle(_payload("xóa dữ liệu của tôi", cid="f1", nick="Nguyen Thi Hoa"))
    active = {i["item_id"] for i in engine.learning.items("active")}
    assert mine not in active and ids not in active and other in active
    steps = {}
    with engine.privacy._db:
        row = engine.privacy._db.execute("SELECT steps_json FROM privacy_requests").fetchone()
    steps = {s["step"]: s for s in json.loads(row[0])}
    assert steps["learning"]["status"] == "excluded" and steps["learning"]["excluded"] == 2


def test_privacy_forget_route_passes_derived_stores(engine):
    engine.handle(_payload("TC01 giá bao nhiêu", cid="p1", uid="77", nick="Lê Văn Tèo"))
    key = engine.memory.customer_key(platform_user_id="77")[0]
    legacy = engine.learning.add_item(kind="fact", statement="lê văn teo mua nhiều", source="correction",
                                      status="active")
    res = engine.privacy_forget({"customer_key": key})
    steps = {s["step"]: s for s in engine.privacy.request(res["request_id"])["steps"]}
    assert steps["learning"]["status"] == "excluded"
    assert legacy not in {i["item_id"] for i in engine.learning.items("active")}


def test_legacy_targeting_name_shorter_than_three_chars_not_matched(tmp_path):
    from learning import LearningStore
    privacy = PrivacyService(tmp_path / "db.sqlite3", tmp_path / "k")
    store = LearningStore(tmp_path / "l.sqlite3")
    try:
        item = store.add_item(kind="fact", statement="ab cd ef", source="correction", status="active")
        ids = privacy.legacy_identifiers("tiktok:name:", "Bo")
        assert ids == []
        assert store.forget_customer(privacy.key_hmac("x"), identifiers=ids)["excluded"] == 0
        assert item in {i["item_id"] for i in store.items("active")}
    finally:
        privacy.close()


# ---------------------------------------------------------------- item 5 (F04a)
class _Service:
    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    def create_auto(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("dpapi")
        return {"backup_id": "b1", "scope": "partial"}


def test_startup_auto_backup_v2_follows_v1_throttle_and_never_raises():
    svc = _Service()
    assert backup_policy.run_startup_auto_backup({"created": False, "reason": "recent_backup"}, lambda: svc)["created"] is False
    assert svc.calls == 0
    assert backup_policy.run_startup_auto_backup({"created": True}, lambda: svc)["backup_id"] == "b1"
    assert svc.calls == 1
    bad = backup_policy.run_startup_auto_backup({"created": True}, lambda: _Service(fail=True))
    assert bad["created"] is False and bad["reason"] == "v2_failed"
    assert backup_policy.run_startup_auto_backup({"created": True}, lambda: 1 / 0)["created"] is False


def test_app_entry_cancels_deferred_backup_on_immediate_exit(tmp_path):
    called = threading.Event()

    class Eng:
        closed = False
        def close(self): self.closed = True
        def _backup_v2_service(self):
            called.set()
            return _Service()

    class Srv:
        def __init__(self, *_): pass
        def serve_forever(self, *_): return None
        def server_close(self): pass
        def shutdown(self): pass

    with (mock.patch("sys.argv", ["app_entry.py", "--port", "0", "--no-ui"]),
          mock.patch.object(app_entry, "ROOT", tmp_path),
          mock.patch.object(app_entry, "LOG_DIR", tmp_path / "logs"),
          mock.patch.object(backup_policy, "ensure_startup_backup", side_effect=AssertionError("startup backup forbidden")),
          mock.patch.object(backup_policy, "run_startup_auto_backup", side_effect=AssertionError("startup backup forbidden")),
          mock.patch.object(app_entry, "ReplyEngine", return_value=Eng()),
          mock.patch.object(app_entry, "discover_plugins", return_value=({}, {})),
          mock.patch.object(app_entry, "LoopbackHTTPServer", Srv),
          mock.patch.object(app_entry.signal, "signal")):
        assert app_entry.main() == 0
    assert not called.wait(.1)


# ---------------------------------------------------------------- item 6 (C04)
def test_library_changes_bump_revision_and_change_media_key(engine, tmp_path):
    from media_assets import media_key
    media = tmp_path / "v.mp4"
    media.write_bytes(b"x" * 10)
    engine.video_analysis = SimpleNamespace(
        finish_upload=lambda job, title: {"ok": True},
        publish=lambda *a, **k: {"ok": True})
    engine.workspace.video_job_detail = lambda job: {"draft": {"events": []}, "revision": "1"}
    assert engine._library_revision() == 0
    key0 = media_key(media, engine._library_revision())
    engine.video_upload_finish({"job_id": "j"})
    assert engine._library_revision() == 1
    engine.video_analysis_publish({"job_id": "j"})
    assert engine._library_revision() == 2
    engine.workspace.delete = lambda *a, **k: {"ok": True}
    engine.workspace_delete("videos", "v1")
    engine.workspace_delete("products", "p1")  # not a video: no bump
    assert engine._library_revision() == 3
    assert media_key(media, engine._library_revision()) != key0
    engine.workspace.restore = lambda *a, **k: {"ok": True}
    engine.workspace_restore("videos", "v1")
    assert engine._library_revision() == 4


def test_asset_state_passes_library_revision(engine, tmp_path):
    media = tmp_path / "v.mp4"
    media.write_bytes(b"x")
    seen = []
    engine.video_assets = SimpleNamespace(
        describe=lambda path, library_revision=0: seen.append(library_revision) or {"ok": True, "key": ""},
        retry=lambda path, library_revision=0: {"ok": True})
    engine._asset_media_path = lambda video_id="", job_id="": media
    engine.video_library_changed()
    engine.video_assets_state("v1")
    assert seen == [1]


def test_live_tracker_reads_library_revision_from_store(tmp_path, monkeypatch):
    from obs_source import MediaBindingStore
    folder = Path(__file__).resolve().parents[2] / "plugins" / "dong-bo-video"
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location("w1_live_sync", folder / "live_sync.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import config
    monkeypatch.setattr(config, "MEDIA_BINDINGS_DB", tmp_path / "b.sqlite3")
    monkeypatch.setattr(module, "_BINDINGS", None)
    assert module.current_library_revision() == 0
    MediaBindingStore(tmp_path / "b.sqlite3").bump_library_revision()
    assert module.current_library_revision() == 1
    media = tmp_path / "m.mp4"
    media.write_bytes(b"1")
    client = SimpleNamespace(
        media_inputs=lambda: [{"media_path": str(media), "local_file": True, "input_name": "m"}],
        request=lambda *a, **k: {}, close=lambda: None)
    monkeypatch.setattr(module, "load_references", lambda: [])
    monkeypatch.setattr(module, "verify_obs_file", lambda p, r: "vid")
    obs = module.VerifiedObs(client)
    obs.library_revision = module.current_library_revision()
    obs.media_inputs()
    assert next(iter(obs.cache))[3] == 1


# ---------------------------------------------------------------- item 7
def test_comment_queue_and_delivery_store_receive_privacy(engine):
    assert engine.comment_queue.privacy is engine.privacy
    assert engine.delivery_store.privacy is engine.privacy
    assert engine.privacy is engine.memory.privacy
