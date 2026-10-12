"""Small, data-independent launcher for selecting an installed Kid TE core.

The launcher deliberately does not import ``config`` or ``server``. It only
touches update metadata below ``App`` until a core process has passed the
launcher-specific readiness handshake.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8793
STARTUP_TIMEOUT = 120.0
POLL_INTERVAL = 0.25


class LauncherError(RuntimeError):
    """A safe startup could not be completed."""


class AlreadyRunning(LauncherError):
    """An instance for this data root is already serving the user."""


BASELINE_IDS = ("core", "runtime", "app", "extensions")
SPLIT_IDS = ("runtime", "app", "extensions")
LAUNCHER_CAPS = "split-components restart-exit"
MAX_RESTARTS_PER_WINDOW = 3


def _prepare_installer_core(app_root, store):
    """Consume a signed installer bootstrap after LIVE/process preflight succeeds.

    The bootstrap lists either the legacy monolithic ``core`` or the split
    ``runtime`` + ``app`` + ``extensions``; every listed package must match the
    files the installer laid down in ``App`` byte for byte.
    """
    request = app_root / "bootstrap-request.json"
    if not request.is_file():
        return
    from component_store import _check_ancestry
    from update_catalog import _version_key, resolve_components, verify_catalog
    from update_trust import TRUSTED_KEYS

    _check_ancestry(request)
    if request.stat().st_size > 2 * 1024 * 1024:
        raise LauncherError("Installer bootstrap catalog is too large.")
    raw = request.read_bytes()
    # A downloaded installer remains usable offline after its feed expires.
    # Signature, high-water mark, exact baseline and downgrade checks still apply;
    # expiration remains mandatory for catalogs fetched by the network updater.
    catalog = verify_catalog(raw, TRUSTED_KEYS, channel="beta",
                             minimum_sequence=store.read_highwater(), allow_expired=True)
    baseline = {p["id"]: p for p in catalog["components"] if p["id"] in BASELINE_IDS}
    split = bool(set(baseline) & set(SPLIT_IDS))
    if split and ("core" in baseline or not {"runtime", "app"} <= set(baseline)):
        raise LauncherError("Installer bootstrap must list runtime and app together.")
    if not baseline:
        raise LauncherError("Installer bootstrap has no baseline package.")
    for package in baseline.values():
        if not store.baseline_matches(package):
            raise LauncherError("Installer files do not match the signed bootstrap catalog.")
    active = store.load_active()
    pending = store.load_pending()
    # The Kid TE release number lives in ``core`` (legacy) or ``app`` (split).
    for mapping in (active, pending):
        components = (mapping or {}).get("components", {})
        for key, package in baseline.items():
            old = components.get(key)
            if old is None and key in {"core", "app"}:
                old = components.get("app") or components.get("core")
            if old and _version_key(package["version"]) < _version_key(old["version"]):
                raise LauncherError("Installer package is older than an active or pending package.")
    # Keep feature installs/removals that the user has already prepared.
    current = (pending or active or {}).get("components", {})
    if split:
        current = {key: row for key, row in current.items() if key != "core"}
    installed = {key: row["package"] for key, row in current.items()}
    selection = resolve_components(catalog, {key: package["version"] for key, package in baseline.items()},
                                   installed)
    if set(selection) - set(baseline):
        raise LauncherError("Installer bootstrap requires separate feature updates; use the updater.")
    for package in baseline.values():
        store.stage_baseline(package)
    # prepare replaces pending atomically only after validating every package.
    # Cancelling it first would lose a previous update if preparation failed.
    store.prepare({**current, **baseline}, catalog_sequence=catalog["sequence"])
    _check_ancestry(request)
    request.unlink()


def _canon(path: str | os.PathLike) -> Path:
    return Path(path).expanduser().resolve()


def _same_path(left: str | os.PathLike, right: str | os.PathLike) -> bool:
    return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
        os.path.abspath(os.fspath(right))
    )


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return kernel.GetLastError() == 5
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_process_identity(pid: int):
    """Return (absolute exe, creation time) without depending on psutil."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                   wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(FILETIME),
                                       ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                                       ctypes.POINTER(FILETIME)]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        image = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(image))
        created, exited, kernel_time, user_time = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not (kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)) and
                kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                       ctypes.byref(kernel_time), ctypes.byref(user_time))):
            return None
        ticks = (created.high << 32) | created.low
        created_at = ticks / 10_000_000 - 11_644_473_600
        return os.path.normcase(os.path.abspath(image.value)), round(created_at, 3)
    finally:
        kernel.CloseHandle(handle)


