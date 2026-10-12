"""User-requested update preparation; activation belongs to the next launcher run."""
from __future__ import annotations

import copy
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from component_store import ComponentStore
from update_catalog import COMPONENT_POLICIES, resolve_components, verify_catalog
from update_transport import GitHubTransport
from update_trust import TRUSTED_KEYS

REPOSITORY = "kidlost1412/KidTE"
SUPPORTED_COMPONENTS = set(COMPONENT_POLICIES)
AUTO_CHECK_DELAY_SECONDS = 60
AUTO_CHECK_INTERVAL_SECONDS = 6 * 60 * 60
logger = logging.getLogger("kidte.update")


class UpdateManager:
    """No network, downloads, activation, or data migration at construction."""

    def __init__(self, app_root=None, *, transport=None, trusted_keys=None, store=None):
        selected = app_root or os.environ.get("KIDTE_APP_ROOT", "")
        if not selected and getattr(sys, "frozen", False):
            selected = Path(sys.executable).resolve().parent
        self.app_root = Path(selected).absolute() if selected else None
        self.transport = transport or GitHubTransport(repository=REPOSITORY, series="p3")
        self.trusted_keys = dict(TRUSTED_KEYS if trusted_keys is None else trusted_keys)
        self._store = store
        self._lock = threading.RLock()
        self._thread = None
        self._catalog_raw = None
        self._channel = "beta"
        self._last_checked_at = None
        self._catalog = None
        self._state = {"operation": "idle", "busy": False, "error": "", "progress": None}

    def _get_store(self):
        if self.app_root is None:
            raise ValueError("update_source_mode_disabled")
        if self._store is None:
            self._store = ComponentStore(self.app_root)
        return self._store

    def _set(self, **changes):
        with self._lock:
            self._state.update(changes)

    @staticmethod
    def _versions(mapping):
        return {key: value["package"]["version"]
                for key, value in (mapping or {}).get("components", {}).items()}

    def state(self):
        with self._lock:
            result = copy.deepcopy(self._state)
            catalog = copy.deepcopy(self._catalog)
        result.update(ok=True, repository=REPOSITORY, channel=self._channel,
                      enabled=self.app_root is not None, trusted=bool(self.trusted_keys))
        if self.app_root is not None:
            try:
                store = self._get_store()
                result["installed"] = self._versions(store.load_active())
                pending = store.load_pending()
                result["pending"] = self._versions(pending)
                result["restart_required"] = pending is not None
            except (OSError, ValueError, RuntimeError) as exc:
                result["error"] = str(exc)
        installed = result.get("installed") or {}
        available = []
        for p in (catalog or {}).get("components", []):
            if p["id"] not in SUPPORTED_COMPONENTS:
                continue
            installed_version = installed.get(p["id"])
            # Only packages already installed count as updates; an uninstalled optional feature is not an update.
            available.append({"id": p["id"], "version": p["version"], "download_bytes": p["archive_bytes"],
                              "unpacked_bytes": p["unpacked_bytes"], "dependencies": p["dependencies"],
                              "installed_version": installed_version,
                              "newer": installed_version is not None and installed_version != p["version"]})
        result["available"] = available
        newer = [row for row in available if row["newer"]]
        result["update_available"] = bool(newer)
        result["update_total_bytes"] = sum(row["download_bytes"] for row in newer)
        with self._lock:
            result["last_checked_at"] = self._last_checked_at
        return result

    def action(self, action, payload):
        if not isinstance(payload, dict):
            raise ValueError("update_payload_invalid")  # noqa: TRY004 - consistent HTTP validation contract
        if action not in {"check", "plan", "install", "remove", "rollback", "cancel"}:
            raise ValueError("update_action_not_found")
        store = self._get_store()
        with self._lock:
            if self._state["busy"]:
                raise ValueError("update_job_busy")
            if action == "check":
                channel = payload.get("channel", "beta")
                if channel not in {"stable", "beta"}:
                    raise ValueError("update_channel_invalid")
                if not self.trusted_keys:
                    raise ValueError("update_trust_not_configured")
                self._channel = channel
                self._catalog_raw = None
                self._catalog = None
                target = lambda: self._check(store, channel)
            elif action in {"plan", "install"}:
                requested = payload.get("components")
                if (not isinstance(requested, dict) or not requested
                        or not set(requested).issubset(SUPPORTED_COMPONENTS)
                        or any(value is not None and not isinstance(value, str)
                               for value in requested.values())):
                    raise ValueError("update_selection_invalid")
                if self._catalog_raw is None:
                    raise ValueError("update_check_required")
                if action == "plan":
                    _, selection = self._plan(store, requested)
                    local_core = "core" in selection and store.baseline_matches(selection["core"])
                    return {"ok": True, "download_bytes": sum(p["archive_bytes"] for k, p in selection.items() if k != "core" or not local_core),
                            "packages": [{"id": p["id"], "version": p["version"],
                                          "download_bytes": 0 if k == "core" and local_core else p["archive_bytes"],
                                          "local_reuse": k == "core" and local_core} for k, p in selection.items()]}
                if store.load_pending() is not None:
                    raise ValueError("update_restart_required")
                target = lambda: self._install(store, requested)
            elif action == "remove":
                component = payload.get("component")
                if component not in SUPPORTED_COMPONENTS - {"core"}:
                    raise ValueError("update_selection_invalid")
                if store.load_pending() is not None:
                    raise ValueError("update_restart_required")
                current = (store.load_active() or {}).get("components", {})
                if component not in current:
                    raise ValueError("component_not_installed")
                if any(component in row["package"]["dependencies"] for key, row in current.items() if key != component):
                    raise ValueError("component_in_use")
                target = lambda: store.prepare({key: row for key, row in current.items() if key != component},
                                                catalog_sequence=store.read_highwater())
            elif action == "rollback":
                target = store.request_rollback
            else:
                target = store.cancel_pending
            self._state.update(operation=action, busy=True, error="", progress=None)
            self._thread = threading.Thread(target=self._run, args=(target,), daemon=True,
                                            name="kid-te-update")
            self._thread.start()
        return {"ok": True, "accepted": True}

    def _run(self, target):
        try:
            target()
            self._set(operation="ready", busy=False)
        except Exception as exc:  # noqa: BLE001 - report worker failure without changing active generation
            self._set(operation="failed", busy=False, error=str(exc), progress=None)

    def auto_check(self):
        """Background check: same as POST /api/updates/check, silent, never installs.

        Skips when disabled, busy, or a restart is pending. Failures are logged only.
        """
        try:
            if self.app_root is None or not self.trusted_keys:
                return {"skipped": "disabled"}
            store = self._get_store()
            with self._lock:
                if self._state["busy"]:
                    return {"skipped": "busy"}
            if store.load_pending() is not None:
                return {"skipped": "restart_pending"}
            self.action("check", {"channel": self._channel})
            return {"started": True, "channel": self._channel}
        except ValueError as exc:  # busy raced in, or config refused: nothing to report to the owner
            logger.info("update auto-check skipped: %s", exc)
            return {"skipped": str(exc)}
        except Exception as exc:  # noqa: BLE001 - background path must never disturb the app
            logger.warning("update auto-check failed: %s", exc)
            return {"skipped": "error"}

    def _check(self, store, channel):
        raw = self.transport.fetch_catalog(channel=channel)
        catalog = verify_catalog(raw, self.trusted_keys, repository=REPOSITORY,
                                 channel=channel, minimum_sequence=store.read_highwater())
        store.accept_sequence(catalog["sequence"])
        with self._lock:
            self._catalog_raw = raw
            self._catalog = catalog
            self._last_checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


    def _plan(self, store, requested):
        # Recheck signature, expiry, and replay floor after the operator selects packages.
        catalog = verify_catalog(self._catalog_raw, self.trusted_keys, repository=REPOSITORY,
                                 channel=self._channel, minimum_sequence=store.read_highwater())
        active = store.load_active() or {}
        installed = {key: value["package"] for key, value in active.get("components", {}).items()}
        selection = resolve_components(catalog, requested, installed=installed, core_api="1.0.0")
        if not set(selection).issubset(SUPPORTED_COMPONENTS):
            raise ValueError("update_component_not_supported")
        return catalog, selection

    def _install(self, store, requested):
        catalog, selection = self._plan(store, requested)
        if not selection:
            # The catalog and replay floor were still revalidated by _plan,
            # but there is no package work to stage or restart for.
            self._set(progress=None)
            return
        components = dict((store.load_active() or {}).get("components", {}))
        download_root = store.download_root()
        for component_id, package in selection.items():
            if component_id == "core" and store.baseline_matches(package):
                store.stage_baseline(package)
                components[component_id] = package
                continue
            archive = download_root / (package["archive_sha256"] + ".zip")
            self._set(progress={"component": component_id, "received": 0,
                                "total": package["archive_bytes"]})

            def progress(received, total, component=component_id):
                self._set(progress={"component": component, "received": received, "total": total})

            self.transport.download(package["url"], archive,
                                    expected_sha256=package["archive_sha256"],
                                    expected_bytes=package["archive_bytes"], progress=progress)
            store.stage_package(package, archive)
            components[component_id] = package
        # Retain installed dependencies/components. Native libraries cannot be switched in this process.
        store.prepare(components, catalog_sequence=catalog["sequence"])
        self._set(progress=None)


