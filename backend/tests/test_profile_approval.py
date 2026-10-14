"""Duyệt hồ sơ: hồ sơ rỗng không bao giờ được AI dùng; nháp hàng loạt; auto-match."""
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import server  # noqa: E402
from customer_memory import CustomerMemory  # noqa: E402
from knowledge import KnowledgeBase, is_approvable, profile_completeness  # noqa: E402
from server import LiveCatalogStore, MissingReporter, ReplyEngine  # noqa: E402

HERE = Path(__file__).resolve().parent
SEED = HERE.parent / "prompts" / "knowledge_seed.json"


def _titles():
    return json.loads((HERE / "fixtures" / "live_titles.json").read_text(encoding="utf-8"))["titles"]


def _seed_content():
    return json.loads(SEED.read_text(encoding="utf-8"))["content"]


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _kb(tmp_path, shared_records=None, assignments=None, overrides=None):
    data = tmp_path / "data"
    content = ({"nhom_san_pham": [{"san_pham": shared_records}]} if shared_records is not None
               else {"nhom_san_pham": []})
    _write(data / "shared" / "product_knowledge.json", content)
    _write(data / "machine" / "catalog_mapping.json", {"assignments": assignments or {}})
    if overrides is not None:
        _write(data / "local_product_overrides.json", {"schema_version": 1, "overrides": overrides})
    _write(tmp_path / "p.json", {"products": {}})
    return KnowledgeBase(data / "shared" / "product_knowledge.json", tmp_path / "p.json",
                         data / "machine" / "catalog_mapping.json")


def _assignment(code):
    return {"product_code": code, "approved_for_live": True}


# ---------------------------------------------------------------- tiêu chuẩn duyệt
def test_approvable_needs_name_and_uses_or_directions():
    assert not is_approvable({})
    assert not is_approvable({"ten_san_pham": "A"})
    assert not is_approvable({"ten_san_pham": "A", "cong_dung": [], "huong_dan_su_dung": ["  "]})
    assert not is_approvable({"ten_san_pham": "A", "luu_y": "chỉ lưu ý", "thanh_phan_dang_ky": ["N"]})
    assert not is_approvable({"cong_dung": ["x"]})
    assert is_approvable({"ten_san_pham": "A", "cong_dung": ["Kích rễ"]})
    assert is_approvable({"ten_san_pham": "A", "huong_dan_su_dung": "Pha 10ml/16 lít"})
    assert profile_completeness({"cong_dung": ["x"], "luu_y": ""}) == {
        "thanh_phan": False, "cong_dung": True, "huong_dan": False, "luu_y": False}


def test_created_local_empty_profile_is_not_force_approved(tmp_path):
    kb = _kb(tmp_path, overrides={"LOCAL-1": {"ten_san_pham": "Rỗng", "created_local": True,
                                              "approved_for_live": False}})
    record = next(r for r in kb.all_records() if r["san_pham_id"] == "LOCAL-1")
    assert record["approved_for_live"] is False and record["profile_status"] == "draft"


def test_empty_draft_never_reaches_the_ai_even_if_mapped_by_hand(tmp_path):
    kb = _kb(tmp_path, assignments={"sku1": _assignment("LOCAL-1")},
             overrides={"LOCAL-1": {"ten_san_pham": "Rỗng", "created_local": True}})
    kb.reload()
    assert kb.match("Rỗng", product_id="sku1") is None
    assert kb.records == []


def test_mapping_an_empty_profile_is_refused_and_writes_nothing(tmp_path):
    kb = _kb(tmp_path, overrides={"LOCAL-1": {"ten_san_pham": "Rỗng", "created_local": True,
                                              "approved_for_live": False}})
    with pytest.raises(ValueError, match="profile_not_approvable"):
        kb.apply_mapping({"sku1": "LOCAL-1"})
    assert json.loads(kb.mapping_path.read_text(encoding="utf-8"))["assignments"] == {}


def test_filled_draft_is_approved_by_saving_the_mapping(tmp_path):
    kb = _kb(tmp_path, overrides={"LOCAL-1": {
        "ten_san_pham": "Đủ", "created_local": True, "approved_for_live": False,
        "cong_dung": ["Kích rễ"]}})
    assert kb.match("x", product_id="sku1") is None
    assert kb.apply_mapping({"sku1": "LOCAL-1"}) == 1
    kb.reload()
    assert kb.match("", product_id="sku1")["san_pham_id"] == "LOCAL-1"
    saved = json.loads(kb.overrides_path.read_text(encoding="utf-8"))["overrides"]["LOCAL-1"]
    assert saved["approved_for_live"] is True