def _lock_pid_is_same_app(app_root: Path, pid: int, value: dict) -> bool:
    current = _windows_process_identity(pid) if os.name == "nt" else None
    recorded = value.get("process_identity")
    if current is None:
        # If process identity cannot be read, fail closed rather than risk a
        # second writer to the same data directory.
        return True
    if isinstance(recorded, dict):
        try:
            return (current[0] == os.path.normcase(os.path.abspath(recorded["executable"]))
                    and abs(current[1] - float(recorded["created_at"])) < 1.0)
        except (KeyError, TypeError, ValueError):
            return True
    # Legacy locks lack a creation timestamp. Recognize KidTE.exe only inside
    # this installation tree; a reused PID belonging to another program is safe.
    image = Path(current[0])
    try:
        image.relative_to(app_root.resolve())
        return image.name.casefold() == "kidte.exe"
    except ValueError:
        return False


class _LauncherBusy(AlreadyRunning):
    """Another launcher holds the launcher lock."""


@contextmanager
def _launcher_lock(app_root: Path):
    """Hold an OS byte-range lock separate from ComponentStore's lock."""
    path = app_root / ".kidte-launcher.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            if path.stat().st_size == 0:
                stream.seek(0)
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise _LauncherBusy("Kid TE launcher is already starting or running.") from exc
            unlock = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise _LauncherBusy("Kid TE launcher is already starting or running.") from exc
            unlock = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        try:
            yield
        finally:
            unlock()
    finally:
        stream.close()


class _WindowsJob:
    """Kill the child if this launcher exits, including on forced termination."""
    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC_LIMIT),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel = ctypes.windll.kernel32
        kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   wintypes.LPVOID, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise LauncherError("Could not create a child-process lifetime guard.")
        limits = EXTENDED_LIMIT()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel.CloseHandle(handle)
            raise LauncherError("Could not configure a child-process lifetime guard.")
        self.handle = handle

    def attach(self, process):
        if self.handle is None:
            return
        import ctypes

        if not ctypes.windll.kernel32.AssignProcessToJobObject(
            self.handle, int(process._handle)
        ):
            self.close()
            raise LauncherError("Could not attach Kid TE to the launcher lifetime guard.")

    def close(self):
        if self.handle is not None:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


def _app_root(executable: str | os.PathLike | None = None) -> Path:
    if executable is None:
        executable = sys.executable
    return _canon(executable).parent


def _data_root(app_root: Path, environ: dict[str, str]) -> Path:
    for key in ("KID_TE_ROOT", "LIVE_RE_ROOT"):
        explicit = (environ.get(key) or "").strip()
        if explicit:
            return _canon(explicit)
    return (app_root.parent / "DuLieu").resolve()


def _port(environ: dict[str, str]) -> int:
    raw = environ.get("CHAT_PORT", str(DEFAULT_PORT)).strip()
    try:
        port = int(raw)
    except ValueError as exc:
        raise LauncherError("CHAT_PORT must be a valid TCP port.") from exc
    if not 1 <= port <= 65535:
        raise LauncherError("CHAT_PORT must be between 1 and 65535.")
    return port


def _launcher_args(argv):
    host = DEFAULT_HOST
    port = None
    child_args = []
    index = 0
    args = list(argv)
    while index < len(args):
        item = args[index]
        if item in {"--host", "--port"}:
            if index + 1 >= len(args):
                raise LauncherError(f"{item} requires a value.")
            value = args[index + 1]
            index += 2
        elif item.startswith("--host="):
            value = item.partition("=")[2]
            item = "--host"
            index += 1
        elif item.startswith("--port="):
            value = item.partition("=")[2]
            item = "--port"
            index += 1
        else:
            child_args.append(item)
            index += 1
            continue
        if item == "--host":
            host = value
        else:
            try:
                port = int(value)
            except ValueError as exc:
                raise LauncherError("CHAT_PORT must be a valid TCP port.") from exc
    return host, port, child_args


def _probe_update_ready(port: int, *, timeout=0.35):
    try:
        with urllib.request.urlopen(
            f"http://{DEFAULT_HOST}:{port}/api/update-ready", timeout=timeout
        ) as response:
            if response.status != 200:
                return None
            value = json.loads(response.read(64 * 1024).decode("utf-8"))
            return value if isinstance(value, dict) else None
    except (OSError, TimeoutError, urllib.error.URLError, UnicodeError, json.JSONDecodeError):
        return None


def _port_is_occupied(port: int) -> bool:
    try:
        with socket.create_connection((DEFAULT_HOST, port), timeout=0.2):
            return True
    except OSError:
        return False


