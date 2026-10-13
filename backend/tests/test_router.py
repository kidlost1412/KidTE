"""Gói B: router nhỏ trước khi định tuyến — kiểm tra đầu ra, ưu tiên mã, làn, fail-open, Shopee."""
import json
import time
from types import SimpleNamespace

import pytest

import context_budget as cb
import ctx_harness as h
import router
import server as server_module
from shopee_ai import ShopeeAiEngine

CATALOG = [("p1", "Kích Rễ TC01"), ("p2", "Nano Tonic"), ("p3", "Đạm Cá 5 Lít")]
IDS = {"p1", "p2", "p3"}


def answer(**kw):
    base = {"acts": ["question"], "tone": "neutral", "product_ids": [], "topics": [], "clarify": False,
            "confidence": 0.9}
    base.update(kw)
    return base


def resp(content, usage=None):
    msg = SimpleNamespace(content=content if isinstance(content, str) else json.dumps(content))
    u = SimpleNamespace(prompt_tokens=300, completion_tokens=20, prompt_cache_hit_tokens=250, **(usage or {}))
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=u)


def write_cfg(monkeypatch, tmp_path, **values):
    path = tmp_path / "router.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setattr(router, "CONFIG_PATH", path)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "CONFIG_PATH", tmp_path / "missing_router.json")
    monkeypatch.setattr(cb, "FLAGS_PATH", tmp_path / "none.json")


# ------------------------------------------------------------ validate / route
def test_validate_rejects_bad_json_and_shapes():
    for raw in ("not json", "[1]", "{}", json.dumps({"acts": "x"}), None):
        assert router.validate(raw, IDS) is None


def test_validate_drops_foreign_ids_extra_topics_and_clamps():
    out = router.validate(json.dumps(answer(product_ids=["p1", "zzz", "p1", "p2", "p3", "p4"],
                                            topics=["price", "weather", "dose"], confidence=7,
                                            acts=["greeting", "nonsense", "question"], tone="angry")), IDS)
    assert out["product_ids"] == ["p1", "p2", "p3"] and out["topics"] == ["price", "dose"]
    assert out["confidence"] == 1.0 and out["acts"] == ["greeting", "question"] and out["tone"] == "neutral"
    assert out["kind"] == "product" and not out["pure_smalltalk"]
    assert router.validate(json.dumps(answer(confidence="high")), IDS)["confidence"] == 0.0


def test_validate_pure_smalltalk_only_for_light_acts_and_legacy_kind():
    assert router.validate(json.dumps(answer(acts=["greeting", "thanks"])), IDS)["pure_smalltalk"]
    assert not router.validate(json.dumps(answer(acts=["greeting", "question"])), IDS)["pure_smalltalk"]
    assert not router.validate(json.dumps(answer(acts=["banter", "objection"])), IDS)["pure_smalltalk"]
    assert not router.validate(json.dumps(answer(acts=[])), IDS)["pure_smalltalk"]
    legacy = router.validate(json.dumps({"kind": "smalltalk", "confidence": 0.8}), IDS)
    assert legacy["kind"] == "smalltalk" and legacy["acts"] == ["banter"]


def test_route_request_shape_and_meta():
    seen = []

    def call(request):
        seen.append(request)
        return resp(answer(product_ids=["p2"]))
    meta = {}
    out = router.route(call, "giá nano", catalog_names=CATALOG, recent_turns=[{"comment": "a", "response": "b"}] * 5,
                       pinned="p1", platform="tiktok", model="m", meta=meta)
    request = seen[0]
    assert out["product_ids"] == ["p2"] and request["max_tokens"] <= 120 and request["timeout"] == 4.0
    assert request["response_format"] == {"type": "json_object"}
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert request["messages"][-1]["content"].count("Khách:") == 2   # tối đa 2 lượt
    assert meta["router_prompt_tokens"] == 300 and meta["router_cache_hit_tokens"] == 250
    assert meta["router_completion_tokens"] == 20 and "router_ms" in meta


def test_catalog_block_is_byte_identical_across_turns():
    sent = []

    def call(request):
        sent.append(request["messages"])
        return resp(answer())
    shuffled = list(reversed(CATALOG))
    router.route(call, "câu một", catalog_names=CATALOG, pinned="p1")
    router.route(call, "câu hai khác hẳn", catalog_names=shuffled, pinned="p2",
                 recent_turns=[{"comment": "x", "response": "y"}])
    assert sent[0][0] == sent[1][0] and sent[0][1] == sent[1][1]
    assert sent[0][1]["content"].index("p1|") < sent[0][1]["content"].index("p3|")


