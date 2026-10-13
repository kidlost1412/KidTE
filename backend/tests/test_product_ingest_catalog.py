"""Thêm sản phẩm bằng AI: danh mục nhiều sản phẩm, chọn nhà cung cấp, tệp lớn tải thô, lỗi 413 đọc được.

Không gọi mạng: nhà cung cấp là đồ giả.
"""
import base64
import io
import json
import sys
import threading
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import product_ingest as pi  # noqa: E402
from test_product_ingest import FakeProvider, good_json, text_payload  # noqa: E402
from test_profile_approval import _engine  # noqa: E402


def b64(data):
    return base64.b64encode(data).decode()


def png_bytes():
    out = io.BytesIO()
    Image.new("RGB", (300, 200), (10, 120, 30)).save(out, "PNG")
    return out.getvalue()


def _flat(*names, **extra):
    items = []
    for name in names:
        items.append({"ten_san_pham": name, "nha_san_xuat": "Cty ABC", "quy_cach_dong_goi": "30gr",
                      "cong_dung": ["Kích rễ"], "huong_dan_su_dung": ["Pha 20ml cho bình 16 lít"],
                      "thanh_phan_dang_ky": [{"ten": "Lân", "ham_luong": "10%"}], "luu_y": None,
                      "nguon": "n1", "can_xem": ["nha_san_xuat"], **extra})
    return json.dumps({"san_pham": items}, ensure_ascii=False)


# ---------------------------------------------------------------- nhiều sản phẩm
def test_validate_products_catalog_lists_every_product_with_flat_fields():
    out = pi.validate_products(_flat("NANO LÂN", "NANO KALI", "nano lân"), ["n1"])
    names = [p["fields"]["ten_san_pham"] for p in out["products"]]
    assert names == ["NANO LÂN", "NANO KALI"]                  # trùng tên (khác hoa thường) gộp một
    first = out["products"][0]
    assert first["fields"]["thanh_phan_dang_ky"] == {"Lân": "10%"}
    assert first["nguon"]["ten_san_pham"] == "n1"
    assert "nha_san_xuat" in first["uncertain"]                 # model đánh dấu can_xem
    assert "ten_san_pham" not in first["uncertain"] and "luu_y" in first["uncertain"]
    assert out["truncated"] is False


def test_validate_products_accepts_legacy_cells_single_and_bare_object():
    one = pi.validate_products(good_json(), ["n1"])
    assert len(one["products"]) == 1 and one["products"][0]["fields"]["ten_san_pham"] == "Đạm Cá Amino 5L"
    bare = pi.validate_products(json.dumps({"ten_san_pham": "Kali 1kg", "cong_dung": "Nuôi trái"}))
    assert bare["products"][0]["fields"]["cong_dung"] == ["Nuôi trái"]
    listed = pi.validate_products(json.dumps([{"ten_san_pham": "A1"}, {"ten_san_pham": "B2"}]))
    assert len(listed["products"]) == 2


def test_validate_products_salvages_truncated_json_and_drops_nameless():
    full = _flat("SP Một", "SP Hai", "SP Ba")
    cut = full[:full.rindex('{"ten_san_pham": "SP Ba"') + 40]     # cắt giữa sản phẩm thứ ba
    out = pi.validate_products(cut, ["n1"])
    assert [p["fields"]["ten_san_pham"] for p in out["products"]] == ["SP Một", "SP Hai"]
    assert out["truncated"] is True
    mixed = json.dumps({"san_pham": [{"ten_san_pham": None, "cong_dung": ["x"]}, {"ten_san_pham": "Có tên"}]})
    assert [p["fields"]["ten_san_pham"] for p in pi.validate_products(mixed)["products"]] == ["Có tên"]
    with pytest.raises(pi.ExtractionInvalid):
        pi.validate_products(json.dumps({"san_pham": [{"ten_san_pham": None}]}))
    with pytest.raises(pi.ExtractionInvalid):
        pi.validate_products("không có json")


