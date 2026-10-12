"""Local-only tools for building update ZIPs and signing catalog payloads.

Examples:
  python packaging/update_release.py package dist/KidTE --component app --out app.zip   (also: runtime, extensions)
  python packaging/update_release.py keygen --private-key %LOCALAPPDATA%\\KidTE\\release-key.dpapi --public-key release-public.json
  python packaging/update_release.py sign catalog-payload.json --private-key ... --out kidte-catalog-v2.json

The keygen command protects the raw Ed25519 private key with Windows DPAPI for
the current user and writes only public metadata beside it.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import stat
import sys
import tempfile
import zipfile
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "packaging"))
import stage_runtime
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from update_catalog import (
    COMPONENT_POLICIES,
    _contains_user_data,
    _parse,
    _safe_path,
    canonical_json,
    verify_catalog,
)


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes):
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))), buf


def _check_local_path(path: Path):
    absolute = Path(path).absolute()
    for item in reversed((absolute, *absolute.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("reparse_point_not_allowed")


def _private_key_outside_checkout(path: Path):
    path = Path(path).absolute()
    _check_local_path(path)
    resolved = path.resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError:
        return resolved
    raise ValueError("private_key_must_be_outside_checkout")


def _atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    _check_local_path(path.parent)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _check_local_path(path)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _dpapi(data: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI key storage is available only on Windows")
    source, _keepalive = _blob(data)
    target = DATA_BLOB()
    crypt = ctypes.WinDLL("Crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("Kernel32", use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    if decrypt:
        fn.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        fn.restype = wintypes.BOOL
        ok = fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        desc = ctypes.create_unicode_buffer("KidTE release signing key")
        fn.argtypes = [ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        fn.restype = wintypes.BOOL
        ok = fn(ctypes.byref(source), desc, None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(target.pbData)


def keygen(private_path: Path, public_path: Path):
    private_path = _private_key_outside_checkout(private_path)
    public_path = public_path.resolve()
    _check_local_path(private_path)
    _check_local_path(public_path)
    if private_path.exists() or public_path.exists():
        raise FileExistsError("keygen refuses to overwrite existing key files")
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    private_path.parent.mkdir(parents=True, exist_ok=True)
    key_id = hashlib.sha256(pub).hexdigest()[:16]
    public_bytes = (json.dumps({"key_id": key_id, "public_key": base64.b64encode(pub).decode("ascii")}, indent=2) + "\n").encode("utf-8")
    encrypted = _dpapi(raw)
    _check_local_path(private_path.parent)
    _check_local_path(public_path.parent)
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    private_created = False
    public_created = False
    try:
        with private_path.open("xb") as stream:
            private_created = True
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        with public_path.open("xb") as stream:
            public_created = True
            stream.write(public_bytes)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if private_created:
            private_path.unlink(missing_ok=True)
        if public_created:
            public_path.unlink(missing_ok=True)
        raise
    return key_id


def sign_catalog(payload_path: Path, private_path: Path, output_path: Path):
    payload = _parse(payload_path.read_bytes())
    if not isinstance(payload, dict):
        raise TypeError("catalog payload must be a JSON object")
    private_path = _private_key_outside_checkout(private_path)
    _check_local_path(private_path)
    raw = _dpapi(private_path.read_bytes(), decrypt=True)
    key = Ed25519PrivateKey.from_private_bytes(raw)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = {"schema_version": 2, "key_id": hashlib.sha256(pub).hexdigest()[:16], "signed": payload, "signature": base64.b64encode(key.sign(canonical_json(payload))).decode("ascii")}
    result = canonical_json(envelope) + b"\n"
    verify_catalog(result, {envelope["key_id"]: base64.b64encode(pub).decode("ascii")}, channel=payload.get("channel"))
    _atomic_write(output_path, result)


def package_directory(source: Path, output: Path, component="core"):
    if component not in COMPONENT_POLICIES:
        raise ValueError("unsupported component")
    source = Path(source).absolute()
    _check_local_path(source)
    source = source.resolve()
    output_abs = output.resolve()
    if source == output_abs or source in output_abs.parents:
        raise ValueError("output must be outside package source")
    _check_local_path(source)
    split = component in stage_runtime.SPLIT_COMPONENT_IDS
    if component == "core" or split:
        for path in source.rglob("*"):
            _check_local_path(path)
        state_names = {"active.json", "previous.json", "pending.json", "update-journal.json", "catalog-sequence.json", ".component-store.lock",
                       "extensions-sync.json", "launcher-startup.log"}
        if any(path.name.casefold() in state_names or {part.casefold() for part in path.relative_to(source).parts}.intersection({".staging", "packages", ".pycache"})
               for path in source.rglob("*")):
            raise ValueError("dirty_package_source_contains_update_state")
        # A split package is cut out of the merged install tree by ownership
        # (runtime / app / extensions); only its own files are validated.
        stage_runtime.validate(source, profile="cpu", minimal=True, only={component} if split else None)
    files = []
    for path in sorted(source.rglob("*"), key=lambda p: p.relative_to(source).as_posix().casefold()):
        _check_local_path(path)
        if not path.is_file():
            continue
        rel = path.relative_to(source).as_posix()
        if component == "core" and rel.casefold() in {"kidtelauncher.exe", "release-manifest.json", "bootstrap-request.json"}:
            continue
        if split and stage_runtime.component_for(rel) != component:
            continue
        if not _safe_path(rel) or _contains_user_data(rel):
            raise ValueError("unsafe_windows_path")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as src:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        files.append((rel, path, size, digest.hexdigest()))
    if not files:
        raise ValueError("empty package")
    if sum(size for _, _, size, _ in files) > 8 * 1024**3:
        raise ValueError("package exceeds unpacked size limit")
    seen = set()
    for rel, _, _, _ in files:
        if rel.casefold() in seen:
            raise ValueError("case-colliding package paths")
        seen.add(rel.casefold())
    output.parent.mkdir(parents=True, exist_ok=True)
    _check_local_path(output.parent)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(descriptor)
    temp_output = Path(temp_name)
    try:
        with zipfile.ZipFile(temp_output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for rel, path, size, source_digest in files:
                info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 0
                info.external_attr = 0
                digest = hashlib.sha256()
                count = 0
                with path.open("rb") as src, archive.open(info, "w") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        count += len(chunk)
                        digest.update(chunk)
                        dst.write(chunk)
                if count != size or digest.hexdigest() != source_digest:
                    raise ValueError("source changed while packaging")
        # Read back the completed archive and compare every member to the inventory.
        with zipfile.ZipFile(temp_output, "r") as archive:
            expected = {rel.casefold(): (size, file_digest) for rel, _, size, file_digest in files}
            if {info.filename.casefold() for info in archive.infolist()} != set(expected):
                raise ValueError("archive inventory mismatch")
            for info in archive.infolist():
                size, wanted = expected[info.filename.casefold()]
                digest = hashlib.sha256()
                count = 0
                with archive.open(info, "r") as src:
                    for chunk in iter(lambda: src.read(1024 * 1024), b""):
                        count += len(chunk)
                        digest.update(chunk)
                if count != size or digest.hexdigest() != wanted:
                    raise ValueError("archive content verification failed")
        digest = hashlib.sha256()
        with temp_output.open("rb") as src:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                digest.update(chunk)
        archive_bytes = temp_output.stat().st_size
        if archive_bytes > 2 * 1024**3:
            raise ValueError("package exceeds GitHub asset size limit")
        _check_local_path(output)
        os.replace(temp_output, output)
    finally:
        temp_output.unlink(missing_ok=True)
    inventory = [{"path": rel, "size": size, "sha256": file_sha} for rel, _, size, file_sha in files]
    return {"archive_bytes": archive_bytes, "archive_sha256": digest.hexdigest(), "unpacked_bytes": sum(size for _, _, size, _ in files), "files": inventory}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    kg = sub.add_parser("keygen")
    kg.add_argument("--private-key", required=True, type=Path)
    kg.add_argument("--public-key", required=True, type=Path)
    sg = sub.add_parser("sign")
    sg.add_argument("payload", type=Path)
    sg.add_argument("--private-key", required=True, type=Path)
    sg.add_argument("--out", required=True, type=Path)
    pk = sub.add_parser("package")
    pk.add_argument("source", type=Path)
    pk.add_argument("--out", required=True, type=Path)
    pk.add_argument("--component", choices=tuple(COMPONENT_POLICIES), required=True)
    args = parser.parse_args(argv)
    if args.command == "keygen":
        print(json.dumps({"key_id": keygen(args.private_key, args.public_key), "public_metadata": str(args.public_key)}))
    elif args.command == "sign":
        sign_catalog(args.payload, args.private_key, args.out)
    else:
        print(json.dumps(package_directory(args.source, args.out, component=args.component), indent=2))


if __name__ == "__main__":
    main()