def test_route_timeout_error_and_custom_client_flags():
    def slow(request):
        time.sleep(1.0)
        return resp(answer())
    started = time.monotonic()
    assert router.route(slow, "hi", catalog_names=CATALOG, timeout=0.2) is None
    assert time.monotonic() - started < 0.8

    def boom(request):
        raise RuntimeError("down")
    assert router.route(boom, "hi", catalog_names=CATALOG) is None
    seen = []
    router.route(lambda r: seen.append(r) or resp("garbage"), "hi", catalog_names=CATALOG, custom=True)
    assert "extra_body" not in seen[0]


# ------------------------------------------------------------ cấu hình
def test_config_missing_is_disabled_and_parses_custom_endpoint(monkeypatch, tmp_path):
    assert router.load_config()["enabled"] is False and not router.enabled()
    write_cfg(monkeypatch, tmp_path, enabled=True, base_url="http://localhost:11434/v1", model="qwen2.5:3b",
              timeout_s=2, api_key_env="LOCAL_KEY")
    cfg = router.load_config()
    assert cfg == {"enabled": True, "base_url": "http://localhost:11434/v1", "model": "qwen2.5:3b",
                   "timeout_s": 2.0, "api_key_env": "LOCAL_KEY"}
    write_cfg(monkeypatch, tmp_path, enabled="yes", timeout_s=999, base_url=5)
    cfg = router.load_config()
    assert cfg["enabled"] is False and cfg["timeout_s"] == 4.0 and cfg["base_url"] == ""
    (tmp_path / "router.json").write_text("{bad", encoding="utf-8")
    assert router.load_config()["enabled"] is False


def test_make_call_reuses_main_client_or_builds_custom(monkeypatch):
    sink = []
    main = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: sink.append(kw) or resp(answer()))))
    responder = SimpleNamespace(client=main, model="deepseek-flash")
    call, model, custom = router.make_call(responder, dict(router.DEFAULT_CONFIG))
    assert model == "deepseek-flash" and not custom
    call({"x": 1})
    assert sink == [{"x": 1}]
    assert router.make_call(SimpleNamespace(client=None, model="m"), dict(router.DEFAULT_CONFIG)) is None
    monkeypatch.setenv("LOCAL_KEY", "abc")
    call, model, custom = router.make_call(responder, {**router.DEFAULT_CONFIG, "base_url": "http://127.0.0.1:1/v1",
                                                      "model": "tiny", "api_key_env": "LOCAL_KEY"})
    assert model == "tiny" and custom and callable(call)


# ------------------------------------------------------------ TikTok
class Client:
    """Client giả: lời gọi router (nhận qua lời nhắc hệ thống) trả `route_answer`; còn lại như harness."""

    def __init__(self, route_answer=None):
        self.route_answer = route_answer
        self.router_calls, self.main_calls = [], []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **inference):
        if inference["messages"][0]["content"].endswith(router.SYSTEM_PROMPT):
            self.router_calls.append(inference)
            if isinstance(self.route_answer, Exception):
                raise self.route_answer
            return resp(self.route_answer)
        self.main_calls.append(inference)
        text = "Dạ em gửi anh chị ạ."
        msg = SimpleNamespace(content=json.dumps({"action": "reply", "reply": text, "messages": [text],
                                                  "awaiting_fields": []}, ensure_ascii=False), reasoning_content=None)
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=10, prompt_cache_hit_tokens=40,
                                prompt_cache_miss_tokens=60)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=usage)


def tiktok(monkeypatch, tmp_path, route_answer, enabled=True):
    monkeypatch.setattr(server_module, "LIVE_CATALOG_PATH", tmp_path / "live.json")
    engine, _ = h.make_engine(tmp_path)
    client = Client(route_answer)
    engine.responder.client = client
    if enabled:
        write_cfg(monkeypatch, tmp_path, enabled=True)
    return engine, client


def ask(engine, text, *, pin="", uid="u1", cid="c1"):
    body = h.payload(text, cid=cid, uid=uid)
    body["live_context"]["pin_id"] = pin
    return engine.handle(body)


def current_product_id(client):
    turn = client.main_calls[-1]["messages"][-1]["content"]
    return json.loads(turn.split("\n", 1)[1].split("\nKHÁCH")[0])["current_product"]["product_id"]


def test_tiktok_router_off_never_calls_router(monkeypatch, tmp_path):
    engine, client = tiktok(monkeypatch, tmp_path, answer(product_ids=["p2"]), enabled=False)
    try:
        ask(engine, "hello")
        assert client.router_calls == []
    finally:
        engine.close()


def test_tiktok_code_explicit_product_beats_router(monkeypatch, tmp_path):
    engine, client = tiktok(monkeypatch, tmp_path, answer(product_ids=["p2"]))
    try:
        result = ask(engine, "TC01 giá bao nhiêu", pin="p2")
        assert result["should_send"] and current_product_id(client) == "p1"
        assert client.router_calls == []          # mã đã chắc: không tốn lời gọi
    finally:
        engine.close()


