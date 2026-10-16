"""“Bổ sung bằng Google”: Gemini có Google Search grounding (HTTP giả, không gọi API thật)."""
import io
import json
import urllib.error

import pytest

from ai_providers import GeminiProvider
from product_enrich import EnrichError, ProductEnrich, build_proposal, missing_fields


class _Resp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


RAW = json.dumps({
    "thanh_phan_dang_ky": [{"ten": "Abamectin", "ham_luong": "36 g/l"}],
    "cong_dung": ["Trừ sâu cuốn lá, nhện đỏ", "Giá chỉ 50.000đ"],
    "doi_tuong_cay_trong": "không tìm thấy",
    "huong_dan_su_dung": ["Pha 10 ml cho bình 16 lít, phun khi sâu non mới nở"],
    "luu_y": "Đeo khẩu trang khi phun",
    "thoi_gian_cach_ly": "7 ngày",
}, ensure_ascii=False, indent=1)


def _meta(raw, cited=("Abamectin", "Trừ sâu cuốn lá", "Pha 10 ml", "7 ngày", "không tìm thấy")):
    return {
        "webSearchQueries": ["Sâu Xanh 36EC nhãn"],
        "groundingChunks": [{"web": {"uri": "https://nsx.example.vn/sau-xanh", "title": "nsx.example.vn"}},
                            {"web": {"uri": "https://ppd.example.gov.vn/danh-muc", "title": "ppd.example.gov.vn"}}],
        "groundingSupports": [{"segment": {"text": text}, "groundingChunkIndices": [i % 2]}
                              for i, text in enumerate(cited)],
    }


def _payload(raw=RAW, meta=None):
    return {"candidates": [{"content": {"parts": [{"text": raw}]},
                            "groundingMetadata": _meta(raw) if meta is None else meta}]}


PROFILE = {"id": "p1", "ten_san_pham": "Sâu Xanh 36EC", "nha_san_xuat": "Cty A",
           "cong_dung": [], "thanh_phan_dang_ky": {}, "huong_dan_su_dung": None,
           "doi_tuong_cay_trong": ["Lúa"], "luu_y": ""}


def test_proposal_has_sources_and_drops_unsourced_price_and_existing():
    out = build_proposal(PROFILE, RAW, _meta(RAW))
    fields = out["fields"]
    assert fields["thanh_phan_dang_ky"]["value"] == {"Abamectin": "36 g/l"}
    assert fields["thanh_phan_dang_ky"]["sources"][0]["url"] == "https://nsx.example.vn/sau-xanh"
    assert fields["cong_dung"]["value"] == ["Trừ sâu cuốn lá, nhện đỏ"]          # dòng giá bị lọc
    assert "doi_tuong_cay_trong" not in fields                                    # đã có -> không đụng
    assert "Thời gian cách ly: 7 ngày" in fields["luu_y"]["value"]
    assert fields["luu_y"]["sources"]
    assert fields["huong_dan_su_dung"]["old"] is None


def test_unmatched_field_gets_general_sources_and_no_grounding_drops_all():
    meta = _meta(RAW, cited=("Abamectin",))
    out = build_proposal(PROFILE, RAW, meta)
    assert out["fields"]["thanh_phan_dang_ky"]["general_sources"] is False
    assert out["fields"]["cong_dung"]["general_sources"] is True          # có grounding, không gán được
    assert len(out["fields"]["cong_dung"]["sources"]) == 2
    empty = build_proposal(PROFILE, RAW, {})
    assert empty["fields"] == {} and empty["dropped"]["cong_dung"] == "không có nguồn"
    assert empty["diagnostic"]["chunks"] == 0


CELL_RAW = "Đây là kết quả:\n```json\n" + json.dumps({
    "cong_dung": {"gia_tri": ["Trừ nhện đỏ"], "nguon": ["https://www.nsx.example.vn/san-pham"]},
    "huong_dan_su_dung": {"gia_tri": ["Pha 10 ml/bình 16 lít"], "nguon": ["ppd.example.gov.vn"]},
    "luu_y": {"gia_tri": "Đeo găng tay", "nguon": ["khac.example.com"]},
    "thanh_phan_dang_ky": {"gia_tri": "không tìm thấy", "nguon": []},
}, ensure_ascii=False) + "\n```\nHết."


def test_misaligned_supports_fall_back_to_domain_match_and_fenced_json():
    redirect = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/"
    meta = {"groundingChunks": [{"web": {"uri": redirect + "AAA", "title": "nsx.example.vn"}},
                                {"web": {"uri": redirect + "BBB", "title": "ppd.example.gov.vn"}}],
            "groundingSupports": [{"segment": {"startIndex": 99999, "endIndex": 100050, "text": "chữ không có"},
                                   "groundingChunkIndices": [0]}]}
    out = build_proposal(PROFILE, CELL_RAW, meta)
    f = out["fields"]
    assert f["cong_dung"]["value"] == ["Trừ nhện đỏ"]
    assert [s["title"] for s in f["cong_dung"]["sources"]] == ["nsx.example.vn"]
    assert f["cong_dung"]["general_sources"] is False
    assert [s["title"] for s in f["huong_dan_su_dung"]["sources"]] == ["ppd.example.gov.vn"]
    assert f["luu_y"]["general_sources"] is True                          # tên miền không khớp chunk nào
    assert "thanh_phan_dang_ky" not in f
    assert out["diagnostic"] == {"chunks": 2, "supports": 1, "queries": 0}