def test_shared_profile_without_content_cannot_be_mapped(tmp_path):
    kb = _kb(tmp_path, shared_records=[{"san_pham_id": "WEB-1", "ten_san_pham": "Trống"}])
    with pytest.raises(ValueError, match="profile_not_approvable"):
        kb.apply_mapping({"sku1": "WEB-1"})


# ---------------------------------------------------------------- dữ liệu cũ trên máy đang chạy
def test_existing_created_local_records_keep_or_lose_approval_by_content(tmp_path):
    overrides = {
        "LOCAL-FULL": {"ten_san_pham": "Đủ", "created_local": True, "cong_dung": ["Kích rễ"]},
        "LOCAL-EMPTY": {"ten_san_pham": "Rỗng", "created_local": True},
        "LOCAL-FULL-UNMAPPED": {"ten_san_pham": "Đủ chưa gắn", "created_local": True,
                                "huong_dan_su_dung": ["Pha 10ml"]},
        "LOCAL-NEW": {"ten_san_pham": "Mới", "created_local": True, "approved_for_live": False},
    }
    kb = _kb(tmp_path, assignments={"s1": _assignment("LOCAL-FULL"), "s2": _assignment("LOCAL-EMPTY")},
             overrides=overrides)
    assert kb.match("", product_id="s1")["san_pham_id"] == "LOCAL-FULL"   # vẫn chạy
    assert kb.match("", product_id="s2") is None                           # rỗng -> nháp
    status = {r["san_pham_id"]: r["profile_status"] for r in kb.all_records() if r.get("created_local")}
    assert status == {"LOCAL-FULL": "approved", "LOCAL-EMPTY": "draft",
                      "LOCAL-FULL-UNMAPPED": "draft", "LOCAL-NEW": "draft"}
    report = kb.legacy_approval_report()
    assert report == {"created_local": 4, "giu_duyet": 1, "thanh_nhap": 2,
                      "thanh_nhap_dang_gan_sku_rong": 1, "da_co_co_rieng": 1}
    before = kb.overrides_path.read_bytes()
    kb.legacy_approval_report()
    assert kb.overrides_path.read_bytes() == before                      # báo cáo chỉ đọc


# ---------------------------------------------------------------- engine + HTTP
def _engine(tmp_path, records=None, overrides=None):
    records = _seed_content()["nhom_san_pham"][0]["san_pham"] if records is None else records
    kb = _kb(tmp_path, shared_records=records, overrides=overrides)
    catalog = LiveCatalogStore()
    catalog.snapshot = {"room_id": "r1", "synced_at": "now", "products": [
        {"product_id": t["product_id"], "title": t["title"], "bag_index": i + 1}
        for i, t in enumerate(_titles())]}
    reporter = MissingReporter()
    reporter.path = tmp_path / "missing.jsonl"
    memory = CustomerMemory(tmp_path / "memory.sqlite3", tmp_path / "journals", session_id="s1")
    responder = server._model_processor.DeepSeekResponder()
    responder.client = None
    return ReplyEngine(responder=responder, memory=memory, knowledge=kb,
                       catalog_store=catalog, missing_reporter=reporter)


def test_create_profile_with_product_id_does_not_map_or_approve(tmp_path):
    engine = _engine(tmp_path)
    pid = _titles()[0]["product_id"]
    out = engine.workspace.create_profile({"ten_san_pham": "Hồ sơ mới", "product_id": pid})
    assert out["status"] == "draft" and out["mapped"] is False
    assert engine.knowledge.mapping_assignments().get(pid) is None
    assert pid not in engine.knowledge.by_shop_id
    row = next(p for p in engine.mapping_state()["profiles"] if p["id"] == out["id"])
    assert row["status"] == "draft" and row["approvable"] is False and row["draft_for"] == pid


def test_auto_match_endpoint_logic_is_read_only_and_tiered(tmp_path):
    engine = _engine(tmp_path)
    before = engine.knowledge.mapping_path.read_bytes()
    out = engine.auto_match_mapping({})
    assert out["ok"] and out["total"] == 41
    assert sum(out["counts"].values()) == 41
    assert out["counts"]["chac_chan"] >= 5
    for item in out["results"]:
        assert item["tier"] in {"chac_chan", "can_xem", "khong_co"}
        assert {"profile_id", "score", "size_match", "reasons", "tier"} <= set(item)
        if item["tier"] == "chac_chan":
            assert item["size_match"] is not False and not item["is_combo"]
    assert engine.knowledge.mapping_path.read_bytes() == before        # chỉ gợi ý, không ghi