def test_validate_products_prices_still_scrubbed_per_product():
    raw = json.dumps({"san_pham": [{"ten_san_pham": "Nano Lân", "cong_dung": ["Kích rễ", "Giá bán 150.000đ"],
                                    "luu_y": "Giảm giá 20%"}]})
    p = pi.validate_products(raw)["products"][0]
    assert p["fields"]["cong_dung"] == ["Kích rễ"] and p["fields"]["luu_y"] is None and p["filtered_price"] is True


def test_extract_returns_products_list_and_first_product_at_root():
    prov = FakeProvider("gemini", [_flat("NANO LÂN", "NANO KALI", "ĐẠM CÁ")])
    out = pi.ProductIngest(None, prov, None).extract(text_payload("x" * 40))
    assert [p["fields"]["ten_san_pham"] for p in out["products"]] == ["NANO LÂN", "NANO KALI", "ĐẠM CÁ"]
    assert out["fields"]["ten_san_pham"] == "NANO LÂN"           # tương thích: sản phẩm đầu ở gốc
    assert "MỌI sản phẩm" in prov.calls[0]["system"] and "danh sách" in prov.calls[0]["user"]


# ---------------------------------------------------------------- nhà cung cấp
def test_provider_choice_gemini_deepseek_and_image_rule():
    gem = FakeProvider("gemini", [good_json()])
    deep = FakeProvider("deepseek", [good_json()], supports_files=False)
    ingest = pi.ProductIngest(None, gem, deep)
    out = ingest.extract({**text_payload(), "provider": "deepseek"})
    assert out["provider"] == "deepseek" and not gem.calls          # chọn DeepSeek dù Gemini có khoá
    with pytest.raises(pi.IngestError, match="DeepSeek chỉ đọc chữ"):
        ingest.extract({"images": [{"name": "a.png", "data": b64(png_bytes())}], "provider": "deepseek"})
    out = ingest.extract({**text_payload(), "provider": "gemini"})
    assert out["provider"] == "gemini"
    deep.configured = False
    with pytest.raises(pi.IngestError, match="khóa DeepSeek"):
        ingest.extract({**text_payload(), "provider": "deepseek"})
    gem.configured = False
    with pytest.raises(pi.IngestError, match="khóa Gemini"):
        ingest.extract({**text_payload(), "provider": "gemini"})
    with pytest.raises(pi.IngestError, match="không hợp lệ"):
        ingest.extract({**text_payload(), "provider": "gpt"})


def test_deepseek_rejects_scanned_pdf():
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    buf = io.BytesIO()
    writer.write(buf)
    deep = FakeProvider("deepseek", [good_json()], supports_files=False)
    ingest = pi.ProductIngest(None, FakeProvider("gemini", []), deep)
    with pytest.raises(pi.IngestError, match="PDF scan"):
        ingest.extract({"files": [{"name": "scan.pdf", "data": b64(buf.getvalue())}], "provider": "deepseek"})


