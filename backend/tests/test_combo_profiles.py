import ast
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "backend/src"
sys.path.insert(0, str(SRC))
import combo_profiles as c
import product_blocklist as block
import product_ingest as p
from PIL import Image


class Provider:
    configured = True
    name = "gemini"
    supports_files = True
    model = "gemini-test"
    last_model = ""

    def __init__(self, response=None):
        self.response = response or {
            "components": [
                {"profile_id": "A", "reason": "Ảnh A"},
                {"profile_id": "B", "reason": "Tên B"},
            ],
            "suggested_name": "Combo A + B",
            "summary": "Giữ riêng hai hồ sơ",
            "questions": [],
        }
        self.calls = []

    def generate(self, system, user, files):
        self.calls.append((system, user, files))
        return json.dumps(self.response)


class Workspace:
    def __init__(self, root):
        self.root = root
        self.created = []

    def create_composed_profile(self, fields, pid, components, combo):
        self.created.append((fields, pid, components, combo))
        return {"ok": True, "id": "LOCAL-COMBO", "approved_for_live": False}

    def _touch(self, *args):
        pass


class ComboTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        image = Image.new("RGB", (20, 20), "red")
        self.image_path = Path(self.tmp.name) / "listing.jpg"
        image.save(self.image_path)
        self.records = [
            {
                "san_pham_id": "A",
                "ten_san_pham": "A",
                "thanh_phan_dang_ky": {"N": "10%"},
                "cong_dung": ["Lá"],
                "huong_dan_su_dung": ["20ml / 16 lít"],
                "quy_cach_dong_goi": "1 lít",
            },
            {
                "san_pham_id": "B",
                "ten_san_pham": "B",
                "thanh_phan_dang_ky": {"N": "5%"},
                "cong_dung": ["Rễ"],
                "huong_dan_su_dung": ["10ml / 16 lít"],
                "quy_cach_dong_goi": "500ml",
            },
        ]
        self.listing = {"product_id": "sku", "title": "Combo A + B", "profile_id": ""}
        self.workspace = Workspace(self.tmp.name)
        self.engine = types.SimpleNamespace(
            workspace=self.workspace,
            knowledge=types.SimpleNamespace(all_records=lambda: self.records),
            mapping_state=lambda: {"products": [self.listing]},
            product_thumb_path=lambda pid: self.image_path,
            _profile_image_url=lambda r: "/api/profile/thumb/" + r["san_pham_id"],
        )
        self.provider = Provider()
        self.ingest = p.ProductIngest(self.engine, self.provider, None)
        self.service = c.ComboProfiles(self.engine, self.ingest)

    def test_malformed_component_details_does_not_create_a_partial_draft(self):
        out = self.service.propose({"product_id": "sku"})
        with self.assertRaises(p.IngestError):
            self.service.create_draft(
                {
                    "proposal_id": out["proposal_id"],
                    "component_ids": ["A"],
                    "component_details": {"A": "invalid"},
                }
            )
        self.assertEqual(self.workspace.created, [])

    def test_single_variant_has_own_package_without_combined_fields_or_scaled_dose(
        self,
    ):
        self.listing["title"] = "Đạm cá 1L"
        self.records[0]["quy_cach_dong_goi"] = "5 lít"
        self.provider.response = {
            "components": [{"profile_id": "A"}],
            "suggested_name": "Đạm cá 1L",
        }
        out = self.service.propose({"product_id": "sku"})
        self.assertTrue(out["variant"])
        self.assertEqual(out["package"], "1 lít")
        self.service.create_draft(
            {"proposal_id": out["proposal_id"], "component_ids": ["A"]}
        )
        fields, _, refs, combo = self.workspace.created[0]
        self.assertFalse(combo)
        self.assertEqual(fields["quy_cach_dong_goi"], "1 lít")
        self.assertEqual(fields["thanh_phan_dang_ky"], {"N": "10%"})
        self.assertEqual(fields["huong_dan_su_dung"], ["20ml / 16 lít"])
        self.assertEqual(refs[0]["package"], "1 lít")

    def test_combo_quantity_affects_package_only_not_dose(self):
        out = self.service.propose({"product_id": "sku"})
        self.service.create_draft(
            {
                "proposal_id": out["proposal_id"],
                "component_ids": ["A", "B"],
                "component_details": {
                    "A": {"quantity": 1, "package": "1 lít"},
                    "B": {"quantity": 5, "package": "500ml"},
                },
            }
        )
        fields, _, refs, _ = self.workspace.created[0]
        self.assertIn("5 × 500ml", fields["quy_cach_dong_goi"])
        self.assertEqual(refs[1]["quantity"], 5)
        self.assertIn("B: 10ml / 16 lít", fields["huong_dan_su_dung"])
        self.assertNotIn("50ml", str(fields["huong_dan_su_dung"]))

    def test_bulk_shares_one_call_catalog_and_maps_images_by_id(self):
        products = [
            dict(self.listing, product_id=f"sku{i}", title=f"Combo {i}")
            for i in range(12)
        ]
        self.engine.mapping_state = lambda: {"products": products}
        self.provider.response = {
            "products": [
                dict(
                    product_id=p["product_id"],
                    suggested_name=p["title"],
                    components=[{"profile_id": "A", "reason": p["title"]}],
                )
                for p in reversed(products)
            ]
        }
        out = self.service.propose_batch(
            {"product_ids": [p["product_id"] for p in products]}
        )
        self.assertEqual(out["ai_calls"], 1)
        self.assertEqual(len(self.provider.calls), 1)
        prompt = json.loads(self.provider.calls[0][1])
        self.assertEqual(len(prompt["available_single_product_profiles"]), 2)
        self.assertEqual(len(self.provider.calls[0][2]), 12)
        self.assertEqual(
            [p["image_index"] for p in prompt["listings"]], list(range(1, 13))
        )
        self.assertEqual(
            [p["product_id"] for p in out["products"]],
            [p["product_id"] for p in products],
        )
        self.assertEqual(out["errors"], [])
        self.assertEqual(len(self.service._proposals), 12)
        # Earlier entries remain savable after the full batch (> former cache cap of 8).
        saved = self.service.create_draft(
            {"proposal_id": out["products"][0]["proposal_id"], "component_ids": ["A"]}
        )
        self.assertEqual(saved["product_id"], "sku0")
        self.assertEqual(len(self.provider.calls), 1)

    def test_bulk_rejects_duplicate_or_missing_result_without_cross_assignment(self):
        self.engine.mapping_state = lambda: {
            "products": [self.listing, dict(self.listing, product_id="other")]
        }
        row = {"product_id": "sku", "components": [{"profile_id": "A"}]}
        self.provider.response = {
            "products": [
                row,
                row,
                {"product_id": "alien", "components": [{"profile_id": "B"}]},
            ]
        }
        out = self.service.propose_batch({"product_ids": ["sku", "other"]})
        self.assertEqual(out["products"], [])
        self.assertEqual({e["product_id"] for e in out["errors"]}, {"sku", "other"})
        self.assertEqual(out["ai_calls"], 1)

    def test_bulk_partial_groups_survive_overload(self):
        products = [dict(self.listing, product_id=f"sku{i}") for i in range(25)]
        self.engine.mapping_state = lambda: {"products": products}
        count = 0

        def generate(system, user, files):
            nonlocal count
            count += 1
            if count == 2:
                raise c.AIProviderError("overloaded", "Google quá tải")
            return json.dumps(
                {
                    "products": [
                        {
                            "product_id": r["product_id"],
                            "components": [{"profile_id": "A"}],
                        }
                        for r in json.loads(user)["listings"]
                    ]
                }
            )

        self.provider.generate = generate
        out = self.service.propose_batch(
            {"product_ids": [p["product_id"] for p in products], "provider": "gemini"}
        )
        self.assertEqual(len(out["products"]), 20)
        self.assertEqual(len(out["errors"]), 5)
        self.assertEqual(out["ai_calls"], 2)

    def test_ai_identifies_and_draft_keeps_source_doses_with_photo(self):
        out = self.service.propose({"product_id": "sku"})
        self.assertEqual(len(out["components"]), 2)
        self.assertTrue(out["read_image"])
        self.assertTrue(self.provider.calls[0][2])
        self.assertIn("20ml / 16 lít", json.dumps(out["preview"], ensure_ascii=False))
        self.assertNotIn("30ml", json.dumps(out["preview"]))
        saved = self.service.create_draft(
            {
                "proposal_id": out["proposal_id"],
                "component_ids": ["A", "B"],
                "name": "Combo A + B",
            }
        )
        self.assertTrue(saved["image_saved"])
        self.assertFalse(saved["approved_for_live"])
        self.assertTrue(self.ingest.staged_image_path(saved["id"]).exists())
        fields, _, refs, combo = self.workspace.created[0]
        self.assertTrue(combo)
        self.assertEqual(len(refs), 2)
        self.assertEqual(fields["thanh_phan_dang_ky"], {"A — N": "10%", "B — N": "5%"})
        self.assertNotIn("15%", str(fields))
        self.assertEqual(
            self.service.create_draft(
                {"proposal_id": out["proposal_id"], "component_ids": ["A", "B"]}
            ),
            saved,
        )
        self.assertEqual(len(self.workspace.created), 1)

    def test_unknown_ai_component_and_stale_source_are_rejected(self):
        self.provider.response["components"].append({"profile_id": "unknown"})
        out = self.service.propose({"product_id": "sku"})
        self.assertEqual(len(out["components"]), 2)
        self.assertTrue(out["questions"])
        with self.assertRaises(p.IngestError):
            self.service.create_draft(
                {"proposal_id": out["proposal_id"], "component_ids": ["unknown"]}
            )
        self.records[0]["huong_dan_su_dung"] = ["999ml / 16 lít"]
        with self.assertRaises(p.IngestError):
            self.service.create_draft(
                {"proposal_id": out["proposal_id"], "component_ids": ["A"]}
            )
        self.assertEqual(self.workspace.created, [])

    def test_auto_falls_back_to_deepseek_without_sending_photo(self):
        ds = Provider()
        ds.name = "deepseek"
        ds.supports_files = False
        self.ingest.deepseek = ds
        with patch.object(
            self.provider,
            "generate",
            side_effect=p.AIProviderError("overloaded", "busy"),
        ):
            out = self.service.propose({"product_id": "sku"})
        self.assertEqual(out["provider"], "deepseek")
        self.assertFalse(out["read_image"])
        self.assertEqual(ds.calls[0][2], [])

    def test_no_match_is_not_fabricated_and_user_can_choose_source(self):
        self.provider.response["components"] = []
        out = self.service.propose({"product_id": "sku"})
        self.assertEqual(out["components"], [])
        with self.assertRaises(p.IngestError):
            self.service.create_draft(
                {"proposal_id": out["proposal_id"], "component_ids": []}
            )
        out = self.service.create_draft(
            {
                "proposal_id": out["proposal_id"],
                "component_ids": ["A"],
                "use_image": False,
            }
        )
        self.assertFalse(out["image_saved"])

    def test_missing_dose_remains_missing(self):
        self.records[0]["huong_dan_su_dung"] = None
        fields = c.compose_fields("Combo A + B", self.records)
        self.assertEqual(fields["huong_dan_su_dung"], ["B: 10ml / 16 lít"])
        self.assertIn("nguồn chưa có liều dùng", fields["luu_y"])

    def test_gift_is_not_default_combo_component_and_questions_are_kept(self):
        self.provider.response["components"][1].update(
            {"kind": "gift", "reason": "Quà tặng kèm trong ảnh"}
        )
        self.provider.response["questions"] = ["Xác nhận quy cách A trước khi duyệt"]
        out = self.service.propose({"product_id": "sku"})
        self.assertEqual([p["id"] for p in out["components"]], ["A"])
        self.service.create_draft(
            {"proposal_id": out["proposal_id"], "component_ids": ["A"]}
        )
        self.assertIn("Xác nhận quy cách A", self.workspace.created[0][0]["luu_y"])

    def test_real_workspace_persists_draft_component_metadata_atomically(self):
        from workspace import WorkspaceStore

        store = WorkspaceStore.__new__(WorkspaceStore)
        store.db_path = Path(self.tmp.name) / "workspace.sqlite3"
        store.override_path = Path(self.tmp.name) / "overrides.json"
        store.engine = types.SimpleNamespace(
            knowledge=types.SimpleNamespace(reload=lambda: None)
        )
        store._touch = lambda *args: None
        fields = c.compose_fields("Combo A + B", self.records)
        saved = store.create_composed_profile(
            fields, "sku", [{"profile_id": "A"}, {"profile_id": "B"}], True
        )
        entry = json.loads(store.override_path.read_text(encoding="utf-8"))[
            "overrides"
        ][saved["id"]]
        self.assertFalse(entry["approved_for_live"])
        self.assertTrue(entry["is_combo"])
        self.assertEqual(entry["draft_for_product_id"], "sku")
        self.assertEqual(
            entry["combo_components"], [{"profile_id": "A"}, {"profile_id": "B"}]
        )
        original = store.override_path.read_bytes()

        def failed_reload():
            raise RuntimeError("reload failed")

        store.engine.knowledge.reload = failed_reload
        with self.assertRaises(RuntimeError):
            store.create_composed_profile(fields, "other", [{"profile_id": "A"}], False)
        self.assertEqual(
            json.loads(store.override_path.read_text(encoding="utf-8")),
            json.loads(original),
        )

    def test_removal_survives_sync_restore_and_shared_rules(self):
        with (
            patch.object(block, "REMOVED_PATH", Path(self.tmp.name) / "removed.json"),
            patch.object(block, "PATH", Path(self.tmp.name) / "rules.json"),
        ):
            block.set_removed(self.listing)
            self.assertEqual(block.filter_products([self.listing]), [])
            self.assertIn("sku", block.removed_products())
            block.set_removed(self.listing, False)
            self.assertEqual(block.filter_products([self.listing]), [self.listing])
            block.PATH.write_text(
                json.dumps({"product_ids": ["other"], "title_contains": ["blocked"]})
            )
            block.set_removed(self.listing)
            self.assertEqual(
                block.filter_products(
                    [self.listing, {"product_id": "other"}, {"title": "blocked name"}]
                ),
                [],
            )

    def test_live_catalog_snapshot_filters_even_if_all_rows_removed(self):
        tree = ast.parse((SRC / "server.py").read_text(encoding="utf-8"))
        cls = next(
            n
            for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == "LiveCatalogStore"
        )
        import threading

        namespace = {
            "threading": threading,
            "logging": __import__("logging"),
            "product_blocklist": block,
            "LIVE_CATALOG_PATH": Path(self.tmp.name) / "live.json",
            "LIVE_RAW_CATALOG_PATH": Path(self.tmp.name) / "raw.json",
            "json": json,
            "clean": lambda v, n: str(v or "")[:n],
            "now_iso": lambda: "now",
            "atomic_json": lambda path, data: path.write_text(json.dumps(data)),
            "product_cover_sources": __import__("media_assets").product_cover_sources,
        }
        exec(
            compile(ast.Module(body=[cls], type_ignores=[]), "server.py", "exec"),
            namespace,
        )
        with (
            patch.object(block, "REMOVED_PATH", Path(self.tmp.name) / "removed.json"),
            patch.object(block, "PATH", Path(self.tmp.name) / "rules.json"),
        ):
            store = namespace["LiveCatalogStore"]()
            store.sync({"products": [self.listing], "pin_id": "sku", "host": {"user_id": "u1"}})
            block.set_removed(self.listing)
            self.assertEqual(store.snapshot["products"], [])
            self.assertEqual(store.snapshot["pin_id"], "")
            self.assertEqual(store.sync({"products": [self.listing], "host": {"user_id": "u1"}})["products"], [])
            block.set_removed(self.listing, False)
            self.assertEqual(store.snapshot["products"], [self.listing])


if __name__ == "__main__":
    unittest.main(verbosity=2)