def test_auto_match_skips_mapped_listings_and_unapprovable_profiles(tmp_path):
    records = [dict(r) for r in _seed_content()["nhom_san_pham"][0]["san_pham"]]
    for r in records:
        if r["san_pham_id"] == "WEB-1006":
            r["cong_dung"], r["huong_dan_su_dung"] = [], []             # hồ sơ rỗng: không được gợi ý
    engine = _engine(tmp_path, records=records)
    dau_phong = next(t for t in _titles() if "CHUYÊN ĐẬU PHỘNG" in t["title"])
    first = engine.auto_match_mapping({})
    row = next(r for r in first["results"] if r["product_id"] == dau_phong["product_id"])
    assert row["profile_id"] != "WEB-1006"
    pid = _titles()[1]["product_id"]
    engine.save_mapping({"assignments": {pid: "WEB-1016"}})
    again = engine.auto_match_mapping({})
    assert pid not in {r["product_id"] for r in again["results"]}
    assert again["total"] == 40


def test_bulk_draft_creates_local_drafts_not_mapped_not_approved(tmp_path):
    engine = _engine(tmp_path)
    ids = [t["product_id"] for t in _titles()[:3]]
    out = engine.bulk_draft_profiles({"product_ids": ids + ["khong-co-trong-gio"]})
    assert len(out["created"]) == 3
    assert out["skipped"] == [{"product_id": "khong-co-trong-gio", "reason": "not_in_catalog"}]
    state = engine.mapping_state()
    drafts = {p["id"]: p for p in state["profiles"] if p["created_local"]}
    assert len(drafts) == 3
    titles = {t["product_id"]: t["title"] for t in _titles()}
    for created in out["created"]:
        record = next(r for r in engine.knowledge.all_records() if r["san_pham_id"] == created["profile_id"])
        assert record["listing_aliases"] == [" ".join(titles[created["product_id"]].split())]
        assert record["profile_status"] == "draft" and record["approved_for_live"] is False
        assert 2 <= len(record["ten_san_pham"]) <= 80
        assert drafts[created["profile_id"]]["status"] == "draft"
        assert drafts[created["profile_id"]]["approvable"] is False
        assert created["product_id"] not in engine.knowledge.by_shop_id
    assert not engine.knowledge.mapping_assignments()
    # Chạy lại: không tạo trùng.
    again = engine.bulk_draft_profiles({"product_ids": ids})
    assert again["created"] == [] and {s["reason"] for s in again["skipped"]} == {"draft_exists"}


def test_bulk_draft_parses_package_size(tmp_path):
    engine = _engine(tmp_path)
    dam_ca = next(t for t in _titles() if "Can 5 Lít" in t["title"] and "Đạm Cá Hữu Cơ" in t["title"])
    out = engine.bulk_draft_profiles({"product_ids": [dam_ca["product_id"]]})
    record = next(r for r in engine.knowledge.all_records()
                  if r["san_pham_id"] == out["created"][0]["profile_id"])
    assert record["quy_cach_dong_goi"] == "5 lít"


def test_bulk_draft_validates_payload(tmp_path):
    engine = _engine(tmp_path)
    with pytest.raises(ValueError, match="product_ids_required"):
        engine.bulk_draft_profiles({"product_ids": []})
    with pytest.raises(ValueError, match="too_many_products"):
        engine.bulk_draft_profiles({"product_ids": [str(i) for i in range(500)]})


def test_approve_blocked_until_approvable_then_maps_the_listing(tmp_path):
    engine = _engine(tmp_path)
    pid = _titles()[0]["product_id"]
    created = engine.bulk_draft_profiles({"product_ids": [pid]})["created"][0]
    profile_id = created["profile_id"]
    blocked = engine.approve_profiles({"items": [{"profile_id": profile_id, "product_id": pid}]})
    assert blocked["approved"] == 0
    assert blocked["results"][0] == {"profile_id": profile_id, "ok": False,
                                     "error": "profile_not_approvable"}
    assert pid not in engine.knowledge.by_shop_id
    engine.workspace.patch("profiles", profile_id, {"changes": {"cong_dung": ["Kích rễ mạnh"]}})
    ok = engine.approve_profiles({"items": [{"profile_id": profile_id, "product_id": pid}]})
    assert ok["approved"] == 1
    assert engine.knowledge.by_shop_id[pid]["san_pham_id"] == profile_id
    assert engine.knowledge.mapping_assignments()[pid] == profile_id
    assert engine.knowledge.mapping_history_count() == 1               # có lịch sử để hoàn tác
    engine.undo_mapping()
    assert pid not in engine.knowledge.by_shop_id