# ---------------------------------------------------------------- tệp lớn
def _text_pdf(text, pad_bytes=0):
    """PDF tối thiểu có chữ; pad_bytes > 0 thêm luồng rác để PDF nặng như danh mục nhiều ảnh."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>"]
    stream = f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode("latin-1")
    objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    if pad_bytes:
        objects.append(b"<< /Length %d >>\nstream\n" % pad_bytes + b"\0" * pad_bytes + b"\nendstream")
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _docx(paragraphs, pad_bytes=0):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
        z.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')
        if pad_bytes:
            z.writestr("word/media/image1.bin", b"\0" * pad_bytes)
    return buf.getvalue()


def test_text_pdf_and_docx_fixtures_extract_with_vietnamese_names():
    pdf = _text_pdf("Nano Lan sieu ra re. Cong dung kich re cho cay mai vang.")
    docx = _docx(["Kiến thức phân bón: NANO KALI giúp chắc hạt.", "Đạm cá hữu cơ kích rễ."])
    got = pi.prepare_sources({"files": [{"name": "DANH MỤC SP CÂY MAI VÀNG.pdf", "data": b64(pdf)},
                                        {"name": "Kiến thức phấn bón và sản phẩm.docx", "data": b64(docx)}]})
    assert [s["kind"] for s in got] == ["file", "file"]
    assert "Nano Lan" in got[0]["text"] and "NANO KALI" in got[1]["text"]
    assert got[0]["label"] == "DANH MỤC SP CÂY MAI VÀNG.pdf"


def test_upload_staging_resolves_ids_expires_and_is_bounded(monkeypatch):
    clock = [1000.0]
    ingest = pi.ProductIngest(None, FakeProvider("gemini", [_flat("A1", "B2")]), None, clock=lambda: clock[0])
    meta = ingest.add_upload("DANH MỤC SP CÂY MAI VÀNG.pdf", _text_pdf("Nano Lan sieu ra re cong dung kich re"))
    out = ingest.extract({"files": [{"name": meta["name"], "upload_id": meta["upload_id"]}]})
    assert [p["fields"]["ten_san_pham"] for p in out["products"]] == ["A1", "B2"]
    assert out["sources"][0]["label"] == "DANH MỤC SP CÂY MAI VÀNG.pdf"
    with pytest.raises(pi.IngestError, match="hết hạn"):
        ingest.extract({"files": [{"name": "x.pdf", "upload_id": "khong-co"}]})
    clock[0] += pi.UPLOAD_TTL + 1
    ingest.add_upload("moi.txt", b"x" * 100)
    assert len(ingest._uploads) == 1                              # tệp cũ hết hạn bị dọn
    with pytest.raises(pi.IngestError, match="rỗng"):
        ingest.add_upload("rong.txt", b"")
    monkeypatch.setattr(pi, "MAX_UPLOADS_BYTES", 250)
    for i in range(5):
        clock[0] += 1
        ingest.add_upload(f"f{i}.txt", b"y" * 100)
    assert sum(len(v["data"]) for v in ingest._uploads.values()) <= 250


def test_vision_payload_over_gemini_limit_is_refused_with_reason(monkeypatch):
    monkeypatch.setattr(pi, "MAX_VISION_BYTES", 10)
    ingest = pi.ProductIngest(None, FakeProvider("gemini", [good_json()]), None)
    with pytest.raises(pi.IngestError, match="15 MB"):
        ingest.extract({"images": [{"name": "a.png", "data": b64(png_bytes())}]})


# ---------------------------------------------------------------- HTTP
def _serve(tmp_path, provider):
    import server
    engine = _engine(tmp_path)
    engine.ingest = pi.ProductIngest(engine, provider, None)

    class Handler(server.Handler):
        pass
    Handler.engine = engine
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
    worker.start()
    return engine, http, worker, f"http://127.0.0.1:{http.server_port}"


def test_http_oversize_json_gets_readable_413_not_a_dropped_connection(tmp_path):
    """Gốc rễ "Mất kết nối": thân > 40 MB bị từ chối TRƯỚC khi đọc nên kết nối bị cắt khi trình duyệt còn đang gửi."""
    import http.client as httpclient
    engine, http, worker, origin = _serve(tmp_path, FakeProvider("gemini", [good_json()]))
    try:
        conn = httpclient.HTTPConnection("127.0.0.1", http.server_port, timeout=30)
        conn.putrequest("POST", "/api/ingest/extract")
        for key, value in (("Content-Type", "application/json"), ("Origin", origin),
                           ("Content-Length", str(41 * 1024 * 1024))):
            conn.putheader(key, value)
        conn.endheaders()
        chunk = b"x" * (1024 * 1024)
        for _ in range(41):
            conn.send(chunk)                                      # vẫn gửi hết thân như trình duyệt
        response = conn.getresponse()
        payload = json.loads(response.read())
        assert response.status == 413 and payload["ok"] is False and "quá lớn" in payload["error"]
        conn.close()
    finally:
        http.shutdown()
        http.server_close()
        worker.join()


def test_http_large_vietnamese_named_files_upload_then_extract_catalog(tmp_path):
    """Tái hiện máy chủ shop: PDF danh mục > 40 MB + DOCX tên tiếng Việt, tải thô rồi đọc ra nhiều sản phẩm."""
    from urllib.parse import quote
    from urllib.request import Request, urlopen
    provider = FakeProvider("gemini", [_flat("NANO LÂN", "NANO KALI", "ĐẠM CÁ AMINO")])
    engine, http, worker, origin = _serve(tmp_path, provider)

    def post(path, data, ctype):
        req = Request(origin + path, data=data, method="POST", headers={"Content-Type": ctype, "Origin": origin})
        with urlopen(req, timeout=60) as response:
            return json.loads(response.read())
    try:
        pdf = _text_pdf("Nano Lan sieu ra re. Cong dung kich re cho cay mai vang.", pad_bytes=48 * 1024 * 1024)
        docx = _docx(["Kiến thức phấn bón và sản phẩm: NANO KALI chắc hạt.", "Đạm cá amino kích rễ."],
                     pad_bytes=3 * 1024 * 1024)
        assert len(pdf) > 40 * 1024 * 1024                         # to hơn giới hạn JSON cũ 40 MB
        names = ["DANH MỤC SP CÂY MAI VÀNG.pdf", "Kiến thức phấn bón và sản phẩm.docx"]
        ups = [post("/api/ingest/upload?name=" + quote(n), blob, "application/octet-stream")
               for n, blob in zip(names, (pdf, docx))]
        assert [u["name"] for u in ups] == names and all(u["ok"] for u in ups)
        out = post("/api/ingest/extract", json.dumps({
            "files": [{"name": u["name"], "upload_id": u["upload_id"]} for u in ups], "provider": "gemini"}).encode(),
            "application/json")
        assert [p["fields"]["ten_san_pham"] for p in out["products"]] == ["NANO LÂN", "NANO KALI", "ĐẠM CÁ AMINO"]
        assert [s["label"] for s in out["sources"]] == names
        assert "NANO KALI" in provider.calls[0]["user"] and "Nano Lan" in provider.calls[0]["user"]
        drafts = [post("/api/ingest/draft", json.dumps({"fields": p["fields"], "session_id": out["session_id"]}).encode(),
                       "application/json") for p in out["products"][:2]]
        assert len({d["id"] for d in drafts}) == 2
        assert all(d["status"] == "draft" and not d["approved_for_live"] for d in drafts)
    finally:
        http.shutdown()
        http.server_close()
        worker.join()


def test_http_upload_rejects_empty_and_unauthorised(tmp_path):
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen
    engine, http, worker, origin = _serve(tmp_path, FakeProvider("gemini", []))
    try:
        for data, origin_header, code in ((b"", origin, 400), (b"abc", "https://other.example", 403)):
            req = Request(origin + "/api/ingest/upload?name=a.txt", data=data, method="POST",
                          headers={"Origin": origin_header})
            with pytest.raises(HTTPError) as err:
                urlopen(req, timeout=10)
            assert err.value.code == code
    finally:
        http.shutdown()
        http.server_close()
        worker.join()


# ---------------------------------------------------------------- hồ sơ bị xoá hàng loạt: dữ liệu còn nguyên trong Thùng rác
def test_bulk_deleted_profiles_empty_the_mapping_dropdown_until_restored(tmp_path):
    engine = _engine(tmp_path)
    before = {p["id"] for p in engine.mapping_state()["profiles"]}
    assert len(before) >= 2
    for record_id in sorted(before):
        engine.workspace.delete("profiles", record_id, {"reason": "test"})
    assert engine.mapping_state()["profiles"] == []                 # đúng triệu chứng: ô chọn chỉ còn "Chưa gắn hồ sơ"
    trash = engine.workspace_trash({"resource": "profiles", "limit": 100})
    assert {t["record_id"] for t in trash["items"]} == before       # dữ liệu còn nguyên, khôi phục được
    for record_id in sorted(before):
        engine.workspace_restore("profiles", record_id)
    assert {p["id"] for p in engine.mapping_state()["profiles"]} == before
