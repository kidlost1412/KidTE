"""Schema audit: init every SQLite-owning store in a tmp dir, dump the schema, compare to baseline.

Usage: python tools/schema_audit.py [--write-baseline] [--baseline PATH]
Exit code 1 when the live schema differs from docs/schema-baseline.json.
"""
from __future__ import annotations

import argparse
import gc
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src"))
DEFAULT_BASELINE = BACKEND.parent / "docs" / "schema-baseline.json"
# Stores that own no SQLite file (checked by grep sqlite3.connect): documented, not dumped.
NON_SQLITE = ["metrics_store", "dose_rules"]


def dump_db(path):
    """tables -> {columns: [[name, type, notnull, default, pk]], indexes: [[name, unique, [cols]]]}"""
    out = {}
    with closing(sqlite3.connect(str(path))) as db:
        names = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for name in names:
            cols = [[r[1], r[2], r[3], r[4], r[5]] for r in db.execute(f'PRAGMA table_info("{name}")')]
            idx = []
            for r in db.execute(f'PRAGMA index_list("{name}")'):
                if r[3] == "pk" or r[1].startswith("sqlite_autoindex"):
                    continue
                idx.append([r[1], r[2], [c[2] for c in db.execute(f'PRAGMA index_info("{r[1]}")')]])
            out[name] = {"columns": cols, "indexes": sorted(idx)}
    return out


def _stores(tmp: Path):
    """name -> (db path, factory, closer)."""
    from customer_memory import CustomerMemory
    from delivery_store import DeliveryStore
    from comment_queue import CommentQueue
    from workspace import WorkspaceStore
    from learning import LearningStore
    from privacy import PrivacyService
    from captcha_upload import CaptchaUploads
    from obs_source import MediaBindingStore
    from maintenance import JobStore

    def close(obj):
        gc.collect()  # MediaBindingStore leaks `with sqlite3.connect()` handles until GC
        for attr in ("close",):
            if hasattr(obj, attr):
                obj.close()
                return
        for attr in ("_db", "db"):
            db = getattr(obj, attr, None)
            if hasattr(db, "close"):
                db.close()
                return

    def workspace():
        engine = SimpleNamespace(knowledge=SimpleNamespace(overrides_path=tmp / "ws" / "ov.json"))
        return WorkspaceStore(engine, root=tmp / "ws")

    return {
        "customer_memory": (tmp / "cm" / "memory.sqlite3",
                            lambda: CustomerMemory(tmp / "cm" / "memory.sqlite3", tmp / "cm" / "j")),
        "delivery_store": (tmp / "ds" / "delivery.sqlite3", lambda: DeliveryStore(tmp / "ds" / "delivery.sqlite3")),
        "comment_queue": (tmp / "cq" / "queue.sqlite3", lambda: CommentQueue(tmp / "cq" / "queue.sqlite3")),
        "workspace": (tmp / "ws" / "data" / "operator_workspace.sqlite3", workspace),
        "learning": (tmp / "lr" / "learning.sqlite3", lambda: LearningStore(tmp / "lr" / "learning.sqlite3")),
        "privacy": (tmp / "pv" / "privacy.sqlite3",
                    lambda: PrivacyService(tmp / "pv" / "privacy.sqlite3", tmp / "pv" / "key.bin")),
        "captcha_upload": (tmp / "cu" / "captcha-uploads.sqlite3", lambda: CaptchaUploads(tmp / "cu")),
        "media_binding": (tmp / "mb" / "media.sqlite3", lambda: MediaBindingStore(tmp / "mb" / "media.sqlite3")),
        "maintenance_jobs": (tmp / "mj" / "jobs.sqlite3", lambda: JobStore(tmp / "mj" / "jobs.sqlite3")),
    }, close


def init_all(tmp: Path, times=1):
    stores, close = _stores(Path(tmp))
    result = {}
    for name, (path, factory) in stores.items():
        path.parent.mkdir(parents=True, exist_ok=True)  # CommentQueue does not create its parent
        for _ in range(times):
            obj = factory()
            close(obj)
        result[name] = dump_db(path)
    return result


def collect(times=1):
    with tempfile.TemporaryDirectory(prefix="schema-audit-", ignore_cleanup_errors=True) as tmp:
        return {"stores": init_all(Path(tmp), times), "non_sqlite": NON_SQLITE}


def diff(baseline, current):
    problems = []
    for store in sorted(set(baseline["stores"]) | set(current["stores"])):
        old, new = baseline["stores"].get(store), current["stores"].get(store)
        if old is None or new is None:
            problems.append(f"{store}: store {'added' if old is None else 'removed'}")
            continue
        for table in sorted(set(old) | set(new)):
            if table not in new:
                problems.append(f"{store}.{table}: table removed")
            elif table not in old:
                problems.append(f"{store}.{table}: table added")
            elif old[table] != new[table]:
                problems.append(f"{store}.{table}: columns/indexes changed")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    args = parser.parse_args(argv)
    current = collect()
    path = Path(args.baseline)
    if args.write_baseline:
        path.write_text(json.dumps(current, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {path}")
        return 0
    problems = diff(json.loads(path.read_text(encoding="utf-8")), current)
    for line in problems:
        print(line)
    print("schema OK" if not problems else f"{len(problems)} schema difference(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
