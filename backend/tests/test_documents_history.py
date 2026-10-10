"""Tài liệu cho AI + lịch sử khách theo phiên LIVE + hồ sơ tự tạo."""
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from customer_history import CustomerHistory
from documents import DocumentLibrary, chunk_text, extract_text
from knowledge import KnowledgeBase


def _docx(*paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def test_extract_text_formats_and_rejections():
    assert "Đắk Lắk" in extract_text("a.docx", _docx("Shop thành lập tại Đắk Lắk.", "Chuyên phân hữu cơ."))
    assert extract_text("a.html", "<p>Xin chào</p><script>x()</script><p>nhà vườn thân mến</p>".encode()) \
        == "Xin chào\n nhà vườn thân mến"
    with pytest.raises(ValueError, match="Chỉ nhận"):
        extract_text("a.exe", b"MZ")
    with pytest.raises(ValueError, match=".docx"):
        extract_text("a.docx", b"not a zip")
    chunks = chunk_text("Câu một dài. " * 200)
    assert len(chunks) > 1 and all(len(c) <= 1100 for c in chunks)


def test_library_search_finds_relevant_part_and_respects_toggle(tmp_path):
    lib = DocumentLibrary(tmp_path)
    doc = lib.add("benh.txt", ("Bệnh xì mủ sầu riêng do nấm Phytophthora. Cạo vết bệnh, quét gốc đồng.\n\n"
                               "Cà phê vàng lá do tuyến trùng rễ.").encode(), category="nong_nghiep")
    found = lib.search("sầu riêng bị xì mủ xử lý sao")
    assert found and found[0]["tai_lieu"] == "benh" and "Phytophthora" in found[0]["doan"]
    assert lib.search("giá bao nhiêu vậy shop") == []
    lib.update(doc["id"], {"enabled": False})
    assert lib.search("sầu riêng bị xì mủ xử lý sao") == []
    lib.delete(doc["id"])
    assert lib.list() == []
    with pytest.raises(ValueError, match="document_not_found"):
        lib.detail("../../etc")


def _stream(tmp_path, rows):
    folder = tmp_path / "raw"
    folder.mkdir(exist_ok=True)
    with (folder / "stream_20260920.jsonl").open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return folder


def test_customer_history_groups_by_live_session_and_never_mixes_people(tmp_path):
    base = {"user_id": "111", "unique_id": "anh.ba", "nickname": "Anh Ba", "is_host": False}
    folder = _stream(tmp_path, [
        {**base, "ts": "2026-09-13T13:00:00Z", "room_id": "R1", "msg_id": "m1", "text": "đạm cá bón sầu riêng được không?"},
        {"ts": "2026-09-13T13:02:00Z", "room_id": "R1", "is_host": True, "reply_to_username": "anh.ba",
         "text": "@anh.ba được anh, pha 50ml bình 20 lít"},
        {**base, "ts": "2026-09-20T13:00:00Z", "room_id": "R2", "msg_id": "m2", "text": "hôm trước mua rồi, giờ cây ra đọt non"},
        # Người KHÁC trùng tên hiển thị: không được lẫn vào lịch sử của Anh Ba.
        {"user_id": "222", "unique_id": "khac", "nickname": "Anh Ba", "is_host": False,
         "ts": "2026-09-20T13:01:00Z", "room_id": "R2", "msg_id": "m3", "text": "tôi là người khác"},
        {**base, "ts": "2026-09-24T13:00:00Z", "room_id": "R3", "msg_id": "m4", "text": "chai này giá sao"},
    ])
    history = CustomerHistory(folder)
    out = history.for_customer("tiktok:id:111", current_room="R3", current_msg_id="m4", current_text="chai này giá sao")
    assert out["dinh_danh"] == "tiktok:id:111" and out["so_phien_da_binh_luan"] == 3
    assert [s["ngay"] for s in out["phien_truoc"]][0].startswith("20/09")
    first = out["phien_truoc"][-1]["binh_luan"][0]
    assert first["tra_loi"].startswith("Chủ shop:") and "50ml" in first["tra_loi"]
    assert out["phien_nay"] == []                       # câu đang hỏi không tự nhắc lại chính nó
    assert "tôi là người khác" not in json.dumps(out, ensure_ascii=False)
    assert any("đã mua" in note for note in out["luu_y"])
    # Khách chỉ có tên hiển thị: không dựng lịch sử (không chắc là cùng người).
    assert history.for_customer("tiktok:name:anh ba") == {}
    # Luồng ghi thêm giữa phiên: chỉ đọc phần mới.
    _stream(tmp_path, [{**base, "ts": "2026-09-24T13:05:00Z", "room_id": "R3", "msg_id": "m5", "text": "alo shop"}])
    later = history.for_customer("tiktok:id:111", current_room="R3", current_msg_id="m6", current_text="x")
    assert [c["khach"] for c in later["phien_nay"]] == ["chai này giá sao", "alo shop"]


def test_locally_created_profile_is_a_real_record(tmp_path):
    data = tmp_path / "data"
    (data / "shared").mkdir(parents=True)
    (data / "local_product_overrides.json").write_text(json.dumps({"overrides": {
        "LOCAL-ABC": {"created_local": True, "ten_san_pham": "Đạm Cá 5L", "huong_dan_su_dung": ["20ml cho bình 16 lít"]},
        "LOCAL-OFF": {"created_local": True, "ten_san_pham": "Đã xoá", "hidden": True}}}), encoding="utf-8")
    kb = KnowledgeBase(data / "shared" / "product_knowledge.json", data / "profiles.json",
                       mapping_path=data / "machine" / "catalog_mapping.json")
    records = {r["san_pham_id"]: r for r in kb.all_records()}
    assert set(records) == {"LOCAL-ABC"} and records["LOCAL-ABC"]["ten_san_pham"] == "Đạm Cá 5L"
    kb.apply_mapping({"sku1": "LOCAL-ABC"})
    kb.reload()
    assert kb.match("", product_id="sku1")["huong_dan_su_dung"] == ["20ml cho bình 16 lít"]
