"""Crash-safe storage and activation journal for signed update components.

Only records that have already passed ``update_catalog.verify_catalog`` should
be passed to this module. Archive contents are independently checked against
the signed inventory before they enter the immutable version store.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from update_catalog import (COMPONENT_POLICIES, SPLIT_COMPONENTS, CatalogError, _range_satisfies,
                            check_generation, provided)

try:  # Windows uses a byte-range lock; POSIX keeps the module testable.
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - exercised on non-Windows CI
    msvcrt = None
    import fcntl  # type: ignore


class ComponentStoreError(RuntimeError):
    """Invalid package, unsafe filesystem state, or failed transaction."""


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_FILES = 100_000
_MAX_JSON = 64 * 1024 * 1024
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _local_lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(key, threading.RLock())


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attrs = getattr(info, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _check_ancestry(path: Path) -> None:
    """Reject symlinks/junctions from the volume root through ``path``."""
    absolute = path.absolute()
    chain = list(reversed((absolute, *absolute.parents)))
    for item in chain:
        if _is_reparse(item):
            raise ComponentStoreError(f"reparse point in store ancestry: {item}")


def _safe_member(name: str) -> bool:
    if not isinstance(name, str) or not name or len(name) > 240:
        return False
    if name.startswith(("/", "\\")) or "\\" in name or ":" in name or "\x00" in name:
        return False
    if PurePosixPath(name).is_absolute():
        return False
    parts = name.split("/")
    if any(p in {"", ".", ".."} or p.endswith((".", " ")) for p in parts):
        return False
    for part in parts:
        if any(ord(ch) < 32 or ch in '<>"|?*' for ch in part):
            return False
        if part.split(".", 1)[0].upper() in _RESERVED:
            return False
    return True


def _validate_record(record: dict) -> None:
    try:
        # Share the exact signed-catalog validation rules instead of allowing
        # the store to grow a second, subtly different package schema.
        from update_catalog import _validate_package

        _validate_package(record, "kidlost1412/KidTE")
    except Exception as exc:
        raise ComponentStoreError("invalid signed package record") from exc
    if not isinstance(record.get("files"), list) or len(record["files"]) > _MAX_FILES:
        raise ComponentStoreError("invalid file inventory")
    if sum(row["size"] for row in record["files"]) != record["unpacked_bytes"]:
        raise ComponentStoreError("signed unpacked byte count does not match inventory")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _read_json(path: Path):
    if _is_reparse(path):
        raise ComponentStoreError(f"reparse point metadata: {path.name}")
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_JSON + 1)
        if len(raw) > _MAX_JSON:
            raise ComponentStoreError("metadata too large")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ComponentStoreError("duplicate metadata key")
                result[key] = value
            return result
        return json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ComponentStoreError("invalid metadata number")))
    except FileNotFoundError:
        return None
    except (UnicodeError, ValueError, OSError) as exc:
        if isinstance(exc, ComponentStoreError):
            raise
        raise ComponentStoreError(f"invalid metadata: {path.name}") from exc


def _atomic_json(path: Path, value: object, checkpoint=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _check_ancestry(path.parent)
    if _is_reparse(path):
        raise ComponentStoreError(f"reparse point metadata: {path.name}")
    data = _json_bytes(value)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if checkpoint:
            checkpoint(f"before_replace:{path.name}")
        os.replace(temp, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


class ComponentStore:
    """Immutable package versions plus atomic active/pending mappings.

    ``crash_hook`` is an optional test seam. It receives durable transaction
    checkpoint names and may raise to model abrupt process termination.
    """

    def __init__(self, app_root: str | os.PathLike, *, crash_hook=None, lock_timeout: float = 30.0):
        self.app_root = Path(os.path.abspath(os.fspath(app_root)))
        self.crash_hook = crash_hook
        if isinstance(lock_timeout, bool) or not isinstance(lock_timeout, (int, float)) or not 0 < lock_timeout <= 30:
            raise ValueError("lock_timeout must be greater than zero and at most 30 seconds")
        self.lock_timeout = float(lock_timeout)
        self.packages_dir = self.app_root / "packages"
        self.staging_dir = self.app_root / ".staging"
        self.active_path = self.app_root / "active.json"
        self.previous_path = self.app_root / "previous.json"
        self.pending_path = self.app_root / "pending.json"
        self.journal_path = self.app_root / "update-journal.json"
        self.sequence_path = self.app_root / "catalog-sequence.json"
        self.lock_path = self.app_root / ".component-store.lock"
        _check_ancestry(self.app_root)
        _check_ancestry(self.staging_dir)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        _check_ancestry(self.packages_dir)
        self.packages_dir.mkdir(parents=True, exist_ok=True)
        _check_ancestry(self.staging_dir)

    def _checkpoint(self, name: str) -> None:
        if self.crash_hook:
            self.crash_hook(name)

    @contextmanager
    def _locked(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        _check_ancestry(self.lock_path)
        local_lock = _local_lock_for(self.lock_path)
        with local_lock:
            try:
                descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                try:
                    os.write(descriptor, b"\0")
                finally:
                    os.close(descriptor)
            _check_ancestry(self.lock_path)
            with self.lock_path.open("r+b") as stream:
                if msvcrt is not None:
                    stream.seek(0)
                    deadline = time.monotonic() + self.lock_timeout
                    while True:
                        try:
                            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError as exc:
                            winerror = getattr(exc, "winerror", None)
                            is_lock_busy = (winerror in {33, 36} or
                                            (winerror is None and exc.errno in {errno.EACCES, errno.EDEADLK}))
                            if not is_lock_busy:
                                raise
                            if time.monotonic() >= deadline:
                                raise ComponentStoreError("component store lock timeout") from exc
                            time.sleep(0.05)
                else:  # pragma: no cover - Windows is the production target
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if msvcrt is not None:
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:  # pragma: no cover
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _read_mapping(self, path: Path, *, optional=True):
        value = _read_json(path)
        if value is None and optional:
            return None
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ComponentStoreError(f"invalid {path.name}")
        return value

    def _validate_journal(self, journal: dict) -> None:
        required = {"schema_version", "state", "old_active", "new_generation", "catalog_sequence"}
        if (set(journal) != required or journal.get("schema_version") != 1 or
                journal.get("state") not in {"prepared", "probing", "committed", "rolled_back"} or
                type(journal.get("catalog_sequence")) is not int or journal["catalog_sequence"] < 1 or
                not isinstance(journal.get("new_generation"), str)):
            raise ComponentStoreError("invalid update journal")
        try:
            uuid.UUID(journal["new_generation"])
        except ValueError as exc:
            raise ComponentStoreError("invalid journal generation") from exc
        if journal["old_active"] is not None:
            self._validate_mapping(journal["old_active"])

    def _discard_pending_if_generation(self, generation: str) -> None:
        try:
            pending = self._read_mapping(self.pending_path)
        except ComponentStoreError:
            # Ownership cannot be inferred from damaged metadata. Leave it for
            # the launcher's explicit pending rejection, never retire active.
            return
        # Recovery owns only the generation named by its journal. A newer
        # pending generation is validated by load/activation, where the
        # launcher can reject it while retaining the healthy active core.
        if pending is not None and pending.get("generation") == generation:
            self.pending_path.unlink(missing_ok=True)

    def load_active(self) -> dict | None:
        with self._locked():
            value = self._read_mapping(self.active_path)
            if value is not None:
                self._validate_mapping(value)
            return value

    def load_pending(self) -> dict | None:
        with self._locked():
            value = self._read_mapping(self.pending_path)
            if value is not None:
                self._validate_mapping(value)
            return value

    def selected_record(self) -> dict | None:
        """Return the validated active core component mapping for the launcher."""
        active = self.load_active()
        if active is None:
            return None
        return active["components"].get("runtime") or active["components"].get("core")

    def download_root(self) -> Path:
        """Return the checked private download directory under the staging root."""
        path = self.staging_dir / "downloads"
        _check_ancestry(path)
        path.mkdir(parents=True, exist_ok=True)
        _check_ancestry(path)
        return path

    def _validate_mapping(self, mapping: dict, *, verify_files: bool = False) -> None:
        if (set(mapping) != {"schema_version", "generation", "catalog_sequence", "components"} or
                mapping.get("schema_version") != 1 or not isinstance(mapping.get("generation"), str) or
                type(mapping.get("catalog_sequence")) is not int or mapping["catalog_sequence"] < 1 or
                not isinstance(mapping.get("components"), dict)):
            raise ComponentStoreError("invalid generation mapping")
        try:
            uuid.UUID(mapping["generation"])
        except ValueError as exc:
            raise ComponentStoreError("invalid generation id") from exc
        records = {}
        for component_id, component in mapping["components"].items():
            if (not isinstance(component, dict) or
                    set(component) != {"package", "path", "record_sha256", "manifest_sha256",
                                       "entrypoint", "kind", "version", "files", "compatibility"} or
                    component_id not in COMPONENT_POLICIES):
                raise ComponentStoreError("invalid component mapping")
            record = component.get("package")
            _validate_record(record)
            rel = component.get("path")
            expected = f"packages/{component_id}/{record['version']}"
            if (rel != expected or component_id != record["id"] or
                    component.get("version") != record["version"] or
                    component.get("kind") != record["kind"] or
                    component.get("entrypoint") != record["entrypoint"]):
                raise ComponentStoreError("invalid component path")
            record_hash = hashlib.sha256(_json_bytes(record)).hexdigest()
            if component.get("record_sha256") != record_hash:
                raise ComponentStoreError("invalid package record digest")
            manifest_hash = None
            if component_id in {"gpu-runtime", "gpu-cuda12"}:
                manifest = next((row for row in record["files"]
                                 if row["path"].casefold() == "gpu-runtime.json"), None)
                if manifest is None:
                    raise ComponentStoreError("GPU runtime manifest is missing from inventory")
                manifest_hash = manifest["sha256"]
            if component.get("manifest_sha256") != manifest_hash:
                raise ComponentStoreError("invalid component manifest digest")
            if component.get("files") != record["files"] or component.get("compatibility") != record["compatibility"]:
                raise ComponentStoreError("invalid component metadata")
            full = self.app_root / Path(*PurePosixPath(rel).parts)
            _check_ancestry(full)
            if not full.is_dir():
                raise ComponentStoreError("package is not staged: stored directory is missing")
            if verify_files and not self._verify_existing(full, record):
                raise ComponentStoreError("stored package files failed inventory verification")
            records[component_id] = record

        # A generation is activated as one unit, so validate dependency closure
        # here as well as in catalog planning. This also protects persisted
        # active/pending mappings that did not originate in UpdateManager.
        for component_id, record in records.items():
            for dependency, version_range in record["dependencies"].items():
                target = provided(records, dependency)
                if target is None:
                    raise ComponentStoreError("component dependency is missing")
                if not _range_satisfies(target["version"], version_range):
                    raise ComponentStoreError("component dependency version mismatch")
        try:
            check_generation(records)
        except CatalogError as exc:
            raise ComponentStoreError(f"invalid runtime/app generation: {exc}") from exc

    def _verify_existing(self, target: Path, record: dict) -> bool:
        if not target.exists():
            return False
        _check_ancestry(target)
        expected = {row["path"].casefold(): row for row in record["files"]}
        actual = {}
        for path in target.rglob("*"):
            if _is_reparse(path):
                raise ComponentStoreError("reparse point in installed package")
            if path.is_file():
                rel = path.relative_to(target).as_posix()
                actual[rel.casefold()] = rel
        if set(actual) != set(expected):
            return False
        for row in expected.values():
            path = target / Path(*row["path"].split("/"))
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    digest.update(block)
            if size != row["size"] or digest.hexdigest() != row["sha256"]:
                return False
        return True

    def baseline_matches(self, package: dict) -> bool:
        """Verify installer core against a signed catalog without downloading it."""
        _validate_record(package)
        if package["id"] not in {"core", *SPLIT_COMPONENTS}:
            return False
        try:
            for row in package["files"]:
                path = self.app_root / row["path"]
                _check_ancestry(path)
                if not path.is_file() or path.stat().st_size != row["size"]:
                    return False
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != row["sha256"]:
                    return False
            return True
        except OSError:
            return False

    def stage_baseline(self, package: dict) -> Path:
        """Copy verified installer files into an immutable package version; never hardlink."""
        if not self.baseline_matches(package):
            raise ComponentStoreError("installer package does not match signed inventory")
        target = self.packages_dir / package["id"] / package["version"]
        stage = self.staging_dir / f"{package['id']}-local-{uuid.uuid4().hex}"
        _check_ancestry(stage)
        if shutil.disk_usage(self.app_root).free < package["unpacked_bytes"] + 1024 * 1024:
            raise ComponentStoreError("insufficient disk space")
        stage.mkdir()
        try:
            for row in package["files"]:
                source = self.app_root / row["path"]
                destination = stage / row["path"]
                _check_ancestry(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            if not self._verify_existing(stage, package):
                raise ComponentStoreError("installer package changed while staging")
            with self._locked():
                _check_ancestry(target)
                if target.exists():
                    if not self._verify_existing(target, package):
                        raise ComponentStoreError("installed version exists with different contents")
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(stage, target)
            return target
        finally:
            if stage.exists():
                _check_ancestry(stage)
                shutil.rmtree(stage)

    def stage_package(self, package: dict, archive_path: str | os.PathLike) -> Path:
        """Verify and install one archive under its immutable version path."""
        _validate_record(package)
        archive = Path(archive_path)
        _check_ancestry(archive.absolute())
        if _is_reparse(archive):
            raise ComponentStoreError("archive is a reparse point")
        target = self.packages_dir / package["id"] / package["version"]
        with self._locked():
            _check_ancestry(target.parent)
            already_installed = target.exists()
        if already_installed:
            if self._verify_existing(target, package):
                return target
            raise ComponentStoreError("installed version exists with different contents")
        try:
            try:
                archive_size = archive.stat().st_size
            except OSError as exc:
                raise ComponentStoreError("archive unavailable") from exc
            if archive_size != package["archive_bytes"]:
                raise ComponentStoreError("archive size mismatch")
            free = shutil.disk_usage(self.app_root).free
            if free < package["unpacked_bytes"] + 1024 * 1024:
                raise ComponentStoreError("insufficient disk space")
            archive_hash = hashlib.sha256()
            with archive.open("rb") as stream:
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    archive_hash.update(block)
            if archive_hash.hexdigest() != package["archive_sha256"]:
                raise ComponentStoreError("archive digest mismatch")
            expected = {row["path"].casefold(): row for row in package["files"]}
            stage = self.staging_dir / f"{package['id']}-{package['version']}-{uuid.uuid4().hex}"
            _check_ancestry(stage)
            stage.mkdir()
            with zipfile.ZipFile(archive, "r") as zf:
                if zf.comment:
                    raise ComponentStoreError("archive comment metadata is not allowed")
                infos = zf.infolist()
                if len(infos) > _MAX_FILES:
                    raise ComponentStoreError("too many archive entries")
                seen = set()
                file_infos = []
                allowed_dirs = set()
                for row in package["files"]:
                    parts = row["path"].split("/")[:-1]
                    for index in range(1, len(parts) + 1):
                        allowed_dirs.add("/".join(parts[:index]).casefold())
                explicit_dirs = set()
                for info in infos:
                    name = info.filename[:-1] if info.is_dir() and info.filename.endswith("/") else info.filename
                    if info.is_dir():
                        if name and not _safe_member(name):
                            raise ComponentStoreError("unsafe archive directory")
                        if name:
                            key = name.casefold()
                            if key not in allowed_dirs or key in explicit_dirs:
                                raise ComponentStoreError("unlisted or duplicate archive directory")
                            explicit_dirs.add(key)
                        if info.extra or info.comment:
                            raise ComponentStoreError("archive entry metadata is not allowed")
                        continue
                    if not _safe_member(name):
                        raise ComponentStoreError("unsafe archive path")
                    key = name.casefold()
                    if key in seen:
                        raise ComponentStoreError("duplicate or case-colliding archive path")
                    seen.add(key)
                    mode = (info.external_attr >> 16) & 0xFFFF
                    file_type = stat.S_IFMT(mode)
                    if stat.S_ISLNK(mode) or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                        raise ComponentStoreError("non-regular archive entry")
                    if info.flag_bits & 1:
                        raise ComponentStoreError("encrypted archive entry")
                    if info.extra or info.comment:
                        raise ComponentStoreError("archive entry metadata is not allowed")
                    file_infos.append((name, info))
                if seen != set(expected):
                    raise ComponentStoreError("archive has extra or missing files")
                total = 0
                for name, info in file_infos:
                    row = expected[name.casefold()]
                    if info.file_size != row["size"]:
                        raise ComponentStoreError("archive file size metadata mismatch")
                    destination = stage / Path(*name.split("/"))
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _check_ancestry(destination.parent)
                    digest = hashlib.sha256()
                    count = 0
                    with zf.open(info, "r") as source, destination.open("xb") as output:
                        while True:
                            block = source.read(min(1024 * 1024, row["size"] - count + 1))
                            if not block:
                                break
                            count += len(block)
                            total += len(block)
                            if count > row["size"] or total > package["unpacked_bytes"]:
                                raise ComponentStoreError("archive expands beyond signed size")
                            digest.update(block)
                            output.write(block)
                        output.flush()
                        os.fsync(output.fileno())
                    if count != row["size"] or digest.hexdigest() != row["sha256"]:
                        raise ComponentStoreError("file size or digest mismatch")
                if total > package["unpacked_bytes"]:
                    raise ComponentStoreError("unpacked size exceeds signed limit")
            if not self._verify_existing(stage, package):
                raise ComponentStoreError("staged package inventory mismatch")
            digest_after = hashlib.sha256()
            with archive.open("rb") as stream:
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    digest_after.update(block)
            if digest_after.hexdigest() != package["archive_sha256"]:
                raise ComponentStoreError("archive changed while staging")
            with self._locked():
                _check_ancestry(target.parent)
                already_installed = target.exists()
                if not already_installed:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _check_ancestry(target.parent)
                    os.replace(stage, target)
            if already_installed:
                if self._verify_existing(target, package):
                    return target
                raise ComponentStoreError("installed version exists with different contents")
            return target
        except Exception:
            if "stage" in locals() and stage.exists():
                # Only this transaction's private staging directory is removed.
                shutil.rmtree(stage, ignore_errors=True)
            raise

    def _sequence(self) -> int:
        data = _read_json(self.sequence_path)
        if data is None:
            return 0
        if (not isinstance(data, dict) or set(data) != {"schema_version", "sequence"} or
                data.get("schema_version") != 1 or type(data.get("sequence")) is not int or data["sequence"] < 0):
            raise ComponentStoreError("invalid catalog high-water mark")
        return data["sequence"]

    def read_highwater(self) -> int:
        """Return the persisted catalog anti-replay sequence."""
        with self._locked():
            return self._sequence()

    def _write_highwater(self, sequence: int) -> None:
        current = self._sequence()
        _atomic_json(self.sequence_path, {"schema_version": 1, "sequence": max(sequence, current)}, self._checkpoint)
        self._checkpoint("sequence_written")

    def accept_sequence(self, sequence: int) -> None:
        """Advance the global catalog high-water mark without decreasing it."""
        if type(sequence) is not int or sequence < 1:
            raise ComponentStoreError("invalid catalog sequence")
        with self._locked():
            if sequence < self._sequence():
                raise ComponentStoreError("catalog sequence rollback")
            self._write_highwater(sequence)

    def prepare(self, packages: dict[str, dict], catalog_sequence: int) -> dict:
        """Persist a complete pending generation without changing active state."""
        if type(catalog_sequence) is not int or catalog_sequence < 1:
            raise ComponentStoreError("invalid catalog sequence")
        if not isinstance(packages, dict) or not packages or set(packages) - set(COMPONENT_POLICIES):
            raise ComponentStoreError("invalid component selection")
        components = {}
        for component_id, supplied in packages.items():
            if isinstance(supplied, dict) and "package" in supplied:
                record = supplied["package"]
                expected_path = f"packages/{component_id}/{record.get('version', '')}" if isinstance(record, dict) else None
                if supplied.get("path") != expected_path or supplied.get("record_sha256") != hashlib.sha256(_json_bytes(record)).hexdigest():
                    raise ComponentStoreError("invalid installed component record")
            else:
                record = supplied
            _validate_record(record)
            if record["id"] != component_id:
                raise ComponentStoreError("component key mismatch")
            components[component_id] = {
                "package": record,
                "path": f"packages/{component_id}/{record['version']}",
                "record_sha256": hashlib.sha256(_json_bytes(record)).hexdigest(),
                "manifest_sha256": next((row["sha256"] for row in record["files"]
                                          if row["path"].casefold() == "gpu-runtime.json"), None)
                if component_id in {"gpu-runtime", "gpu-cuda12"} else None,
                "entrypoint": record["entrypoint"],
                "kind": record["kind"],
                "version": record["version"],
                "files": record["files"],
                "compatibility": record["compatibility"],
            }
        pending = {"schema_version": 1, "generation": str(uuid.uuid4()),
                   "catalog_sequence": catalog_sequence, "components": components}
        self._validate_mapping(pending)
        for component_id, component in components.items():
            package_path = self.packages_dir / component_id / component["version"]
            if not self._verify_existing(package_path, component["package"]):
                raise ComponentStoreError("package is not staged or does not match inventory")
        with self._locked():
            if catalog_sequence < self._sequence():
                raise ComponentStoreError("catalog sequence rollback")
            # Persist anti-replay state before making the generation visible.
            self._write_highwater(catalog_sequence)
            _atomic_json(self.pending_path, pending, self._checkpoint)
            self._checkpoint("pending_written")
            return pending

    def restore_pending(self, pending: dict) -> dict:
        """Re-queue a generation whose activation was abandoned without being tried.

        A fresh generation id is used so recover() cannot discard it together with the
        rolled-back journal entry that carried the old id.
        """
        with self._locked():
            requeued = dict(pending)
            requeued["generation"] = str(uuid.uuid4())
            self._validate_mapping(requeued)
            _atomic_json(self.pending_path, requeued, self._checkpoint)
            self._checkpoint("pending_restored")
            return requeued

    def cancel_pending(self) -> None:
        """Cancel a not-yet-activated generation while keeping staged versions."""
        with self._locked():
            self.pending_path.unlink(missing_ok=True)
            self._checkpoint("pending_cancelled")

    def request_rollback(self) -> dict:
        """Queue the saved previous generation for activation on next startup."""
        with self._locked():
            previous = self._read_mapping(self.previous_path)
            if (not isinstance(previous, dict) or set(previous) != {"schema_version", "active"} or
                    previous.get("schema_version") != 1):
                raise ComponentStoreError("rollback_not_available")
            old = previous["active"]
            if old is not None:
                self._validate_mapping(old)
                components = old["components"]
            else:
                components = {}
            sequence = max(1, self._sequence())
            pending = {"schema_version": 1, "generation": str(uuid.uuid4()),
                       "catalog_sequence": sequence, "components": components}
            self._validate_mapping(pending, verify_files=True)
            _atomic_json(self.pending_path, pending, self._checkpoint)
            self._checkpoint("rollback_requested")
            return pending

    def activate_pending(self) -> dict:
        """Switch active mapping at launcher startup, then await health confirm."""
        with self._locked():
            pending = self._read_mapping(self.pending_path)
            if pending is None:
                raise ComponentStoreError("no pending generation")
            try:
                self._validate_mapping(pending, verify_files=True)
            except ComponentStoreError:
                self.pending_path.unlink(missing_ok=True)
                self._checkpoint("pending_rejected")
                raise
            old = self._read_mapping(self.active_path)
            if old is not None:
                self._validate_mapping(old)
            _atomic_json(self.previous_path, {"schema_version": 1, "active": old}, self._checkpoint)
            self._checkpoint("previous_written")
            journal = {"schema_version": 1, "state": "prepared", "old_active": old,
                       "new_generation": pending["generation"], "catalog_sequence": pending["catalog_sequence"]}
            _atomic_json(self.journal_path, journal, self._checkpoint)
            self._checkpoint("journal_prepared")
            _atomic_json(self.active_path, pending, self._checkpoint)
            self._checkpoint("active_written")
            journal["state"] = "probing"
            _atomic_json(self.journal_path, journal, self._checkpoint)
            self._checkpoint("probing")
            return pending

    def confirm_active(self, generation: str) -> dict:
        with self._locked():
            active = self._read_mapping(self.active_path)
            if active is None or active.get("generation") != generation:
                raise ComponentStoreError("active generation does not match confirmation")
            self._validate_mapping(active)
            journal = self._read_mapping(self.journal_path)
            if journal is None or journal.get("new_generation") != generation or journal.get("state") != "probing":
                raise ComponentStoreError("generation is not awaiting confirmation")
            self._validate_journal(journal)
            journal["state"] = "committed"
            _atomic_json(self.journal_path, journal, self._checkpoint)
            self._checkpoint("committed")
            self.pending_path.unlink(missing_ok=True)
            return active

    def rollback(self) -> dict | None:
        with self._locked():
            previous = self._read_mapping(self.previous_path)
            if (not isinstance(previous, dict) or set(previous) != {"schema_version", "active"} or
                    previous.get("schema_version") != 1):
                raise ComponentStoreError("no rollback mapping")
            old = previous["active"]
            if old is None:
                self.active_path.unlink(missing_ok=True)
            else:
                self._validate_mapping(old)
                _atomic_json(self.active_path, old, self._checkpoint)
            journal = self._read_mapping(self.journal_path)
            if journal is not None:
                self._validate_journal(journal)
                journal["state"] = "rolled_back"
                _atomic_json(self.journal_path, journal, self._checkpoint)
            self.pending_path.unlink(missing_ok=True)
            self._checkpoint("rolled_back")
            return old

    def recover(self) -> dict | None:
        """Repair an interrupted switch; unconfirmed generations roll back."""
        with self._locked():
            journal = self._read_mapping(self.journal_path)
            if journal is None:
                active = self._read_mapping(self.active_path)
                if active is not None:
                    self._validate_mapping(active)
                return active
            self._validate_journal(journal)
            state = journal.get("state")
            if state == "committed":
                active = self._read_mapping(self.active_path)
                if active is None or active.get("generation") != journal.get("new_generation"):
                    raise ComponentStoreError("committed generation is missing")
                self._validate_mapping(active)
                self._discard_pending_if_generation(journal["new_generation"])
                return active
            if state == "rolled_back":
                active = self._read_mapping(self.active_path)
                if active is not None:
                    self._validate_mapping(active)
                self._discard_pending_if_generation(journal["new_generation"])
                return active
            if state in {"prepared", "probing"}:
                old = journal["old_active"]
                if old is None:
                    self.active_path.unlink(missing_ok=True)
                else:
                    self._validate_mapping(old)
                    _atomic_json(self.active_path, old, self._checkpoint)
                journal["state"] = "rolled_back"
                _atomic_json(self.journal_path, journal, self._checkpoint)
                self._discard_pending_if_generation(journal["new_generation"])
                self._checkpoint("recovered_rollback")
                return old
            raise ComponentStoreError("unknown transaction journal state")

    # ------------------------------------------------------------------
    # Local garbage collection (Chrome / VS Code style): after a generation is
    # confirmed healthy keep exactly the active generation plus one rollback.
    # ------------------------------------------------------------------
    @staticmethod
    def _mapping_refs(mapping) -> set:
        if not isinstance(mapping, dict):
            return set()
        return {(cid, row["version"]) for cid, row in (mapping.get("components") or {}).items()
                if isinstance(row, dict) and isinstance(row.get("version"), str)}

    @staticmethod
    def _rmtree(path: Path) -> None:
        def retry(func, target, _exc):
            try:
                os.chmod(target, stat.S_IWRITE)
                func(target)
            except OSError:
                pass
        shutil.rmtree(path, onerror=retry)

    @staticmethod
    def _newest_mtime(path: Path, cutoff: float) -> float:
        """Newest mtime under ``path``; stops early once something newer than ``cutoff`` is seen."""
        newest = path.lstat().st_mtime
        if newest > cutoff or not path.is_dir() or _is_reparse(path):
            return newest
        for base, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if not _is_reparse(Path(base) / d)]
            for name in files:
                try:
                    newest = max(newest, (Path(base) / name).lstat().st_mtime)
                except OSError:
                    continue
                if newest > cutoff:
                    return newest
        return newest

    def collect_garbage(self, *, grace_seconds: float = 3600.0, staging_max_age: float = 86400.0,
                        now: float | None = None) -> dict:
        """Delete package versions that are neither active nor previous.

        Safe by construction: nothing is touched while a pending generation or an
        unconfirmed activation exists, or when metadata cannot be read. Versions
        younger than ``grace_seconds`` are kept (they may be staged for an install
        that has not written ``pending.json`` yet). A version is first renamed to
        ``.staging/.trash-*`` (atomic, so a crash never leaves a half-deleted
        package that a mapping could reference) and removed afterwards; stale
        trash and staging leftovers older than ``staging_max_age`` are swept.
        """
        now = time.time() if now is None else float(now)
        report = {"skipped": None, "removed": [], "kept": [], "busy": [], "staging_removed": []}
        trash: list[Path] = []
        with self._locked():
            try:
                if self.pending_path.exists():
                    report["skipped"] = "pending_generation"
                    return report
                journal = self._read_mapping(self.journal_path)
                if journal is not None and journal.get("state") not in {"committed", "rolled_back"}:
                    report["skipped"] = "activation_in_progress"
                    return report
                active = self._read_mapping(self.active_path)
                previous = self._read_mapping(self.previous_path)
                if active is None:
                    report["skipped"] = "no_active_generation"
                    return report
                self._validate_mapping(active)
                keep = self._mapping_refs(active)
                if previous is not None:
                    if set(previous) != {"schema_version", "active"}:
                        raise ComponentStoreError("invalid previous.json")
                    keep |= self._mapping_refs(previous["active"])
            except ComponentStoreError as exc:
                report["skipped"] = f"metadata_unreadable:{exc}"
                return report
            _check_ancestry(self.packages_dir)
            for id_dir in sorted(self.packages_dir.iterdir()):
                if not id_dir.is_dir() or _is_reparse(id_dir):
                    continue
                for version_dir in sorted(id_dir.iterdir()):
                    key = (id_dir.name, version_dir.name)
                    if key in keep:
                        report["kept"].append(f"{key[0]}/{key[1]}")
                        continue
                    if not version_dir.is_dir() or _is_reparse(version_dir):
                        continue
                    if now - version_dir.lstat().st_mtime < grace_seconds:  # immutable: dir time = staging time
                        report["kept"].append(f"{key[0]}/{key[1]}")
                        continue
                    destination = self.staging_dir / f".trash-{uuid.uuid4().hex}"
                    try:
                        os.replace(version_dir, destination)
                    except OSError:
                        report["busy"].append(f"{key[0]}/{key[1]}")  # in use; retried next time
                        continue
                    trash.append(destination)
                    report["removed"].append(f"{key[0]}/{key[1]}")
                try:
                    id_dir.rmdir()  # only succeeds when empty
                except OSError:
                    pass
            self._checkpoint("gc_renamed")
        for path in trash:
            self._rmtree(path)
        report["staging_removed"] = self._sweep_staging(now, staging_max_age)
        return report

    def _sweep_staging(self, now: float, max_age: float) -> list[str]:
        removed = []
        try:
            entries = list(self.staging_dir.iterdir())
        except OSError:
            return removed
        cutoff = now - max_age
        for entry in entries:
            try:
                if _is_reparse(entry):
                    entry.unlink()
                    removed.append(entry.name)
                    continue
                if entry.name == "downloads" and entry.is_dir():
                    for item in list(entry.iterdir()):
                        if item.is_file() and not _is_reparse(item) and item.lstat().st_mtime < cutoff:
                            item.unlink()
                            removed.append(f"downloads/{item.name}")
                    continue
                if entry.name.startswith(".trash-") or self._newest_mtime(entry, cutoff) < cutoff:
                    if entry.is_dir():
                        self._rmtree(entry)
                    else:
                        entry.unlink()
                    removed.append(entry.name)
            except OSError:
                continue
        return removed
