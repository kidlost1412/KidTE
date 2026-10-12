"""User-requested update preparation; activation belongs to the next launcher run."""
from __future__ import annotations

import copy
import os
import sys
import threading
from pathlib import Path

from component_store import ComponentStore
from update_catalog import COMPONENT_POLICIES, SPLIT_COMPONENTS, resolve_components, verify_catalog
from update_transport import GitHubTransport
from update_trust import TRUSTED_KEYS

REPOSITORY = "kidlost1412/KidTE"
SUPPORTED_COMPONENTS = set(COMPONENT_POLICIES)
BASELINE_COMPONENTS = {"core", *SPLIT_COMPONENTS}
LAUNCHER_CAPS_NEEDED = "split-components"


class UpdateManager:
    """No network, downloads, activation, or data migration at construction."""

    def __init__(self, app_root=None, *, transport=None, trusted_keys=None, store=None, launcher_caps=None):
        selected = app_root or os.environ.get("KIDTE_APP_ROOT", "")
        if not selected and getattr(sys, "frozen", False):
            selected = Path(sys.executable).resolve().parent
        self.app_root = Path(selected).absolute() if selected else None
        # Series p4 lists the split runtime/app/extensions components; older
        # launchers/apps only ever read p3 and keep seeing the monolithic core.
        self.transport = transport or GitHubTransport(repository=REPOSITORY, series="p4")
        self._launcher_caps = (os.environ.get("KIDTE_LAUNCHER_CAPS", "")
                               if launcher_caps is None else launcher_caps)
        self.trusted_keys = dict(TRUSTED_KEYS if trusted_keys is None else trusted_keys)
        self._store = store
        self._lock = threading.RLock()
        self._thread = None
        self._catalog_raw = None
        self._channel = "beta"
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
                result["split_layout"] = "runtime" in ((store.load_active() or {}).get("components") or {})
                result["launcher_split_capable"] = LAUNCHER_CAPS_NEEDED in self._launcher_caps.split()
            except (OSError, ValueError, RuntimeError) as exc:
                result["error"] = str(exc)
        result["available"] = [
            {"id": p["id"], "version": p["version"], "download_bytes": p["archive_bytes"],
             "unpacked_bytes": p["unpacked_bytes"], "dependencies": p["dependencies"]}
            for p in (catalog or {}).get("components", []) if p["id"] in SUPPORTED_COMPONENTS
        ]
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
                    local = {k for k, p in selection.items() if k in BASELINE_COMPONENTS and store.baseline_matches(p)}
                    return {"ok": True, "download_bytes": sum(p["archive_bytes"] for k, p in selection.items() if k not in local),
                            "packages": [{"id": p["id"], "version": p["version"],
                                          "download_bytes": 0 if k in local else p["archive_bytes"],
                                          "local_reuse": k in local} for k, p in selection.items()]}
                if store.load_pending() is not None:
                    raise ValueError("update_restart_required")
                current = (store.load_active() or {}).get("components", {})
                self._expand_split(requested, current)  # refuse early if the launcher cannot start it
                target = lambda: self._install(store, requested)
            elif action == "remove":
                component = payload.get("component")
                if component not in SUPPORTED_COMPONENTS - BASELINE_COMPONENTS:
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

    def _check(self, store, channel):
        raw = self.transport.fetch_catalog(channel=channel)
        catalog = verify_catalog(raw, self.trusted_keys, repository=REPOSITORY,
                                 channel=channel, minimum_sequence=store.read_highwater())
        store.accept_sequence(catalog["sequence"])
        with self._lock:
            self._catalog_raw = raw
            self._catalog = catalog

    def _plan(self, store, requested):
        # Recheck signature, expiry, and replay floor after the operator selects packages.
        catalog = verify_catalog(self._catalog_raw, self.trusted_keys, repository=REPOSITORY,
                                 channel=self._channel, minimum_sequence=store.read_highwater())
        active = store.load_active() or {}
        installed = {key: value["package"] for key, value in active.get("components", {}).items()}
        requested = self._expand_split(requested, installed)
        selection = resolve_components(catalog, requested, installed=installed, core_api="1.0.0")
        if not set(selection).issubset(SUPPORTED_COMPONENTS):
            raise ValueError("update_component_not_supported")
        return catalog, selection

    def _expand_split(self, requested, installed):
        """Turn a split-component request into a complete runtime+app+extensions set.

        An install that still runs the monolithic ``core`` (or none) migrates as
        a whole generation, which only a launcher that understands split
        generations can start; refuse early rather than strand the install.
        """
        if not any(key in SPLIT_COMPONENTS for key in requested):
            return requested
        if "runtime" in installed:
            return requested
        if LAUNCHER_CAPS_NEEDED not in self._launcher_caps.split():
            raise ValueError("update_launcher_upgrade_required")
        return {**{key: None for key in SPLIT_COMPONENTS}, **requested}

    def _install(self, store, requested):
        catalog, selection = self._plan(store, requested)
        if not selection:
            # The catalog and replay floor were still revalidated by _plan,
            # but there is no package work to stage or restart for.
            self._set(progress=None)
            return
        components = dict((store.load_active() or {}).get("components", {}))
        if any(key in SPLIT_COMPONENTS for key in selection):
            components.pop("core", None)  # replaced by runtime + app + extensions
        download_root = store.download_root()
        for component_id, package in selection.items():
            if component_id in BASELINE_COMPONENTS and store.baseline_matches(package):
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


def select_active_gpu(app_root):
    """Read committed selection only; never recover or activate from inside the core."""
    if os.environ.get("KIDTE_GPU_RUNTIME_ROOT") or os.environ.get("KIDTE_GPU_RUNTIME_SHA256"):
        return  # P1 explicit operator selection still has precedence.
    active = ComponentStore(app_root).load_active()
    gpu = (active or {}).get("components", {}).get("gpu-cuda12")
    if gpu:
        os.environ["KIDTE_GPU_RUNTIME_ROOT"] = str(Path(app_root) / gpu["path"])
        os.environ["KIDTE_GPU_RUNTIME_SHA256"] = gpu["manifest_sha256"]
