"""Validation and dependency resolution for signed KidTE update catalogs."""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

REPOSITORY = "kidlost1412/KidTE"
COMPONENT_POLICIES = {
    "core": ("core", "KidTE.exe"),
    "gpu-cuda12": ("gpu-runtime", "gpu-runtime.json"),
    "media-tools": ("media-tools", "tools/ffmpeg.exe"),
    "captcha-solver": ("service", "Captcha/KidTECaptcha.exe"),
    "video-sync": ("service", "KidTEVideo.exe"),
    "video-analysis-cpu": ("worker", "KidTETranscription.exe"),
}
CORE_DATA_SCHEMA = "b9a391a4195837dde253fc772fba0f9b5d18346b04eb634a86106c1c3b8389a3"
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$")
_CATALOG_FIELDS = {"schema_version", "repository", "channel", "sequence", "issued_at", "expires_at", "components"}
_PACKAGE_FIELDS = {"id", "kind", "version", "platform", "architecture", "core_api", "dependencies", "url", "archive_sha256", "archive_bytes", "unpacked_bytes", "files", "entrypoint", "restart_required", "migration", "source_commit", "compatibility"}
_FILE_FIELDS = {"path", "size", "sha256"}


class CatalogError(ValueError):
    """The catalog is malformed, untrusted, stale, or incompatible."""


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _pairs_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("duplicate_json_key")
        result[key] = value
    return result


def _parse(raw):
    try:
        if isinstance(raw, bytes):
            if len(raw) > 2 * 1024 * 1024:
                raise CatalogError("catalog_too_large")
            raw = raw.decode("utf-8", "strict")
        elif isinstance(raw, str) and len(raw.encode("utf-8")) > 2 * 1024 * 1024:
            raise CatalogError("catalog_too_large")
        return json.loads(raw, object_pairs_hook=_pairs_no_duplicates, parse_constant=lambda _: (_ for _ in ()).throw(CatalogError("invalid_json_number")))
    except CatalogError:
        raise
    except (UnicodeError, TypeError, ValueError) as exc:
        raise CatalogError("invalid_json") from exc


def _semver(value):
    if not isinstance(value, str) or not _SEMVER.fullmatch(value):
        raise CatalogError("invalid_semver")
    m = _SEMVER.fullmatch(value)
    pre = m.group(4)
    return tuple(map(int, m.group(1, 2, 3))), pre


def _version_key(value):
    nums, pre = _semver(value)
    if pre is None:
        return (*nums, 1, ())
    parts = tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split("."))
    return (*nums, 0, parts)


def _range_satisfies(version, expr):
    _semver(version)
    if not isinstance(expr, str) or not expr or len(expr) > 200:
        return False
    for token in expr.split(","):
        token = token.strip()
        match = re.fullmatch(r"(>=|<=|>|<|==|=)?\s*(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)", token)
        if not match:
            return False
        op, bound = match.groups()
        a, b = _version_key(version), _version_key(bound)
        op = op or "="
        if not {">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b, "=": a == b, "==": a == b}[op]:
            return False
    return True


def _safe_path(path):
    if not isinstance(path, str) or not path or len(path) > 240 or "\\" in path or path.startswith("/"):
        return False
    parts = path.split("/")
    return all(p not in {"", ".", ".."} and not p.endswith((".", " ")) and not re.search(r'[<>:"|?*\x00-\x1f]', p) and p.split(".")[0].upper() not in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))} for p in parts)