def _live_lock_state(data_root: Path, app_root: Path) -> tuple[str, int | None]:
    """Read-only check for a direct/legacy Kid TE core holding its instance lock."""
    lock = data_root / "runtime" / "live-re.lock"
    try:
        value = json.loads(lock.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "free", None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "unknown", None
    pid = value.get("pid") if isinstance(value, dict) else None
    if not isinstance(pid, int) or pid <= 0:
        return "unknown", None
    if not _is_pid_alive(pid):
        return "free", pid
    return ("running", pid) if _lock_pid_is_same_app(app_root, pid, value) else ("free", pid)


def _call_focus(port: int) -> None:
    request = urllib.request.Request(
        f"http://{DEFAULT_HOST}:{port}/api/focus", data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=1).close()
    except (OSError, TimeoutError, urllib.error.URLError, ValueError):
        pass


def _preflight(app_root: Path, data_root: Path, port: int, *, ready_probe, occupied_probe):
    state, pid = _live_lock_state(data_root, app_root)
    if state == "unknown":
        raise LauncherError(f"Cannot safely read the existing Kid TE lock in {data_root}.")
    if state == "running":
        _call_focus(port)
        raise AlreadyRunning(f"Kid TE is already running (PID {pid}).")

    response = ready_probe(port)
    if response is not None:
        existing_root = response.get("data_root")
        existing_pid = response.get("pid")
        if (isinstance(existing_pid, int) and existing_pid > 0 and
                isinstance(existing_root, str) and _same_path(existing_root, data_root)):
            _call_focus(port)
            raise AlreadyRunning(f"Kid TE is already running (PID {existing_pid}).")
        raise LauncherError(f"Port {port} is serving another application; update was not activated.")
    if occupied_probe(port):
        raise LauncherError(f"Port {port} is already in use; update was not activated.")


def _mapping_executable(app_root: Path, component: dict) -> Path:
    if not isinstance(component, dict):
        raise LauncherError("Installed core record is invalid.")
    raw_path = component.get("path")
    package = component.get("package")
    if not isinstance(raw_path, str) or not isinstance(package, dict):
        raise LauncherError("Installed core record is incomplete.")
    posix = PurePosixPath(raw_path)
    if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts) or "\\" in raw_path:
        raise LauncherError("Installed core path is unsafe.")
    root = (app_root / Path(*posix.parts)).resolve()
    try:
        root.relative_to(app_root.resolve())
    except ValueError as exc:
        raise LauncherError("Installed core escaped the application directory.") from exc
    entrypoint = package.get("entrypoint")
    files = package.get("files")
    if entrypoint != "KidTE.exe" or not isinstance(files, list):
        raise LauncherError("Installed core entry point is invalid.")
    expected = next((item for item in files
                     if isinstance(item, dict) and item.get("path", "").casefold() == "kidte.exe"), None)
    if not expected or not isinstance(expected.get("sha256"), str):
        raise LauncherError("Installed core has no signed entry-point digest.")
    executable = root / "KidTE.exe"
    try:
        if not executable.is_file() or _sha256(executable) != expected["sha256"]:
            raise LauncherError("Installed core executable failed its signed digest check.")
    except OSError as exc:
        raise LauncherError("Installed core executable is unavailable.") from exc
    return executable


def _core_component(active):
    """The component the launcher executes: ``runtime`` (split) or legacy ``core``.

    For a split generation the returned copy carries the matching app record
    under ``_app`` so schema/baseline decisions can look at both.
    """
    if not isinstance(active, dict):
        return None
    components = active.get("components") or {}
    runtime = components.get("runtime")
    if isinstance(runtime, dict):
        return {**runtime, "_app": components.get("app")}
    return components.get("core")


def _active_executable(app_root: Path, active: dict | None) -> Path:
    component = _core_component(active)
    return (_mapping_executable(app_root, component) if component is not None
            else app_root / "KidTE.exe")


def _safe_package_dir(app_root: Path, raw_path) -> Path:
    posix = PurePosixPath(raw_path) if isinstance(raw_path, str) else None
    if posix is None or posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts) or "\\" in raw_path:
        raise LauncherError("Installed package path is unsafe.")
    root = (app_root / Path(*posix.parts)).resolve()
    try:
        root.relative_to(app_root.resolve())
    except ValueError as exc:
        raise LauncherError("Installed package escaped the application directory.") from exc
    return root


