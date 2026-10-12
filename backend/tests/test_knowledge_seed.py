"""Gói hồ sơ chính thức đi kèm bộ cài: dựng, kiểm hash, cài lần đầu, thông báo bản mới."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parent
sys.path.insert(0, str(BACKEND / "src"))

import knowledge_package as kp  # noqa: E402
from knowledge import KnowledgeBase, is_approvable  # noqa: E402
from knowledge_diff import TRUONG_RIENG_CUA_MAY  # noqa: E402

SEED = BACKEND / "prompts" / "knowledge_seed.json"


def _load_tool():
    spec = importlib.util.spec_from_file_location("build_knowledge_seed", BACKEND / "tools" / "build_knowledge_seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _products(package):
    return [p for g in package["content"]["nhom_san_pham"] for p in g["san_pham"]]


def _package(version, products, published="2026-09-13T00:00:00Z"):
    content = {"nhom_san_pham": [{"san_pham": products}]}
    return {"schema_version": 1, "version": version, "published_at": published,
            "content_sha256": kp.content_hash(content), "content": content}


def _raw(package):
    return json.dumps(package, ensure_ascii=False).encode("utf-8")


SAMPLE = {"san_pham_id": "WEB-1", "ten_san_pham": "A", "cong_dung": ["Kích rễ"]}


# ------------------------------------------------------------ gói đã dựng
def test_shipped_seed_is_valid_for_apply_package():
    package = json.loads(SEED.read_text(encoding="utf-8"))
    kp.validate_package(package)                       # version + sha256 hợp lệ
    assert package["version"] and len(package["content_sha256"]) == 64
    assert kp.content_hash(package["content"]) == package["content_sha256"]
    assert len(_products(package)) >= 40


def test_shipped_seed_has_no_forbidden_or_machine_fields():
    tool = _load_tool()
    package = json.loads(SEED.read_text(encoding="utf-8"))
    assert tool.verify(package) == []
    forbidden = set(TRUONG_RIENG_CUA_MAY) | {"dose_rules", "legacy_profile_id", "shop_product_ids",
                                             "created_local", "profile_overrides"}
    for product in _products(package):
        assert not forbidden & set(product), product["san_pham_id"]
        assert not product["san_pham_id"].startswith("LEGACY-")
        assert is_approvable(product), product["san_pham_id"]
    text = SEED.read_text(encoding="utf-8")
    for needle in ("approved_for_live", "dose_rules", "legacy_profile_id", "shop_product_ids",
                   "customer", "khach_hang", "unique_id"):
        assert needle not in text, needle


def test_seed_is_free_of_company_contact_lines_and_has_no_mapping_data():
    text = SEED.read_text(encoding="utf-8")
    assert "assignments" not in text and "product_code" not in text
    assert "approved_live_profiles" not in text


def test_builder_strips_machine_fields_and_legacy_and_is_deterministic(tmp_path):
    tool = _load_tool()
    source = tmp_path / "product_knowledge.json"
    dirty = {"schema_version": 1, "published_at": "2026-09-13T05:12:09+00:00", "content": {
        "nhom_san_pham": [{"nhom": "g", "san_pham": [
            {**SAMPLE, "approved_for_live": True, "approved_by": "x", "legacy_profile_id": "SP-1",
             "dose_rules": [{"ml": 10}], "gia_goc_duyet": "1", "live_promotion": "p",
             "catalog_status": "s", "listing_aliases": ["a"], "quy_cach_dong_goi": "1L",
             "shop_product_ids": ["99"], "ghi_chu_rieng": "khách A", "huong_dan_su_dung": ["Pha"]},
            {"san_pham_id": "LEGACY-SP-01", "ten_san_pham": "Cũ", "cong_dung": ["x"]},
        ]}]}}
    source.write_text(json.dumps(dirty, ensure_ascii=False), encoding="utf-8")
    package = tool.build(source)
    assert package["version"] == "2026.09.13"
    products = _products(package)
    assert [p["san_pham_id"] for p in products] == ["WEB-1"]
    assert set(products[0]) == {"san_pham_id", "ten_san_pham", "cong_dung", "huong_dan_su_dung"}
    assert tool.build(source) == package
    kp.validate_package(package)


def test_builder_refuses_package_without_approvable_content(tmp_path):
    tool = _load_tool()
    source = tmp_path / "k.json"
    source.write_text(json.dumps({"published_at": "2026-01-02", "content": {"nhom_san_pham": [
        {"san_pham": [{"san_pham_id": "WEB-9", "ten_san_pham": "Trống"}]}]}}), encoding="utf-8")
    with pytest.raises(ValueError, match="không đủ nội dung"):
        tool.build(source)


def test_seed_applies_and_gives_a_working_knowledge_base(tmp_path):
    target = tmp_path / "data" / "shared" / "product_knowledge.json"
    result = kp.apply_package(SEED.read_bytes(), target)
    assert result["version"] == json.loads(SEED.read_text(encoding="utf-8"))["version"]
    kb = KnowledgeBase(target, tmp_path / "p.json", tmp_path / "data" / "machine" / "catalog_mapping.json")
    mappable = kb.mappable_records()
    assert len(mappable) >= 40
    # Máy trắng: có hồ sơ để gắn nhưng chưa gắn gì -> AI chưa dùng hồ sơ nào.
    assert kb.records == [] and kb.by_shop_id == {}


# ------------------------------------------------------------ cài lần đầu
def test_first_run_missing_shared_file_is_seeded(tmp_path):
    target = tmp_path / "shared" / "product_knowledge.json"
    out = kp.seed_first_run(SEED, target)
    assert out["status"] == "applied" and out["version"]
    stored = json.loads(target.read_text(encoding="utf-8"))
    assert stored["version"] == out["version"]
    # Hồi quy: gop() từng làm rơi sản phẩm khi tệp đích chưa có nhóm nào (máy trắng).
    assert len(_products(stored)) == len(_products(json.loads(SEED.read_text(encoding="utf-8")))) >= 40
    assert kp.content_hash(stored["content"]) == stored["content_sha256"]
    assert kp.seed_status(SEED, target)["state"] == "current"


def test_first_run_never_overwrites_an_existing_file(tmp_path):
    target = tmp_path / "shared" / "product_knowledge.json"
    target.parent.mkdir()
    target.write_text('{"mine": true}', encoding="utf-8")
    out = kp.seed_first_run(SEED, target)
    assert out["status"] == "existing_untouched"
    assert target.read_text(encoding="utf-8") == '{"mine": true}'
    assert not (target.parent / ".previous").exists()
    # kể cả tệp hỏng / rỗng
    target.write_text("", encoding="utf-8")
    assert kp.seed_first_run(SEED, target)["status"] == "existing_untouched"
    assert target.read_text(encoding="utf-8") == ""


def test_first_run_is_deferred_during_live(tmp_path):
    target = tmp_path / "shared" / "product_knowledge.json"
    out = kp.seed_first_run(SEED, target, live_running=True)
    assert out["status"] == "deferred_live"
    assert not target.exists()
    assert kp.seed_status(SEED, target)["state"] == "missing"        # Cài đặt vẫn mời cài sau
    with pytest.raises(RuntimeError, match="LIVE"):
        kp.apply_package(SEED.read_bytes(), target, live_running=True)


def test_first_run_without_a_seed_file_is_a_noop(tmp_path):
    target = tmp_path / "shared" / "product_knowledge.json"
    assert kp.seed_first_run(tmp_path / "none.json", target)["status"] == "no_seed"
    assert not target.exists()
    assert kp.seed_status(tmp_path / "none.json", target)["state"] == "no_seed"


def test_first_run_with_a_corrupt_seed_fails_closed(tmp_path):
    bad = tmp_path / "seed.json"
    package = _package("2026.01.01", [SAMPLE])
    package["content_sha256"] = "0" * 64
    bad.write_bytes(_raw(package))
    target = tmp_path / "shared" / "product_knowledge.json"
    out = kp.seed_first_run(bad, target)
    assert out["ok"] is False and out["status"] == "failed"
    assert not target.exists()


# ------------------------------------------------------------ gói đi kèm mới hơn
def test_version_key_orders_numerically():
    assert kp.version_key("2026.10.01") > kp.version_key("2026.9.30")
    assert kp.version_key("2026.09.13") == kp.version_key("2026.9.13")
    assert kp.version_key("") < kp.version_key("0.0.1")


def test_newer_seed_is_reported_not_applied(tmp_path):
    seed = tmp_path / "seed.json"
    target = tmp_path / "shared" / "product_knowledge.json"
    seed.write_bytes(_raw(_package("2026.09.13", [SAMPLE])))
    kp.seed_first_run(seed, target)
    seed.write_bytes(_raw(_package("2026.10.20", [SAMPLE, {**SAMPLE, "san_pham_id": "WEB-2"}])))
    before = target.read_bytes()
    status = kp.seed_status(seed, target)
    assert status == {"state": "newer", "seed_version": "2026.10.20", "installed_version": "2026.09.13"}
    assert kp.seed_first_run(seed, target)["status"] == "existing_untouched"
    assert target.read_bytes() == before                               # không tự áp dụng


def test_newer_seed_goes_through_preview_apply_and_undo(tmp_path):
    seed = tmp_path / "seed.json"
    target = tmp_path / "shared" / "product_knowledge.json"
    seed.write_bytes(_raw(_package("2026.09.13", [SAMPLE])))
    kp.seed_first_run(seed, target)
    new = _package("2026.10.20", [SAMPLE, {**SAMPLE, "san_pham_id": "WEB-2", "ten_san_pham": "B"}])
    seed.write_bytes(_raw(new))
    preview = kp.xem_truoc(kp.read_source(str(seed)), target)
    assert preview["khac_biet"]["tom_tat"]["them_moi"] == 1
    kp.update_from_source(str(seed), target)
    assert kp.seed_status(seed, target)["state"] == "current"
    assert kp.installed_version(target) == "2026.10.20"
    kp.hoan_tac(target)
    assert kp.seed_status(seed, target)["state"] == "newer"


def test_same_content_or_older_seed_is_current(tmp_path):
    seed = tmp_path / "seed.json"
    target = tmp_path / "shared" / "product_knowledge.json"
    seed.write_bytes(_raw(_package("2026.09.13", [SAMPLE])))
    kp.seed_first_run(seed, target)
    assert kp.seed_status(seed, target)["state"] == "current"
    seed.write_bytes(_raw(_package("2026.01.01", [SAMPLE])))
    assert kp.seed_status(seed, target)["state"] == "current"


def test_legacy_shared_file_without_version_uses_published_at(tmp_path):
    """Tệp trên máy đang chạy chưa ghi version: so theo ngày phát hành, không báo oan."""
    target = tmp_path / "shared" / "product_knowledge.json"
    seed = tmp_path / "seed.json"
    seed.write_bytes(_raw(_package("2026.09.13", [SAMPLE])))
    content = {"nhom_san_pham": [{"san_pham": [SAMPLE]}]}
    target.parent.mkdir()
    target.write_text(json.dumps({"schema_version": 1, "published_at": "2026-09-13T05:12:09+00:00",
                                  "content": content, "content_sha256": "x" * 64}), encoding="utf-8")
    assert kp.installed_version(target) == "2026-09-13"
    assert kp.seed_status(seed, target)["state"] == "current"


# ------------------------------------------------------------ chính sách đóng gói
def _stage_runtime():
    spec = importlib.util.spec_from_file_location("stage_runtime", PROJECT / "packaging" / "stage_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packaging_requires_the_seed_but_still_forbids_machine_catalog_files():
    stage = _stage_runtime()
    source = (PROJECT / "packaging" / "stage_runtime.py").read_text(encoding="utf-8")
    assert "backend/prompts/knowledge_seed.json" in source
    for name in ("product_knowledge.json", "approved_live_profiles.json", "company_catalog_mapping.json",
                 "local_product_overrides.json", "catalog_mapping.json", "shopee_live_mappings.json"):
        assert name in stage.FORBIDDEN_NAMES
    assert "knowledge_seed.json" not in stage.FORBIDDEN_NAMES
    assert "prompts" not in stage.FORBIDDEN_PARTS


def test_prompts_ship_in_the_app_package_not_in_the_frozen_runtime():
    spec = (PROJECT / "packaging" / "KidTE.spec").read_text(encoding="utf-8")
    assert 'str(BACKEND / "prompts")' not in spec  # the frozen runtime carries no Kid TE resources
    stage_source = (PROJECT / "packaging" / "stage_runtime.py").read_text(encoding="utf-8")
    assert 'copy_tree(PROJECT / "backend" / "prompts", destination / "backend" / "prompts")' in stage_source


def test_validate_fails_when_the_seed_is_missing_and_passes_with_it(tmp_path):
    from test_packaging_policy import _bo_khung_hop_le
    stage = _stage_runtime()
    root = _bo_khung_hop_le(tmp_path)
    seed = root / "backend" / "prompts" / "knowledge_seed.json"
    assert seed.is_file()
    assert stage.validate(root)["ok"] is True
    seed.unlink()
    with pytest.raises(RuntimeError, match="knowledge_seed.json"):
        stage.validate(root)
