"""Gói C: sổ tay tình huống — seed, validate, khớp dấu hiệu, nền cố định, endpoint GET/PUT."""
import copy
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

import context_budget as cb
import sales_playbook as sp
from server import Handler, ReplyEngine

SEED = Path(__file__).resolve().parents[1] / "prompts" / "sales_playbook.default.json"
REQUIRED = {"ban_chiu", "tra_gop", "hang_that_gia", "ai_hay_nguoi", "live_hay_phat_lai", "che_dat_xin_giam",
            "ship_lau", "doi_tra_hu_hong", "shop_o_dau", "noi_that_khong", "khen_treu_nguoi_dan",
            "cong_kich_lua_dao"}
ENTRY = {"id": "mau", "tinh_huong": "Mẫu", "dau_hieu": ["một cụm"], "huong_xu_ly": "Xử lý khéo",
         "du_kien_duoc_dung": ["thong_tin_shop.giao_hang"], "cam": ["không bịa"]}


@pytest.fixture
def path(tmp_path, monkeypatch):
    target = tmp_path / "shared" / "sales_playbook.json"
    monkeypatch.setattr(sp, "PLAYBOOK_PATH", target)
    monkeypatch.setattr(cb, "FLAGS_PATH", tmp_path / "no_flags.json")
    sp._cache["by_key"] = {}
    return target


def seeded(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SEED.read_text(encoding="utf-8"), encoding="utf-8")


# ------------------------------------------------------------------ seed
def test_seed_has_all_required_entries_and_is_valid():
    entries = sp.validate(json.loads(SEED.read_text(encoding="utf-8")))
    assert {e["id"] for e in entries} == REQUIRED
    assert [e["id"] for e in entries] == sorted(e["id"] for e in entries)
    for entry in entries:
        assert entry["dau_hieu"] and entry["huong_xu_ly"] and entry["tinh_huong"]
    chiu = next(e for e in entries if e["id"] == "ban_chiu")
    text = chiu["huong_xu_ly"].lower()
    assert "cod" in text and "xem hàng" in text           # thực chất là COD, không hứa nợ thật
    assert any("không hứa" in c for c in chiu["cam"])
    live = next(e for e in entries if e["id"] == "live_hay_phat_lai")
    assert any("phát lại" in c for c in live["cam"])      # phản chiếu quy tắc đã học
    ai = next(e for e in entries if e["id"] == "ai_hay_nguoi")
    assert any("không nói dối" in c for c in ai["cam"])


