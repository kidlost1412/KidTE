"""Khung dựng ReplyEngine + DeepSeekResponder THẬT với client giả, để chụp đúng messages gửi model.

Chỉ dùng API có sẵn từ trước Package A: script tạo golden chạy được trên mã cũ.
"""
import hashlib
import json
import types
from pathlib import Path

import server as server_module
from customer_memory import CustomerMemory
from knowledge import KnowledgeBase
from server import LiveCatalogStore, MissingReporter, ReplyEngine

PROFILE_JUNK = {
    "nguon_chinh_thuc": "https://example.test/p", "website_product_ids": ["1206"],
    "source_modified": "2026-04-01T08:40:13", "source_kind": "company_website",
    "website_categories": ["sản phẩm bán chạy nhất"], "featured_image_url": "https://example.test/i.jpg",
    "featured_image_alt": "", "catalog_status": "official_profile_not_directly_bound",
    "ten_tren_website": "KÍCH RỄ TC01", "legacy_profile_id": "SP-03",
}

PRODUCTS = [
    ("p1", "Kích Rễ Giâm Cành TC01 - Chuyên Dùng Cho Mai Vàng, Cây cảnh, Bonsai, Rau Màu (chai 100ml) humic tưới",
     "120.000₫", "90.000₫", ""),
    ("p2", "Nano Tonic 250ml Giúp Phòng Nấm Rễ Tuyến Trùng Chuyên Trị Cây Trồng", "250.000₫", "", ""),
    ("p3", "Đạm Cá Hữu Cơ Amino Giúp Cây Bật Mầm Xanh Lá (Can 5 Lít) bảo hành chính hãng", "289.000₫", "",
     '{"status":2,"activity_id":"1","format_activity_price":"234.090₫"}'),
]

PROFILES = [
    {"san_pham_id": "SP-03", "ten_san_pham": "Kích Rễ Giâm Cành TC01", "approved_for_live": True,
     "shop_product_ids": ["p1"], "listing_aliases": "TC01",
     "huong_dan_su_dung": ["Cây mới trồng: pha 10ml cho bình 16 lít", "Cây ăn trái: 20ml/16 lít"],
     "dose_extrapolation_rule": "nearest_endpoint",
     "cong_dung": ["Hỗ trợ kích rễ", "Giúp cây đâm chồi"], "luu_y": "Không pha chung với chất có tính kiềm mạnh.",
     "thanh_phan_dang_ky": ["Đạm 10%", "Lân 8%"], "doi_tuong_cay_trong": ["Cây ăn trái", "Bonsai"],
     "mo_ta_ngan": "Kích rễ cực mạnh", "hieu_qua_chinh": "Kích rễ", "gia_goc_duyet": "120.000₫",
     "live_promotion": "Mua 10 tặng 1.", "quy_cach_dong_goi": "Chai 100ml", **PROFILE_JUNK},
    {"san_pham_id": "SP-04", "ten_san_pham": "Nano Tonic 250ml", "approved_for_live": True,
     "shop_product_ids": ["p2"], "listing_aliases": "nano tonic",
     "huong_dan_su_dung": ["Pha 20ml cho bình 16 lít, 7 ngày/lần"],
     "cong_dung": ["Phòng nấm rễ", "Phòng tuyến trùng", "Giúp bộ rễ khỏe"],
     "doi_tuong_cay_trong": ["Cà phê", "Hồ tiêu"], "luu_y": "Phun sáng sớm.",
     "thanh_phan_dang_ky": ["Đồng 5%"], **PROFILE_JUNK},
    {"san_pham_id": "SP-13", "ten_san_pham": "Đạm Cá Hữu Cơ Amino (5 lít)", "approved_for_live": True,
     "shop_product_ids": ["p3"], "listing_aliases": "đạm cá",
     "huong_dan_su_dung": ["Cây con: 30–50ml cho 20 lít nước"], "cong_dung": ["Phục hồi cây sau thu hoạch"],
     "luu_y": "Dùng đúng liều.", "doi_tuong_cay_trong": ["Sầu riêng"], **PROFILE_JUNK},
]


