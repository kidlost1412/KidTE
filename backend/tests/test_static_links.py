"""Catch missing files referenced by the web UI, Chrome extension and plugins."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[2]
WEB = PROJECT / "backend" / "src" / "web"
EXTENSION = PROJECT / "extension"


class _LocalLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in {"link", "script", "img"}:
            self.links.extend(value for key, value in attrs
                              if key in {"href", "src"} and value)


def test_web_html_and_javascript_links_exist():
    html = WEB / "operator.html"
    parser = _LocalLinks()
    parser.feed(html.read_text(encoding="utf-8"))
    for link in parser.links:
        if link.startswith("/assets/"):
            assert (WEB / link.removeprefix("/assets/")).is_file(), link

    # Check every local ES module import, including those below the entry page.
    import_pattern = re.compile(
        r"\b(?:from\s*|import\s*\(\s*|import\s*)['\"](\.[^'\"]+)['\"]"
    )
    for source in (WEB / "app").rglob("*.js"):
        for relative in import_pattern.findall(source.read_text(encoding="utf-8")):
            target = (source.parent / relative).resolve()
            assert target.is_relative_to(WEB.resolve()), (source, relative)
            assert target.is_file(), (source, relative)


def test_every_web_module_is_reachable_from_bootstrap():
    import_pattern = re.compile(
        r"\b(?:from\s*|import\s*\(\s*|import\s*)['\"](\.[^'\"]+)['\"]"
    )
    entry = WEB / "app" / "bootstrap.js"
    pending = [entry]
    reached = set()
    while pending:
        source = pending.pop()
        if source in reached:
            continue
        reached.add(source)
        for relative in import_pattern.findall(source.read_text(encoding="utf-8")):
            pending.append((source.parent / relative).resolve())
    all_modules = set((WEB / "app").rglob("*.js"))
    assert all_modules == reached, sorted(str(item.relative_to(WEB))
                                       for item in all_modules - reached)


def test_extension_manifest_and_popup_links_exist():
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    referenced = list(manifest.get("icons", {}).values())
    action = manifest.get("action", {})
    referenced.extend(action.get("default_icon", {}).values())
    referenced.append(action["default_popup"])
    referenced.append(manifest["background"]["service_worker"])
    for script in manifest.get("content_scripts", []):
        referenced.extend(script.get("js", []))
        referenced.extend(script.get("css", []))
    for name in referenced:
        assert (EXTENSION / name).is_file(), name

    parser = _LocalLinks()
    parser.feed((EXTENSION / action["default_popup"]).read_text(encoding="utf-8"))
    for link in parser.links:
        if not link.startswith(("http://", "https://", "data:")):
            assert (EXTENSION / link).is_file(), link


def test_plugin_commands_exist():
    for manifest_path in (PROJECT / "plugins").glob("*/plugin.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        command = manifest.get("lenh") or []
        assert command, manifest_path
        assert (manifest_path.parent / command[0]).is_file(), manifest_path


def test_env_template_uses_current_names():
    template = (PROJECT / "backend" / ".env.example").read_text(encoding="utf-8")
    assert "LIVE_RE_" not in template
    for name in ("KID_TE_AUDIO_DEVICE", "KID_TE_WHISPER_MODEL", "KID_TE_EXTENSION_DIR"):
        assert re.search(rf"^{name}=", template, re.M), name
