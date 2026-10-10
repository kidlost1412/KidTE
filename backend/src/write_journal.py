"""Crash journal for file + DB writes (E04).

Protocol: begin (intent) -> tmp files written -> DB commit -> os.replace -> done.
Recovery decides per pending op: DB revision moved past the expected one means
the DB committed, so roll forward (replace tmp -> target); otherwise roll back
(delete tmp). Recovery is idempotent.
"""
import json
import logging
import os
import threading
import uuid
from pathlib import Path

_LOCK = threading.Lock()
_LOG = logging.getLogger("write_journal")


class WriteJournal:
    def __init__(self, path):
        self.path = Path(path)

    def _append(self, record):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with _LOCK, open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def begin(self, kind, targets, db_key=None, db_rev_expected=0):
        """targets: list of (target_path, tmp_path[, prev_path]). Returns op_id.

        prev_path (optional) is a copy of the old target taken before an early
        replace (file-first writers such as merge/rollback). When the DB did
        not commit, recovery restores it over the target."""
        op_id = uuid.uuid4().hex
        entries = []
        for item in targets:
            entry = {"target": str(item[0]), "tmp": str(item[1])}
            if len(item) > 2 and item[2] is not None:
                entry["prev"] = str(item[2])
            if len(item) > 3 and item[3]:
                # target did not exist before: rollback removes the created file
                entry["created"] = True
            entries.append(entry)
        self._append({
            "op_id": op_id, "event": "intent", "kind": kind,
            "target_paths": entries,
            "db_key": db_key, "db_rev_expected": db_rev_expected,
        })
        return op_id

    def begin_moves(self, kind, moves, db_key=None, db_rev_expected=0):
        """moves: list of (src, dst) file/directory renames done after this call.
        Recovery rolls forward (finish src->dst) if the DB committed, otherwise
        moves dst back to src. Never deletes; if both sides exist it keeps both."""
        op_id = uuid.uuid4().hex
        self._append({
            "op_id": op_id, "event": "intent", "kind": kind, "target_paths": [],
            "moves": [{"src": str(a), "dst": str(b)} for a, b in moves],
            "db_key": db_key, "db_rev_expected": db_rev_expected,
        })
        return op_id

    def mark_db_committed(self, op_id):
        self._append({"op_id": op_id, "event": "db_committed"})

    def done(self, op_id):
        self._append({"op_id": op_id, "event": "done"})

    def pending(self):
        ops = {}
        if not self.path.is_file():
            return []
        for raw in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue  # torn last line from a crash
            op = ops.setdefault(rec.get("op_id"), {})
            if rec.get("event") == "intent":
                op.update(rec)
            elif rec.get("event") == "db_committed":
                op["db_committed"] = True
            elif rec.get("event") == "done":
                op["finished"] = True
        return [op for op in ops.values() if op.get("event") == "intent" and not op.get("finished")]

    def recover(self, current_revision):
        """current_revision(db_key) -> int. Returns list of (op_id, action)."""
        results = []
        for op in self.pending():
            committed = op.get("db_committed") or (
                op.get("db_key") is not None
                and current_revision(op["db_key"]) > int(op.get("db_rev_expected") or 0))
            for item in op.get("target_paths", []):
                tmp, target = Path(item["tmp"]), Path(item["target"])
                prev = Path(item["prev"]) if item.get("prev") else None
                if committed:
                    if tmp.exists():
                        os.replace(tmp, target)
                    if prev is not None:
                        prev.unlink(missing_ok=True)
                else:
                    tmp.unlink(missing_ok=True)
                    if prev is not None and prev.exists():
                        os.replace(prev, target)
                    elif item.get("created") and prev is None:
                        target.unlink(missing_ok=True)
            ambiguous = False
            for move in op.get("moves", []):
                src, dst = Path(move["src"]), Path(move["dst"])
                first, second = (src, dst) if committed else (dst, src)
                # forward: src -> dst ; backward: dst -> src
                if first.exists() and not second.exists():
                    second.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(first, second)
                elif first.exists() and second.exists():
                    ambiguous = True
                    _LOG.warning("write_journal: both %s and %s exist; keeping both (op %s)",
                                 src, dst, op["op_id"])
            self.done(op["op_id"])
            action = "rolled_forward" if committed else "rolled_back"
            results.append((op["op_id"], "kept_both" if ambiguous else action))
        return results
