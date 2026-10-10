"""Build a deterministic, hash-manifested optional NVIDIA runtime ZIP.

Only the CUDA 12 CTranslate2 DLL allowlist and its NVIDIA license files are
written. The command never downloads packages or modifies the application.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

MANIFEST_NAME = "gpu-runtime.json"
SCHEMA_VERSION = 1
CUDA_PACKAGES = (
    ("nvidia-cublas-cu12", "nvidia/cublas/bin"),
    ("nvidia-cudnn-cu12", "nvidia/cudnn/bin"),
    ("nvidia-cuda-nvrtc-cu12", "nvidia/cuda_nvrtc/bin"),
)
DLL_ALLOWLIST = {
    "nvidia/cublas/bin": frozenset({
        "cublas64_12.dll", "cublaslt64_12.dll", "nvblas64_12.dll",
    }),
    "nvidia/cudnn/bin": frozenset({
        "cudnn64_9.dll", "cudnn_adv64_9.dll", "cudnn_cnn64_9.dll",
        "cudnn_engines_precompiled64_9.dll", "cudnn_engines_runtime_compiled64_9.dll",
        "cudnn_engines_tensor_ir64_9.dll", "cudnn_ext64_9.dll", "cudnn_graph64_9.dll",
        "cudnn_heuristic64_9.dll", "cudnn_ops64_9.dll",
    }),
    "nvidia/cuda_nvrtc/bin": frozenset({
        "nvrtc-builtins64_129.dll", "nvrtc64_120_0.alt.dll", "nvrtc64_120_0.dll",
    }),
}
CUDA_RUNTIME = "cuda12"
PLATFORM = "windows"
ARCHITECTURE = "x86_64"
CUDNN_MAJOR = 9
CHUNK_SIZE = 1024 * 1024
TEXT_LICENSE_SUFFIXES = {".txt", ".md", ".rst", ".html", ".htm", ".xml", ".json"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _has_reparse_point(root: Path, path: Path) -> bool:
    root = root.resolve(strict=True)
    try:
        relative = path.absolute().relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        try:
            attributes = getattr(current.lstat(), "st_file_attributes", 0)
        except OSError:
            return True
        if current.is_symlink() or attributes & 0x400:
            return True
        is_junction = getattr(current, "is_junction", None)
        if is_junction and is_junction():
            return True
    return False


def _safe_source_path(root: Path, path: Path) -> Path:
    if _has_reparse_point(root, path) or not path.is_file() or not _inside(root, path):
        raise ValueError(f"unsafe or missing source file: {path}")
    return path


def _file_record(path: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "size": size}


def _distributions(site_packages: Path) -> dict[str, importlib.metadata.Distribution]:
    result = {}
    for dist in importlib.metadata.distributions(path=[str(site_packages)]):
        name = dist.metadata.get("Name", "")
        if name:
            result[_normalize_distribution(name)] = dist
    return result


def _record_digest(site_packages: Path, dist: importlib.metadata.Distribution) -> str:
    files = dist.files or ()
    record = next((item for item in files if item.name.lower() == "record"), None)
    if record is None:
        raise ValueError(f"distribution has no RECORD: {dist.metadata.get('Name')}")
    record_path = Path(dist.locate_file(record))
    return _file_record(_safe_source_path(site_packages, record_path))["sha256"]


def _license_candidates(site_packages: Path, dist: importlib.metadata.Distribution):
    candidates = []
    for item in dist.files or ():
        path = PurePosixPath(str(item).replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or "\\" in str(item):
            raise ValueError(f"unsafe license source path: {item}")
        basename = path.name.lower()
        is_license = (any(part.lower() in {"license", "licenses"} for part in path.parts)
                      or basename.startswith(("license", "notice", "copying", "copyright")))
        if not is_license:
            continue
        source = Path(dist.locate_file(item))
        if source.is_file() and not _has_reparse_point(site_packages, source) and _inside(site_packages, source):
            if source.suffix.lower() not in TEXT_LICENSE_SUFFIXES:
                raise ValueError(f"license metadata must be text, got: {path.as_posix()}")
            candidates.append((path.as_posix(), source))
    return sorted(candidates, key=lambda row: row[0].casefold())


def _source_payload(site_packages: Path):
    site_packages = Path(site_packages).resolve(strict=True)
    if not site_packages.is_dir():
        raise NotADirectoryError(site_packages)
    dists = _distributions(site_packages)
    required_names = {name for name, _ in CUDA_PACKAGES}
    missing_dists = sorted(required_names - dists.keys())
    if missing_dists:
        raise ValueError(f"missing NVIDIA distribution metadata: {missing_dists}")
    ctranslate = dists.get("ctranslate2")
    if ctranslate is None:
        raise ValueError("missing ctranslate2 distribution metadata")

    payload: dict[str, dict] = {}
    windows_paths = set()
    provenance_distributions = []
    license_metadata = []
    dll_directories = []
    for distribution_name, relative_directory in CUDA_PACKAGES:
        dist = dists[distribution_name]
        provenance_distributions.append({
            "name": distribution_name,
            "version": dist.version,
            "record_sha256": _record_digest(site_packages, dist),
        })
        source_directory = site_packages.joinpath(*relative_directory.split("/"))
        if (not source_directory.is_dir() or _has_reparse_point(site_packages, source_directory)
                or not _inside(site_packages, source_directory)):
            raise ValueError(f"missing or unsafe NVIDIA DLL directory: {relative_directory}")
        dlls = sorted(source_directory.glob("*.dll"), key=lambda path: path.name.casefold())
        if not dlls:
            raise ValueError(f"no DLLs found in {relative_directory}")
        package_versions = {
            "nvidia-cublas-cu12": 12,
            "nvidia-cudnn-cu12": 9,
            "nvidia-cuda-nvrtc-cu12": 12,
        }
        expected_major = package_versions[distribution_name]
        if not re.match(rf"^{expected_major}(?:\.|\+|$)", dist.version):
            raise ValueError(f"{distribution_name} version {dist.version} is not CUDA/cuDNN major {expected_major}")
        found_names = set()
        for source in dlls:
            if (_has_reparse_point(site_packages, source)
                    or source.name.casefold() not in DLL_ALLOWLIST[relative_directory]):
                raise ValueError(f"unexpected GPU DLL for {distribution_name}: {source.name}")
            found_names.add(source.name.casefold())
            archive_path = f"{relative_directory}/{source.name}"
            folded = archive_path.casefold()
            if folded in windows_paths:
                raise ValueError(f"case-insensitive archive path collision: {archive_path}")
            windows_paths.add(folded)
            safe_source = _safe_source_path(site_packages, source)
            payload[archive_path] = {"source": safe_source, **_file_record(safe_source)}
            dll_directories.append(relative_directory)
        expected_names = DLL_ALLOWLIST[relative_directory]
        missing_names = sorted(expected_names - found_names)
        if missing_names:
            raise ValueError(f"missing required {distribution_name} DLLs: {missing_names}")

        license_files = _license_candidates(site_packages, dist)
        if not license_files:
            raise ValueError(f"no license metadata found for {distribution_name}")
        for source_relative, source in license_files:
            filename = PurePosixPath(source_relative).name
            archive_path = f"licenses/{distribution_name}/{filename}"
            if archive_path in payload:
                # Keep exact content when two source paths share a basename.
                relative_parent = PurePosixPath(source_relative).parent
                archive_path = f"licenses/{distribution_name}/{relative_parent.as_posix()}/{filename}"
            folded = archive_path.casefold()
            if folded in windows_paths:
                raise ValueError(f"duplicate license archive path: {archive_path}")
            windows_paths.add(folded)
            safe_source = _safe_source_path(site_packages, source)
            try:
                license_bytes = safe_source.read_bytes()
                license_bytes.decode("utf-8-sig")
            except (OSError, UnicodeError) as exc:
                raise ValueError(f"license metadata must be readable UTF-8 text: {source_relative}") from exc
            if b"\0" in license_bytes:
                raise ValueError(f"license metadata contains binary data: {source_relative}")
            payload[archive_path] = {"source": safe_source, **_file_record(safe_source)}
            license_metadata.append({
                "path": archive_path,
                "distribution": distribution_name,
                "source_path": source_relative,
            })

    return (payload, sorted(set(dll_directories)),
            sorted(provenance_distributions, key=lambda row: row["name"]),
            sorted(license_metadata, key=lambda row: row["path"].casefold()),
            ctranslate.version)


def _valid_commit(value: str) -> bool:
    return bool(re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value))


def build_manifest(payload: dict[str, dict], *, version: str, python_abi: str,
                   ctranslate2_version: str, source_commit: str,
                   dll_directories: list[str], distributions: list[dict],
                   license_metadata: list[dict]) -> dict:
    if not version.strip():
        raise ValueError("component version is required")
    if not re.fullmatch(r"cp\d{2,3}", python_abi):
        raise ValueError("python_abi must look like cp311")
    if not ctranslate2_version.strip():
        raise ValueError("ctranslate2 version is required")
    if not _valid_commit(source_commit):
        raise ValueError("source commit must be a 40- or 64-character Git SHA")
    folded_paths = [path.casefold() for path in payload]
    if len(folded_paths) != len(set(folded_paths)):
        raise ValueError("archive contains case-insensitive duplicate paths")
    files = [{"path": path, "sha256": record["sha256"], "size": record["size"]}
             for path, record in sorted(payload.items(), key=lambda row: row[0].casefold())]
    return {
        "schema_version": SCHEMA_VERSION,
        "component": "gpu-cuda12",
        "version": version,
        "platform": PLATFORM,
        "architecture": ARCHITECTURE,
        "runtime": CUDA_RUNTIME,
        "python_abi": python_abi,
        "ctranslate2_version": ctranslate2_version,
        "cudnn_major": CUDNN_MAJOR,
        "dll_directories": sorted(set(dll_directories)),
        "files": files,
        "provenance": {
            "source_commit": source_commit.lower(),
            "distributions": sorted(distributions, key=lambda row: row["name"]),
            "license_metadata": sorted(license_metadata, key=lambda row: row["path"].casefold()),
        },
    }


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.flag_bits = 0
    info.extra = b""
    return info


def _write_file(archive: zipfile.ZipFile, member: str, record: dict, source_root: Path):
    info = _zip_info(member)
    digest = hashlib.sha256()
    size = 0
    source_path = _safe_source_path(source_root, Path(record["source"]))
    with source_path.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            digest.update(chunk)
            size += len(chunk)
            target.write(chunk)
    if size != record["size"] or digest.hexdigest() != record["sha256"]:
        raise ValueError(f"source changed during archive creation: {member}")


def _hash_path(path: Path) -> str:
    return _file_record(path)["sha256"]


def build(site_packages: Path, output: Path, *, version: str, python_abi: str,
          source_commit: str) -> dict:
    payload, dll_directories, distributions, license_metadata, ctranslate_version = _source_payload(site_packages)
    manifest = build_manifest(
        payload, version=version, python_abi=python_abi,
        ctranslate2_version=ctranslate_version, source_commit=source_commit,
        dll_directories=dll_directories, distributions=distributions,
        license_metadata=license_metadata)
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=output.name + ".", suffix=".incomplete",
                                     dir=output.parent)
    os.close(fd)
    temporary = Path(temp_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=9, strict_timestamps=True) as archive:
            archive_members = {name: record for name, record in payload.items()}
            archive_members[MANIFEST_NAME] = {"manifest": manifest_bytes}
            for name in sorted(archive_members, key=str.casefold):
                record = archive_members[name]
                if name == MANIFEST_NAME:
                    archive.writestr(_zip_info(name), record["manifest"],
                                     compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
                else:
                    _write_file(archive, name, record, Path(site_packages).resolve())
        with zipfile.ZipFile(temporary, "r") as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise ValueError(f"ZIP CRC verification failed: {bad_member}")
            if archive.read(MANIFEST_NAME) != manifest_bytes:
                raise ValueError("written manifest does not match the generated manifest")
            expected_names = set(payload) | {MANIFEST_NAME}
            if set(archive.namelist()) != expected_names:
                raise ValueError("written ZIP members differ from the GPU manifest payload")
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "manifest": manifest,
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "archive": str(output),
        "archive_bytes": output.stat().st_size,
        "archive_sha256": _hash_path(output),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    pack = subparsers.add_parser("build", help="create an explicit GPU runtime ZIP")
    pack.add_argument("site_packages", type=Path)
    pack.add_argument("output", type=Path)
    pack.add_argument("--version", required=True, help="GPU component version")
    pack.add_argument("--python-abi", required=True, help="e.g. cp311")
    pack.add_argument("--source-commit", required=True, help="40- or 64-character Git SHA")
    args = parser.parse_args(argv)
    result = build(args.site_packages, args.output, version=args.version,
                   python_abi=args.python_abi, source_commit=args.source_commit)
    # Keep console output concise; exact manifest is present in the archive.
    print(json.dumps({key: value for key, value in result.items() if key != "manifest"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
