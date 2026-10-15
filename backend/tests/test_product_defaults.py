import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend/src"))
import io

from combo_profiles import compose_fields, revision
from knowledge import KnowledgeBase
from knowledge_package import content_hash
from PIL import Image
from product_defaults import DefaultProfiles, audit
from product_ingest import ProductIngest


class DefaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.catalog = self.root / "data/shared/product_knowledge.json"
        self.k = KnowledgeBase(
            self.catalog,
            self.root / "data/approved.json",
            self.root / "data/machine/mapping.json",
        )
        self.workspace = types.SimpleNamespace(
            root=self.root,
            db_path=self.root / "data/workspace.sqlite3",
            _touch=lambda *args: None,
        )
        self.engine = types.SimpleNamespace(
            knowledge=self.k, workspace=self.workspace, _is_live_running=lambda: False
        )
        self.ingest = ProductIngest(self.engine, None, None)
        self.service = DefaultProfiles(
            self.engine, self.ingest, seed_path=self.root / "seed.json"
        )
        self.source = {
            "san_pham_id": "A",
            "ten_san_pham": "Nguồn A",
            "quy_cach_dong_goi": "250ml",
            "thanh_phan_dang_ky": {"N": "10%"},
            "huong_dan_su_dung": ["20ml / 16L"],
            "cong_dung": ["Lá"],
            "approved_for_live": True,
            "gia_goc_duyet": "secret price",
            "shop_product_ids": ["secret sku"],
            "listing_aliases": ["machine alias"],
        }

    def write_profiles(self):
        meta = [
            {
                "profile_id": "A",
                "name": "Nguồn A",
                "quantity": 5,
                "package": "250ml",
                "source_revision": revision(self.source),
            }
        ]
        combo = {
            **compose_fields("Combo 5 A", [self.source], components=meta),
            "san_pham_id": "B",
            "approved_for_live": True,
            "is_combo": True,
            "combo_components": meta,
            "draft_for_product_id": "machine sku",
        }
        self.catalog.parent.mkdir(parents=True, exist_ok=True)
        self.catalog.write_text(
            json.dumps(
                {"content": {"nhom_san_pham": [{"san_pham": [self.source, combo]}]}}
            ),
            encoding="utf-8",
        )
        image = io.BytesIO()
        Image.new("RGB", (30, 30), "red").save(image, format="WEBP")
        self.ingest.staged_image_path("A", create=True).write_bytes(image.getvalue())

    def test_roundtrip_keeps_packages_doses_photos_and_no_machine_data(self):
        self.write_profiles()
        self.k.reload()
        out = self.service.save()
        self.assertEqual(out["products"], 2)
        self.assertEqual(out["images"], 1)
        raw = Path(out["path"]).read_text(encoding="utf-8")
        self.assertNotIn("secret sku", raw)
        self.assertNotIn("secret price", raw)
        self.assertNotIn("machine alias", raw)
        self.assertNotIn("machine sku", raw)
        self.catalog.unlink()
        self.ingest.staged_image_path("A").unlink()
        self.k.reload()
        self.assertTrue(self.service.status()["needs_choice"])
        self.service.choose({"mode": "default"})
        rows = self.k.all_records()
        self.assertEqual(len(rows), 2)
        self.assertEqual(audit(rows), [])
        self.assertEqual(rows[0]["quy_cach_dong_goi"], "250ml")
        combo = next(r for r in rows if r["san_pham_id"] == "B")
        self.assertEqual(combo["combo_components"][0]["quantity"], 5)
        self.assertEqual(combo["huong_dan_su_dung"], ["Nguồn A: 20ml / 16L"])
        self.assertTrue(self.ingest.staged_image_path("A").exists())
        self.assertFalse(self.service.status()["needs_choice"])

    def test_default_combo_names_remain_available_for_new_machine_matching(self):
        from listing_match import ProfileIndex

        self.write_profiles()
        self.k.reload()
        index = ProfileIndex(self.k.all_records())
        combo = next(r for r in index.rows if r["id"] == "B")
        self.assertIn("Combo 5 A", combo["heads"])
        self.assertIn("Nguồn A", combo["heads"])
        self.assertTrue(combo["identity"])

    def test_custom_choice_stays_empty_and_does_not_seed(self):
        self.assertTrue(self.service.status()["needs_choice"])
        self.service.choose({"mode": "custom"})
        self.assertEqual(self.k.all_records(), [])
        self.assertEqual(self.service.status()["mode"], "custom")
        self.assertFalse(self.service.status()["needs_choice"])

    def test_live_and_corrupted_package_cannot_mutate_catalog(self):
        self.write_profiles()
        out = self.service.save()
        before = self.catalog.read_bytes()
        self.engine._is_live_running = lambda: True
        with self.assertRaises(ValueError):
            self.service.choose({"mode": "default"})
        self.engine._is_live_running = lambda: False
        package = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
        package["content"]["images"]["A"] = "invalid base64"
        package["content_sha256"] = content_hash(package["content"])
        Path(out["path"]).write_text(json.dumps(package), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.service.choose({"mode": "default"})
        self.assertEqual(self.catalog.read_bytes(), before)

    def test_reference_sources_remain_available_but_cannot_be_mapped_as_approved_products(
        self,
    ):
        self.write_profiles()
        package = json.loads(self.catalog.read_text(encoding="utf-8"))
        package["content"]["nhom_san_pham"][0]["san_pham"][0]["approved_for_live"] = (
            False
        )
        self.catalog.write_text(json.dumps(package), encoding="utf-8")
        self.k.reload()
        out = self.service.save()
        self.catalog.unlink()
        self.k.reload()
        self.service.choose({"mode": "default", "source": out["path"]})
        self.assertEqual({r["san_pham_id"] for r in self.k.all_records()}, {"A", "B"})
        self.assertEqual({r["san_pham_id"] for r in self.k.mappable_records()}, {"B"})
        with self.assertRaises(ValueError):
            self.k.apply_mapping({"shop-sku": "A"})

    def test_nested_machine_metadata_is_not_exported(self):
        self.write_profiles()
        package = json.loads(self.catalog.read_text(encoding="utf-8"))
        package["content"]["nhom_san_pham"][0]["san_pham"][1]["combo_components"][0][
            "product_id"
        ] = "private-shop-sku"
        self.catalog.write_text(json.dumps(package), encoding="utf-8")
        self.k.reload()
        out = self.service.save()
        self.assertNotIn(
            "private-shop-sku", Path(out["path"]).read_text(encoding="utf-8")
        )

    def test_invalid_image_map_shape_is_rejected_before_mutation(self):
        self.write_profiles()
        out = self.service.save()
        before = self.catalog.read_bytes()
        package = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
        package["content"]["images"] = ["invalid"]
        package["content_sha256"] = content_hash(package["content"])
        Path(out["path"]).write_text(json.dumps(package), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.service.choose({"mode": "default"})
        self.assertEqual(self.catalog.read_bytes(), before)

    def test_invalid_combo_excluded_from_saved_defaults(self):
        self.write_profiles()
        self.k.reload()
        self.source["approved_for_live"] = True
        package = json.loads(self.catalog.read_text(encoding="utf-8"))
        package["content"]["nhom_san_pham"][0]["san_pham"][1]["combo_components"][0][
            "quantity"
        ] = None
        self.catalog.write_text(json.dumps(package), encoding="utf-8")
        self.k.reload()
        out = self.service.save()
        self.assertEqual(out["products"], 1)
        self.assertTrue(out["excluded"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class IncomingBundleGuardTests(DefaultTests):
    def _bundle_file(self, rows):
        content = {"nhom_san_pham": [{"nhom": "X", "san_pham": rows}], "images": {}}
        bundle = {"schema_version": 1, "version": "2026-01-01T00:00:00+00:00",
                  "publisher": "x", "content": content,
                  "content_sha256": content_hash(content)}
        path = self.root / "incoming.json"
        path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_incoming_prices_promotions_stripped_and_pending(self):
        row = {**self.source, "san_pham_id": "N", "approved_for_live": True,
               "gia_goc_duyet": "99k", "live_promotion": "tang 1", "la_truong_la": 1,
               "combo_components": []}
        row.pop("shop_product_ids")
        out = self.service.choose({"mode": "default", "source": self._bundle_file([row])})
        rec = next(r for r in self.k.all_records() if r["san_pham_id"] == "N")
        raw = self.catalog.read_text(encoding="utf-8")
        for bad in ("99k", "tang 1", "la_truong_la", "machine alias"):
            self.assertNotIn(bad, raw)
        self.assertFalse(rec.get("approved_for_live"))
        self.assertEqual(out["pending_approval"], 1)

    def test_owner_approved_profile_not_downgraded(self):
        self.write_profiles()
        self.k.reload()
        before = next(r for r in self.k.all_records() if r["san_pham_id"] == "A")
        incoming = {**self.source, "approved_for_live": False,
                    "gia_goc_duyet": "other price", "quy_cach_dong_goi": "1L",
                    "huong_dan_su_dung": ["99ml / 16L"], "cong_dung": ["Rễ"]}
        out = self.service.choose(
            {"mode": "default", "source": self._bundle_file([incoming])}
        )
        rec = next(r for r in self.k.all_records() if r["san_pham_id"] == "A")
        self.assertEqual(rec, before)
        self.assertTrue(rec["approved_for_live"])
        self.assertEqual(rec["gia_goc_duyet"], "secret price")
        self.assertEqual(out["skipped_existing"], 1)

    def test_orphan_approved_mapping_does_not_activate_new_profile(self):
        mapping = self.root / "data/machine/mapping.json"
        mapping.parent.mkdir(parents=True, exist_ok=True)
        mapping.write_text(json.dumps({"assignments": {"sku-1": {
            "product_code": "X", "approved_for_live": True}}}), encoding="utf-8")
        row = {**self.source, "san_pham_id": "X"}
        row.pop("shop_product_ids")
        out = self.service.choose({"mode": "default", "source": self._bundle_file([row])})
        self.k.reload()
        self.assertIn("X", {r["san_pham_id"] for r in self.k.all_records()})
        self.assertNotIn("X", self.k.approved_product_codes())
        self.assertNotIn("sku-1", self.k.by_shop_id)
        self.assertEqual(out["unmapped_stale"], 1)
