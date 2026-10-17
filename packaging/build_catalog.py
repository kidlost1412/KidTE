"""Derive the next update-catalog payload from the currently published catalog.

Only the components named with --component change; every other entry is copied
verbatim (same URL/hash). Sequence is published+1, times are fresh. The result
is an UNSIGNED payload; sign it with update_release.py sign.

Split release (runtime / app / extensions) additionally uses
  --add-component ID ZIP URL VERSION SOURCE_COMMIT   introduce a component (first split catalog)
  --remove-component ID                              drop one (the legacy monolithic ``core``)
A component whose ZIP has exactly the published file inventory is refused
(``component_content_unchanged``): leave it out and its published record, URL and
hash stay as they are, which is what makes app-only updates small.

Example:
  python packaging/build_catalog.py --published old-catalog.json --out payload.json
      --component core dist/core.zip https://github.com/kidlost1412/KidTE/releases/download/v1.14.258-p3/core.zip 1.14.258 <commit>
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
from update_catalog import CORE_DATA_SCHEMA, COMPONENT_POLICIES, CatalogError, _parse, verify_catalog  # noqa: E402


def _utc(moment):
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def zip_facts(zip_path):
    """Archive hash/bytes plus per-file inventory read back from the ZIP itself."""
    zip_path = Path(zip_path)
    digest = hashlib.sha256()
    with zip_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    files = []
    with zipfile.ZipFile(zip_path) as archive:
        for info in sorted(archive.infolist(), key=lambda i: i.filename.casefold()):
            if info.is_dir():
                continue
            file_digest, size = hashlib.sha256(), 0
            with archive.open(info) as src:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    file_digest.update(chunk)
                    size += len(chunk)
            files.append({"path": info.filename, "size": size, "sha256": file_digest.hexdigest()})
    return {"archive_sha256": digest.hexdigest(), "archive_bytes": zip_path.stat().st_size,
            "unpacked_bytes": sum(f["size"] for f in files), "files": files}


def new_component_record(component_id, change, *, runtime_api=1):
    """Full signed record for a component that is not in the published catalog yet."""
    if component_id not in ("runtime", "app", "extensions"):
        raise ValueError(f"cannot_add_component:{component_id}")
    kind, entrypoint = COMPONENT_POLICIES[component_id]
    if component_id == "runtime":
        dependencies, compatibility = {}, {"runtime_api": runtime_api}
    elif component_id == "app":
        dependencies = {"runtime": f">={runtime_api}.0.0,<{runtime_api + 1}.0.0"}
        compatibility = {"runtime_api": runtime_api, "data_schema": CORE_DATA_SCHEMA}
    else:
        dependencies, compatibility = {}, {"extension_api": 1}
    record = {"id": component_id, "kind": kind, "version": change["version"], "platform": "windows",
              "architecture": "x86_64", "core_api": ">=1.0.0,<2.0.0", "dependencies": dependencies,
              "url": change["url"], "entrypoint": entrypoint, "restart_required": True,
              "migration": "none", "source_commit": change.get("source_commit") or "",
              "compatibility": compatibility}
    record.update(zip_facts(change["zip"]))
    return record


def build_payload(published, changes, *, now=None, days=90, additions=None, removals=(), runtime_api=1):
    """published: signed payload dict (or envelope). changes: {id: {zip, url, version, source_commit?}}."""
    signed = published.get("signed", published)
    now = now or datetime.now(timezone.utc)
    payload = copy.deepcopy(signed)
    additions = additions or {}
    known = {c["id"] for c in payload["components"]}
    unknown = set(changes) - known
    if unknown:
        raise ValueError(f"unknown_component:{','.join(sorted(unknown))}")
    if set(additions) & known:
        raise ValueError(f"component_already_published:{','.join(sorted(set(additions) & known))}")
    if set(removals) - known:
        raise ValueError(f"unknown_component:{','.join(sorted(set(removals) - known))}")
    if not changes and not additions and not removals:
        raise ValueError("no_component_changes")
    payload["components"] = [c for c in payload["components"] if c["id"] not in set(removals)]
    for component in payload["components"]:
        change = changes.get(component["id"])
        if change is None:
            continue
        if change["url"] == component["url"] or change["version"] == component["version"]:
            raise ValueError(f"component_not_changed:{component['id']}")
        facts = zip_facts(change["zip"])
        by_path = lambda rows: sorted(rows, key=lambda row: row["path"])  # noqa: E731
        if by_path(facts["files"]) == by_path(component["files"]):
            raise ValueError(f"component_content_unchanged:{component['id']}")
        component.update(facts)
        component["version"] = change["version"]
        component["url"] = change["url"]
        if change.get("source_commit"):
            component["source_commit"] = change["source_commit"]
    for component_id, change in additions.items():
        payload["components"].append(new_component_record(component_id, change, runtime_api=runtime_api))
    payload["sequence"] = int(signed["sequence"]) + 1
    payload["issued_at"] = _utc(now)
    payload["expires_at"] = _utc(now + timedelta(days=days))
    return payload


def renew_payload(published, *, now=None, days=90):
    """Same components as the published catalog, sequence+1, fresh issued_at/expires_at.

    No package URL/hash changes, so no new build is needed: this only extends the
    validity window so clients keep accepting the feed. Unsigned; sign it with
    ``update_release.py sign``.
    """
    signed = published.get("signed", published)
    now = now or datetime.now(timezone.utc)
    payload = copy.deepcopy(signed)
    payload["sequence"] = int(signed["sequence"]) + 1
    payload["issued_at"] = _utc(now)
    payload["expires_at"] = _utc(now + timedelta(days=days))
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--published", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--channel", default="beta")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--component", nargs="+", action="append", default=[],
                        metavar="ID ZIP URL VERSION [SOURCE_COMMIT]")
    parser.add_argument("--add-component", nargs="+", action="append", default=[],
                        metavar="ID ZIP URL VERSION SOURCE_COMMIT")
    parser.add_argument("--remove-component", action="append", default=[], metavar="ID")
    parser.add_argument("--runtime-api", type=int, default=1)
    parser.add_argument("--no-verify-published", action="store_true")
    args = parser.parse_args(argv)
    changes = {}
    for item in args.component:
        if len(item) not in (4, 5):
            parser.error("--component needs ID ZIP URL VERSION [SOURCE_COMMIT]")
        changes[item[0]] = {"zip": item[1], "url": item[2], "version": item[3],
                            "source_commit": item[4] if len(item) == 5 else ""}
    additions = {}
    for item in args.add_component:
        if len(item) != 5:
            parser.error("--add-component needs ID ZIP URL VERSION SOURCE_COMMIT")
        additions[item[0]] = {"zip": item[1], "url": item[2], "version": item[3], "source_commit": item[4]}
    raw = args.published.read_bytes()
    if not args.no_verify_published:
        from update_trust import TRUSTED_KEYS
        try:
            verify_catalog(raw, TRUSTED_KEYS, channel=args.channel)
        except CatalogError as exc:
            # A genuine but expired feed still carries a usable sequence; signature/metadata failures are fatal.
            if str(exc) != "catalog_expired_or_not_yet_valid":
                raise
    payload = build_payload(_parse(raw), changes, days=args.days, additions=additions,
                            removals=tuple(args.remove_component), runtime_api=args.runtime_api)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"sequence": payload["sequence"], "changed": sorted(changes),
                      "added": sorted(additions), "removed": sorted(args.remove_component)}))


if __name__ == "__main__":
    main()
