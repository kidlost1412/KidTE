"""Offline black-box smoke test for the packaged component updater.

The smoke run copies a built App directory into a new work directory, prepares
signed catalog updates with a local-only transport, and starts the real
KidTELauncher.exe for each activation. Runtime data is isolated in a fresh
DuLieu directory. The tool never uses a signing key, calls a release service,
or invokes a provider API.

Steps JSON format::

    {"steps": [{
      "catalog": "catalog-1.json",
      "archives": {"core": "core-1.zip"},
      "request": {"core": "1.4.0"},
      "expected_versions": {"core": "1.4.0"},
      "expect_rollback": false
    }]}

Catalog and archive paths are resolved relative to the steps file. For a
signed but unlaunchable core fixture, set ``expect_rollback`` to true and set
``expected_versions`` to the versions active before that step.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from psutil import NoSuchProcess
from psutil import Process as PsutilProcess

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "backend" / "src"))

from component_store import ComponentStore
from update_catalog import verify_catalog
from update_manager import UpdateManager
from update_trust import TRUSTED_KEYS

READY_TIMEOUT_S = 180
MANAGER_TIMEOUT_S = 300
CATALOG_MAX_BYTES = 16 * 1024 * 1024
OUTPUT_NAME = "component-update-smoke-evidence.json"
_HTTP = build_opener(ProxyHandler({}))
_COPY_EXCLUDE = {
    ".staging", "packages", ".component-store.lock", ".kidte-launcher.lock",
    "active.json", "previous.json", "pending.json", "update-journal.json",
    "catalog-sequence.json", "launcher-startup.log", "data", "dulieu",
    "models", "weights", "logs", "runtime", ".env", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".git",
}


class SmokeFailure(RuntimeError):
    """Stable failure code suitable for a privacy-safe evidence file."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _require_directory(path: Path, code: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError:
        raise SmokeFailure(code) from None
    if not resolved.is_dir():
        raise SmokeFailure(code)
    return resolved


def _check_source_tree(source: Path) -> None:
    for directory, dirs, files in os.walk(source, followlinks=False):
        parent = Path(directory)
        for name in (*dirs, *files):
            if _is_reparse(parent / name):
                raise SmokeFailure("app_source_contains_reparse_point")


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name.casefold() in _COPY_EXCLUDE}


def _prepare_sandbox(app_dir: Path, work_dir: Path) -> tuple[Path, Path]:
    if work_dir.exists():
        raise SmokeFailure("work_dir_must_be_new")
    app_copy = work_dir / "App"
    data_root = work_dir / "DuLieu"
    work_dir.mkdir(parents=True)
    _check_source_tree(app_dir)
    try:
        shutil.copytree(app_dir, app_copy, ignore=_copy_ignore, symlinks=True)
    except (OSError, shutil.Error):
        raise SmokeFailure("app_copy_failed") from None
    for required in (app_copy / "KidTELauncher.exe", app_copy / "KidTE.exe"):
        if not required.is_file() or _is_reparse(required):
            raise SmokeFailure("packaged_launcher_or_core_missing")
    data_root.mkdir()
    return app_copy, data_root


def _load_config(path: Path) -> tuple[dict, Path]:
    try:
        resolved = path.expanduser().resolve(strict=True)
        if resolved.stat().st_size > CATALOG_MAX_BYTES:
            raise SmokeFailure("steps_file_too_large")
        config = json.loads(resolved.read_text(encoding="utf-8"))
    except SmokeFailure:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise SmokeFailure("steps_file_invalid") from None
    if not isinstance(config, dict) or set(config) != {"steps"}:
        raise SmokeFailure("steps_schema_invalid")
    steps = config.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > 10:
        raise SmokeFailure("steps_schema_invalid")
    for step in steps:
        if (not isinstance(step, dict) or
                not set(step).issubset({"catalog", "archives", "request", "expected_versions",
                                       "expect_rollback", "channel"}) or
                not {"catalog", "archives", "request", "expected_versions"}.issubset(step) or
                type(step.get("expect_rollback", False)) is not bool or
                not isinstance(step.get("archives"), dict) or not step["archives"] or
                not isinstance(step.get("request"), dict) or not step["request"] or
                not isinstance(step.get("expected_versions"), dict) or
                "core" not in step["expected_versions"] or
                step.get("channel", "stable") not in {"stable", "beta"}):
            raise SmokeFailure("steps_schema_invalid")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in step["archives"].items()):
            raise SmokeFailure("steps_schema_invalid")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in step["request"].items()):
            raise SmokeFailure("steps_schema_invalid")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in step["expected_versions"].items()):
            raise SmokeFailure("steps_schema_invalid")
        if not isinstance(step["catalog"], str):
            raise SmokeFailure("steps_schema_invalid")
    return config, resolved.parent