def test_tiktok_ambiguous_uses_single_router_product(monkeypatch, tmp_path):
    engine, client = tiktok(monkeypatch, tmp_path, answer(product_ids=["p2"], topics=["price"]))
    try:
        result = ask(engine, "giá bao nhiêu ạ")
        assert result["should_send"] and result["metrics"]["selection_source"] == "router"
        assert current_product_id(client) == "p2"
        m = result["metrics"]
        assert m["router_used"] and m["router_prompt_tokens"] == 300 and m["router_acts"] == ["question"]
        assert m["router_tone"] == "neutral" and "router_ms" in m
        assert "doc_y_khach" in client.main_calls[-1]["messages"][-1]["content"]
    finally:
        engine.close()


def test_tiktok_router_off_same_question_clarifies_as_before(monkeypatch, tmp_path):
    engine, client = tiktok(monkeypatch, tmp_path, None, enabled=False)
    try:
        result = ask(engine, "giá bao nhiêu ạ")
        assert result["metrics"]["reply_source"] == "clarification" and "router_used" not in result["metrics"]
    finally:
        engine.close()


def test_tiktok_multiple_router_products_clarify_with_candidates(monkeypatch, tmp_path):
    engine, client = tiktok(monkeypatch, tmp_path, answer(product_ids=["p3", "p2"], clarify=True))
    try:
        result = ask(engine, "5 lít")      # không ý định giá/liều: trước đây đi tiếp/im lặng
        assert result["should_send"] and result["metrics"]["reply_source"] == "clarification"
        assert result["metrics"]["selection_source"] == "router_clarify"
        ask_ctx = json.loads(client.main_calls[-1]["messages"][-1]["content"].split("\n", 1)[1].split("\nKHÁCH")[0])
        assert [c["product_id"] for c in ask_ctx["catalog_candidates"]][:2] == ["p3", "p2"]
    finally:
        engine.close()


def test_tiktok_low_confidence_router_changes_nothing(monkeypatch, tmp_path):
    engine, client = tiktok(monkeypatch, tmp_path, answer(product_ids=["p2"], confidence=0.4))
    try:
        result = ask(engine, "giá bao nhiêu ạ")
        assert result["metrics"]["reply_source"] == "clarification"
        assert result["metrics"].get("selection_source") in (None, "code")
    finally:
        engine.close()


def lane_of(result):
    return (result.get("metrics") or {}).get("context_lane", "full")


def test_tiktok_smalltalk_needs_code_and_router_to_agree(monkeypatch, tmp_path):
    engine, client = tiktok(monkeypatch, tmp_path, answer(acts=["greeting"], tone="playful"))
    try:
        assert lane_of(ask(engine, "hello", uid="a", cid="a")) == "smalltalk"
        # Router thấy phản đối/câu hỏi -> làn đầy đủ dù mã nghĩ là xã giao.
        client.route_answer = answer(acts=["greeting", "objection"])
        assert lane_of(ask(engine, "ok", uid="b", cid="b")) == "full"
        # Router nói xã giao nhưng mã thấy ý định giá -> không bao giờ vào làn xã giao.
        client.route_answer = answer(acts=["banter"])
        result = ask(engine, "TC01 giá bao nhiêu", uid="c", cid="c")
        assert lane_of(result) == "full"
        # Mã không coi là xã giao (từ lạ) thì router một mình không kéo vào làn nhẹ.
        assert lane_of(ask(engine, "Toàn nói thật", uid="d", cid="d")) == "full"
    finally:
        engine.close()


def test_tiktok_doc_y_khach_goes_to_turn_context_only_when_router_reads(monkeypatch, tmp_path):
    engine, client = tiktok(monkeypatch, tmp_path, answer(acts=["banter", "question"], tone="playful"))
    try:
        ask(engine, "Halt đầu tàu mà", uid="e", cid="e")
        body = client.main_calls[-1]["messages"][-1]["content"]
        assert '"doc_y_khach":{"acts":["banter","question"],"tone":"playful"}' in body
    finally:
        engine.close()


def _main_messages(client):
    return [call["messages"] for call in client.main_calls]


