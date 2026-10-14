import ast
import base64
import io
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
import product_ingest as p
from PIL import Image
from product_web import parse_product_page

BASE = "https://example.com/"


def jpeg(color="red"):
    out = io.BytesIO()
    Image.new("RGB", (30, 30), color).save(out, "JPEG")
    return out.getvalue()


def page(url, title="", links=(), image=None):
    return {
        "url": url,
        "text": title + " text from manufacturer with technical details",
        "product_page": bool(title),
        "product_name": title,
        "links": list(links),
        "og_image": image,
        "image_url": BASE + title + ".jpg" if image else "",
    }


class Provider:
    configured = True
    supports_files = True
    name = "gemini"
    model = "fake-for-regression-only"

    def __init__(self):
        self.calls = []

    def generate(self, system, user, files):
        self.calls.append(user)
        import re

        rows = []
        for sid, body in re.findall(r"=== NGUỒN (n\d+) [^\n]+\n([^=]+)", user):
            rows.append(
                {
                    "ten_san_pham": body.split(" text from manufacturer")[0]
                    .strip()
                    .split("\n")[-1],
                    "nguon": sid,
                }
            )
        return json.dumps({"san_pham": rows})


class Workspace:
    def __init__(self, root):
        self.root, self.records, self.images = root, [], {}

    def create_profile(self, payload):
        record = {"id": "local_" + str(len(self.records)), **payload}
        self.records.append(record)
        return record

    def patch(self, resource, rid, payload):
        next(r for r in self.records if r["id"] == rid).update(payload["changes"])

    def _touch(self, *groups):
        pass

    def set_draft_product(self, rid, pid):
        pass

    def save_product_image(self, pid, payload):
        self.images[pid] = base64.b64decode(payload["data_url"].split(",")[1])


