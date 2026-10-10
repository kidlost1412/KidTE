"""Package A trên đường Shopee: chiếu trường theo ý định, bỏ product_ref; cờ tắt = như cũ."""
import json
import time
from types import SimpleNamespace

import pytest

import context_budget as cb
from shopee_ai import ShopeeAiEngine

JUNK = {"website_product_ids": ["1"], "featured_image_url": "https://x/i.jpg", "source_kind": "company_website",
        "nguon_chinh_thuc": "https://x", "catalog_status": "c", "ten_tren_website": "t", "legacy_profile_id": "SP-1"}
PROFILE = {"san_pham_id": "SP-1", "ten_san_pham": "Kích Rễ TC01", "cong_dung": ["Kích rễ"],
           "huong_dan_su_dung": ["Pha 10ml cho bình 16 lít"], "luu_y": "Phun sáng sớm", **JUNK}


class Recorder:
    def __init__(self):
        self.contexts = []

    def reply(self, nickname, comment, context, max_chars, **kwargs):
        self.contexts.append(context)
        return {"action": "reply", "reply": "Dạ dùng theo hướng dẫn nha", "metrics": {}}


class Knowledge:
    def match(self, title, comment=""):
        return {"ten_san_pham": title} if title == "Kích Rễ TC01" else None

    def context(self, record):
        return dict(PROFILE)


def make(tmp_path):
    catalog = {"session_id": "123", "updated_at": time.time(), "products": [
        {"shop_id": "10", "product_id": "20", "title": "Kích Rễ TC01",
         "price_data": {"currency": "VND", "price": "80000", "price_before_discount": "80000"}}]}
    owner = SimpleNamespace(responder=Recorder(), knowledge=Knowledge(), shopee_mapping_state=lambda: {
        "catalog": catalog, "videos": {"v": {"mappings": {"tc01": {"shop_id": "10", "product_id": "20"}}}}})
    return owner, ShopeeAiEngine(owner, tmp_path / "m.sqlite3", tmp_path / "j")


def ask(engine, text, cid):
    return engine.reply({"session_id": "123", "video_id": "v", "comment_id": cid, "user_id": "77", "nickname": "Mai",
                         "comment": text, "shop_id": "10", "product_id": "20", "title": "Kích Rễ TC01"})


@pytest.fixture(autouse=True)
def default_flags(monkeypatch, tmp_path):
    monkeypatch.setattr(cb, "FLAGS_PATH", tmp_path / "none.json")


def test_flags_on_projects_profile_and_drops_product_ref(tmp_path):
    owner, engine = make(tmp_path)
    try:
        assert ask(engine, "TC01 dùng sao?", "a1")["should_send"]
        context = owner.responder.contexts[-1]
        assert "product_ref" not in context
        approved = context["approved_product_knowledge"]
        assert not set(JUNK) & set(approved) and approved["huong_dan_su_dung"]
        assert context["price"]["quotable_price"]
    finally:
        engine.close()


def test_flags_off_keeps_old_shape(tmp_path, monkeypatch):
    path = tmp_path / "flags.json"
    path.write_text(json.dumps({"smalltalk_lane": False, "intent_fields": False, "compact_catalog": False}))
    monkeypatch.setattr(cb, "FLAGS_PATH", path)
    owner, engine = make(tmp_path)
    try:
        assert ask(engine, "TC01 dùng sao?", "a2")["should_send"]
        context = owner.responder.contexts[-1]
        assert "product_ref" in context and "featured_image_url" not in context["approved_product_knowledge"]
        # approved đã được lọc theo technical_fields từ trước (không có trường rác) — vẫn nguyên như cũ
        assert context["approved_product_knowledge"]["huong_dan_su_dung"] == ["Pha 10ml cho bình 16 lít"]
    finally:
        engine.close()


def test_failure_in_projection_never_blocks_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "project_profile", lambda *a, **k: 1 / 0)
    owner, engine = make(tmp_path)
    try:
        assert ask(engine, "TC01 dùng sao?", "a3")["should_send"]
    finally:
        engine.close()