class UpdateScheduler:
    """Daemon thread: first check after a delay, then every interval. Never raises."""

    def __init__(self, manager, *, initial_delay=AUTO_CHECK_DELAY_SECONDS,
                 interval=AUTO_CHECK_INTERVAL_SECONDS):
        self.manager = manager
        self.initial_delay = initial_delay
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="kid-te-update-scheduler")
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _run(self):
        if self._stop.wait(self.initial_delay):
            return
        while True:
            try:
                self.manager.auto_check()
            except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
                logger.warning("update scheduler error: %s", exc)
            if self._stop.wait(self.interval):
                return


def select_active_gpu(app_root):
    """Read committed selection only; never recover or activate from inside the core."""
    if os.environ.get("KIDTE_GPU_RUNTIME_ROOT") or os.environ.get("KIDTE_GPU_RUNTIME_SHA256"):
        return  # P1 explicit operator selection still has precedence.
    active = ComponentStore(app_root).load_active()
    gpu = (active or {}).get("components", {}).get("gpu-cuda12")
    if gpu:
        os.environ["KIDTE_GPU_RUNTIME_ROOT"] = str(Path(app_root) / gpu["path"])
        os.environ["KIDTE_GPU_RUNTIME_SHA256"] = gpu["manifest_sha256"]