def _select_app(app_root: Path, active: dict | None):
    """Verified directory of the active ``app`` package, or None (legacy/baseline)."""
    if not isinstance(active, dict):
        return None
    components = active.get("components") or {}
    if "runtime" not in components:
        return None
    component = components.get("app")
    package = component.get("package") if isinstance(component, dict) else None
    if not isinstance(package, dict) or package.get("entrypoint") != "backend/src/app_entry.py":
        raise LauncherError("Installed app record is invalid.")
    root = _safe_package_dir(app_root, component.get("path"))
    expected = next((item for item in package.get("files", [])
                     if isinstance(item, dict) and item.get("path") == "backend/src/app_entry.py"), None)
    entry = root / "backend" / "src" / "app_entry.py"
    try:
        if not expected or not entry.is_file() or _sha256(entry) != expected.get("sha256"):
            raise LauncherError("Installed app entry point failed its signed digest check.")
    except OSError as exc:
        raise LauncherError("Installed app entry point is unavailable.") from exc
    return root


def _baseline_compatible(component: dict | None) -> bool:
    """Only signed, no-migration cores pinned to today's schema can use baseline."""
    if not isinstance(component, dict) or not isinstance(component.get("package"), dict):
        return False
    from update_catalog import CORE_DATA_SCHEMA

    package = component["package"]
    if package.get("id") == "runtime":
        # The data schema is pinned by the app package that travels with it.
        app = component.get("_app")
        app_package = app.get("package") if isinstance(app, dict) else None
        return (package.get("migration") == "none" and isinstance(app_package, dict)
                and app_package.get("id") == "app" and app_package.get("migration") == "none"
                and (app_package.get("compatibility") or {}).get("data_schema") == CORE_DATA_SCHEMA)
    return (package.get("id") == "core" and package.get("migration") == "none" and
            package.get("compatibility") == {"data_schema": CORE_DATA_SCHEMA})


def _record_recovery(app_root: Path, message: str) -> None:
    try:
        with (app_root / "launcher-startup.log").open("a", encoding="utf-8") as stream:
            stream.write(f"[Kid TE launcher] {message}\n")
    except OSError:
        pass


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _select_gpu(app_root: Path, active: dict | None):
    if not isinstance(active, dict):
        return None, None
    components = active.get("components") or {}
    component = components.get("gpu-runtime") or components.get("gpu-cuda12")
    if not isinstance(component, dict):
        return None, None
    package = component.get("package")
    if not isinstance(package, dict) or not isinstance(package.get("files"), list):
        raise LauncherError("Installed GPU runtime record is invalid.")
    item = next((entry for entry in package["files"]
                 if isinstance(entry, dict) and entry.get("path") == "gpu-runtime.json"), None)
    if not item or not isinstance(item.get("sha256"), str):
        raise LauncherError("Installed GPU runtime has no signed manifest digest.")
    raw_path = component.get("path")
    posix = PurePosixPath(raw_path) if isinstance(raw_path, str) else None
    if posix is None or posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts) or "\\" in raw_path:
        raise LauncherError("Installed GPU runtime path is unsafe.")
    runtime_root = (app_root / Path(*posix.parts)).resolve()
    try:
        runtime_root.relative_to(app_root.resolve())
    except ValueError as exc:
        raise LauncherError("Installed GPU runtime escaped the application directory.") from exc
    if not (runtime_root / "gpu-runtime.json").is_file():
        raise LauncherError("Installed GPU runtime manifest is missing.")
    return runtime_root, item["sha256"]


def _environment(app_root: Path, data_root: Path, port: int, environ: dict[str, str],
                 generation: str, nonce: str, gpu_root: Path | None, gpu_hash: str | None,
                 app_dir: Path | None = None):
    child = dict(environ)
    # Tell the runtime which verified app package to load (absent: app lives
    # next to KidTE.exe, the installer/legacy layout) and that this launcher
    # understands split generations.
    child.pop("KIDTE_APP_PACKAGE_DIR", None)
    if app_dir is not None:
        child["KIDTE_APP_PACKAGE_DIR"] = str(app_dir)
    child["KIDTE_LAUNCHER_CAPS"] = LAUNCHER_CAPS
    child["KIDTE_APP_ROOT"] = str(app_root.resolve())
    # Normalize explicit overrides to their absolute target while retaining the
    # legacy LIVE_RE_ROOT variable for callers that still inspect it.
    child["KID_TE_ROOT"] = str(data_root.resolve())
    child["CHAT_HOST"] = DEFAULT_HOST
    child["CHAT_PORT"] = str(port)
    child["KIDTE_LAUNCH_NONCE"] = nonce
    child["KIDTE_LAUNCH_GENERATION"] = generation
    # An explicit operator-provided trust anchor has priority. Otherwise the
    # launcher's selection always comes from the signed active component map.
    if not (environ.get("KIDTE_GPU_RUNTIME_ROOT") or "").strip() and not (
        environ.get("KIDTE_GPU_RUNTIME_SHA256") or ""
    ).strip():
        child.pop("KIDTE_GPU_RUNTIME_ROOT", None)
        child.pop("KIDTE_GPU_RUNTIME_SHA256", None)
        if gpu_root is not None:
            child["KIDTE_GPU_RUNTIME_ROOT"] = str(gpu_root)
            child["KIDTE_GPU_RUNTIME_SHA256"] = str(gpu_hash)
    return child


