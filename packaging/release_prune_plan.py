"""KEEP/DELETE decision logic for pruning old GitHub releases of Kid TE.

Pure functions (no network). The PowerShell driver packaging/prune_releases.ps1
downloads the release list and the signed catalogs, then calls this module.

Rules:
  KEEP    tag referenced by any catalog component url (.../releases/download/<tag>/...)
  KEEP    channel feed tag (starts with "updates-")
  KEEP    release holding the newest installer (asset named *Setup*.exe)
  KEEP    newest N core versions (tags v<X.Y.Z>-p<N>..., any series), N = keep_core
  DELETE  everything else
  KEEP    releases without a tag (never touched: not understood, so not pruned)

Safety: every existing feed release (tag updates-*) must come with its catalog via --feed TAG=PATH,
otherwise the script exits 3 and nothing is decided/deleted.

Run as a script:
  python -I release_prune_plan.py --releases R.json [--catalog C.json] --feed updates-beta-p4=P4.json
         [--keep-core 2] [--plan-out P.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

CATALOG_URL_RE = re.compile(r"/releases/download/([^/\s\"'\\]+)/")
CORE_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)-p[34](?:[.\-_].*)?$")
INSTALLER_RE = re.compile(r"(?i)setup.*\.exe$")
FEED_PREFIX = "updates-"


class PlanError(Exception):
    """Dữ liệu không đủ tin cậy để quyết định xoá: DỪNG, không xoá gì."""


def check_feeds(releases: Iterable[Dict[str, Any]], feed_catalogs: Dict[str, Any]) -> None:
    """Mỗi release feed (tag updates-*) đang tồn tại PHẢI có catalog đã tải và đọc được.

    Thiếu một catalog = không biết tag nào còn được tham chiếu = có thể xoá nhầm tag runtime/app.
    """
    for release in releases:
        tag = release.get("tag_name") or ""
        if not tag.startswith(FEED_PREFIX):
            continue
        catalog = feed_catalogs.get(tag)
        if not isinstance(catalog, dict) or not isinstance(((catalog.get("signed") or catalog).get("components")), list):
            raise PlanError("feed %s: khong co catalog doc duoc - dung, khong xoa gi" % tag)
        if not referenced_tags(catalog):
            raise PlanError("feed %s: catalog khong tham chieu tag nao - dung, khong xoa gi" % tag)


def referenced_tags(catalog: Any) -> Set[str]:
    """Collect every release tag referenced by a catalog (walks the whole JSON)."""
    tags: Set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and "/releases/download/" in node:
            for match in CATALOG_URL_RE.finditer(node):
                tags.add(match.group(1))

    walk(catalog)
    return tags


def core_version(tag: str) -> Optional[Tuple[int, int, int]]:
    match = CORE_TAG_RE.match(tag or "")
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def has_installer(release: Dict[str, Any]) -> bool:
    for asset in release.get("assets") or []:
        if INSTALLER_RE.search(str(asset.get("name", ""))):
            return True
    return False


def newest_installer_release_id(releases: Sequence[Dict[str, Any]]) -> Optional[Any]:
    """Release id of the newest installer: highest core version, then latest published_at."""
    best_key = None
    best_id = None
    for release in releases:
        if not has_installer(release):
            continue
        version = core_version(release.get("tag_name") or "") or (0, 0, 0)
        key = (version, str(release.get("published_at") or release.get("created_at") or ""))
        if best_key is None or key > best_key:
            best_key = key
            best_id = release.get("id")
    return best_id


def build_plan(
    releases: Iterable[Dict[str, Any]],
    catalogs: Iterable[Any],
    keep_core: int = 2,
) -> List[Dict[str, Any]]:
    """Return one plan row per release: tag, id, name, decision (KEEP/DELETE), reason."""
    if keep_core < 0:
        raise ValueError("keep_core must be >= 0")
    release_list = list(releases)

    refs: Set[str] = set()
    for catalog in catalogs:
        refs |= referenced_tags(catalog)

    core_tags = sorted(
        {r.get("tag_name") for r in release_list if core_version(r.get("tag_name") or "")},
        key=lambda t: core_version(t),
        reverse=True,
    )
    kept_core = set(core_tags[:keep_core])
    installer_id = newest_installer_release_id(release_list)

    plan: List[Dict[str, Any]] = []
    for release in release_list:
        tag = release.get("tag_name") or ""
        row = {
            "tag": tag,
            "id": release.get("id"),
            "name": release.get("name") or tag,
            "published_at": release.get("published_at") or release.get("created_at") or "",
        }
        if not tag:
            row.update(decision="KEEP", reason="no tag (never pruned)")
        elif tag in refs:
            row.update(decision="KEEP", reason="referenced by catalog")
        elif tag.startswith(FEED_PREFIX):
            row.update(decision="KEEP", reason="channel feed")
        elif release.get("id") is not None and release.get("id") == installer_id:
            row.update(decision="KEEP", reason="newest installer (Setup exe)")
        elif tag in kept_core:
            row.update(decision="KEEP", reason="newest %d core versions" % keep_core)
        else:
            row.update(decision="DELETE", reason="old, not referenced by any catalog")
        plan.append(row)

    plan.sort(key=lambda r: (r["decision"], r["tag"]))
    return plan


def _print_table(plan: List[Dict[str, Any]]) -> None:
    print("%-8s %-24s %-10s %s" % ("DECISION", "TAG", "RELEASE_ID", "REASON"))
    for row in plan:
        print("%-8s %-24s %-10s %s" % (row["decision"], row["tag"] or "-", row["id"], row["reason"]))
    keep = sum(1 for r in plan if r["decision"] == "KEEP")
    delete = sum(1 for r in plan if r["decision"] == "DELETE")
    print("SUMMARY: KEEP=%d DELETE=%d total=%d" % (keep, delete, len(plan)))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Plan which old Kid TE releases to delete.")
    parser.add_argument("--releases", required=True, help="JSON list of GitHub release objects")
    parser.add_argument("--catalog", action="append", default=[], help="signed catalog JSON (repeatable)")
    parser.add_argument("--feed", action="append", default=[], metavar="TAG=PATH",
                        help="catalog của feed updates-* đã tải (repeatable); feed nào tồn tại mà thiếu -> dừng")
    parser.add_argument("--keep-core", type=int, default=2)
    parser.add_argument("--plan-out", default="", help="write plan JSON here")
    args = parser.parse_args(argv)

    if not args.catalog and not args.feed:
        print("LOI: can it nhat mot catalog", file=sys.stderr)
        return 2
    with open(args.releases, "r", encoding="utf-8") as handle:
        releases = json.load(handle)
    catalogs = []
    for path in args.catalog:
        with open(path, "r", encoding="utf-8") as handle:
            catalogs.append(json.load(handle))

    feed_catalogs = {}
    for item in args.feed:
        tag, sep, path = item.partition("=")
        if not sep or not tag or not path:
            print("LOI: --feed can dang TAG=PATH", file=sys.stderr)
            return 2
        with open(path, "r", encoding="utf-8") as handle:
            feed_catalogs[tag] = json.load(handle)
    try:
        check_feeds(releases, feed_catalogs)
    except PlanError as exc:
        print("LOI: %s" % exc, file=sys.stderr)
        return 3
    plan = build_plan(releases, [*catalogs, *feed_catalogs.values()], keep_core=args.keep_core)
    _print_table(plan)
    if args.plan_out:
        with open(args.plan_out, "w", encoding="utf-8") as handle:
            json.dump(plan, handle, ensure_ascii=True, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