def _validate_package(p, repository):
    if not isinstance(p, dict) or set(p) != _PACKAGE_FIELDS:
        raise CatalogError("invalid_package_fields")
    if not isinstance(p["id"], str) or p["id"] not in COMPONENT_POLICIES or p["kind"] != COMPONENT_POLICIES[p["id"]][0]:
        raise CatalogError("unsupported_component")
    _semver(p["version"])
    if p["platform"] != "windows" or p["architecture"] != "x86_64":
        raise CatalogError("unsupported_platform")
    if p["core_api"] != ">=1.0.0,<2.0.0":
        raise CatalogError("unsupported_core_api")
    deps = p["dependencies"]
    if (not isinstance(deps, dict) or len(deps) > 10
            or any(k not in COMPONENT_POLICIES or k == p["id"] or not _valid_range(v)
                   for k, v in deps.items())):
        raise CatalogError("invalid_dependencies")
    parsed = urlparse(p["url"] if isinstance(p["url"], str) else "")
    prefix = f"/{repository}/releases/download/"
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.port is not None or not parsed.path.startswith(prefix) or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise CatalogError("invalid_package_url")
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", parsed.path[len(prefix):]):
        raise CatalogError("invalid_package_url")
    for field in ("archive_sha256",):
        if not isinstance(p[field], str) or not _SHA256.fullmatch(p[field]):
            raise CatalogError("invalid_archive_digest")
    for field in ("archive_bytes", "unpacked_bytes"):
        maximum = 2 * 1024**3 if field == "archive_bytes" else 8 * 1024**3
        if type(p[field]) is not int or not 1 <= p[field] <= maximum:
            raise CatalogError("invalid_archive_size")
    if type(p["restart_required"]) is not bool or p["restart_required"] is not True or p["migration"] != "none":
        raise CatalogError("unsupported_activation")
    if not isinstance(p["source_commit"], str) or not _SHA.fullmatch(p["source_commit"]):
        raise CatalogError("invalid_source_commit")
    if p["id"] == "core":
        if p["entrypoint"] != "KidTE.exe" or p["dependencies"] or p["compatibility"] != {"data_schema": CORE_DATA_SCHEMA}:
            raise CatalogError("invalid_core_package")
    elif p["id"] == "gpu-cuda12":
        c = p["compatibility"]
        if (not isinstance(c, dict) or p["entrypoint"] != "gpu-runtime.json"
                or set(c) != {"python_abi", "ctranslate2_version", "cuda_major", "cudnn_major"}
                or c["python_abi"] != "cp311" or c["ctranslate2_version"] != "4.8.2"
                or c["cuda_major"] != 12 or c["cudnn_major"] != 9):
            raise CatalogError("invalid_gpu_compatibility")
    else:
        expected_compatibility = ({"protocol": 1, "python_abi": "cp311", "ctranslate2_version": "4.8.2"}
                                  if p["id"] == "video-analysis-cpu" else {"component_api": 1})
        if (p["entrypoint"] != COMPONENT_POLICIES[p["id"]][1]
                or p["compatibility"] != expected_compatibility
                or type(p["compatibility"].get("protocol" if p["id"] == "video-analysis-cpu" else "component_api")) is not int
                or p["dependencies"].get("core") != ">=1.14.255,<2.0.0"
                or (p["id"] in {"captcha-solver", "video-analysis-cpu"}
                    and p["dependencies"].get("media-tools") != ">=1.0.0,<2.0.0")):
            raise CatalogError("invalid_optional_component")
    files = p["files"]
    if not isinstance(files, list) or not files or len(files) > 100_000:
        raise CatalogError("invalid_file_inventory")
    names = set()
    parent_paths = set()
    total = 0
    for f in files:
        if not isinstance(f, dict) or set(f) != _FILE_FIELDS or not _safe_path(f["path"]) or _contains_user_data(f["path"]) or f["path"].casefold() in names or type(f["size"]) is not int or f["size"] < 0 or not isinstance(f["sha256"], str) or not _SHA256.fullmatch(f["sha256"]):
            raise CatalogError("invalid_file_inventory")
        key = f["path"].casefold()
        ancestors = [key[:index] for index, char in enumerate(key) if char == "/"]
        if key in parent_paths or any(parent in names for parent in ancestors):
            raise CatalogError("colliding_file_paths")
        names.add(key)
        parent_paths.update(ancestors)
        total += f["size"]
    if total != p["unpacked_bytes"] or p["entrypoint"].casefold() not in names:
        raise CatalogError("invalid_inventory_entrypoint")
    if p["id"] == "media-tools" and not {"tools/ffmpeg.exe", "tools/ffprobe.exe"}.issubset(names):
        raise CatalogError("missing_media_tools")