@pytest.mark.parametrize("bad", ["garbage", RuntimeError("down"), {"acts": "x"}])
def test_tiktok_fail_open_equals_router_off(monkeypatch, tmp_path, bad):
    outputs = []
    for enabled, folder in ((False, "off"), (True, "on")):
        sub = tmp_path / folder
        sub.mkdir()
        engine, client = tiktok(monkeypatch, sub, bad, enabled=enabled)
        try:
            replies = [ask(engine, q, uid=f"u{i}", cid=f"c{i}") for i, q in enumerate(h.QUESTIONS)]
            outputs.append(([r.get("reply") for r in replies], [r.get("reason") for r in replies],
                            _main_messages(client)))
            if enabled:
                assert client.router_calls            # router thật sự được gọi rồi thất bại
        finally:
            engine.close()
    assert outputs[0] == outputs[1]


# ------------------------------------------------------------ Shopee
class ShopeeClient(Client):
    pass


class Recorder:
    def __init__(self, client):
        self.client, self.model, self.contexts = client, "deepseek-flash", []

    def reply(self, nickname, comment, context, max_chars, **kwargs):
        self.contexts.append(context)
        return {"action": "reply", "reply": "Dạ em hỗ trợ ngay ạ", "metrics": {}}


class Knowledge:
    def match(self, title, comment=""):
        return {"ten_san_pham": title} if title == "Kích Rễ TC01" else None

    def context(self, record):
        return {"san_pham_id": "SP-1", "ten_san_pham": "Kích Rễ TC01", "cong_dung": ["Kích rễ"],
                "huong_dan_su_dung": ["Pha 10ml cho bình 16 lít"]}


def shopee(monkeypatch, tmp_path, route_answer, enabled=True):
    products = [{"shop_id": "10", "product_id": pid, "title": title,
                 "price_data": {"currency": "VND", "price": "80000", "price_before_discount": "80000"}}
                for pid, title in (("20", "Kích Rễ TC01"), ("21", "Đạm Cá Amino 5 Lít"), ("22", "Đạm Cá Amino 1 Lít"))]
    catalog = {"session_id": "123", "updated_at": time.time(), "products": products}
    client = ShopeeClient(route_answer)
    owner = SimpleNamespace(responder=Recorder(client), knowledge=Knowledge(), shopee_mapping_state=lambda: {
        "catalog": catalog, "videos": {"v": {"mappings": {"tc01": {"shop_id": "10", "product_id": "20"}}}}})
    if enabled:
        write_cfg(monkeypatch, tmp_path, enabled=True)
    return owner, ShopeeAiEngine(owner, tmp_path / "m.sqlite3", tmp_path / "j"), client


def sask(engine, text, cid):
    return engine.reply({"session_id": "123", "video_id": "v", "comment_id": cid, "user_id": "77",
                         "nickname": "Mai", "comment": text, "shop_id": "", "product_id": "", "title": ""})


def test_shopee_ambiguous_without_router_clarifies_and_off_has_no_router_calls(monkeypatch, tmp_path):
    owner, engine, client = shopee(monkeypatch, tmp_path, None, enabled=False)
    try:
        result = sask(engine, "5 lít", "s1")
        assert result["should_send"] and result["reply_source"] == "clarification"
        assert client.router_calls == []
    finally:
        engine.close()


def test_shopee_router_multi_candidates_clarify(monkeypatch, tmp_path):
    owner, engine, client = shopee(monkeypatch, tmp_path, answer(product_ids=["22", "21"], clarify=True))
    try:
        result = sask(engine, "5 lít", "s2")
        assert result["should_send"] and result["reply_source"] == "clarification"
        candidates = owner.responder.contexts[-1]["catalog_candidates"]
        assert [c["product_id"] for c in candidates][:2] == ["22", "21"]
    finally:
        engine.close()


def test_shopee_router_single_product_fills_gap(monkeypatch, tmp_path):
    owner, engine, client = shopee(monkeypatch, tmp_path, answer(product_ids=["20"], topics=["usage"]))
    try:
        result = sask(engine, "cái đó dùng sao", "s3")
        context = owner.responder.contexts[-1]
        assert result["should_send"] and context["current_product"]["product_id"] == "20"
        assert context["product_selection"]["reason"] == "router"
        assert context["doc_y_khach"]["acts"] == ["question"]
    finally:
        engine.close()


def test_shopee_banter_lane_needs_both_and_fail_open_clarifies(monkeypatch, tmp_path):
    owner, engine, client = shopee(monkeypatch, tmp_path, answer(acts=["banter"]))
    try:
        result = sask(engine, "hihi haha", "s4")
        assert result["should_send"] and result["reply_source"] == "deepseek"
        assert "current_product" not in owner.responder.contexts[-1]
        # Mã không coi là xã giao -> không vào làn dù router nói vậy: đường cũ (hỏi lại SKU).
        assert sask(engine, "Toàn nói thật", "s5")["reply_source"] == "clarification"
        client.route_answer = "garbage"
        assert sask(engine, "hello", "s6")["reply_source"] == "clarification"
    finally:
        engine.close()
