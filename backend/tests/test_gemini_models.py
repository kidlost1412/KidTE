import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
from ai_providers import AIProviderError, GeminiProvider, _ModelRejected
from config_settings import clear_gemini_model, save_gemini_model


def model(name, methods=("generateContent",)):
    return {
        "name": "models/" + name,
        "displayName": name.upper(),
        "supportedGenerationMethods": list(methods),
    }


class Response(io.BytesIO):
    def __init__(self, value):
        super().__init__(json.dumps(value).encode())


class ModelTests(unittest.TestCase):
    def test_per_read_override_leaves_global_model_unchanged(self):
        import product_ingest

        original = GeminiProvider("test-key", "gemini-3.8-flash")
        override = GeminiProvider("test-key", "gemini-3.5-flash")
        service = product_ingest.ProductIngest(None, original, None)
        with (
            patch.object(
                product_ingest, "GeminiProvider", return_value=override
            ) as factory,
            patch.object(
                override,
                "generate",
                return_value='{"san_pham":[{"ten_san_pham":"P0","nguon":"n1"}]}',
            ),
        ):
            result = service.extract(
                {
                    "text": "P0 technical details from the manufacturer.",
                    "provider": "gemini",
                    "gemini_model": "gemini-3.5-flash",
                }
            )
        factory.assert_called_once_with("test-key", "gemini-3.5-flash")
        self.assertEqual(original.explicit_model, "gemini-3.8-flash")
        self.assertEqual(result["model"], "gemini-3.5-flash")

    def test_primary_list_pagination_cache_and_metadata_only(self):
        calls = []

        def opener(req, timeout):
            calls.append(req.full_url)
            self.assertEqual(req.get_method(), "GET")
            if "pageToken=" not in req.full_url:
                return Response(
                    {"models": [model("gemini-3.8-flash")], "nextPageToken": "next"}
                )
            return Response(
                {
                    "models": [
                        model("gemini-3.5-flash"),
                        model("gemini-2.5-flash-lite"),
                        model("gemini-flash-latest"),
                        model("gemini-3-image-flash"),
                        model("text-embedding", ("embedContent",)),
                    ]
                }
            )

        p = GeminiProvider("secret-test-key", opener=opener)
        out = p.available_models()
        self.assertEqual(
            {m["id"] for m in out["models"]},
            {
                "gemini-3.8-flash",
                "gemini-3.5-flash",
                "gemini-2.5-flash-lite",
                "gemini-flash-latest",
            },
        )
        self.assertNotIn("secret-test-key", json.dumps(out))
        self.assertEqual(len(calls), 2)
        p.available_models()
        self.assertEqual(len(calls), 2)
        p.available_models(force=True)
        self.assertEqual(len(calls), 4)

    def test_fixed_model_fallback_does_not_upgrade(self):
        p = GeminiProvider("key", "gemini-3.5-flash")
        p._candidates = [
            "gemini-flash-latest",
            "gemini-3.8-flash",
            "gemini-3.5-flash",
            "gemini-2.5-flash",
            "gemini-3.1-flash-lite",
        ]
        self.assertEqual(
            p._chain(),
            ["gemini-3.5-flash", "gemini-2.5-flash", "gemini-3.1-flash-lite"],
        )
        tried = []

        def post(name, body):
            tried.append(name)
            if name == "gemini-3.5-flash":
                raise _ModelRejected(
                    503, name, AIProviderError("gemini_overloaded", "busy")
                )
            return '{"ok":true}'

        with (
            patch.object(p, "_post", side_effect=post),
            patch.object(p, "_refresh", return_value=True),
        ):
            p._sleep = lambda _: None
            p.generate("system", "user")
        self.assertEqual(tried, ["gemini-3.5-flash", "gemini-2.5-flash"])
        self.assertEqual(p.last_model, "gemini-2.5-flash")
        self.assertEqual(p.explicit_model, "gemini-3.5-flash")

    def test_auto_and_latest_alias_still_use_all_candidates(self):
        for chosen in ("", "gemini-flash-latest"):
            p = GeminiProvider("key", chosen)
            p._candidates = [
                "gemini-flash-latest",
                "gemini-3.8-flash",
                "gemini-3.5-flash",
            ]
            self.assertIn("gemini-3.8-flash", p._chain())

    def test_error_keeps_cached_list_and_never_exposes_key(self):
        fail = [False]

        def opener(req, timeout):
            if fail[0]:
                raise urllib.error.HTTPError(
                    req.full_url, 503, "busy", {}, io.BytesIO(b"{}")
                )
            return Response({"models": [model("gemini-3.5-flash")]})

        p = GeminiProvider("hidden-secret", opener=opener)
        p.available_models()
        fail[0] = True
        out = p.available_models(force=True)
        self.assertTrue(out["stale"])
        self.assertEqual(len(out["models"]), 1)
        self.assertTrue(out["error"])
        self.assertNotIn("hidden-secret", json.dumps(out))

    def test_missing_key_and_key_change(self):
        p = GeminiProvider()
        self.assertEqual(p.available_models()["models"], [])
        self.assertTrue(p.available_models()["error"])
        p._model_options = [{"id": "old"}]
        p.configure(api_key="new-key")
        self.assertEqual(p._model_options, [])

    def test_saving_fixed_model_and_returning_to_auto_preserves_other_settings(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text(
                "GEMINI_API_KEY=keep-key\nOTHER=keep-other\n", encoding="utf-8"
            )
            save_gemini_model("gemini-3.5-flash", env)
            self.assertIn("GEMINI_MODEL=gemini-3.5-flash", env.read_text())
            clear_gemini_model(env)
            self.assertNotIn("GEMINI_MODEL=", env.read_text())
            self.assertIn("GEMINI_API_KEY=keep-key", env.read_text())
            self.assertIn("OTHER=keep-other", env.read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