def _ready_mismatches(value, process, nonce: str, generation: str, data_root: Path) -> list[str]:
    """Why a readiness answer is not ours (empty list = it is ours)."""
    if not isinstance(value, dict) or value.get("ok") is not True:
        return []  # nothing (or not Kid TE) answering yet
    problems = []
    if value.get("pid") != process.pid:
        problems.append(f"pid expected {process.pid} got {value.get('pid')}")
    if value.get("launcher_nonce") != nonce:
        problems.append("nonce differs (answer is from a core this launcher did not start)")
    if value.get("launcher_generation") != generation:
        problems.append(f"generation expected {generation} got {value.get('launcher_generation')}")
    raw_root = value.get("data_root")
    if not isinstance(raw_root, str) or not _same_path(raw_root, data_root):
        problems.append(f"data_root expected {data_root} got {raw_root}")
    return problems


def _matches_ready(value, process, nonce: str, generation: str, data_root: Path) -> bool:
    return (isinstance(value, dict) and value.get("ok") is True and
            not _ready_mismatches(value, process, nonce, generation, data_root))


class _ForeignInstance(AlreadyRunning):
    """Another Kid TE core already owns this data folder / port."""


def _wait_ready(process, nonce, generation, data_root, port, *, ready_probe, timeout, sleep,
                log=None):
    from update_restart import ALREADY_RUNNING_EXIT_CODE

    deadline = time.monotonic() + timeout
    last_reason = None
    foreign = None
    while time.monotonic() < deadline:
        answer = ready_probe(port)
        code = process.poll()
        if code is not None:
            if code == ALREADY_RUNNING_EXIT_CODE or foreign is not None:
                raise _ForeignInstance(
                    "another Kid TE core already owns this data folder"
                    + (f" (pid {foreign})" if foreign is not None else ""))
            return False, f"core exited before readiness (exit code {code})"
        if _matches_ready(answer, process, nonce, generation, data_root):
            return True, "ready"
        problems = _ready_mismatches(answer, process, nonce, generation, data_root)
        if problems:
            reason = "; ".join(problems)
            if reason != last_reason:
                last_reason = reason
                if log is not None:
                    log(f"readiness mismatch: {reason}")
            root = answer.get("data_root")
            if isinstance(root, str) and _same_path(root, data_root) and answer.get("pid") != process.pid:
                foreign = answer.get("pid")
        sleep(POLL_INTERVAL)
    return False, (f"core did not become ready within {int(timeout)} seconds"
                   + (f" (last mismatch: {last_reason})" if last_reason else ""))

def _terminate_owned(process, job, *, timeout=5):
    if process is None:
        if job is not None:
            job.close()
        return
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=timeout)
            except (OSError, subprocess.TimeoutExpired):
                pass
    if job is not None:
        job.close()