def _contains_user_data(path):
    # Product/user state is never distributed inside an update archive.
    if path.casefold() == "captcha/_internal/cv2/data/__init__.py":
        return False  # OpenCV package initialization code in the audited CAPTCHA sidecar.
    parts = [part.casefold() for part in path.split("/")]
    blocked = {
        "data", "userdata", "user-data", "user_data", "backups", "logs", "cache",
        "video_uploads", "profiles", "sessions", "__pycache__", "raw_stream", "reports",
        "videos", "machine", "trash", "customer_sessions", "missing_answers", "metrics",
        "dulieu", "model_cache", "whisper_models", "huggingface", "hub", ".staging", "packages",
    }
    forbidden_names = {
        ".env", "customer_memory.sqlite3", "shopee_customers.sqlite3", "operator_workspace.sqlite3",
        "live_products_current.json", "live_products_raw_current.json", "transcript.json",
        "fingerprint.npz", "timeline.json", "timeline.review.json", "draft.json", "job.json",
        "product_knowledge.json", "approved_live_profiles.json", "thong_tin_shop.json",
        "company_catalog_mapping.json", "local_product_overrides.json", "catalog_mapping.json",
        "shopee_live_mappings.json", "ai_learning.sqlite3", "kidtelauncher.exe", "release-manifest.json",
        "active.json", "previous.json", "pending.json", "update-journal.json", "catalog-sequence.json",
        ".component-store.lock",
    }
    forbidden_suffixes = {".pyc", ".sqlite3", ".log", ".jsonl", ".part", ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wav", ".safetensors", ".pt", ".pth", ".ckpt", ".gguf"}
    leaf = parts[-1]
    if (any(part in blocked for part in parts) or leaf in forbidden_names
            or leaf == ".env" or leaf.startswith(".env.")
            or any(leaf.endswith(suffix) for suffix in forbidden_suffixes)):
        return True
    suffix = Path(leaf).suffix.casefold()
    ctranslate2_models = any(parts[i:i + 2] == ["ctranslate2", "models"] for i in range(len(parts) - 1))
    if "models" in parts and not ctranslate2_models:
        return True
    if ctranslate2_models and suffix not in {".py", ".pyi", ".pyd", ".dll"}:
        return True
    if suffix == ".bin" and (
            {"model_cache", "whisper_models", "huggingface", "hub", "weights"}.intersection(parts)
            or leaf in {"model.bin", "pytorch_model.bin", "weights.bin"}
            or leaf.startswith(("pytorch_model-", "model-"))):
        return True
    return suffix == ".onnx" and path.casefold() not in {"_internal/faster_whisper/assets/silero_vad_v6.onnx", "faster_whisper/assets/silero_vad_v6.onnx"}


def _valid_range(expr):
    if not isinstance(expr, str) or not expr:
        return False
    return all(re.fullmatch(r"(>=|<=|>|<|==|=)?\s*\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", x.strip()) for x in expr.split(","))


def verify_catalog(raw, trusted_keys: dict[str, str], repository=REPOSITORY, channel="stable", minimum_sequence=0, now=None):
    envelope = _parse(raw)
    if not isinstance(envelope, dict) or set(envelope) != {"schema_version", "key_id", "signed", "signature"} or envelope["schema_version"] != 2:
        raise CatalogError("invalid_envelope")
    key_id = envelope["key_id"]
    if not isinstance(key_id, str) or key_id not in trusted_keys:
        raise CatalogError("unknown_signing_key")
    try:
        pub = base64.b64decode(trusted_keys[key_id], validate=True)
        sig = base64.b64decode(envelope["signature"], validate=True)
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, canonical_json(envelope["signed"]))
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise CatalogError("invalid_signature") from exc
    s = envelope["signed"]
    if not isinstance(s, dict) or set(s) != _CATALOG_FIELDS or s["schema_version"] != 2 or s["repository"] != repository or s["channel"] != channel or channel not in {"stable", "beta"}:
        raise CatalogError("invalid_catalog_metadata")
    if type(s["sequence"]) is not int or s["sequence"] < 1 or s["sequence"] < minimum_sequence:
        raise CatalogError("catalog_rollback")
    try:
        if not _UTC.fullmatch(s["issued_at"]) or not _UTC.fullmatch(s["expires_at"]):
            raise ValueError
        issued = datetime.fromisoformat(s["issued_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(s["expires_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CatalogError("invalid_catalog_time") from exc
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if issued > current or expires <= current or expires <= issued:
        raise CatalogError("catalog_expired_or_not_yet_valid")
    if not isinstance(s["components"], list) or not s["components"] or len(s["components"]) > 100:
        raise CatalogError("invalid_components")
    ids = set()
    for p in s["components"]:
        _validate_package(p, repository)
        if p["id"] in ids:
            raise CatalogError("duplicate_component")
        ids.add(p["id"])
    return s


def resolve_components(catalog, requested: dict[str, str | None], installed=None, core_api="1.0.0"):
    """Resolve requested updates and validate the full installed/resulting set.

    The return value contains only packages that should be staged. Compatible
    installed dependencies remain in place. An installed component is upgraded
    only when a requested change would otherwise leave the combination invalid.
    """
    installed = installed or {}
    allowed = set(COMPONENT_POLICIES)
    if not isinstance(requested, dict) or any(k not in allowed for k in requested):
        raise CatalogError("unsupported_component")
    if not isinstance(installed, dict) or any(k not in allowed for k in installed):
        raise CatalogError("invalid_installed_components")
    installed_records = {}
    for component, pkg in installed.items():
        try:
            _validate_package(pkg, REPOSITORY)
        except CatalogError as exc:
            raise CatalogError("invalid_installed_component") from exc
        if pkg["id"] != component:
            raise CatalogError("invalid_installed_component")
        installed_records[component] = pkg
    available = {}
    for pkg in catalog.get("components", []):
        if isinstance(pkg, dict) and pkg.get("id") in allowed:
            available.setdefault(pkg["id"], []).append(pkg)
    for versions in available.values():
        versions.sort(key=lambda p: _version_key(p["version"]), reverse=True)
    selected = {}
    visiting = set()

    def add(component, wanted=None, constraint=None):
        if component in visiting:
            raise CatalogError("dependency_cycle")
        if component in selected:
            pkg = selected[component]
            if ((wanted is not None and pkg["version"] != wanted)
                    or (constraint is not None and not _range_satisfies(pkg["version"], constraint))):
                raise CatalogError("dependency_conflict")
            return
        candidates = available.get(component, [])
        if wanted is not None:
            candidates = [p for p in candidates if p["version"] == wanted]
        if constraint is not None:
            candidates = [p for p in candidates if _range_satisfies(p["version"], constraint)]
        if not candidates:
            raise CatalogError("component_version_unavailable")
        pkg = candidates[0]
        if not _range_satisfies(core_api, pkg["core_api"]):
            raise CatalogError("core_api_incompatible")
        selected[component] = pkg
        visiting.add(component)
        try:
            for dep, expr in pkg["dependencies"].items():
                if dep in visiting:
                    raise CatalogError("dependency_cycle")
                existing = selected.get(dep) or installed_records.get(dep)
                if existing and _range_satisfies(existing["version"], expr):
                    continue
                add(dep, constraint=expr)
        finally:
            visiting.remove(component)

    for component, version in requested.items():
        add(component, wanted=version)

    def merged():
        result = dict(installed_records)
        result.update(selected)
        return result

    def unsatisfied(record, combined):
        for dep, expr in record["dependencies"].items():
            target = combined.get(dep)
            if target is None or not _range_satisfies(target["version"], expr):
                return dep, expr
        return None

    # Validate dependencies from retained packages too. If a requested update
    # invalidates one, stage a catalog version of that retained parent only if
    # its new dependency closure makes the combination valid.
    for _ in range(5):
        combined = merged()
        changed = False
        for component, record in list(combined.items()):
            issue = unsatisfied(record, combined)
            if issue is None:
                continue
            dep, expr = issue
            if dep not in combined:
                add(dep, constraint=expr)
                changed = True
                break
            if component in selected:
                add(dep, constraint=expr)
                changed = True
                break
            candidates = [p for p in available.get(component, []) if p["version"] != record["version"]]
            replacement = None
            for candidate in candidates:
                trial_selected = dict(selected)
                selected.clear()
                selected.update(trial_selected)
                selected[component] = candidate
                try:
                    for child, child_expr in candidate["dependencies"].items():
                        child_record = selected.get(child) or installed_records.get(child)
                        if not child_record or not _range_satisfies(child_record["version"], child_expr):
                            add(child, constraint=child_expr)
                    trial_combined = merged()
                    if all(unsatisfied(item, trial_combined) is None for item in trial_combined.values()):
                        replacement = candidate
                        break
                except CatalogError:
                    pass
                selected.clear()
                selected.update(trial_selected)
            if replacement is None:
                raise CatalogError("dependency_conflict")
            changed = True
            break
        if not changed:
            return {key: record for key, record in selected.items() if installed_records.get(key) != record}
    raise CatalogError("dependency_conflict")