class ImportTests(unittest.TestCase):
    def test_repeated_ingredient_names_preserve_each_distinct_amount(self):
        import product_ingest

        pairs = product_ingest._as_pairs(
            [
                {"ten": "Zn", "ham_luong": "5000ppm"},
                {"ten": "Zn", "ham_luong": "50ppm"},
                {"ten": "Zn", "ham_luong": "50ppm"},
            ]
        )
        self.assertEqual(pairs["Zn"], "5000ppm")
        self.assertEqual(pairs["Zn (dòng 2)"], "50ppm")
        self.assertEqual(len(pairs), 2)

    def test_parser_import_without_frozen_missing_stdlib_modules(self):
        import subprocess

        code = """import sys
class MissingParser:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('html.parser', '_markupbase'):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, MissingParser())
sys.path.insert(0, sys.argv[1])
import product_ingest, product_web
assert product_web.HTMLParser.__module__ == 'product_html_parser'
assert product_web.parse_product_page('<h1 class="product_title">A</h1>', 'https://example.com/product/a')['product_page']
"""
        result = subprocess.run(
            [sys.executable, "-c", code, str(SRC)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_real_site_accordion_keeps_ingredients_and_dose(self):
        raw = (ROOT / "backend/tests/fixtures/product-ingest/canxi-bo.html").read_text(
            encoding="utf-8"
        )
        result = parse_product_page(
            raw,
            "https://nongduoctoancaugroup.com/san-pham/canxi-bo-sua-ra-hoa-dau-qua-bong-qua-chong-rung-hoa/",
        )
        self.assertIn("10.7%", result["product_text"])
        self.assertIn("40.000 ppm", result["product_text"])
        self.assertIn("20-25ml", result["product_text"])
        self.assertIn("16 lít", result["product_text"])

    def test_lazy_gallery_beats_site_logo_and_excludes_related(self):
        raw = """<meta property="og:image" content="/logo.jpg"><h1 class="product_title">A</h1>
        <div class="woocommerce-product-gallery"><img src="data:image/svg+xml,x" data-src="/a.jpg"></div>
        <div class="product-short-description">Directions A</div><div id="tab-description">Ingredients A</div>
        <div class="related products"><h2>B</h2><img src="/b.jpg"></div>"""
        result = parse_product_page(raw, BASE + "product/a/")
        self.assertEqual(result["image_urls"][0], BASE + "a.jpg")
        self.assertNotIn("B", result["product_text"])

    def test_jsonld_product_images(self):
        raw = (
            '<h1>A</h1><script type="application/ld+json">'
            + json.dumps(
                {
                    "@graph": [
                        {"@type": "Product", "name": "A", "image": {"url": "/a.jpg"}}
                    ]
                }
            )
            + "</script>"
        )
        result = parse_product_page(raw, BASE + "items/a/")
        self.assertTrue(result["product_page"])
        self.assertEqual(result["image_urls"], [BASE + "a.jpg"])

    def test_no_logo_on_catalog(self):
        result = parse_product_page(
            '<meta property="og:image" content="/logo.jpg">', BASE
        )
        self.assertFalse(result["product_page"])
        self.assertEqual(result["image_urls"], [])

    def test_catalog_url_defaults_to_crawling_pagination(self):
        pages, stats = self.catalog(), {}
        p.prepare_sources({"url": BASE + "shop/"}, fetch=pages.__getitem__, stats=stats)
        self.assertTrue(stats["enabled"])
        self.assertEqual(stats["products"], 12)

    def test_link_discovery_filters_actions_and_unrelated_siblings(self):
        raw = """<a href="/san-pham/">All</a><div class="product-small"><a href="/items/a/">A</a></div>
        <a href="/news/">News</a><a href="/san-pham/?add-to-cart=1">Cart</a>
        <a class="page-numbers" href="/san-pham/page/2/">2</a>
        <a href="https://other.com/product/a/">External</a><a href="/items/a/?utm_source=x">A</a>"""
        links = p.find_product_links(raw, BASE)
        self.assertEqual(
            links, [BASE + "san-pham/", BASE + "san-pham/page/2/", BASE + "items/a/"]
        )

    def catalog(self, count=12):
        urls = [BASE + "product/p" + str(i) + "/" for i in range(count)]
        catalog, second = BASE + "shop/", BASE + "shop/page/2/"
        pages = {
            BASE: page(BASE, links=[catalog, urls[0]]),
            catalog: page(catalog, links=urls[:6] + [second]),
            second: page(second, links=urls[6:] + [catalog]),
        }
        pages.update(
            {
                url: page(url, "P" + str(i), image=jpeg("red" if i % 2 else "blue"))
                for i, url in enumerate(urls)
            }
        )
        return pages

    def test_recursive_pagination_and_image_sources(self):
        pages, seen, stats = self.catalog(), [], {}

        def fetch(url):
            seen.append(url)
            return pages[url]

        sources = p.prepare_sources(
            {"url": BASE, "crawl": True}, fetch=fetch, stats=stats
        )
        self.assertEqual(stats["products"], 12)
        self.assertEqual(stats["images"], 12)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertLess(
            seen.index(BASE + "shop/page/2/"), seen.index(BASE + "product/p0/")
        )
        self.assertEqual(len([s for s in sources if s.get("page_source_id")]), 12)
        self.assertEqual(len(p.extraction_batches(sources)), 3)

    def test_failed_page_and_limit_reported(self):
        pages, stats = self.catalog(), {}

        def fetch(url):
            if url.endswith("p0/"):
                raise p.IngestError("unavailable")
            return pages[url]

        p.prepare_sources({"url": BASE, "crawl": True}, fetch=fetch, stats=stats)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["products"], 11)
        with patch.object(p, "CRAWL_MAX_PAGES", 3):
            p.prepare_sources({"url": BASE, "crawl": True}, fetch=fetch, stats=stats)
        self.assertTrue(stats["limited"])

    def test_multi_product_extract_save_link_roundtrip(self):
        pages, provider = self.catalog(), Provider()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(tmp)
            service = p.ProductIngest(
                types.SimpleNamespace(workspace=workspace),
                provider,
                None,
                fetch=pages.__getitem__,
            )
            service.suggest_skus = lambda rid: []
            result = service.extract({"url": BASE, "provider": "gemini", "crawl": True})
            self.assertEqual(len(result["products"]), 12)
            self.assertEqual(len(provider.calls), 3)
            selected = set()
            for i, product in enumerate(result["products"]):
                image_id = product["default_image_source_id"]
                self.assertTrue(image_id)
                self.assertNotIn(image_id, selected)
                selected.add(image_id)
                out = service.create_draft(
                    {
                        "fields": product["fields"],
                        "source_url": product["source_url"],
                        "session_id": result["session_id"],
                        "image": {"source_id": image_id},
                    }
                )
                self.assertTrue(out["image_saved"])
                self.assertEqual(
                    workspace.records[-1]["nguon_chinh_thuc"], product["source_url"]
                )
                self.assertTrue(service.staged_image_path(out["id"]).is_file())
                self.assertTrue(service.link_draft(out["id"], str(i))["image_applied"])
                self.assertTrue(workspace.images[str(i)])

    def test_wrong_source_id_repaired_by_exact_product_title(self):
        sources = [
            dict(
                page(BASE + "product/a/", "A"),
                kind="url",
                id="n1",
                label=BASE + "product/a/",
            ),
            {"kind": "og_image", "id": "n2", "page_source_id": "n1"},
        ]
        product = p.validate_products(
            {"san_pham": [{"ten_san_pham": "A", "nguon": "n999"}]}
        )["products"][0]
        p.attach_product_sources([product], sources)
        self.assertEqual(product["default_image_source_id"], "n2")
        self.assertEqual(p._name_key("CANXI BỔ SỮA"), p._name_key("canxi bo sua"))

    def test_expired_image_does_not_create_partial_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(tmp)
            now = [0]
            service = p.ProductIngest(
                types.SimpleNamespace(workspace=workspace),
                Provider(),
                None,
                clock=lambda: now[0],
            )
            sid = service._remember([{"id": "n1", "kind": "image", "data": jpeg()}])
            now[0] = p.SESSION_TTL + 1
            with self.assertRaises(p.IngestError):
                service.create_draft(
                    {
                        "fields": {"ten_san_pham": "A product"},
                        "session_id": sid,
                        "image": {"source_id": "n1"},
                    }
                )
            self.assertEqual(workspace.records, [])

    def test_partial_ai_failure_keeps_success_with_warning(self):
        pages, provider = self.catalog(), Provider()
        generate = provider.generate

        def fail_second(*args):
            if provider.calls:
                raise p.AIProviderError("gemini_quota", "quota reached")
            return generate(*args)

        provider.generate = fail_second
        service = p.ProductIngest(None, provider, None, fetch=pages.__getitem__)
        result = service.extract({"url": BASE, "crawl": True})
        self.assertEqual(len(result["products"]), 4)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["batches_remaining"], 2)

    def test_source_details_batch_does_not_contain_homepage_cards(self):
        sources = p.prepare_sources({"url": BASE}, fetch=self.catalog().__getitem__)
        batches = p.extraction_batches(sources)
        self.assertTrue(all(s.get("product_page") for batch in batches for s in batch))

    def test_internal_urls_still_blocked(self):
        with patch.object(p, "_resolve", return_value=["127.0.0.1"]):
            with self.assertRaises(p.IngestError):
                p.check_public_http_url(BASE)

    def test_profile_thumbnail_prefers_saved_local_image(self):
        # Exercise the actual server methods without starting its workers/databases.
        tree = ast.parse((SRC / "server.py").read_text(encoding="utf-8"))
        methods = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and n.name in {"_profile_image_url", "profile_thumb_path"}
        ]
        namespace = {"clean": lambda value, limit: str(value or "")[:limit]}
        exec(
            compile(ast.Module(body=methods, type_ignores=[]), "server.py", "exec"),
            namespace,
        )
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "a.webp"
            local.write_bytes(jpeg())
            record = {"san_pham_id": "a"}
            engine = types.SimpleNamespace(
                ingest=types.SimpleNamespace(staged_image_path=lambda rid: local),
                knowledge=types.SimpleNamespace(all_records=lambda: [record]),
            )
            self.assertTrue(
                namespace["_profile_image_url"](engine, record).startswith(
                    "/api/profile/thumb/a?"
                )
            )
            self.assertEqual(namespace["profile_thumb_path"](engine, "a"), local)
            with self.assertRaises(ValueError):
                namespace["profile_thumb_path"](engine, "missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