class Launcher:
    """Startup coordinator. Dependencies are injectable for non-LIVE tests."""
    def __init__(self, *, store_factory=None, popen=None, ready_probe=None,
                 occupied_probe=None, job_factory=None, sleep=time.sleep,
                 startup_timeout=STARTUP_TIMEOUT, environ=None):
        self.store_factory = store_factory
        self.popen = popen or subprocess.Popen
        self.ready_probe = ready_probe or _probe_update_ready
        self.occupied_probe = occupied_probe or _port_is_occupied
        self.job_factory = job_factory or _WindowsJob
        self.sleep = sleep
        self.startup_timeout = startup_timeout
        self.environ = dict(os.environ if environ is None else environ)
        self._last_nonce = ""

    def _store(self, app_root):
        if self.store_factory:
            return self.store_factory(app_root)
        from component_store import ComponentStore
        return ComponentStore(app_root)

    def _sync_extensions(self, app_root, active):
        """Refresh the stable Chrome extension folders; never blocks startup."""
        if not isinstance(active, dict) or "extensions" not in (active.get("components") or {}):
            return
        try:
            from extension_sync import sync_extensions
            result = sync_extensions(app_root, active)
            if result.get("changed"):
                _record_recovery(app_root, "extension folders refreshed; reload the unpacked "
                                 "extensions in chrome://extensions")
        except Exception as exc:  # noqa: BLE001 - extension refresh must not stop Kid TE
            _record_recovery(app_root, f"extension refresh failed: {exc}")

    def _collect_garbage(self, store):
        """After a healthy start keep only the active + previous package versions."""
        collect = getattr(store, "collect_garbage", None)
        if collect is None:
            return
        try:
            grace = float(self.environ.get("KIDTE_GC_GRACE_SECONDS", "3600"))
            report = collect(grace_seconds=max(0.0, grace))
            if report.get("removed") or report.get("staging_removed"):
                _record_recovery(store.app_root, "cleanup removed " + ", ".join(
                    [*report.get("removed", []), *report.get("staging_removed", [])]))
        except Exception as exc:  # noqa: BLE001 - cleanup is best effort
            try:
                _record_recovery(store.app_root, f"cleanup skipped: {exc}")
            except Exception:  # noqa: BLE001
                pass

    def _spawn(self, executable, app_root, data_root, port, generation, active,
               passthrough_args=()):
        nonce = secrets.token_urlsafe(32)
        gpu_root, gpu_hash = _select_gpu(app_root, active)
        app_dir = _select_app(app_root, active)
        self._sync_extensions(app_root, active)
        env = _environment(app_root, data_root, port, self.environ, generation,
                           nonce, gpu_root, gpu_hash, app_dir)
        command = [str(executable), *passthrough_args]
        job = self.job_factory()
        process = None
        self._last_nonce = nonce
        try:
            flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                     | getattr(subprocess, "CREATE_NO_WINDOW", 0))
            process = self.popen(command, cwd=str(app_root), env=env, creationflags=flags)
            job.attach(process)
            ready, detail = _wait_ready(
                process, nonce, generation, data_root, port,
                ready_probe=self.ready_probe, timeout=self.startup_timeout, sleep=self.sleep,
                log=lambda m: _record_recovery(app_root, m),
            )
            if not ready:
                _terminate_owned(process, job)
                return None, detail
            return (process, job), "ready"
        except BaseException:
            _terminate_owned(process, job)
            raise

    def run(self, executable: str | os.PathLike | None = None, *, passthrough_args=()) -> int:
        app_root = _app_root(executable)
        data_root = _data_root(app_root, self.environ)
        host, argument_port, child_args = _launcher_args(passthrough_args)
        if host.casefold() not in {"127.0.0.1", "localhost"}:
            raise LauncherError("Kid TE launcher only supports localhost.")
        port = argument_port if argument_port is not None else _port(self.environ)
        if not 1 <= port <= 65535:
            raise LauncherError("CHAT_PORT must be between 1 and 65535.")
        try:
            with _launcher_lock(app_root):
                recent: list[float] = []
                while True:
                    _preflight(app_root, data_root, port, ready_probe=self.ready_probe,
                               occupied_probe=self.occupied_probe)
                    code = self._run_once(app_root, data_root, port, child_args)
                    if not self._restart_requested(app_root, code):
                        return code
                    # The core asked for a restart (UI "Khoi dong lai"): run the normal
                    # selection again so a pending generation is activated through the
                    # usual readiness handshake / rollback. Cap runaway loops.
                    now = time.monotonic()
                    recent = [t for t in recent if now - t < 120.0] + [now]
                    if len(recent) > MAX_RESTARTS_PER_WINDOW:
                        _record_recovery(app_root, "restart requested too often; not restarting again")
                        return code
                    _record_recovery(app_root, "restart requested by core; re-running startup selection")
        except _LauncherBusy:
            # A second open while a launcher is starting/running: just bring the window up.
            _call_focus(port)
            raise

    def _defer_for_foreign_instance(self, store, app_root, port, activated, pending_snapshot, exc):
        """Another core owns the data folder: that is not this generation's failure.

        An unconfirmed generation we just activated is put back as a *pending* update
        (fresh generation id) instead of being rolled back and lost, and the running
        window is focused.
        """
        _record_recovery(app_root, f"another Kid TE instance is running ({exc}); "
                         "this launcher is exiting without changing the healthy generation")
        if activated is not None:
            try:
                store.rollback()
                if pending_snapshot is not None:
                    store.restore_pending(pending_snapshot)
                    _record_recovery(app_root, "pending update kept for the next restart")
            except Exception as err:  # noqa: BLE001 - best effort
                _record_recovery(app_root, f"could not requeue pending update: {err}")
        _call_focus(port)

    def _restart_requested(self, app_root, code) -> bool:
        from update_restart import RESTART_EXIT_CODE, consume_marker

        nonce, self._last_nonce = self._last_nonce, ""
        return code == RESTART_EXIT_CODE and consume_marker(app_root, nonce)

    def _run_once(self, app_root, data_root, port, child_args) -> int:
        from update_restart import marker_path

        try:
            marker_path(app_root).unlink()
        except OSError:
            pass
        store = self._store(app_root)
        store.recover()
        try:
            _prepare_installer_core(app_root, store)
        except (OSError, RuntimeError, ValueError) as exc:
            _record_recovery(app_root, f"installer bootstrap rejected: {exc}")
        activated = None
        pending_snapshot = None
        pending_attempt = False
        try:
            pending = store.load_pending()
            pending_snapshot = pending
            pending_attempt = pending is not None
            active = store.activate_pending() if pending is not None else store.load_active()
            activated = active if pending is not None else None
        except (OSError, RuntimeError, ValueError) as exc:
            # Validation may reject a damaged pending archive before the switch,
            # or a filesystem error may interrupt the switch. Recover the journal
            # before starting the old generation; never execute the rejected one.
            store.recover()
            store.cancel_pending()
            active = store.load_active()
            _record_recovery(app_root, f"pending update rejected before startup: {exc}")
        generation = active.get("generation") if isinstance(active, dict) else "baseline"
        if not isinstance(generation, str) or not generation:
            raise LauncherError("Active component generation is invalid.")
        component = _core_component(active)
        baseline_recovery_allowed = _baseline_compatible(component)
        rollback_attempted = False
        recovery_note = ""
        try:
            selected = _active_executable(app_root, active)
        except Exception as exc:
            if activated is not None:
                detail = f"pending core validation failed: {exc}"
                store.rollback()
                activated = None
                active = store.load_active()
                generation = active.get("generation") if isinstance(active, dict) else "baseline"
                component = _core_component(active)
                try:
                    selected = _active_executable(app_root, active)
                except LauncherError:
                    if component is not None and not _baseline_compatible(component):
                        raise LauncherError(
                            "The previous core failed validation, and its signed data schema "
                            "does not permit starting the legacy baseline."
                        )
                    selected = app_root / "KidTE.exe"
            elif component is not None:
                if not _baseline_compatible(component):
                    raise LauncherError(
                        "The active core failed validation, and its signed data schema "
                        "does not permit starting the legacy baseline."
                    ) from exc
                recovery_note = f"active core validation failed: {exc}"
                try:
                    rollback_attempted = True
                    store.rollback()
                    active = store.load_active()
                    generation = active.get("generation") if isinstance(active, dict) else "baseline"
                    component = _core_component(active)
                    selected = _active_executable(app_root, active)
                except Exception as rollback_exc:  # noqa: BLE001 - corrupt active state must still allow safe baseline recovery
                    recovery_note += f"; previous generation unavailable: {rollback_exc}"
                    active = None
                    generation = "baseline"
                    component = None
                    selected = app_root / "KidTE.exe"
            else:
                raise
        if not selected.is_file():
            if activated is not None:
                store.rollback()
                activated = None
                active = store.load_active()
                generation = active.get("generation") if isinstance(active, dict) else "baseline"
                component = _core_component(active)
                try:
                    selected = _active_executable(app_root, active)
                except LauncherError:
                    if component is not None and not _baseline_compatible(component):
                        raise LauncherError(
                            "The previous core failed validation, and its signed data schema "
                            "does not permit starting the legacy baseline."
                        )
                    selected = app_root / "KidTE.exe"
            elif recovery_note and baseline_recovery_allowed:
                active = None
                generation = "baseline"
                component = None
                selected = app_root / "KidTE.exe"
            if not selected.is_file():
                raise LauncherError(f"Kid TE core executable was not found: {selected}")

        launch_active = active
        launch_generation = generation
        if _same_path(selected, app_root / "KidTE.exe"):
            # The legacy baseline has no signed modular GPU association.
            launch_active = None
            launch_generation = "baseline"
        if recovery_note and _same_path(selected, app_root / "KidTE.exe"):
            _record_recovery(app_root, recovery_note + "; starting legacy baseline")
        try:
            running, detail = self._spawn(selected, app_root, data_root, port,
                                          launch_generation, launch_active, child_args)
        except AlreadyRunning as exc:
            self._defer_for_foreign_instance(store, app_root, port, activated, pending_snapshot, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - startup boundary must rollback on every launch failure
            running, detail = None, f"core launch failed: {exc}"
        if running is not None:
            process, job = running
            if activated is not None:
                try:
                    store.confirm_active(generation)
                except Exception as exc:  # noqa: BLE001 - store confirmation failures require rollback
                    _terminate_owned(process, job)
                    running = None
                    detail = f"new core became ready but activation confirmation failed: {exc}"
                else:
                    activated = None
            if running is not None:
                self._collect_garbage(store)
                try:
                    return int(process.wait())
                finally:
                    job.close()
        if activated is not None:
            try:
                store.rollback()
                previous = store.load_active()
                previous_generation = (previous.get("generation")
                                       if isinstance(previous, dict) else "baseline")
                try:
                    previous_exe = _active_executable(app_root, previous)
                except LauncherError:
                    previous_component = _core_component(previous)
                    if (previous_component is not None and
                            not _baseline_compatible(previous_component)):
                        raise LauncherError(
                            "The previous core failed validation, and its signed data schema "
                            "does not permit starting the legacy baseline."
                        )
                    previous_exe = app_root / "KidTE.exe"
                if previous_exe.is_file():
                    fallback, fallback_detail = self._spawn(
                        previous_exe, app_root, data_root, port,
                        previous_generation, previous, child_args,
                    )
                    if fallback is not None:
                        process, job = fallback
                        try:
                            return int(process.wait())
                        finally:
                            job.close()
                    detail += f"; previous version also failed: {fallback_detail}"
            except AlreadyRunning:
                raise
            except Exception as exc:  # noqa: BLE001 - report all rollback and previous-core failures
                detail += f"; rollback/fallback failed: {exc}"
        elif component is not None and not pending_attempt:
            if not _baseline_compatible(component):
                raise LauncherError(
                    f"The active core failed startup ({detail}); its signed data schema "
                    "does not permit starting the legacy baseline."
                )
            # Retire this failing active generation in favor of its previous
            # signed generation once. If that cannot start, use the baseline
            # only because the failed core explicitly allows its data schema.
            if not rollback_attempted:
                try:
                    rollback_attempted = True
                    store.rollback()
                    previous = store.load_active()
                    previous_generation = (previous.get("generation")
                                           if isinstance(previous, dict) else "baseline")
                    previous_component = _core_component(previous)
                    if previous_component is not None:
                        previous_exe = _active_executable(app_root, previous)
                        if previous_exe.is_file():
                            fallback, fallback_detail = self._spawn(
                                previous_exe, app_root, data_root, port,
                                previous_generation, previous, child_args,
                            )
                            if fallback is not None:
                                process, job = fallback
                                try:
                                    return int(process.wait())
                                finally:
                                    job.close()
                            detail += f"; previous version also failed: {fallback_detail}"
                except AlreadyRunning:
                    raise
                except Exception as exc:  # noqa: BLE001 - report any prior-generation recovery failure
                    detail += f"; previous generation recovery failed: {exc}"
            baseline = app_root / "KidTE.exe"
            if baseline.is_file():
                try:
                    _record_recovery(app_root, detail + "; starting legacy baseline")
                    fallback, fallback_detail = self._spawn(
                        baseline, app_root, data_root, port, "baseline", None, child_args,
                    )
                    if fallback is not None:
                        process, job = fallback
                        try:
                            return int(process.wait())
                        finally:
                            job.close()
                    detail += f"; legacy baseline also failed: {fallback_detail}"
                except AlreadyRunning:
                    raise
                except Exception as exc:  # noqa: BLE001 - report any baseline fallback startup failure
                    detail += f"; legacy baseline launch failed: {exc}"
        raise LauncherError(f"Kid TE could not start safely: {detail}")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        return Launcher().run(passthrough_args=argv)
    except AlreadyRunning:
        return 0
    except LauncherError as exc:
        _report_startup_failure(exc)
        return 2
    except Exception as exc:  # noqa: BLE001 - GUI entrypoint must convert unexpected startup errors to a user message
        _report_startup_failure(f"Launcher error: {type(exc).__name__}: {exc}")
        return 2


def _report_startup_failure(error):
    message = str(error)
    try:
        app_root = _app_root()
        with (app_root / "launcher-startup.log").open("a", encoding="utf-8") as stream:
            stream.write(f"[Kid TE launcher] {message}\n")
    except OSError:
        pass
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None, message, "Kid TE không khởi động được", 0x10 | 0x1000,
            )
            return
        except (AttributeError, OSError):
            pass
    try:
        print(f"[Kid TE] {message}", file=sys.stderr, flush=True)
    except (AttributeError, OSError):
        pass


if __name__ == "__main__":  # pragma: no cover - exercised by the executable
    raise SystemExit(main())