def test_approve_refuses_shared_profiles_and_unknown_ids(tmp_path):
    engine = _engine(tmp_path)
    out = engine.approve_profiles({"items": [{"profile_id": "WEB-1006"}, {"profile_id": "nope"}]})
    assert [r["error"] for r in out["results"]] == ["not_local_profile", "not_local_profile"]
    assert engine.approve_profiles({"items": []})["ok"] is False


def test_http_routes_auto_match_bulk_draft_and_approve(tmp_path):
    engine = _engine(tmp_path)

    class Handler(server.Handler):
        pass
    Handler.engine = engine
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
    worker.start()
    origin = f"http://127.0.0.1:{http.server_port}"

    def post(path, payload, from_origin=origin):
        req = Request(origin + path, data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", "Origin": from_origin})
        with urlopen(req, timeout=5) as response:
            return json.loads(response.read())
    try:
        with pytest.raises(HTTPError) as error:
            post("/api/mapping/auto-match", {}, "https://other.example")
        assert error.value.code == 403
        matched = post("/api/mapping/auto-match", {})
        assert matched["ok"] and matched["total"] == 41
        pid = _titles()[2]["product_id"]
        drafted = post("/api/profiles/bulk-draft", {"product_ids": [pid]})
        assert len(drafted["created"]) == 1
        blocked = post("/api/profiles/approve",
                       {"items": [{"profile_id": drafted["created"][0]["profile_id"], "product_id": pid}]})
        assert blocked["approved"] == 0
    finally:
        http.shutdown()
        http.server_close()
        worker.join()


# ---------------------------------------------------------------- cài hồ sơ chính thức lần đầu ở engine
def _blank_engine(tmp_path, monkeypatch):
    data = tmp_path / "data"
    shared = data / "shared" / "product_knowledge.json"
    kb = KnowledgeBase(shared, tmp_path / "p.json", data / "machine" / "catalog_mapping.json")
    monkeypatch.setattr(server, "APPROVED_CATALOG_PATH", shared)
    monkeypatch.setattr(server, "KNOWLEDGE_SEED_PATH", SEED)
    reporter = MissingReporter()
    reporter.path = tmp_path / "missing.jsonl"
    memory = CustomerMemory(tmp_path / "memory.sqlite3", tmp_path / "journals", session_id="s1")
    responder = server._model_processor.DeepSeekResponder()
    responder.client = None
    engine = ReplyEngine(responder=responder, memory=memory, knowledge=kb,
                         catalog_store=LiveCatalogStore(), missing_reporter=reporter)
    return engine, shared


def test_blank_machine_waits_for_choice_then_loads_defaults(tmp_path, monkeypatch):
    engine, shared = _blank_engine(tmp_path, monkeypatch)
    assert engine.knowledge_seed_status()["state"] == "missing"
    assert engine.knowledge.mappable_records() == []
    assert engine.ensure_seed_knowledge()["status"] == "choice_required"
    assert not shared.exists()
    selected = engine.product_defaults.choose({"mode": "default"})
    assert selected["products"] == 38
    assert selected["images"] == 41
    assert shared.is_file() and len(engine.knowledge.mappable_records()) == 38
    assert len(engine.knowledge.all_records()) == 41  # three source references are not sellable profiles
    assert engine.knowledge.records == []                  # chưa gắn SKU nào: AI chưa dùng hồ sơ nào
    assert engine.knowledge_seed_status()["state"] == "current"
    before = shared.read_bytes()
    assert engine.ensure_seed_knowledge()["status"] == "existing_untouched"
    assert shared.read_bytes() == before


def test_engine_defers_seeding_during_live(tmp_path, monkeypatch):
    import time
    engine, shared = _blank_engine(tmp_path, monkeypatch)
    engine.extension_heartbeat = {"live_running": True, "received_at": time.time()}
    assert engine.ensure_seed_knowledge()["status"] == "choice_required"
    with pytest.raises(ValueError, match="LIVE"):
        engine.product_defaults.choose({"mode": "default"})
    assert not shared.exists()
    assert engine.knowledge_seed_status()["state"] == "missing"


def test_update_endpoint_can_use_the_shipped_seed_without_trusting_client_path(tmp_path, monkeypatch):
    engine, shared = _blank_engine(tmp_path, monkeypatch)
    preview = engine.update_knowledge_package({"seed": True, "source": "C:/evil.json", "che_do": "xem_truoc"})
    assert preview["khac_biet"]["tom_tat"]["them_moi"] >= 40
    assert not shared.exists()                                       # xem trước không ghi
    done = engine.update_knowledge_package({"seed": True, "source": "C:/evil.json"})
    assert done["ok"] and shared.is_file()