def live_snapshot():
    rows = [{"product_id": pid, "title": title, "original_price": orig, "sale_price": sale, "promotion": promo,
             "price": sale or orig, "stock_num": 100, "skus": []} for pid, title, orig, sale, promo in PRODUCTS]
    return {"room_id": "room-1", "pin_id": "p1", "products": rows}


def payload(text, cid="c1", uid="u1", nick="Khách thử nghiệm"):
    return {"comment": text, "comment_id": cid, "user_id": uid, "unique_id": f"user{uid}",
            "nickname": nick, "live_context": live_snapshot()}


class FakeCompletions:
    def __init__(self, sink):
        self.sink = sink

    def create(self, **inference):
        self.sink.append(inference)
        text = "Dạ em gửi anh chị ạ."
        msg = types.SimpleNamespace(content=json.dumps(
            {"action": "reply", "reply": text, "messages": [text], "awaiting_fields": []},
            ensure_ascii=False), reasoning_content=None)
        usage = types.SimpleNamespace(prompt_tokens=100, completion_tokens=10, prompt_cache_hit_tokens=40,
                                      prompt_cache_miss_tokens=60)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")], usage=usage)


def make_engine(tmp_path):
    """Gọi sau khi đã trỏ server_module.LIVE_CATALOG_PATH vào tmp_path."""
    tmp_path = Path(tmp_path)
    catalog = LiveCatalogStore()
    catalog.sync = lambda snap: {"room_id": snap.get("room_id", ""), "pin_id": snap.get("pin_id", ""),
                                 "products": snap.get("products", []), "product_count": len(snap.get("products", [])),
                                 "synced_at": "now"}
    reporter = MissingReporter()
    reporter.path = tmp_path / "missing.jsonl"
    memory = CustomerMemory(tmp_path / "memory.sqlite3", tmp_path / "journals", session_id="s1")
    (tmp_path / "k.json").write_text(json.dumps({"nhom_san_pham": [{"san_pham": PROFILES}]}, ensure_ascii=False),
                                     encoding="utf-8")
    (tmp_path / "p.json").write_text('{"products":{}}', encoding="utf-8")
    captured = []
    responder = server_module._model_processor.DeepSeekResponder()
    responder.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions(captured)))
    responder.check_balance = lambda *a, **k: {"ok": True, "deepseek_available": True}
    engine = ReplyEngine(responder=responder, memory=memory,
                         knowledge=KnowledgeBase(tmp_path / "k.json", tmp_path / "p.json"),
                         catalog_store=catalog, missing_reporter=reporter)
    engine._media_bindings_store = None
    from obs_source import MediaBindingStore
    engine._media_bindings_store = MediaBindingStore(tmp_path / "bindings.sqlite3")
    return engine, captured


QUESTIONS = ["TC01 giá bao nhiêu", "kích rễ pha như nào", "pha 5 lít nước bao nhiêu ml", "bình 16 lít pha bao nhiêu ml",
             "có loại nào phòng nấm rễ và tuyến trùng không?", "kích rễ xịt lên lá được không em",
             "hello", "ok lấy 2 chai"]


def run_fixed(engine, captured, questions=QUESTIONS):
    """Trả danh sách {q, calls:[messages không gồm system, system_sha]} theo từng câu."""
    out = []
    for index, question in enumerate(questions):
        before = len(captured)
        engine.handle(payload(question, cid=f"c{index}", uid=f"u{index}"))
        calls = []
        for call in captured[before:]:
            messages = call["messages"]
            calls.append({"system_sha": hashlib.sha256(messages[0]["content"].encode()).hexdigest()[:16],
                          "messages": messages[1:]})
        out.append({"q": question, "calls": calls})
    return out
