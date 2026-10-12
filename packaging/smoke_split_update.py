"""End-to-end smoke of the split runtime/app/extensions update path (local, TEST key only).

Uses a built ``dist/KidTE`` merged tree (see packaging/build.ps1) and the real frozen
``KidTELauncher.exe`` + ``KidTE.exe`` against a throw-away install directory. The catalog is
signed with a freshly generated TEST key (never the release key); the app-side update code
(UpdateManager) runs in this process with a transport that serves local files, the launcher and
the runtime run as real processes on one local port.

  python packaging/smoke_split_update.py --dist dist/KidTE --work C:\\temp\\kte-smoke --port 48997

Proves: (a) app-only update downloads only the small app zip and activates; (b) a failing new
generation rolls back automatically; (c) local garbage collection leaves active + previous only;
(d) a monolithic core install migrates to runtime+app+extensions (and back via rollback).
Only processes started by this script are ever stopped.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "packaging"))
import build_catalog  # noqa: E402
import stage_runtime  # noqa: E402
import update_launcher  # noqa: E402
import update_release  # noqa: E402
import update_trust  # noqa: E402
from component_store import ComponentStore  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from update_catalog import CORE_DATA_SCHEMA, canonical_json  # noqa: E402
from update_manager import UpdateManager  # noqa: E402

BASE_URL = "https://github.com/kidlost1412/KidTE/releases/download/smoke"
KEY = Ed25519PrivateKey.generate()
KEY_ID = "smoketest"
PUBLIC = base64.b64encode(KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
update_trust.TRUSTED_KEYS[KEY_ID] = PUBLIC
RESULTS: list[tuple[str, bool, str]] = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), str(detail)))
    print(("PASS " if condition else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)
    return bool(condition)


def sign(payload):
    envelope = {"schema_version": 2, "key_id": KEY_ID, "signed": payload,
                "signature": base64.b64encode(KEY.sign(canonical_json(payload))).decode()}
    return canonical_json(envelope) + b"\n"


class LocalTransport:
    """Serves the signed catalog and package archives from local files and records downloads."""

    def __init__(self):
        self.raw = b""
        self.files: dict[str, Path] = {}
        self.downloads: list[tuple[str, int]] = []

    def fetch_catalog(self, channel="stable"):
        return self.raw

    def download(self, url, destination, *, expected_sha256, expected_bytes, progress=None):
        source = self.files[url]
        shutil.copyfile(source, destination)
        self.downloads.append((source.name, source.stat().st_size))
        return destination


def wait_idle(manager, timeout=900):
    deadline = time.monotonic() + timeout
    while manager.state()["busy"] and time.monotonic() < deadline:
        time.sleep(0.1)
    return manager.state()


class Install:
    """A throw-away ``<work>/<name>/App`` + ``DuLieu`` pair."""

    def __init__(self, work: Path, name: str, port: int, dist: Path):
        self.root = work / name
        self.app = self.root / "App"
        self.port = port
        self.dist = dist
        self.launcher_proc = None
        self.child_pid = None

    def lay_down_installer_tree(self):
        shutil.copytree(self.dist, self.app)
        (self.root / "DuLieu").mkdir(exist_ok=True)

    def start(self, *, expect_ready=True, timeout=90):
        env = {**os.environ, "CHAT_PORT": str(self.port), "KIDTE_GC_GRACE_SECONDS": "0"}
        env.pop("KIDTE_APP_ROOT", None)
        env.pop("KIDTE_APP_PACKAGE_DIR", None)
        self.launcher_proc = subprocess.Popen([str(self.app / "KidTELauncher.exe"), "--no-ui"], env=env,
                                              cwd=str(self.app), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        info = None
        while time.monotonic() < deadline and expect_ready:
            if self.launcher_proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/update-ready", timeout=1) as response:
                    value = json.loads(response.read())
                if value.get("ok") and value.get("launcher_nonce"):
                    info = value
                    break
            except Exception:  # noqa: BLE001 - not up yet
                time.sleep(0.2)
        if info is None:
            return None
        self.child_pid = info["pid"]
        # The launcher confirms the generation and then runs garbage collection right after readiness.
        store = ComponentStore(self.app)
        settle = time.monotonic() + 30
        while time.monotonic() < settle:
            journal = json.loads((self.app / "update-journal.json").read_text(encoding="utf-8")) \
                if (self.app / "update-journal.json").exists() else {"state": "committed"}
            if journal["state"] == "committed":
                break
            time.sleep(0.2)
        time.sleep(4)  # let the post-confirm cleanup finish
        del store
        return info

    def health(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=5) as response:
            return json.loads(response.read())

    def runtime_image(self):
        import psutil
        return psutil.Process(self.child_pid).exe()

    def stop(self):
        if self.child_pid:
            subprocess.run(["taskkill", "/PID", str(self.child_pid), "/F"], capture_output=True)
        if self.launcher_proc is not None:
            try:
                self.launcher_proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.launcher_proc.kill()
        self.child_pid = None
        self.launcher_proc = None
        time.sleep(1)

    def active(self):
        return json.loads((self.app / "active.json").read_text(encoding="utf-8"))

    def versions(self):
        return {key: row["version"] for key, row in self.active()["components"].items()}

    def package_dirs(self):
        packages = self.app / "packages"
        return sorted(f"{a.name}/{v.name}" for a in packages.iterdir() if a.is_dir() for v in a.iterdir())

    def manager(self, transport, *, caps="split-components"):
        return UpdateManager(self.app, transport=transport, trusted_keys={KEY_ID: PUBLIC}, launcher_caps=caps)


def package(tree: Path, component: str, out: Path):
    return update_release.package_directory(tree, out, component=component)


def variant_tree(source: Path, target: Path, component: str, edit):
    """Copy the files a package owns from the merged tree, then let ``edit`` change them."""
    for path in source.rglob("*"):
        if path.is_file():
            relative = path.relative_to(source).as_posix()
            if stage_runtime.component_for(relative) == component:
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
    edit(target)
    return target


def catalog_payload(sequence, components):
    now = datetime.now(timezone.utc)
    return {"schema_version": 2, "repository": "kidlost1412/KidTE", "channel": "beta", "sequence": sequence,
            "issued_at": (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), "components": components}


def publish(transport, payload):
    transport.raw = sign(payload)


def check_catalog(manager, transport):
    manager.action("check", {"channel": "beta"})
    state = wait_idle(manager)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True, help="merged tree built by build.ps1 (with KidTELauncher.exe)")
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--port", type=int, default=48997)
    args = parser.parse_args()
    dist, work = args.dist.resolve(), args.work.resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    zips = work / "zips"
    zips.mkdir()

    # ---- release artifacts -------------------------------------------------------------------
    print("== packaging ==", flush=True)
    sizes = {}
    base = {}
    for component in ("runtime", "app", "extensions"):
        facts = package(dist, component, zips / f"{component}-base.zip")
        base[component] = facts
        sizes[component] = (facts["archive_bytes"], facts["unpacked_bytes"], len(facts["files"]))
        print(f"{component:11s} zip={facts['archive_bytes']:>11,} unpacked={facts['unpacked_bytes']:>12,} files={len(facts['files'])}")
    transport = LocalTransport()

    def record(component, version, zip_path, commit="d" * 40, runtime_api=1):
        url = f"{BASE_URL}/{zip_path.name}"
        transport.files[url] = zip_path
        return build_catalog.new_component_record(component, {"zip": str(zip_path), "url": url, "version": version,
                                                              "source_commit": commit}, runtime_api=runtime_api)

    base_runtime = record("runtime", "1.0.0", zips / "runtime-base.zip")
    base_app = record("app", "1.15.0", zips / "app-base.zip")
    base_ext = record("extensions", "1.0.0", zips / "extensions-base.zip")

    def app_zip(version, edit):
        tree = variant_tree(dist, work / f"tree-app-{version}", "app", edit)
        out = zips / f"app-{version}.zip"
        package(tree, "app", out)
        shutil.rmtree(tree)
        return out

    def set_version(version):
        return lambda tree: (tree / "VERSION").write_text(version + "\n", encoding="utf-8")

    def broken(version):
        def edit(tree):
            (tree / "VERSION").write_text(version + "\n", encoding="utf-8")
            (tree / "backend" / "src" / "app_entry.py").write_text("raise SystemExit(9)  # deliberately broken build\n",
                                                                   encoding="utf-8")
        return edit

    # ================================================================== split install: (a)(b)(c)
    print("\n== install A: installer-style split baseline ==", flush=True)
    a = Install(work, "A", args.port, dist)
    a.lay_down_installer_tree()
    bootstrap = catalog_payload(10, [base_runtime, base_app, base_ext])
    (a.app / "bootstrap-request.json").write_bytes(sign(bootstrap))
    update_launcher._prepare_installer_core(a.app, ComponentStore(a.app))
    check("baseline registered without any download", (a.app / "pending.json").exists()
          and not (a.app / "bootstrap-request.json").exists())
    info = a.start()
    check("launcher activated the baseline generation and runtime is ready", info is not None, a.versions() if info else "no ready")
    if info is None:
        return finish()
    image = Path(a.runtime_image())
    check("runtime process image is the package copy",
          image.resolve() == (a.app / "packages" / "runtime" / "1.0.0" / "KidTE.exe").resolve(), image)
    check("app version served = 1.15.0", a.health()["version"] == "1.15.0", a.health()["version"])
    a.stop()

    # (a) app-only update ---------------------------------------------------------------------
    print("\n== (a) app-only update 1.15.0 -> 1.15.1 ==", flush=True)
    app_1 = record("app", "1.15.1", app_zip("1.15.1", set_version("1.15.1")))
    publish(transport, catalog_payload(20, [base_runtime, app_1, base_ext]))
    manager = a.manager(transport)
    check_catalog(manager, transport)
    plan = manager.action("plan", {"components": {"app": "1.15.1"}})
    check("plan lists only the app", [p["id"] for p in plan["packages"]] == ["app"], plan)
    manager.action("install", {"components": {"app": "1.15.1"}})
    state = wait_idle(manager)
    check("only the app archive was downloaded", [d[0] for d in transport.downloads] == ["app-1.15.1.zip"],
          f"{transport.downloads} (full core would be ~{sizes['runtime'][0] + sizes['app'][0] + sizes['extensions'][0]:,} bytes)")
    check("pending generation = runtime 1.0.0 + app 1.15.1 + extensions 1.0.0",
          state["pending"] == {"runtime": "1.0.0", "app": "1.15.1", "extensions": "1.0.0"} and not state["error"], state["pending"])
    ext_marker = (a.app / "extensions-sync.json").read_bytes()
    ext_mtime = (a.app / "extension" / "manifest.json").stat().st_mtime_ns
    info = a.start()
    check("restart activates app 1.15.1", info is not None and a.health()["version"] == "1.15.1",
          a.health()["version"] if info else "no ready")
    check("extension folder untouched (no Chrome reload needed)",
          (a.app / "extension" / "manifest.json").stat().st_mtime_ns == ext_mtime
          and (a.app / "extensions-sync.json").read_bytes() == ext_marker)
    a.stop()

    # (b) failing generation rolls back ----------------------------------------------------------
    print("\n== (b) broken app 1.15.2 -> automatic rollback ==", flush=True)
    app_bad = record("app", "1.15.2", app_zip("1.15.2", broken("1.15.2")))
    publish(transport, catalog_payload(21, [base_runtime, app_bad, base_ext]))
    check_catalog(manager, transport)
    manager.action("install", {"components": {"app": "1.15.2"}})
    wait_idle(manager)
    check("broken generation is pending", ComponentStore(a.app).load_pending()["components"]["app"]["version"] == "1.15.2")
    info = a.start(timeout=120)
    check("launcher fell back and the previous app is serving", info is not None and a.health()["version"] == "1.15.1",
          a.health()["version"] if info else "no ready")
    journal = json.loads((a.app / "update-journal.json").read_text(encoding="utf-8"))
    check("journal records the rollback; active stays 1.15.1", journal["state"] == "rolled_back" and a.versions()["app"] == "1.15.1",
          f"{journal['state']} {a.versions()}")
    check("failed package is not garbage-collected during rollback handling", "app/1.15.2" in a.package_dirs(), a.package_dirs())
    a.stop()

    # (c) next good release -> GC leaves active + previous only -----------------------------------
    print("\n== (c) good app 1.15.3 -> local garbage collection ==", flush=True)
    app_3 = record("app", "1.15.3", app_zip("1.15.3", set_version("1.15.3")))
    publish(transport, catalog_payload(22, [base_runtime, app_3, base_ext]))
    check_catalog(manager, transport)
    before = a.package_dirs()
    manager.action("install", {"components": {"app": "1.15.3"}})
    wait_idle(manager)
    info = a.start()
    check("app 1.15.3 active", info is not None and a.health()["version"] == "1.15.3")
    dirs = a.package_dirs()
    print("packages before:", before, "\npackages after :", dirs)
    check("GC kept exactly current + one rollback", dirs == ["app/1.15.1", "app/1.15.3", "extensions/1.0.0", "runtime/1.0.0"], dirs)
    leftovers = [p.name for p in (a.app / ".staging").iterdir() if p.name != "downloads"]
    check("no trash left in .staging", not [n for n in leftovers if n.startswith(".trash-")], leftovers)
    a.stop()
    manager.action("rollback", {})
    wait_idle(manager)
    info = a.start()
    check("manual rollback still works after GC (previous kept): 1.15.1 serving", info is not None and a.health()["version"] == "1.15.1")
    a.stop()

    # ================================================================== monolith -> split: (d)
    print("\n== install B: monolithic core 1.14.259 -> split via catalog (d) ==", flush=True)
    b = Install(work, "B", args.port, dist)
    b.lay_down_installer_tree()
    core_facts = package(dist, "core", zips / "core-legacy.zip")
    core = {"id": "core", "kind": "core", "version": "1.14.259", "platform": "windows", "architecture": "x86_64",
            "core_api": ">=1.0.0,<2.0.0", "dependencies": {}, "url": f"{BASE_URL}/core-legacy.zip", "entrypoint": "KidTE.exe",
            "restart_required": True, "migration": "none", "source_commit": "e" * 40,
            "compatibility": {"data_schema": CORE_DATA_SCHEMA}, **core_facts}
    media_tree = work / "media-tree"
    (media_tree / "tools").mkdir(parents=True)
    (media_tree / "tools" / "ffmpeg.exe").write_bytes(b"fake-ffmpeg")
    (media_tree / "tools" / "ffprobe.exe").write_bytes(b"fake-ffprobe")
    media_zip = zips / "media-tools.zip"
    media_facts = update_release.package_directory(media_tree, media_zip, component="media-tools")
    media = {"id": "media-tools", "kind": "media-tools", "version": "1.0.0", "platform": "windows", "architecture": "x86_64",
             "core_api": ">=1.0.0,<2.0.0", "dependencies": {"core": ">=1.14.255,<2.0.0"}, "url": f"{BASE_URL}/media-tools.zip",
             "entrypoint": "tools/ffmpeg.exe", "restart_required": True, "migration": "none", "source_commit": "e" * 40,
             "compatibility": {"component_api": 1}, **media_facts}
    transport.files[media["url"]] = media_zip
    store = ComponentStore(b.app)
    store.stage_baseline(core)
    stage_media = zips / "media-tools.zip"
    store.stage_package(media, stage_media)
    store.prepare({"core": core, "media-tools": media}, catalog_sequence=30)
    info = b.start()
    check("monolith generation (core 1.14.259 + media-tools) is serving", info is not None and set(b.versions()) == {"core", "media-tools"},
          b.versions() if info else "no ready")
    b.stop()
    # a libraries change (runtime 1.0.1) so the new runtime must really be downloaded

    def tweak_runtime(tree):
        pem = tree / "_internal" / "certifi" / "cacert.pem"
        pem.write_text(pem.read_text(encoding="utf-8") + "\n# runtime 1.0.1 smoke change\n", encoding="utf-8")

    runtime_tree = variant_tree(dist, work / "tree-runtime-1.0.1", "runtime", tweak_runtime)
    runtime_zip = zips / "runtime-1.0.1.zip"
    package(runtime_tree, "runtime", runtime_zip)
    shutil.rmtree(runtime_tree)
    runtime_new = record("runtime", "1.0.1", runtime_zip)
    split_catalog = catalog_payload(40, [media, runtime_new, app_1, base_ext])
    publish(transport, split_catalog)
    transport.downloads.clear()
    legacy_launcher = b.manager(transport, caps="")
    check_catalog(legacy_launcher, transport)
    try:
        legacy_launcher.action("install", {"components": {"app": None}})
        check("launcher without split capability is refused", False)
    except ValueError as exc:
        check("launcher without split capability is refused (no stranded install)", str(exc) == "update_launcher_upgrade_required"
              and not transport.downloads, str(exc))
    manager_b = b.manager(transport)
    check_catalog(manager_b, transport)
    plan = manager_b.action("plan", {"components": {"app": None}})
    print("plan:", json.dumps(plan))
    manager_b.action("install", {"components": {"app": None}})
    state = wait_idle(manager_b)
    check("migration prepared as one generation without the monolithic core",
          state["pending"] == {"runtime": "1.0.1", "app": "1.15.1", "extensions": "1.0.0", "media-tools": "1.0.0"} and not state["error"],
          state["pending"] or state["error"])
    print("downloads:", transport.downloads)
    info = b.start()
    check("restart serves the split generation: runtime 1.0.1 + app 1.15.1", info is not None and b.health()["version"] == "1.15.1"
          and b.versions() == {"runtime": "1.0.1", "app": "1.15.1", "extensions": "1.0.0", "media-tools": "1.0.0"},
          b.versions() if info else "no ready")
    if info:
        check("process image = packages/runtime/1.0.1",
              Path(b.runtime_image()).resolve() == (b.app / "packages" / "runtime" / "1.0.1" / "KidTE.exe").resolve())
    check("monolithic core kept as the single rollback target", "core/1.14.259" in b.package_dirs(), b.package_dirs())
    b.stop()
    manager_b.action("rollback", {})
    wait_idle(manager_b)
    info = b.start()
    check("rollback to the monolith works", info is not None and "core" in b.versions(), b.versions() if info else "no ready")
    b.stop()
    return finish()


def finish():
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    for name in failed:
        print("FAILED:", name)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
