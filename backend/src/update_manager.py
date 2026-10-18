"""User-requested update preparation; activation belongs to the next launcher run."""
from __future__ import annotations

import copy
import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from component_store import ComponentStore, ComponentStoreError, PackageRepairError, _atomic_json
from update_catalog import CatalogError, _version_key, COMPONENT_POLICIES, SPLIT_COMPONENTS, resolve_components, verify_catalog
from update_transport import GitHubTransport
from update_trust import TRUSTED_KEYS

REPOSITORY = "kidlost1412/KidTE"
SUPPORTED_COMPONENTS = set(COMPONENT_POLICIES)
BASELINE_COMPONENTS = {"core", *SPLIT_COMPONENTS}
LAUNCHER_CAPS_NEEDED = "split-components"
AUTO_CHECK_DELAY_SECONDS = 60
AUTO_CHECK_INTERVAL_SECONDS = 6 * 60 * 60
UPDATE_FAILURE_FILE = "update-failure.json"  # ghi bởi update_launcher khi tự lùi bản
logger = logging.getLogger("kidte.update")


CATALOG_EXPIRY_WARN_DAYS = 21


def catalog_expiry_info(expires_at, now=None, warn_days=CATALOG_EXPIRY_WARN_DAYS):
    """Cờ nhắc sớm: catalog đang dùng còn < warn_days ngày là hết hạn (chỉ nhắc, không chặn gì)."""
    empty = {"catalog_expires_at": None, "catalog_days_left": None, "catalog_expiring_soon": False}
    if not isinstance(expires_at, str) or not expires_at:
        return empty
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return empty
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    days_left = int((expires - current).total_seconds() // 86400)
    return {"catalog_expires_at": expires_at, "catalog_days_left": days_left,
            "catalog_expiring_soon": days_left < warn_days}


def _is_newer(candidate, installed):
    """True only when ``candidate`` is a strictly higher SemVer than ``installed``."""
    try:
        return _version_key(candidate) > _version_key(installed)
    except (CatalogError, TypeError, ValueError):
        return False


class UpdateManager:
    """No network, downloads, activation, or data migration at construction."""

    def __init__(self, app_root=None, *, transport=None, trusted_keys=None, store=None, launcher_caps=None):
        selected = app_root or os.environ.get("KIDTE_APP_ROOT", "")
        if not selected and getattr(sys, "frozen", False):
            selected = Path(sys.executable).resolve().parent
        self.app_root = Path(selected).absolute() if selected else None
        self._launcher_caps = (os.environ.get("KIDTE_LAUNCHER_CAPS", "")
                               if launcher_caps is None else launcher_caps)
        # Series p4 lists the split runtime/app/extensions components and is the
        # only feed a split-capable launcher may follow; p3 (monolithic core) is
        # for a legacy launcher alone. Mixing them trips the anti-replay floor.
        self.series = "p4" if LAUNCHER_CAPS_NEEDED in self._launcher_caps.split() else "p3"
        self.transport = transport or GitHubTransport(repository=REPOSITORY, series=self.series)
        self.trusted_keys = dict(TRUSTED_KEYS if trusted_keys is None else trusted_keys)
        self._store = store
        self._lock = threading.RLock()
        self._thread = None
        self._catalog_raw = None
        self._channel = "beta"
        self._last_checked_at = None
        self._catalog = None
        self._state = {"operation": "idle", "busy": False, "error": "", "progress": None}
        self._initial_attempted = False

    def probe_setup_hardware(self):
        from initial_components import detect_gpu
        self._set(hardware_probing=True)
        def probe():
            try:
                self._set(setup_hardware=detect_gpu())
            finally:
                self._set(hardware_probing=False)
        threading.Thread(target=probe, daemon=True, name="kid-te-update-hardware").start()

    def initial_setup(self):
        """Installer-only first run. Regular background checks never install."""
        if self.app_root is None or not self.trusted_keys:
            return False
        from initial_components import MARKER, read_request
        request = read_request(self.app_root)
        if not request or request.get("status") not in {"requested", "prepared"}:
            return False
        store = self._get_store()
        with self._lock:
            if self._initial_attempted or self._state["busy"] or store.load_pending() is not None:
                return False
            self._initial_attempted = True
            installed = self._versions(store.load_active())
            targets = request.get("components") or {}
            if request.get("status") == "prepared" and targets and all(installed.get(k) == v for k, v in targets.items()):
                _atomic_json(self.app_root / MARKER, {**request, "status": "complete"})
                return False
            self._state.update(operation="initial_setup", busy=True, error="", initial_setup=True)
            self._thread = threading.Thread(target=self._run, args=(lambda: self._initial_setup_worker(store),),
                                            daemon=True, name="kid-te-initial-components")
            self._thread.start()
        return True

    def _initial_setup_worker(self, store):
        from initial_components import MARKER, detect_gpu, selection
        gpu = detect_gpu()
        self._set(setup_hardware=gpu)
        self._check(store, self._channel)
        requested = selection(self._catalog, self._versions(store.load_active()), gpu)
        if requested:
            self._set(operation="install")
            self._install(store, requested)
        _atomic_json(self.app_root / MARKER, {"schema_version": 1, "status": "prepared" if store.load_pending() else "complete",
                                            "components": requested, "hardware": gpu})

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
        installed = result.get("installed") or {}
        available = []
        rows = [p for p in (catalog or {}).get("components", []) if p["id"] in SUPPORTED_COMPONENTS]
        # Install still on the monolithic core: the split set replaces it as one generation,
        # so it is an update only when the catalog's app is strictly newer than that core.
        migrating = "runtime" not in installed and installed.get("core") is not None
        app_row = next((p for p in rows if p["id"] == "app"), None)
        migration_newer = (migrating and app_row is not None
                           and _is_newer(app_row["version"], installed["core"]))
        for p in rows:
            installed_version = installed.get(p["id"])
            if migrating and p["id"] in SPLIT_COMPONENTS:
                installed_version = installed["core"] if p["id"] == "app" else None
                newer = migration_newer
            else:
                # Only installed packages can be updates; and only to a strictly higher version,
                # never to a lower or equal one (downgrade / rollback catalogs stay silent).
                newer = installed_version is not None and _is_newer(p["version"], installed_version)
            available.append({"id": p["id"], "version": p["version"], "download_bytes": p["archive_bytes"],
                              "unpacked_bytes": p["unpacked_bytes"], "dependencies": p["dependencies"],
                              "installed_version": installed_version, "newer": newer})
        result["available"] = available
        result["current_version"] = installed.get("app") or installed.get("core")
        head = next((p for p in rows if p["id"] in ("app", "core")), None)
        result["latest"] = ({"version": head["version"], "date": (catalog or {}).get("issued_at")}
                            if head else None)
        # Display mapping for the monolithic-core -> split migration: the old core
        # number belongs to the app only; runtime/extensions have no installed version.
        result["installed_display"] = {p["id"]: p["installed_version"] for p in available}
        newer = [row for row in available if row["newer"]]
        result["update_ids"] = [row["id"] for row in newer]
        result["update_available"] = bool(newer)
        result["update_total_bytes"] = sum(row["download_bytes"] for row in newer)
        with self._lock:
            result["last_checked_at"] = self._last_checked_at
        result.update(catalog_expiry_info((catalog or {}).get("expires_at")))
        result["extension_reload"] = None
        if self.app_root is not None:
            try:
                from extension_sync import read_notice
                result["extension_reload"] = read_notice(self.app_root)
            except Exception:  # noqa: BLE001 - a notice problem never breaks the state
                pass
        failure = self.last_update_failure()
        result["last_update_failure"] = failure
        if failure:
            # Bản đã lỗi khi khởi động: vẫn hiện để người dùng TỰ bấm thử lại,
            # nhưng đánh dấu để giao diện không mời cập nhật như bản mới bình thường.
            for row in available:
                if row["id"] in ("app", "core") and row["version"] == failure.get("failed_version"):
                    row["previously_failed"] = True
        return result

    def last_update_failure(self):
        """C5: bản cập nhật bị launcher tự lùi lần gần nhất (App/update-failure.json)."""
        if self.app_root is None:
            return None
        path = self.app_root / UPDATE_FAILURE_FILE
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("update failure record unreadable: %s", exc)
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("update failure record invalid: %s", exc)
            return None
        if not isinstance(value, dict):
            return None
        failed, restored = value.get("failed_version"), value.get("restored_version")
        reason = {"startup_timeout": "khởi động quá lâu",
                  "validation_failed": "tệp gói không hợp lệ"}.get(value.get("reason_code"), "lỗi khi khởi động")
        message = (f"Bản {failed or 'mới'} {reason}, Kid TE đã tự quay về bản {restored or 'trước'}. "
                   "Dữ liệu không bị ảnh hưởng. Bạn có thể bấm Cập nhật để thử lại.")
        return {"failed_version": failed, "restored_version": restored,
                "reason_code": value.get("reason_code"), "at": value.get("at"), "message": message}

    def action(self, action, payload):
        if not isinstance(payload, dict):
            raise ValueError("update_payload_invalid")  # noqa: TRY004 - consistent HTTP validation contract
        if action == "ack_extension_notice":
            if self.app_root is not None:
                from extension_sync import clear_notice
                clear_notice(self.app_root)
            return {"ok": True, "extension_reload": None}
        if action == "ack_update_failure":
            if self.app_root is not None:
                (self.app_root / UPDATE_FAILURE_FILE).unlink(missing_ok=True)
            return {"ok": True, "last_update_failure": None}
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
        todo = [(k, p) for k, p in selection.items()
                if not (k in BASELINE_COMPONENTS and store.baseline_matches(p))]
        overall_total = sum(p["archive_bytes"] for _, p in todo)
        done = 0
        order = {k: i + 1 for i, (k, _) in enumerate(todo)}
        for component_id, package in selection.items():
            if component_id in BASELINE_COMPONENTS and store.baseline_matches(package):
                store.stage_baseline(package)
                components[component_id] = package
                continue
            archive = download_root / (package["archive_sha256"] + ".zip")
            base = done
            self._set(progress={"component": component_id, "received": 0, "total": package["archive_bytes"],
                                "overall_received": base, "overall_total": overall_total,
                                "index": order[component_id], "count": len(todo)})

            def progress(received, total, component=component_id, base=base):
                self._set(progress={"component": component, "received": received, "total": total,
                                    "overall_received": base + received, "overall_total": overall_total,
                                    "index": order[component], "count": len(todo)})

            self.transport.download(package["url"], archive,
                                    expected_sha256=package["archive_sha256"],
                                    expected_bytes=package["archive_bytes"], progress=progress)
            store.stage_package(package, archive)
            done += package["archive_bytes"]
            components[component_id] = package
        # Retain installed dependencies/components. Native libraries cannot be switched in this process.
        self._repair_carried(store, catalog, components, selection, download_root)
        store.prepare(components, catalog_sequence=catalog["sequence"])
        self._set(progress=None)

    def _repair_carried(self, store, catalog, components, selection, download_root):
        """Make every component carried over unchanged pass verification before ``prepare``.

        (a) Only unlisted files present (e.g. a service log written into its immutable
        package directory): move them to ``.staging/.trash-<uuid>`` and re-verify.
        (b) Files missing or modified: download the SAME version again from the signed
        catalog (record must be identical to the installed one) and re-stage it.
        (c) Anything else: fail with a Vietnamese message naming component and file.
        """
        for component_id, row in components.items():
            if component_id in selection:
                continue
            record = row["package"] if isinstance(row, dict) and "package" in row else row
            if not isinstance(record, dict) or "files" not in record:
                continue
            try:
                report = store.inspect_package(record)
            except ComponentStoreError as exc:
                raise PackageRepairError(f"Gói {component_id} {record.get('version', '')} không kiểm tra được: {exc}",
                                         component=component_id, version=record.get("version", "")) from exc
            if report["state"] == "ok":
                continue
            if report["state"] == "extra":
                logger.warning("update: %s %s has unlisted files %s; moving them to trash",
                               component_id, record["version"], report["extra"])
                store.quarantine_extras(record, report["extra"])
                if store.inspect_package(record)["state"] == "ok":
                    continue
                report = store.inspect_package(record)
            problem = store.describe_problem(record, report)
            fresh = next((p for p in catalog["components"]
                          if p["id"] == component_id and p["version"] == record["version"]
                          and p["files"] == record["files"] and p["archive_sha256"] == record["archive_sha256"]), None)
            files = report["extra"] + report["missing"] + report["modified"] + report["unsafe"]
            if report["unsafe"] or fresh is None:
                raise PackageRepairError(
                    problem + " Không có bản giống hệt trong danh mục để tải lại; hãy cài lại Kid TE bằng bộ cài.",
                    component=component_id, version=record["version"], files=files)
            logger.warning("update: re-downloading %s %s (%s)", component_id, record["version"], problem)
            self._set(progress={"component": component_id, "received": 0, "total": fresh["archive_bytes"],
                                "overall_received": 0, "overall_total": fresh["archive_bytes"], "index": 1, "count": 1})
            archive = download_root / (fresh["archive_sha256"] + ".zip")
            try:
                self.transport.download(fresh["url"], archive, expected_sha256=fresh["archive_sha256"],
                                        expected_bytes=fresh["archive_bytes"])
                store.restage_package(fresh, archive)
            except PackageRepairError:
                raise
            except Exception as exc:  # noqa: BLE001 - surface a precise, actionable message
                raise PackageRepairError(
                    f"{problem} Tải lại gói để sửa không được ({exc}).",
                    component=component_id, version=record["version"], files=files) from exc
            if store.inspect_package(record)["state"] != "ok":
                raise PackageRepairError(problem + " Sau khi tải lại gói vẫn không khớp danh mục.",
                                         component=component_id, version=record["version"], files=files)


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
            setup = getattr(self.manager, "initial_setup", None)
            if callable(setup):
                try:
                    if not setup():
                        probe = getattr(self.manager, "probe_setup_hardware", None)
                        if callable(probe):
                            probe()
                except Exception as exc:
                    logger.warning("initial component setup could not start: %s", type(exc).__name__)
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