def test_missing_fields():
    assert missing_fields(PROFILE) == ["thanh_phan_dang_ky", "cong_dung", "huong_dan_su_dung", "luu_y"]


class _Workspace:
    def __init__(self, root, profile):
        self.root = root
        self.profile = dict(profile)
        self.patches = []

    def detail(self, resource, record_id):
        if record_id != self.profile["id"]:
            raise ValueError("workspace_record_not_found")
        return {"data": dict(self.profile)}

    def _profiles(self):
        return [dict(self.profile)]

    def patch(self, resource, record_id, payload):
        self.patches.append(payload)
        self.profile.update(payload["changes"])
        return {"ok": True, "undo_available": True, "revision": "r2"}


class _Engine:
    def __init__(self, root):
        self.workspace = _Workspace(root, PROFILE)


def _service(tmp_path, opener, model="gemini-2.5-flash"):
    gemini = GeminiProvider("test-key", model, opener=opener)
    return ProductEnrich(_Engine(tmp_path), gemini, spawn=lambda fn: fn())


def test_end_to_end_uses_google_search_tool_and_applies_only_chosen(tmp_path):
    bodies = []

    def opener(req, timeout):
        bodies.append(json.loads(req.data.decode("utf-8")))
        return _Resp(_payload())
    service = _service(tmp_path, opener)
    job = service.start("p1")
    assert job["state"] == "running"
    status = service.status("p1")
    assert status["job"]["state"] == "done"
    assert bodies[0]["tools"] == [{"google_search": {}}]
    assert "responseMimeType" not in bodies[0]["generationConfig"]
    assert set(status["proposal"]["fields"]) == {"thanh_phan_dang_ky", "cong_dung", "huong_dan_su_dung", "luu_y"}

    ws = service.engine.workspace
    ws.profile["luu_y"] = "Chủ shop tự ghi"          # điền tay trong lúc chờ -> không ghi đè
    out = service.apply("p1", ["cong_dung", "luu_y"])
    assert out["applied"] == ["cong_dung"] and out["skipped"] == ["luu_y"]
    assert ws.patches[-1]["changes"] == {"cong_dung": ["Trừ sâu cuốn lá, nhện đỏ"]}
    assert "Bổ sung bằng Google" in ws.patches[-1]["reason"]
    assert ws.profile["luu_y"] == "Chủ shop tự ghi"
    left = service.status("p1")["proposal"]["fields"]
    assert "cong_dung" not in left and "thanh_phan_dang_ky" in left
    with pytest.raises(EnrichError):
        service.apply("p1", [])


def test_old_model_uses_search_retrieval_tool(tmp_path):
    bodies = []

    def opener(req, timeout):
        bodies.append(json.loads(req.data.decode("utf-8")))
        return _Resp(_payload())
    _service(tmp_path, opener, "gemini-1.5-flash").run_one("p1")
    assert bodies[0]["tools"] == [{"google_search_retrieval": {}}]


def test_model_without_search_support_reports_clear_error(tmp_path):
    def opener(req, timeout):
        body = json.dumps({"error": {"message": "Search Grounding is not supported for this model."}}).encode()
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(body))
    service = _service(tmp_path, opener)
    service.start("p1")
    job = service.status("p1")["job"]
    assert job["state"] == "error"
    assert "không hỗ trợ tra Google" in job["error"]


def test_missing_key_and_quota_errors(tmp_path):
    service = ProductEnrich(_Engine(tmp_path), GeminiProvider(""), spawn=lambda fn: fn())
    with pytest.raises(EnrichError, match="Chưa có khóa Gemini"):
        service.start("p1")

    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, io.BytesIO(b"{}"))
    service = _service(tmp_path, opener)
    service.engine.workspace.profile["id"] = "p1"
    batch = service.start_batch()
    assert batch["state"] == "done" and batch["results"][0]["ok"] is False
    assert "Hết lượt" in batch["results"][0]["error"]


def test_batch_orders_by_missing_count_and_cancels(tmp_path):
    service = _service(tmp_path, lambda req, timeout: _Resp(_payload()))
    ws = service.engine.workspace
    full = {**PROFILE, "id": "full", "cong_dung": ["x"], "thanh_phan_dang_ky": {"a": "1"},
            "huong_dan_su_dung": ["x"], "luu_y": "x"}
    few = {**PROFILE, "id": "few", "cong_dung": ["x"], "thanh_phan_dang_ky": {"a": "1"}, "luu_y": "x"}
    rows = {"p1": PROFILE, "full": full, "few": few}
    ws._profiles = lambda: [dict(r) for r in rows.values()]
    ws.detail = lambda resource, rid: {"data": dict(rows[rid])}
    order = []
    original = service.run_one
    service.run_one = lambda pid: (order.append(pid), original(pid))[1]
    out = service.start_batch()
    assert out["total"] == 2 and order == ["p1", "few"]
    pending = []
    service._spawn = pending.append
    service.start_batch(["p1", "few"])
    service.cancel_batch()
    pending[0]()
    assert service.batch_status()["state"] == "cancelled"
