"""Cài đặt bộ xử lý, sao lưu từng mục, model chép lời, nhật ký dễ đọc."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
ROOT = SRC.parents[1]
sys.path.insert(0, str(SRC))

import backup_policy
import log_viewer
import whisper_models
from processor_loader import load_processor
from processor_settings import ProcessorSettings


# ------------------------------------------------------------ cài đặt bộ xử lý
def test_settings_store_keeps_only_changes_and_validates(tmp_path):
    schema = [
        {"key": "on", "type": "bool", "default": True, "label": "Bật"},
        {"key": "n", "type": "number", "default": 1.6, "min": 0.5, "max": 5, "label": "Ngưỡng"},
        {"key": "words", "type": "list", "default": ["a", "b"], "label": "Từ"},
    ]
    store = ProcessorSettings(tmp_path / "s.json")
    values = store.save("x", schema, {"on": False, "words": ["a", "A", " c ", ""]})
    assert values == {"on": False, "n": 1.6, "words": ["a", "c"]}
    assert json.loads((tmp_path / "s.json").read_text(encoding="utf-8")) == {"x": {"on": False, "words": ["a", "c"]}}
    with pytest.raises(ValueError, match="Ngưỡng"):
        store.save("x", schema, {"n": 99})
    with pytest.raises(ValueError, match="không phải cài đặt"):
        store.save("x", schema, {"la": 1})
    assert store.reset("x", schema) == {"on": True, "n": 1.6, "words": ["a", "b"]}


def _price():
    return load_processor(ROOT / "plugins" / "chinh-sach-gia" / "processor.py", "price-settings-test")


def test_price_toggles_change_behaviour():
    price = _price()
    signal = {"kind": "shopper_visible_price_report", "da_mua_roi": True}
    assert price.pushy_after_purchase("dạ chốt liền nha", signal) == "chốt liền"
    assert price.asserts_voucher("tài khoản anh có mã rồi đó")
    price.configure({"cham_soc_sau_ban": False, "chan_khang_dinh_co_ma": False, "nhac_ma_tiktok": False})
    assert price.pushy_after_purchase("dạ chốt liền nha", signal) == ""
    assert price.asserts_voucher("tài khoản anh có mã rồi đó") == ""
    context = price.price_context({"price": "289.000₫"})
    assert "KHÔNG nhắc" in context["voucher_pitch"]
    assert "kiểm tra mã" not in price.price_policy_text()


def test_every_processor_schema_is_valid_and_defaults_match_settings(tmp_path):
    store = ProcessorSettings(tmp_path / "s.json")
    for plugin_id in ("chinh-sach-gia", "chon-san-pham", "lieu-dung", "model-deepseek", "thu-du-lieu"):
        module = load_processor(ROOT / "plugins" / plugin_id / "processor.py", f"schema-{plugin_id}")
        keys = [field["key"] for field in module.SETTINGS_SCHEMA]
        assert len(keys) == len(set(keys)), plugin_id
        for field in module.SETTINGS_SCHEMA:
            assert field.get("label") and field.get("type") in {"bool", "number", "list", "text", "select"}
            if field.get("depends"):
                assert field["depends"] in keys
        # Giá trị mặc định phải qua được chính bộ kiểm của nó.
        assert store.save(plugin_id, module.SETTINGS_SCHEMA, {}) == module.SETTINGS


def test_selection_explains_its_choice_and_honours_threshold():
    selector = load_processor(ROOT / "plugins" / "chon-san-pham" / "processor.py", "explain-test")
    products = [{"product_id": "tc01", "title": "Kích rễ TC01 mai vàng"},
                {"product_id": "fish", "title": "Đạm cá amino"},
                {"product_id": "humic", "title": "Humic cải tạo đất"},
                {"product_id": "npk", "title": "NPK ra hoa đậu quả"},
                {"product_id": "tonic", "title": "Nano Tonic xoăn lá"}]
    out = selector.explain_selection("tc01 giá sao", products, "fish")
    assert out["product"]["product_id"] == "tc01" and out["reason"] == "enough_evidence"
    selector.configure({"nguong_chac_chan": 5.0})
    assert selector.explain_selection("tc01 giá sao", products, "fish")["reason"] == "ambiguous_keeps_pinned"


def test_dose_can_refuse_extrapolation():
    dose = load_processor(ROOT / "plugins" / "lieu-dung" / "processor.py", "dose-settings-test")
    approved = {"dose_extrapolation_rule": "preserve_concentration_range",
                "huong_dan_su_dung": ["Pha 20ml cho bình 16 - 25 lít"]}
    assert dose.verified_dose_calculations("phuy 200 lít pha sao",
        {"huong_dan_su_dung": approved["huong_dan_su_dung"]}) == []
    assert dose.verified_dose_calculations("phuy 200 lít pha sao", approved)
    assert dose.verified_dose_calculations("phuy 200 lít pha sao", approved)[0]["calculated_ml_min"] == 160
    assert dose.verified_dose_calculations("phuy 200 lít pha sao", approved)[0]["calculated_ml_max"] == 250
    dose.configure({"quy_doi_ngoai_khoang": False})
    assert dose.verified_dose_calculations("phuy 200 lít pha sao", approved) == []
    assert dose.verified_dose_calculations("bình 20 lít pha sao", approved)


def test_raw_stream_retention(tmp_path):
    data = load_processor(ROOT / "plugins" / "thu-du-lieu" / "processor.py", "stream-retention")
    from datetime import date
    for day in ("20260101", "20260920"):
        (tmp_path / f"stream_{day}.jsonl").write_text('{"text": "x"}\n', encoding="utf-8")
    assert data.stream_stats(tmp_path)["rows"] == 2
    assert data.prune_old_streams(tmp_path, 0) == []
    assert data.prune_old_streams(tmp_path, 30, today=date(2026, 9, 24)) == ["stream_20260101.jsonl"]


# ------------------------------------------------------------ sao lưu từng mục
def _point_backup_at(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "videos" / "live1").mkdir(parents=True)
    for name in ("approved.json", "profiles.json", "mapping.json"):
        (data / name).write_text("{}", encoding="utf-8")
    (data / "shop.json").write_text('{"dia_chi": "A"}', encoding="utf-8")
    (data / "videos" / "live1" / "timeline.json").write_text('{"events": [1, 2]}', encoding="utf-8")
    for name in ("memory", "workspace", "learning"):
        with sqlite3.connect(data / f"{name}.sqlite3") as connection:
            connection.execute("CREATE TABLE t (id INTEGER)")
    with sqlite3.connect(data / "memory.sqlite3") as connection:
        connection.execute("CREATE TABLE customers (id INTEGER)")
        connection.executemany("INSERT INTO customers VALUES (?)", [(1,), (2,), (3,)])
    patches = {
        "BACKUP_DIR": tmp_path / "backups", "MEMORY_DB": data / "memory.sqlite3",
        "OPERATOR_WORKSPACE_DB": data / "workspace.sqlite3", "AI_LEARNING_DB": data / "learning.sqlite3",
        "RAW_STREAM_DIR": tmp_path / "raw", "VIDEO_LIBRARY_DIR": data / "videos",
        "APPROVED_CATALOG_PATH": data / "approved.json", "APPROVED_PROFILES_PATH": data / "profiles.json",
        "SHOP_INFO_PATH": data / "shop.json", "CATALOG_MAPPING_PATH": data / "mapping.json",
        "MACHINE_CATALOG_MAPPING_PATH": data / "none-machine.json", "LIVE_CATALOG_PATH": data / "none-live.json",
        "LIVE_RAW_CATALOG_PATH": data / "none-raw.json", "PROCESSOR_SETTINGS_PATH": data / "cai_dat.json",
        "SYSTEM_PROMPT_OVERRIDE_PATH": data / "prompt.txt",
    }
    for name, value in patches.items():
        monkeypatch.setattr(backup_policy, name, value)
    return data


def test_partial_backup_and_restore_only_touch_chosen_parts(tmp_path, monkeypatch):
    data = _point_backup_at(tmp_path, monkeypatch)
    (data / "prompt.txt").write_text("lời nhắc A", encoding="utf-8")
    made = backup_policy.create_operational_backup(parts=["cai_dat", "lich_ghim"])
    assert made["parts"] == ["lich_ghim", "cai_dat"]
    manifest = json.loads((Path(made["backup_dir"]) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["scope"] == "partial"
    assert {f["path"] for f in manifest["files"]} == {
        "thong_tin_shop.json", "loi_nhac_he_thong.txt", "videos/live1/timeline.json"}
    # Bản từng mục không tính là "đã sao lưu đầy đủ".
    assert backup_policy.latest_backup_time(tmp_path / "backups") is None

    (data / "prompt.txt").write_text("lời nhắc B", encoding="utf-8")
    (data / "shop.json").write_text('{"dia_chi": "B"}', encoding="utf-8")
    result = backup_policy.restore_operational_backup(made["backup_id"], parts=["cai_dat"])
    assert result["parts"] == ["cai_dat"] and result["safety_backup"]
    assert (data / "prompt.txt").read_text(encoding="utf-8") == "lời nhắc A"
    assert json.loads((data / "shop.json").read_text(encoding="utf-8")) == {"dia_chi": "A"}
    with pytest.raises(ValueError, match="backup_parts_not_in_backup"):
        backup_policy.restore_operational_backup(made["backup_id"], parts=["khach_hang"])


def test_sales_playbook_is_part_of_cai_dat_backup_and_restore(tmp_path, monkeypatch):
    data = _point_backup_at(tmp_path, monkeypatch)
    playbook = data / "shared" / "sales_playbook.json"
    playbook.parent.mkdir()
    playbook.write_text('{"entries": [{"id": "a"}, {"id": "b"}]}', encoding="utf-8")
    monkeypatch.setattr(backup_policy, "SALES_PLAYBOOK_PATH", playbook)
    assert backup_policy.part_of("sales_playbook.json") == "cai_dat"
    made = backup_policy.create_operational_backup(parts=["cai_dat"])
    manifest = json.loads((Path(made["backup_dir"]) / "manifest.json").read_text(encoding="utf-8"))
    assert "sales_playbook.json" in {f["path"] for f in manifest["files"]}
    playbook.write_text('{"entries": []}', encoding="utf-8")
    detail = backup_policy.backup_detail(made["backup_id"])
    cai_dat = next(p for p in detail["parts"] if p["id"] == "cai_dat")
    assert {"label": "tình huống trong sổ tay", "backup": 2, "current": 0} in cai_dat["counts"]
    backup_policy.restore_operational_backup(made["backup_id"], parts=["cai_dat"])
    assert len(json.loads(playbook.read_text(encoding="utf-8"))["entries"]) == 2


def test_backup_detail_compares_counts_with_current_data(tmp_path, monkeypatch):
    data = _point_backup_at(tmp_path, monkeypatch)
    made = backup_policy.create_operational_backup()
    with sqlite3.connect(data / "memory.sqlite3") as connection:
        connection.execute("DELETE FROM customers WHERE id = 3")
    detail = backup_policy.backup_detail(made["backup_id"])
    assert detail["scope"] == "full" and not detail["integrity_error"]
    customers = next(p for p in detail["parts"] if p["id"] == "khach_hang")
    assert {"label": "khách", "backup": 3, "current": 2} in customers["counts"]
    videos = next(p for p in detail["parts"] if p["id"] == "lich_ghim")
    assert videos["counts"] == [{"label": "mốc ghim", "backup": 2, "current": 2}]


# ------------------------------------------------------------ model chép lời
def test_whisper_recommendation_follows_hardware():
    assert whisper_models.recommend({"ram_gb": 32, "cuda": True, "gpu": {"vram_gb": 6}}) == "large-v3-turbo"
    assert whisper_models.recommend({"ram_gb": 6, "cuda": False}) == "small"
    assert whisper_models.recommend({"ram_gb": 3, "cuda": False}) == "base"


def test_whisper_active_model_prefers_selection_then_best_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(whisper_models, "WHISPER_MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(whisper_models, "SELECTION_PATH", tmp_path / "whisper.json")
    monkeypatch.delenv("LIVE_RE_WHISPER_MODEL", raising=False)
    assert whisper_models.active_model_path() is None

    def install(model_id, complete=True):
        folder = tmp_path / "models" / model_id
        folder.mkdir(parents=True)
        for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
            (folder / name).write_text("x", encoding="utf-8")
        if complete:
            (folder / ".complete").write_text("{}", encoding="utf-8")
        return folder

    install("medium", complete=False)                 # tải dở → chưa tính là có
    assert whisper_models.active_model_path() is None
    small = install("small")
    assert whisper_models.active_model_path() == str(small)
    with pytest.raises(ValueError, match="model_not_installed"):
        whisper_models.select("large-v3")
    whisper_models.select("small")
    assert whisper_models.delete("small") is True
    assert whisper_models.active_model_path() is None


# ------------------------------------------------------------ nhật ký
def test_logs_are_translated_and_problems_filter(tmp_path):
    rows = [
        {"timestamp": "2026-09-24T01:00:00Z", "severity": "info", "event": "backend_ready", "message": "ok"},
        {"timestamp": "2026-09-24T01:01:00Z", "severity": "warning", "event": "ai_reply_failed",
         "message": "AI không trả lời được: deepseek_AuthenticationError"},
        {"timestamp": "2026-09-24T01:02:00Z", "severity": "error", "event": "api_error",
         "message": "POST /api/x", "exception": "Traceback ... sk-abcdefghijklmnop"},
    ]
    (tmp_path / "core.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    out = log_viewer.recent_logs(tmp_path, severity="problems")["rows"]
    assert [r["event"] for r in out] == ["api_error", "ai_reply_failed"]
    assert "Khoá DeepSeek sai" in out[1]["hint"]
    assert "sk-abcdefghijklmnop" not in out[0]["exception"]
    assert log_viewer.recent_logs(tmp_path)["rows"][-1]["title"] == "Ứng dụng đã mở xong"


def test_whisper_hardware_probe_never_blocks_request(monkeypatch):
    import threading
    import time
    release = threading.Event()

    def slow_probe():
        release.wait(5)
        return {"ram_gb": 16, "cpu_cores": 8, "gpu": {"name": "X", "vram_gb": 6}, "cuda": True, "disk_free_gb": 10}

    monkeypatch.setattr(whisper_models, "_HARDWARE", {})
    monkeypatch.setattr(whisper_models, "_HW_THREAD", None)
    monkeypatch.setattr(whisper_models, "_probe_hardware", slow_probe)
    started = time.perf_counter()
    first = whisper_models.hardware()
    assert time.perf_counter() - started < 0.5          # không chờ thăm dò chậm
    assert first["probing"] is True and first["cuda"] is False
    again = whisper_models.hardware()                   # không sinh thêm luồng thăm dò
    assert again["probing"] is True
    release.set()
    done = whisper_models.hardware(wait=True)
    assert done["cuda"] is True and done["probing"] is False
    assert whisper_models.hardware()["cuda"] is True    # đã nhớ, trả ngay
