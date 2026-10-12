"""Xoá hồ sơ sản phẩm: vào Thùng rác, AI ngừng dùng, không "sống lại" sau
gói kiến thức / seed / đồng bộ danh mục, khôi phục được."""
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from knowledge import KnowledgeBase  # noqa: E402
from knowledge_package import apply_package, content_hash, seed_first_run  # noqa: E402
from workspace import WorkspaceStore  # noqa: E402


class FakeEngine:
    def __init__(self, knowledge):
        self.knowledge = knowledge
        self.extension_heartbeat = {}

    def mapping_state(self):
        return {"products": []}

    def current_video_position(self):
        return {"state": "waiting", "video_id": ""}


CONTENT = {"nhom_san_pham": [{"nhom": "live", "san_pham": [
    {"san_pham_id": "SP-01", "ten_san_pham": "Đạm cá 5L", "cong_dung": ["Nuôi cây"],
     "huong_dan_su_dung": ["Pha 20ml cho bình 16 lít"], "approved_for_live": True},
    {"san_pham_id": "SP-02", "ten_san_pham": "Humic 1L", "cong_dung": ["Rễ khoẻ"],
     "huong_dan_su_dung": ["Pha 10ml cho bình 16 lít"], "approved_for_live": True},
]}]}


def package(version, content=CONTENT):
    return json.dumps({"schema_version": 1, "version": version,
                       "content_sha256": content_hash(content), "content": content}).encode()


def setup(tmp_path):
    data = tmp_path / "data"
    (data / "shared").mkdir(parents=True)
    (data / "machine").mkdir()
    shared = data / "shared" / "product_knowledge.json"
    apply_package(package("1"), shared)
    (data / "machine" / "catalog_mapping.json").write_text(json.dumps({"assignments": {
        "shop-1": {"product_code": "SP-01", "approved_for_live": True},
        "shop-2": {"product_code": "SP-02", "approved_for_live": True}}}), encoding="utf-8")
    kb = KnowledgeBase(shared, data / "profiles.json", mapping_path=data / "machine" / "catalog_mapping.json")
    store = WorkspaceStore(FakeEngine(kb), tmp_path)
    return data, shared, kb, store


def ids(kb):
    return {r["san_pham_id"] for r in kb.all_records()}


def test_delete_hides_from_ai_and_matching_and_goes_to_trash(tmp_path):
    data, shared, kb, store = setup(tmp_path)
    assert kb.match("", product_id="shop-1")["san_pham_id"] == "SP-01"
    out = store.delete("profiles", "SP-01", {})
    assert out["undo_available"] is True
    assert ids(kb) == {"SP-02"}
    assert kb.match("", product_id="shop-1") is None
    assert kb.match("Đạm cá 5L") is None
    assert kb.match("", product_id="shop-2")["san_pham_id"] == "SP-02"
    assert kb.hidden_ids() == {"SP-01"}
    trash = store.trash({"resource": "profiles"})["items"]
    assert [t["record_id"] for t in trash] == ["SP-01"] and trash[0]["label"]
    assert store.trash({"resource": "videos"})["items"] == []


def test_deleted_profile_cannot_be_assigned(tmp_path):
    import pytest
    _, _, kb, store = setup(tmp_path)
    store.delete("profiles", "SP-01", {})
    with pytest.raises(ValueError):
        kb.apply_mapping({"shop-9": "SP-01"})


def test_no_resurrection_after_package_apply_seed_and_catalog_sync(tmp_path):
    data, shared, kb, store = setup(tmp_path)
    store.delete("profiles", "SP-01", {})
    # gói mới hơn, chứa lại chính hồ sơ đó
    apply_package(package("2"), shared)
    kb.reload()
    assert "SP-01" not in ids(kb) and kb.match("", product_id="shop-1") is None
    # seed lần đầu: file shared bị mất rồi cài lại từ seed; lớp cục bộ vẫn giữ
    seed = tmp_path / "seed.json"
    seed.write_bytes(package("3"))
    shared.unlink()
    assert seed_first_run(seed, shared)["status"] == "applied"
    kb.reload()
    assert "SP-01" not in ids(kb)
    # đồng bộ danh mục / ánh xạ chỉ ghi lớp shared + machine; lớp xoá cục bộ vẫn còn
    kb.apply_mapping({"shop-2": "SP-02"})
    kb.reload()
    assert "SP-01" not in ids(kb)
    overlay = json.loads((data / "local_product_overrides.json").read_text(encoding="utf-8"))
    assert overlay["overrides"]["SP-01"]["hidden"] is True


def test_restore_brings_profile_and_sku_link_back(tmp_path):
    data, shared, kb, store = setup(tmp_path)
    store.delete("profiles", "SP-01", {})
    apply_package(package("2"), shared)
    restored = store.restore("profiles", "SP-01")
    assert restored["restored"] is True
    assert "SP-01" in ids(kb)
    assert kb.match("", product_id="shop-1")["san_pham_id"] == "SP-01"
    assert store.trash({"resource": "profiles"})["items"] == []
    assert kb.hidden_ids() == set()


def test_purge_keeps_profile_deleted(tmp_path):
    data, shared, kb, store = setup(tmp_path)
    store.delete("profiles", "SP-02", {})
    trash_id = store.trash()["items"][0]["id"]
    assert store.purge_now([trash_id])["purged"] == 1
    apply_package(package("2"), shared)
    kb.reload()
    assert "SP-02" not in ids(kb)