def _input_file(value: str, base: Path, code: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise SmokeFailure(code) from None
    if not resolved.is_file() or _is_reparse(resolved):
        raise SmokeFailure(code)
    return resolved


class LocalTransport:
    """Transport adapter that only copies archives explicitly mapped by ID."""

    def __init__(self, raw_catalog: bytes, packages: list[dict], archives: dict[str, Path]):
        self.raw_catalog = raw_catalog
        self.allowed = {}
        for package in packages:
            source = archives.get(package["id"])
            if source is not None:
                self.allowed[package["url"]] = (package["id"], source,
                                                  package["archive_bytes"], package["archive_sha256"])
        self.calls: list[dict] = []

    def fetch_catalog(self, channel="stable") -> bytes:
        if channel not in {"stable", "beta"}:
            raise SmokeFailure("unsupported_channel")
        self.calls.append({"operation": "catalog", "source": "local"})
        return self.raw_catalog

    def download(self, url, destination, *, expected_sha256, expected_bytes, progress=None):
        approved = self.allowed.get(url)
        if approved is None:
            raise SmokeFailure("package_archive_not_mapped_locally")
        component_id, source, catalog_bytes, catalog_hash = approved
        if expected_bytes != catalog_bytes or expected_sha256 != catalog_hash:
            raise SmokeFailure("transport_package_record_mismatch")
        if source.stat().st_size != expected_bytes or _sha256_file(source) != expected_sha256:
            raise SmokeFailure("local_archive_digest_mismatch")
        if destination.is_file():
            if destination.stat().st_size != expected_bytes or _sha256_file(destination) != expected_sha256:
                raise SmokeFailure("local_cached_archive_digest_mismatch")
            if progress:
                progress(expected_bytes, expected_bytes)
            self.calls.append({"operation": "archive", "component": component_id,
                               "bytes": expected_bytes, "sha256": expected_sha256, "cached": True})
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        count = 0
        with source.open("rb") as input_stream, destination.open("xb") as output_stream:
            while True:
                block = input_stream.read(1024 * 1024)
                if not block:
                    break
                count += len(block)
                if count > expected_bytes:
                    raise SmokeFailure("local_archive_size_mismatch")
                digest.update(block)
                output_stream.write(block)
                if progress:
                    progress(count, expected_bytes)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if count != expected_bytes or digest.hexdigest() != expected_sha256:
            destination.unlink(missing_ok=True)
            raise SmokeFailure("local_archive_digest_mismatch")
        self.calls.append({"operation": "archive", "component": component_id,
                           "bytes": count, "sha256": expected_sha256})
        return destination


def _parse_assignments(values: list[str], base: Path, code: str) -> dict[str, Path]:
    result = {}
    for value in values:
        component, sep, path = value.partition("=")
        if not sep or not component or not path or component in result:
            raise SmokeFailure(code)
        result[component] = _input_file(path, base, code)
    return result


def _wait_manager(manager: UpdateManager, timeout: float, operation: str) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        state = manager.state()
        if not state.get("busy"):
            if state.get("error") or state.get("operation") == "failed":
                raise SmokeFailure(f"manager_{operation}_failed")
            return state
        if time.monotonic() >= deadline:
            raise SmokeFailure(f"manager_{operation}_timeout")
        time.sleep(0.05)


def _state_summary(state: dict) -> dict:
    available = state.get("available", [])
    return {
        "enabled": bool(state.get("enabled")),
        "trusted": bool(state.get("trusted")),
        "installed": state.get("installed", {}),
        "pending": state.get("pending", {}),
        "restart_required": bool(state.get("restart_required")),
        "available": [{"id": item.get("id"), "version": item.get("version"),
                       "download_bytes": item.get("download_bytes")}
                      for item in available if isinstance(item, dict)],
        "busy": bool(state.get("busy")),
        "error_present": bool(state.get("error")),
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _request_json(base: str, path: str, *, method="GET", payload=None, timeout=5) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(base + path, data=data, method=method,
                      headers={"Accept": "application/json",
                               "Content-Type": "application/json"} if data is not None else
                      {"Accept": "application/json"})
    try:
        with _HTTP.open(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024)
            if len(raw) >= 1024 * 1024:
                raise SmokeFailure("localhost_response_too_large")
            value = json.loads(raw.decode("utf-8"))
    except SmokeFailure:
        raise
    except HTTPError as exc:
        raise SmokeFailure(f"localhost_http_{exc.code}") from None
    except (OSError, TimeoutError, URLError, UnicodeError, json.JSONDecodeError):
        raise SmokeFailure("localhost_request_failed") from None
    if not isinstance(value, dict) or value.get("ok") is False:
        raise SmokeFailure("localhost_response_invalid")
    return value


def _wait_ready(process: subprocess.Popen, base: str, data_root: Path, timeout: float) -> tuple[dict, dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SmokeFailure("launcher_exited_before_ready")
        try:
            ready = _request_json(base, "/api/update-ready", timeout=1)
            state = _request_json(base, "/api/updates", timeout=2)
        except SmokeFailure:
            time.sleep(0.25)
            continue
        if (type(ready.get("pid")) is int and ready["pid"] > 0 and
                isinstance(ready.get("launcher_nonce"), str) and len(ready["launcher_nonce"]) >= 16 and
                isinstance(ready.get("launcher_generation"), str) and ready["launcher_generation"] and
                isinstance(ready.get("version"), str) and ready["version"] and
                isinstance(ready.get("data_root"), str) and
                Path(ready["data_root"]).resolve() == data_root.resolve()):
            return ready, state
        time.sleep(0.25)
    raise SmokeFailure("launcher_ready_timeout")


def _process_executable(pid: int) -> tuple[Path, float]:
    try:
        process = PsutilProcess(pid)
        return Path(process.exe()).resolve(strict=True), process.create_time()
    except (NoSuchProcess, OSError, ValueError):
        raise SmokeFailure("ready_child_process_unavailable") from None


def _assert_child_owned(launcher: subprocess.Popen, child_pid: int) -> None:
    try:
        owner = PsutilProcess(launcher.pid)
        descendants = {item.pid for item in owner.children(recursive=True)}
    except (NoSuchProcess, OSError):
        raise SmokeFailure("launcher_process_unavailable") from None
    if child_pid not in descendants:
        raise SmokeFailure("ready_pid_not_owned_by_launcher")


def _wait_child_exit(pid: int, created: float, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            child = PsutilProcess(pid)
            if abs(child.create_time() - created) > 1.0 or not child.is_running():
                return
        except (NoSuchProcess, OSError):
            return
        time.sleep(0.1)
    raise SmokeFailure("core_did_not_exit_after_shutdown")


def _shutdown_owned(launcher: subprocess.Popen, base: str | None,
                    child_identity: tuple[int, float] | None) -> int:
    if launcher.poll() is None and base is not None:
        try:
            _request_json(base, "/api/shutdown", method="POST", payload={}, timeout=5)
        except SmokeFailure:
            pass
    try:
        code = launcher.wait(timeout=45)
    except subprocess.TimeoutExpired:
        # This is the exact launcher process returned by our own Popen call.
        # On Windows its Job Object closes and terminates only its app child.
        launcher.terminate()
        try:
            code = launcher.wait(timeout=10)
        except subprocess.TimeoutExpired:
            launcher.kill()
            code = launcher.wait(timeout=10)
    if child_identity is not None:
        _wait_child_exit(child_identity[0], child_identity[1], 10)
    return int(code)


def _versions(mapping: dict | None) -> dict[str, str]:
    if not isinstance(mapping, dict):
        return {}
    result = {}
    for component_id, component in mapping.get("components", {}).items():
        package = component.get("package") if isinstance(component, dict) else None
        if isinstance(package, dict) and isinstance(package.get("version"), str):
            result[component_id] = package["version"]
    return result


def _wait_for_confirmed_state(app_root: Path, expected_generation: str,
                              expected_versions: dict[str, str], base: str) -> dict:
    deadline = time.monotonic() + 15
    last = None
    while time.monotonic() < deadline:
        last = _request_json(base, "/api/updates", timeout=3)
        installed = last.get("installed")
        if (isinstance(installed, dict) and installed == expected_versions and
                not last.get("pending") and last.get("restart_required") is False):
            active = ComponentStore(app_root).load_active()
            journal_path = app_root / "update-journal.json"
            try:
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                journal = {}
            if (active and active.get("generation") == expected_generation and
                    journal.get("state") in {"committed", "rolled_back"}):
                return {"api": _state_summary(last), "active": active, "journal": journal}
        time.sleep(0.1)
    raise SmokeFailure("activation_confirmation_timeout")


def _run_step(index: int, step: dict, base_dir: Path, app_root: Path,
              data_root: Path, timeout: float) -> dict:
    started = time.monotonic()
    catalog_path = _input_file(step["catalog"], base_dir, "catalog_file_invalid")
    raw_catalog = catalog_path.read_bytes()
    if len(raw_catalog) > CATALOG_MAX_BYTES:
        raise SmokeFailure("catalog_too_large")
    channel = step.get("channel", "stable")
    catalog = verify_catalog(raw_catalog, TRUSTED_KEYS, channel=channel)
    archives = _parse_assignments(
        [f"{key}={value}" for key, value in step["archives"].items()],
        base_dir, "archive_mapping_invalid")
    transport = LocalTransport(raw_catalog, catalog["components"], archives)
    manager = UpdateManager(app_root, transport=transport, trusted_keys=TRUSTED_KEYS)
    before = ComponentStore(app_root).load_active()
    before_versions = _versions(before)

    manager.action("check", {"channel": channel})
    _wait_manager(manager, min(timeout, 30), "check")
    plan = manager.action("plan", {"components": step["request"]})
    if not isinstance(plan, dict) or plan.get("ok") is not True:
        raise SmokeFailure("manager_plan_failed")
    plan_packages = plan.get("packages")
    if not isinstance(plan_packages, list) or not plan_packages:
        raise SmokeFailure("manager_plan_empty")
    planned = {item["id"]: item["version"] for item in plan_packages
               if isinstance(item, dict) and isinstance(item.get("id"), str)
               and isinstance(item.get("version"), str)}
    if len(planned) != len(plan_packages) or not set(planned).issubset(archives):
        raise SmokeFailure("planned_archive_mapping_missing")
    expected_pending = dict(before_versions)
    expected_pending.update(planned)
    manager.action("install", {"components": step["request"]})
    prepared_state = _wait_manager(manager, timeout, "install")
    pending = ComponentStore(app_root).load_pending()
    if pending is None or _versions(pending) != expected_pending:
        raise SmokeFailure("prepared_generation_mismatch")
    if _versions(ComponentStore(app_root).load_active()) != before_versions:
        raise SmokeFailure("prepare_changed_active_generation")
    if manager.state().get("error"):
        raise SmokeFailure("manager_state_error_after_install")

    expected_versions = step["expected_versions"]
    expect_rollback = step.get("expect_rollback", False)
    if expect_rollback:
        if expected_versions != before_versions:
            raise SmokeFailure("rollback_expected_versions_mismatch")
        expected_generation = before.get("generation") if before else "baseline"
    else:
        if expected_versions != expected_pending:
            raise SmokeFailure("expected_versions_do_not_match_plan")
        expected_generation = pending["generation"]
    if "core" not in expected_versions:
        raise SmokeFailure("expected_core_version_missing")

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    for key in list(env):
        folded = key.upper()
        if folded.startswith(("KIDTE_", "KID_TE_", "LIVE_RE_")):
            env.pop(key, None)
        elif any(secret in folded for secret in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            env[key] = ""
    env.update({
        "KID_TE_ROOT": str(data_root.resolve()),
        "CHAT_HOST": "127.0.0.1",
        "CHAT_PORT": str(port),
        "DEEPSEEK_API_KEY": "",
        "OPENAI_API_KEY": "",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
        "NO_PROXY": "127.0.0.1,localhost",
        "PYTHONPATH": "",
        "PYTHONHOME": "",
        "PYTHONUTF8": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "LOCALAPPDATA": str((data_root.parent / "localappdata").resolve()),
        "APPDATA": str((data_root.parent / "appdata").resolve()),
        "TEMP": str((data_root.parent / "temp").resolve()),
        "TMP": str((data_root.parent / "temp").resolve()),
    })
    for key in ("LOCALAPPDATA", "APPDATA", "TEMP"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)

    launcher_path = app_root / "KidTELauncher.exe"
    launcher_hash = _sha256_file(launcher_path)
    process = None
    child_identity = None
    ready = None
    api_state = None
    return_code = None
    try:
        process = subprocess.Popen(
            [str(launcher_path), "--safe-mode", "--no-ui", "--host", "127.0.0.1",
             "--port", str(port)], cwd=str(app_root), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        ready, api_state = _wait_ready(process, base, data_root, READY_TIMEOUT_S)
        child_pid = ready["pid"]
        _assert_child_owned(process, child_pid)
        executable, created = _process_executable(child_pid)
        child_identity = (child_pid, created)
        expected_executable = (app_root / "packages" / "core" /
                               expected_versions["core"] / "KidTE.exe").resolve(strict=True)
        if os.path.normcase(str(executable)) != os.path.normcase(str(expected_executable)):
            raise SmokeFailure("running_core_path_mismatch")
        if ready.get("launcher_generation") != expected_generation:
            raise SmokeFailure("ready_generation_mismatch")
        version_file = app_root / "VERSION"
        expected_app_version = version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else ""
        if expected_app_version and ready.get("version") != expected_app_version:
            raise SmokeFailure("ready_app_version_mismatch")
        if ready.get("data_root") is None or Path(ready["data_root"]).resolve() != data_root.resolve():
            raise SmokeFailure("ready_data_root_mismatch")

        # A second state-only API read is intentional: it checks that serving
        # health/status does not start catalog or package network activity.
        api_state = _request_json(base, "/api/updates", timeout=3)
        if not api_state.get("enabled") or api_state.get("error"):
            raise SmokeFailure("update_state_api_invalid")
        final = _wait_for_confirmed_state(app_root, expected_generation, expected_versions, base)
        journal = final["journal"]
        expected_journal_state = "rolled_back" if expect_rollback else "committed"
        if journal.get("state") != expected_journal_state:
            raise SmokeFailure("activation_journal_state_mismatch")
        if not expect_rollback and journal.get("new_generation") != expected_generation:
            raise SmokeFailure("activation_journal_generation_mismatch")
        if expect_rollback and journal.get("new_generation") != pending["generation"]:
            raise SmokeFailure("rollback_journal_generation_mismatch")
        launcher_pid = process.pid
        return_code = _shutdown_owned(process, base, child_identity)
        process = None
        if return_code != 0:
            raise SmokeFailure("launcher_exit_code_nonzero")
        return {
            "step": index,
            "status": "passed",
            "catalog_sequence": catalog["sequence"],
            "catalog_sha256": hashlib.sha256(raw_catalog).hexdigest(),
            "plan": planned,
            "pending_generation": pending["generation"],
            "active_generation": expected_generation,
            "expected_versions": expected_versions,
            "rolled_back": expect_rollback,
            "journal_state": journal["state"],
            "launcher_sha256": launcher_hash,
            "launcher_pid": launcher_pid,
            "child_pid": child_pid,
            "child_parent_verified": True,
            "running_executable": f"packages/core/{expected_versions['core']}/KidTE.exe",
            "ready_nonce_verified": bool(ready.get("launcher_nonce")),
            "ready_generation_verified": True,
            "ready_data_root_verified": True,
            "ready_version": ready.get("version"),
            "api_state": _state_summary(final["api"]),
            "manager_state": _state_summary(prepared_state),
            "local_transport_calls": transport.calls,
            "api_calls": ["GET /api/update-ready", "GET /api/updates", "POST /api/shutdown"],
            "external_provider_calls": 0,
            "shutdown_exit_code": return_code,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    finally:
        if process is not None:
            try:
                _shutdown_owned(process, base if ready else None, child_identity)
            except SmokeFailure:
                # The caller records the original scenario failure. We only
                # terminate the exact launcher Popen child in this cleanup.
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)


def run_smoke(app_dir: Path, steps_file: Path, work_dir: Path) -> dict:
    evidence = {
        "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "steps": [],
    }
    try:
        source = _require_directory(app_dir, "app_dir_invalid")
        if work_dir.exists():
            raise SmokeFailure("work_dir_must_be_new")
        config, config_dir = _load_config(steps_file)
        work = work_dir.expanduser().absolute()
        try:
            work.relative_to(source)
        except ValueError:
            pass
        else:
            raise SmokeFailure("work_dir_inside_source_app")
        app_root, data_root = _prepare_sandbox(source, work)
        summaries = []
        previous_active = {}
        for index, step in enumerate(config["steps"], start=1):
            summary = _run_step(index, step, config_dir, app_root, data_root, MANAGER_TIMEOUT_S)
            if step.get("expect_rollback", False) and previous_active and summary["expected_versions"] != previous_active:
                raise SmokeFailure("rollback_did_not_restore_prior_versions")
            summaries.append(summary)
            previous_active = summary["expected_versions"]
        evidence["steps"] = summaries
        evidence["status"] = "passed"
        evidence["sandbox"] = {"app_copy_created": True, "fresh_data_root": True,
                               "user_data_copied": False, "external_provider_calls": 0}
    except SmokeFailure as exc:
        evidence["status"] = "failed"
        evidence["failure_code"] = exc.code
    except Exception as exc:  # noqa: BLE001 - keep logs/evidence free of payloads and local paths
        evidence["status"] = "failed"
        evidence["failure_code"] = f"unexpected_{type(exc).__name__}"
    evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
    if work_dir.exists() and work_dir.is_dir():
        report = work_dir / OUTPUT_NAME
        try:
            report.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            evidence["evidence_file"] = OUTPUT_NAME
        except OSError:
            evidence["evidence_write_failed"] = True
    return evidence


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run an offline smoke test of packaged component updates.")
    parser.add_argument("--app-dir", required=True, type=Path,
                        help="Built App directory containing KidTE.exe and KidTELauncher.exe.")
    parser.add_argument("--steps", required=True, type=Path,
                        help="JSON file containing signed local catalogs and component archive mappings.")
    parser.add_argument("--work-dir", required=True, type=Path,
                        help="New, nonexistent directory for the App copy, fresh DuLieu, and evidence JSON.")
    args = parser.parse_args(argv)
    evidence = run_smoke(args.app_dir, args.steps, args.work_dir)
    print(json.dumps({"status": evidence["status"],
                      "failure_code": evidence.get("failure_code"),
                      "steps": len(evidence.get("steps", [])),
                      "evidence_file": evidence.get("evidence_file")}, separators=(",", ":")))
    return 0 if evidence["status"] == "passed" else 2


if __name__ == "__main__":  # pragma: no cover - packaged build tool entry
    raise SystemExit(main())