def test_stable_view_is_compact_sorted_and_byte_stable(path):
    seeded(path)
    first = json.dumps(sp.stable_view(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    second = json.dumps(sp.stable_view(list(reversed(sp.load()))), ensure_ascii=False, separators=(",", ":"),
                        sort_keys=True)
    assert first == second
    assert len(first) < 3200                              # ~1.1k token theo ước lượng 2,6 ký tự/token
    view = sp.stable_view()
    assert [r["id"] for r in view] == sorted(r["id"] for r in view)
    assert all(set(r) <= {"id", "xu_ly", "duoc_dung", "cam"} for r in view)


# ------------------------------------------------------------------ validate
def test_validate_accepts_and_normalizes():
    out = sp.validate({"entries": [ENTRY, {**ENTRY, "id": "a_mau", "du_kien_duoc_dung": "một chuỗi", "cam": []}]})
    assert [e["id"] for e in out] == ["a_mau", "mau"] and out[0]["du_kien_duoc_dung"] == ["một chuỗi"]
    assert sp.validate([ENTRY])[0]["id"] == "mau"


@pytest.mark.parametrize("mutate,code", [
    (lambda e: e.pop("id"), "playbook_id_invalid"),
    (lambda e: e.update(id="Có Dấu"), "playbook_id_invalid"),
    (lambda e: e.update(id="x" * 41), "playbook_id_invalid"),
    (lambda e: e.update(dau_hieu=[]), "playbook_dau_hieu_invalid"),
    (lambda e: e.update(dau_hieu=["x" * 41]), "playbook_dau_hieu_invalid"),
    (lambda e: e.update(dau_hieu=["a"] * 17 + ["b"]), "playbook_dau_hieu_invalid"),
    (lambda e: e.update(huong_xu_ly=""), "playbook_huong_xu_ly_invalid"),
    (lambda e: e.update(huong_xu_ly="x" * 501), "playbook_huong_xu_ly_invalid"),
    (lambda e: e.update(tinh_huong=5), "playbook_tinh_huong_invalid"),
    (lambda e: e.update(cam=["x"] * 7), "playbook_cam_invalid"),
    (lambda e: e.update(du_kien_duoc_dung=[1]), "playbook_du_kien_invalid"),
])
def test_validate_rejects_bad_entries(mutate, code):
    entry = copy.deepcopy(ENTRY)
    mutate(entry)
    with pytest.raises(sp.PlaybookError, match=code):
        sp.validate({"entries": [entry]})


def test_validate_rejects_shape_duplicates_count_and_size():
    for bad in (None, "x", {"entries": "x"}, {"entries": [5]}):
        with pytest.raises(sp.PlaybookError):
            sp.validate(bad)
    with pytest.raises(sp.PlaybookError, match="duplicate"):
        sp.validate({"entries": [ENTRY, ENTRY]})
    with pytest.raises(sp.PlaybookError, match="too_many"):
        sp.validate({"entries": [{**ENTRY, "id": f"m{i}"} for i in range(sp.MAX_ENTRIES + 1)]})
    big = [{**ENTRY, "id": f"m{i}", "huong_xu_ly": "x" * 500,
            "dau_hieu": [f"{k:02d}" + "d" * 38 for k in range(16)],
            "cam": [f"{k}" + "y" * 119 for k in range(6)],
            "du_kien_duoc_dung": [f"{k}" + "z" * 159 for k in range(6)]} for i in range(sp.MAX_ENTRIES)]
    with pytest.raises(sp.PlaybookError, match="too_large"):
        sp.validate({"entries": big})


# ------------------------------------------------------------------ load / save
def test_missing_data_file_uses_shipped_default(path):
    assert not path.exists()
    assert {e["id"] for e in sp.load()} == REQUIRED
    assert set(sp.stable_context()) == {"sales_playbook"}


def test_corrupt_data_file_falls_back_to_default_and_logs(path, caplog):
    path.parent.mkdir(parents=True)
    for bad in ("{không phải json", json.dumps({"entries": [{"id": "x"}]})):
        path.write_text(bad, encoding="utf-8")
        with caplog.at_level("WARNING", logger="kid-te.core"):
            assert {e["id"] for e in sp.load()} == REQUIRED
        assert "mặc định" in caplog.text
        caplog.clear()


def test_valid_data_file_wins_over_default_and_put_never_touches_default(path):
    sp.save({"entries": [ENTRY]})
    assert [e["id"] for e in sp.load()] == ["mau"]
    assert not sp.DEFAULT_PATH.samefile(path)
    assert {e["id"] for e in sp.load(sp.DEFAULT_PATH)} == REQUIRED


def test_default_missing_or_corrupt_too_gives_empty_never_crashes(path, tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "DEFAULT_PATH", tmp_path / "nope.json")
    assert sp.load() == [] and sp.stable_context() == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    monkeypatch.setattr(sp, "DEFAULT_PATH", bad)
    assert sp.load() == []


def test_save_is_atomic_and_reloads(path, monkeypatch):
    sp.save({"entries": [ENTRY]})
    assert not path.with_name(path.name + ".tmp").exists()
    assert [e["id"] for e in sp.load()] == ["mau"]
    before = path.read_text(encoding="utf-8")
    with pytest.raises(sp.PlaybookError):
        sp.save({"entries": [{**ENTRY, "dau_hieu": []}]})
    assert path.read_text(encoding="utf-8") == before   # sai thì file cũ còn nguyên
    sp.save({"entries": [ENTRY, {**ENTRY, "id": "hai"}]})
    assert [e["id"] for e in sp.load()] == ["hai", "mau"]


# ------------------------------------------------------------------ khớp dấu hiệu
@pytest.mark.parametrize("text,expected", [
    ("có bán chịu không em", ["ban_chiu"]), ("ban chiu duoc ko", ["ban_chiu"]),
    ("đắt quá bớt chút đi", ["che_dat_xin_giam"]), ("chốt đặt 2 chai", []),   # "đặt" không phải "đắt"
    ("bot hả", ["ai_hay_nguoi"]), ("live hay phát lại vậy", ["live_hay_phat_lai"]),
    ("hàng thật không shop", ["hang_that_gia"]), ("hello", []), ("chị xinh quá", ["khen_treu_nguoi_dan"]),
    ("shop ở đâu vậy", ["shop_o_dau"]), ("ship lâu quá", ["ship_lau"]), ("bị hỏng rồi đổi trả", ["doi_tra_hu_hong"]),
    ("kích rễ pha sao", []), ("", []),
])
def test_match_dau_hieu(path, text, expected):
    seeded(path)
    assert sp.match(text) == expected


def test_match_loose_only_when_router_flags_objection(path):
    seeded(path)
    text = "giá nhà mình sao mà cao vậy"
    assert sp.match(text) == []
    entries = [{**ENTRY, "dau_hieu": ["giá cao"]}]
    assert sp.match(text, entries=entries) == []
    assert sp.match(text, ["objection"], entries=entries) == ["mau"]
    assert sp.match(text, ["greeting"], entries=entries) == []


def test_hint_ids_respects_flag_and_caps(path, monkeypatch, tmp_path):
    seeded(path)
    assert sp.hint_ids(["xin trả góp hay bán chịu hay đắt hay ship lâu"]) == ["ban_chiu", "che_dat_xin_giam",
                                                                           "ship_lau"]
    flags = tmp_path / "f.json"
    flags.write_text(json.dumps({"playbook": False}), encoding="utf-8")
    monkeypatch.setattr(cb, "FLAGS_PATH", flags)
    assert sp.hint_ids(["bán chịu"]) == [] and sp.stable_context() == {}


def test_stable_context_policy_only_with_prompt_override(path):
    seeded(path)
    assert set(sp.stable_context(False)) == {"sales_playbook"}
    assert set(sp.stable_context(True)) == {"sales_playbook", "the_khach_so_tay_policy"}


def test_add_card_and_hint_never_raises(path, monkeypatch):
    seeded(path)
    out = sp.add_card_and_hint({}, {"quen_la": "quen"}, ["bán chịu không"])
    assert out["the_khach"] == {"quen_la": "quen"} and out["tinh_huong_phu_hop"] == ["ban_chiu"]
    monkeypatch.setattr(sp, "hint_ids", lambda *a, **k: 1 / 0)
    assert sp.add_card_and_hint({}, None, ["x"]) == {}


# ------------------------------------------------------------------ endpoint
def call(port, method, route, body=None, origin="", raw=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    request = urllib.request.Request(f"http://127.0.0.1:{port}{route}", method=method, data=data,
                                     headers={"Content-Type": "application/json", "Origin": origin})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture
def server(path):
    Handler.engine = SimpleNamespace(
        sales_playbook_state=lambda: ReplyEngine.sales_playbook_state(None),
        sales_playbook_save=lambda payload: ReplyEngine.sales_playbook_save(None, payload))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, args=(0.1,), daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def test_endpoint_get_put_roundtrip_and_validation(server, path):
    seeded(path)
    status, body = call(server, "GET", "/api/workspace/sales-playbook")
    assert status == 200 and body["ok"] and {e["id"] for e in body["entries"]} == REQUIRED
    assert body["max_entries"] == sp.MAX_ENTRIES
    entries = body["entries"] + [{**ENTRY, "id": "moi", "huong_xu_ly": "Cách mới"}]
    status, saved = call(server, "PUT", "/api/workspace/sales-playbook", {"entries": entries})
    assert status == 200 and len(saved["entries"]) == len(REQUIRED) + 1
    assert any(e["id"] == "moi" for e in sp.load())
    before = path.read_text(encoding="utf-8")
    for bad in ({"entries": [{**ENTRY, "dau_hieu": []}]}, {"entries": "x"}, {"entries": [ENTRY, ENTRY]}):
        status, body = call(server, "PUT", "/api/workspace/sales-playbook", bad)
        assert status == 400 and body["ok"] is False and body["error"].startswith("playbook_")
    assert call(server, "PUT", "/api/workspace/sales-playbook", raw=b"{bad")[0] == 400
    assert call(server, "PUT", "/api/workspace/sales-playbook", raw=b"[1]")[0] == 400
    assert call(server, "PUT", "/api/workspace/sales-playbook", raw=b"")[0] == 400
    assert call(server, "PUT", "/api/workspace/sales-playbook", raw=b" " * 200_001)[0] == 400
    assert path.read_text(encoding="utf-8") == before


def test_endpoint_is_local_only_and_put_has_no_other_route(server):
    ext = "chrome-extension://abc"
    assert call(server, "GET", "/api/workspace/sales-playbook", origin=ext)[0] == 403
    assert call(server, "PUT", "/api/workspace/sales-playbook", {"entries": [ENTRY]}, origin=ext)[0] == 403
    assert call(server, "PUT", "/api/workspace/other", {"entries": []})[0] == 403
    assert call(server, "PUT", "/api/other", {})[0] == 404


def test_attack_hint_is_exclusive():
    import sales_playbook as sp
    entries = [{"id": "cong_kich_lua_dao", "dau_hieu": ["lừa", "ko tặng"], "tinh_huong": "x", "huong_xu_ly": "y",
                "du_kien_duoc_dung": [], "cam": []},
               {"id": "noi_that_khong", "dau_hieu": ["ko tặng"], "tinh_huong": "x", "huong_xu_ly": "y",
                "du_kien_duoc_dung": [], "cam": []}]
    orig = sp.match
    sp.match = lambda text, acts=(), entries_=entries, limit=3: orig(text, acts, entries_, limit)
    try:
        assert sp.hint_ids(["Bán lừa ngta ko tặng"]) == ["cong_kich_lua_dao"]
    finally:
        sp.match = orig
