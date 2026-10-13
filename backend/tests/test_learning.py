"""Trí nhớ AI: kiến thức đã học, rút kiến thức, học lại, và đường trả lời dùng nó."""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from learning import Learner, LearningStore, compact, relearn_change_ids  # noqa: E402

CATALOG = [
    {"product_id": "p-dam-1l", "bag_index": 2, "name": "Đạm Cá Amino 1L", "title": "Đạm Cá Amino 1L Chất Hữu Cơ 20%"},
    {"product_id": "p-dam-5l", "bag_index": 1, "name": "Đạm Cá Hữu Cơ Amino · 5 Lít", "title": "Đạm Cá Hữu Cơ Amino (Can 5 Lít)"},
    {"product_id": "p-tc01", "bag_index": 9, "name": "Kích Rễ TC01", "title": "Kích Rễ Cực Mạnh TC01"},
]


class FakeCompletions:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def create(self, **request):
        self.calls.append(request)
        content = json.dumps(self.payloads.pop(0), ensure_ascii=False)
        message = types.SimpleNamespace(content=content)
        usage = types.SimpleNamespace(prompt_tokens=100, completion_tokens=20, completion_tokens_details=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message, finish_reason="stop")], usage=usage)


class FakeClient:
    def __init__(self, payloads):
        self.chat = types.SimpleNamespace(completions=FakeCompletions(payloads))

    def with_options(self, **_):
        return self


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "learning.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_alias_matches_spaced_and_joined_spelling_but_not_fragments(self):
        self.store.add_item(kind="alias", statement="Win Gold 20 là Đạm Cá Amino", product_id="p-dam-1l",
                            terms=["win gold 20"], status="active")
        self.assertEqual(self.store.alias_products("phân bón win gold 20 là mã nào"), ["p-dam-1l"])
        self.assertEqual(self.store.alias_products("wingold20 giá sao em"), ["p-dam-1l"])
        self.assertEqual(self.store.alias_products("Win-Gold 20 còn không"), ["p-dam-1l"])
        # "gold" một mình hay "20" không được kích hoạt.
        self.assertEqual(self.store.alias_products("vàng gold đẹp quá"), [])
        self.assertEqual(self.store.alias_products("bình 20 lít pha sao"), [])

    def test_only_active_items_are_used(self):
        self.store.add_item(kind="alias", statement="Win Gold 20 là Đạm Cá", product_id="p-dam-1l", terms=["win gold 20"])
        self.assertEqual(self.store.alias_products("win gold 20"), [], "đề xuất chưa duyệt không được dùng")

    def test_short_or_numeric_terms_are_rejected(self):
        with self.assertRaises(ValueError):
            self.store.add_item(kind="alias", statement="abc la gi", product_id="p", terms=["20", "ab", "cay"])

    def test_relevant_prefers_term_hits_then_product_facts_and_skips_global_rules(self):
        self.store.add_item(kind="fact", statement="Đạm cá có 2 quy cách can 5L và 1L", product_id="p-dam-1l",
                            status="active")
        self.store.add_item(kind="fact", statement="Shop giao COD toàn quốc 2–3 ngày", terms=["cod"],
                            status="active")
        self.store.add_item(kind="rule", statement="Khách hỏi tên lạ thì đối chiếu tên gọi khác trước",
                            status="active")
        rows = self.store.relevant("ship cod không em", product_ids=["p-dam-1l"])
        self.assertEqual(rows[0]["noi_dung"], "Shop giao COD toàn quốc 2–3 ngày")
        self.assertIn("Đạm cá có 2 quy cách can 5L và 1L", [r["noi_dung"] for r in rows])
        self.assertNotIn("rule", [r["loai"] for r in rows])
        self.assertEqual(self.store.rules(), ["Khách hỏi tên lạ thì đối chiếu tên gọi khác trước"])

    def test_personas_keep_multiline_style_and_single_active(self):
        people = self.store.personas()
        self.assertEqual([p["persona_id"] for p in people], ["default"])
        created = self.store.save_persona({"name": "Chị Hoa", "style_guide": "- xưng em\n- gọi khách là bác",
                                           "activate": True})
        self.assertEqual(created["style_guide"], "- xưng em\n- gọi khách là bác")
        self.assertEqual(self.store.persona()["persona_id"], created["persona_id"])
        self.assertEqual(sum(p["is_active"] for p in self.store.personas()), 1)
        self.store.delete_persona(created["persona_id"])
        self.assertEqual(self.store.persona()["persona_id"], "default")


class LearnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "learning.sqlite3")
        self.responder = types.SimpleNamespace(client=None, model="deepseek-flash")
        self.learner = Learner(self.store, responder_provider=lambda: self.responder,
                               catalog_provider=lambda: CATALOG, examples_provider=lambda: [])

    def tearDown(self):
        self.temp.cleanup()

    def test_note_x_la_y_becomes_alias_without_a_model(self):
        run_id = self.learner.submit_distill({"conversation_id": "c1", "note": "wingold 20 là đạm cá amino hữu cơ",
                                              "answer": "dạ nó là can đạm cá amino á bác"}, sync=True)
        run = self.store.run(run_id)
        self.assertEqual(run["status"], "done")
        self.assertFalse(run["output"]["used_model"])
        item = self.store.item(run["output"]["item_ids"][0])
        self.assertEqual(item["kind"], "alias")
        self.assertIn(item["product_id"], {"p-dam-1l", "p-dam-5l"})
        self.assertEqual(item["status"], "suggested")

    def test_model_suggestions_are_validated_and_deduplicated(self):
        self.responder.client = FakeClient([{
            "diagnosis": "AI tưởng Win Gold 20 là mã giảm giá",
            "items": [
                {"kind": "alias", "product_id": "p-dam-1l", "terms": ["win gold 20", "wingold20"],
                 "statement": "Win Gold 20 là tên khách gọi Đạm Cá Amino", "confidence": .9},
                {"kind": "alias", "product_id": "khong-ton-tai", "terms": ["abc xyz"],
                 "statement": "Sản phẩm bịa", "confidence": .9},
                {"kind": "fact", "product_id": "", "terms": [], "statement": "Shop có 2 quy cách", "confidence": .7},
            ],
            "style_notes": ["Gọi khách là bác"],
        }])
        run_id = self.learner.submit_distill({"conversation_id": "c1", "question": "win gold 20 là mã nào",
                                              "answer": "là đạm cá amino"}, sync=True)
        out = self.store.run(run_id)["output"]
        self.assertEqual(len(out["item_ids"]), 2, "alias trỏ tới sản phẩm không có thật phải bị bỏ")
        alias = self.store.item(out["item_ids"][0])
        self.assertEqual(alias["terms"], ["win gold 20"], "hai cách viết cùng dạng liền là một")
        request = self.responder.client.chat.completions.calls[0]
        self.assertEqual(request["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertIn("win gold 20 là mã nào", request["messages"][1]["content"])

    def test_relearn_proposal_apply_and_undo(self):
        a = self.store.add_item(kind="fact", statement="Đạm cá có can 5L", product_id="p-dam-5l", status="active")
        b = self.store.add_item(kind="fact", statement="Đạm cá bán can 5 lít", product_id="p-dam-5l", status="active")
        wrong = self.store.add_item(kind="fact", statement="TC01 dùng cho lúa", product_id="p-tc01", status="active")
        self.responder.client = FakeClient([{
            "summary": "Gộp 2 mục quy cách, bỏ 1 mục sai",
            "items": [{"from_ids": [a, b], "kind": "fact", "product_id": "p-dam-5l", "terms": [],
                       "statement": "Đạm Cá Amino có can 5 lít", "confidence": .8}],
            "retire_ids": [wrong], "conflicts": [],
            "personas": [{"persona_id": "default", "style_guide": "- xưng em\n- câu ngắn"}],
        }])
        run_id = self.learner.submit_relearn(sync=True)
        proposal = self.store.run(run_id)["output"]
        self.assertEqual(proposal["counts"]["merge"], 1)
        self.assertEqual(proposal["retire_ids"], [wrong])
        self.assertEqual(self.store.item(a)["status"], "active", "đề xuất chưa áp dụng thì chưa đổi gì")
        self.learner.apply_relearn(run_id)
        active = self.store.items("active")
        self.assertEqual([i["statement"] for i in active], ["Đạm Cá Amino có can 5 lít"])
        self.assertEqual(self.store.persona("default")["style_guide"], "- xưng em\n- câu ngắn")
        self.learner.undo_relearn(run_id)
        self.assertEqual(sorted(i["item_id"] for i in self.store.items("active")), sorted([a, b, wrong]))
        self.assertEqual(self.store.persona("default")["style_guide"], "")

    def test_relearn_partial_apply_keeps_rejected_changes_and_undo_restores(self):
        a = self.store.add_item(kind="fact", statement="Đạm cá có can 5L", product_id="p-dam-5l", status="active")
        b = self.store.add_item(kind="fact", statement="Đạm cá bán can 5 lít", product_id="p-dam-5l", status="active")
        wrong = self.store.add_item(kind="fact", statement="TC01 dùng cho lúa", product_id="p-tc01", status="active")
        style_before = self.store.persona("default")["style_guide"]
        self.responder.client = FakeClient([{
            "summary": "Gộp 2 mục, thêm 1 mục, bỏ 1 mục sai, đổi giọng",
            "items": [
                {"from_ids": [a, b], "kind": "fact", "product_id": "p-dam-5l", "terms": [],
                 "statement": "Đạm Cá Amino có can 5 lít", "confidence": .8},
                {"from_ids": [], "kind": "fact", "product_id": "p-dam-5l", "terms": [],
                 "statement": "Đạm cá tan trong nước", "confidence": .9},
            ],
            "retire_ids": [wrong], "conflicts": [],
            "personas": [{"persona_id": "default", "style_guide": "- xưng em\n- câu ngắn"}],
        }])
        run_id = self.learner.submit_relearn(sync=True)
        ids = relearn_change_ids(self.store.run(run_id)["output"])
        self.assertEqual(ids, ["item:0", "item:1", f"retire:{wrong}", "style:default"])

        # Bỏ đề xuất gộp và đổi giọng; giữ mục mới và tắt mục sai.
        out = self.learner.apply_relearn(run_id, accepted=["item:1", f"retire:{wrong}"])
        self.assertEqual((out["applied"], out["total"]), (2, 4))
        self.assertEqual(sorted(i["item_id"] for i in self.store.items("active") if i["item_id"] in {a, b}), sorted([a, b]),
                         "merge bị bỏ thì hai mục cũ phải còn nguyên")
        self.assertEqual(self.store.item(wrong)["status"], "retired")
        self.assertIn("Đạm cá tan trong nước", [i["statement"] for i in self.store.items("active")])
        self.assertEqual(self.store.persona("default")["style_guide"], style_before, "giọng bị bỏ thì không đổi")
        saved = self.store.run(run_id)["output"]
        self.assertEqual(saved["applied"], ["item:1", f"retire:{wrong}"])
        self.assertEqual(saved["skipped"], ["item:0", "style:default"])

        self.learner.undo_relearn(run_id)
        self.assertEqual(sorted(i["item_id"] for i in self.store.items("active")), sorted([a, b, wrong]))
        self.assertNotIn("Đạm cá tan trong nước", [i["statement"] for i in self.store.items("active")])
        self.assertEqual(self.store.persona("default")["style_guide"], style_before)

    def test_relearn_apply_validates_accepted_list_without_partial_changes(self):
        a = self.store.add_item(kind="fact", statement="Đạm cá có can 5L", product_id="p-dam-5l", status="active")
        wrong = self.store.add_item(kind="fact", statement="TC01 dùng cho lúa", product_id="p-tc01", status="active")
        self.responder.client = FakeClient([{
            "summary": "Bỏ 1 mục sai",
            "items": [], "retire_ids": [wrong], "conflicts": [], "personas": [],
        }])
        run_id = self.learner.submit_relearn(sync=True)
        with self.assertRaisesRegex(ValueError, "learning_change_unknown"):
            self.learner.apply_relearn(run_id, accepted=["retire:not-real"])
        with self.assertRaisesRegex(ValueError, "learning_nothing_selected"):
            self.learner.apply_relearn(run_id, accepted=[])
        self.assertEqual(self.store.run(run_id)["status"], "done", "lỗi xác thực thì đề xuất vẫn chờ áp dụng")
        self.assertEqual(self.store.item(wrong)["status"], "active")
        self.assertEqual(self.store.item(a)["status"], "active")
        out = self.learner.apply_relearn(run_id, accepted=[f"retire:{wrong}"])
        self.assertEqual((out["applied"], out["total"]), (1, 1))
        self.assertEqual(self.store.item(wrong)["status"], "retired")


class SplitContextTests(unittest.TestCase):
    def test_stable_part_is_separated_for_prefix_cache(self):
        sys.path.insert(0, str(SRC.parents[1] / "plugins" / "model-deepseek"))
        from processor_loader import load_processor
        plugin = load_processor(SRC.parents[1] / "plugins" / "model-deepseek" / "processor.py", "model-deepseek")
        stable, turn = plugin.split_context({"all_live_products": [1], "price_policy": "p", "price": {"a": 1},
                                             "kien_thuc_da_hoc": [1], "quy_tac_da_hoc": ["r"]})
        self.assertEqual(set(stable), {"all_live_products", "price_policy", "quy_tac_da_hoc"})
        self.assertEqual(set(turn), {"price", "kien_thuc_da_hoc"})


def test_compact_folds_accents_and_separators():
    assert compact("Win Gold-20") == "wingold20"
    assert compact("Đạm Cá") == "damca"


class AmbiguousAliasTests(unittest.TestCase):
    """23/09: "can 5l" bị học thành tên gọi của Đạm Cá 5L — trùng Humic/combo 5 lít."""

    TITLES = {"dam5": "Đạm Cá Hữu Cơ Amino (Can 5 Lít)", "humic5": "Humic Can 5 Lít", "dam1": "Đạm Cá Amino 1L"}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "learning.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_generic_pack_size_alias_is_ignored_at_reply_time(self):
        self.store.add_item(kind="alias", statement="can 5 lít là đạm cá", product_id="dam5",
                            terms=["can 5 lít", "can 5l"], status="active")
        self.store.add_item(kind="alias", statement="Win Gold 20 là đạm cá", product_id="dam1",
                            terms=["win gold 20"], status="active")
        self.assertEqual(self.store.alias_products("can 5l humic giá sao", self.TITLES), [])
        self.assertEqual(self.store.alias_products("win gold 20 giá sao", self.TITLES), ["dam1"])

    def test_learner_turns_generic_alias_into_a_fact(self):
        catalog = [{"product_id": pid, "bag_index": i, "name": t, "title": t} for i, (pid, t) in enumerate(self.TITLES.items(), 1)]
        client = FakeClient([{"diagnosis": "", "items": [
            {"kind": "alias", "product_id": "dam5", "terms": ["can 5l"], "statement": "Khách nói can 5l là đạm cá 5L", "confidence": .8}]}])
        learner = Learner(self.store, responder_provider=lambda: types.SimpleNamespace(client=client, model="m"),
                          catalog_provider=lambda: catalog)
        run_id = learner.submit_distill({"conversation_id": "c", "answer": "dạ can 5l đạm cá nha"}, sync=True)
        item = self.store.item(self.store.run(run_id)["output"]["item_ids"][0])
        self.assertEqual(item["kind"], "fact")
        self.assertEqual(item["terms"], [])


def test_deictic_keeps_pinned_product_unless_a_distinct_code_is_named():
    from knowledge import select_live_product
    products = [
        {"product_id": "dam", "title": "Đạm Cá Hữu Cơ Amino Can 5 Lít"},
        {"product_id": "humic", "title": "Siêu Kích Rễ Humic Mai Vàng Cây Công Nghiệp"},
        {"product_id": "tc01", "title": "Kích Rễ Cực Mạnh TC01 Mai Vàng"},
    ]
    assert select_live_product("phân này bón cho cây mai được không", products, "dam")["product_id"] == "dam"
    assert select_live_product("humic mai vàng giá sao", products, "dam")["product_id"] == "humic"
    assert select_live_product("tc01 chai này pha sao", products, "dam")["product_id"] == "tc01"
