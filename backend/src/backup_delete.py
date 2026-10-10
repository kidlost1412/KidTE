"""Delete only validated IDs in the managed v1 store, never arbitrary paths."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import uuid

_LOCK = threading.RLock()


def _linked(path):
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)


def _safe_children(target, root):
    pending = [target]
    while pending:
        folder = pending.pop()
        if _linked(folder) or not folder.resolve().is_relative_to(root):
            raise ValueError('backup_link_unsafe')
        for path in folder.iterdir():
            # Check before descending, including Windows junctions that
            # Path.is_symlink alone does not identify.
            if _linked(path) or not path.resolve().is_relative_to(root):
                raise ValueError('backup_link_unsafe')
            if path.is_dir():
                pending.append(path)
            else:
                yield path


def preview(root, ids):
    root = Path(root).resolve()
    if not isinstance(ids, list) or not ids or len(ids) > 1000 or any(
            not isinstance(item, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', item) for item in ids):
        raise ValueError('backup_ids_invalid')
    rows = []
    for backup_id in dict.fromkeys(ids):
        target = root / backup_id
        if not target.is_dir() or _linked(target) or target.resolve().parent != root:
            raise ValueError('backup_not_found')
        if _linked(target / 'manifest.json'):
            raise ValueError('backup_link_unsafe')
        manifest = json.loads((target / 'manifest.json').read_text(encoding='utf-8'))
        # CAS reference collection is owned by the future v2 implementation.
        if manifest.get('schema_version', 1) != 1:
            raise ValueError('backup_delete_schema_unsupported')
        files = []
        for path in sorted(_safe_children(target, target.resolve())):
            if path.is_file():
                stat = path.stat()
                files.append((path.relative_to(target).as_posix(), stat.st_size, stat.st_mtime_ns))
        rows.append({'id': backup_id, 'bytes': sum(row[1] for row in files), 'files': files})
    token = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    return {'ok': True, 'ids': [row['id'] for row in rows],
            'reclaimable_bytes': sum(row['bytes'] for row in rows), 'preview_token': token}


def execute(root, ids, preview_token, progress=lambda _: None):
    root = Path(root).resolve()
    with _LOCK:
        checked = preview(root, ids)
        if not isinstance(preview_token, str) or checked['preview_token'] != preview_token:
            raise ValueError('backup_preview_changed')
        # Publish deletion intent before mutation. Hidden targets cannot be
        # selected as backups after a crash. A journal retains recovery paths.
        job_id = uuid.uuid4().hex
        journal = root / ('.delete-' + job_id + '.json')
        moves = [(root / item, root / ('.deleting-' + job_id + '-' + item)) for item in checked['ids']]
        with journal.open('x', encoding='utf-8') as handle:
            json.dump({'state': 'deleting', 'moves': [[a.name, b.name] for a, b in moves],
                       'tokens': {item: preview(root, [item])['preview_token'] for item in checked['ids']}}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        for index, (source, target) in enumerate(moves):
            os.replace(source, target)
            progress((index + 1) / (2 * len(moves)))
        for index, (_, target) in enumerate(moves):
            shutil.rmtree(target)
            progress(.5 + (index + 1) / (2 * len(moves)))
        journal.unlink()
        return {'ok': True, 'deleted_ids': checked['ids'], 'reclaimed_bytes': checked['reclaimable_bytes']}


def recover(root, progress=lambda _: None):
    """Finish only journaled deletion intents; refuse changed original backups."""
    root = Path(root).resolve()
    recovered = []
    with _LOCK:
        journals = sorted(root.glob('.delete-*.json'))
        for index, journal in enumerate(journals):
            match = re.fullmatch(r'\.delete-([a-f0-9]{32})\.json', journal.name)
            if not match or _linked(journal):
                raise ValueError('backup_delete_journal_unsafe')
            payload = json.loads(journal.read_text(encoding='utf-8'))
            moves = payload.get('moves')
            tokens = payload.get('tokens')
            if payload.get('state') != 'deleting' or not isinstance(moves, list) or not isinstance(tokens, dict):
                raise ValueError('backup_delete_journal_unsafe')
            validated = []
            for move in moves:
                if (not isinstance(move, list) or len(move) != 2 or not all(isinstance(p, str) for p in move)
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', move[0])
                    or move[1] != '.deleting-' + match[1] + '-' + move[0]):
                    raise ValueError('backup_delete_journal_unsafe')
                source, target = (root / item for item in move)
                if source.exists():
                    if target.exists() or preview(root, [move[0]])['preview_token'] != tokens.get(move[0]):
                        raise ValueError('backup_preview_changed')
                if target.exists():
                    list(_safe_children(target, root))
                validated.append((source, target))
            for source, target in validated:
                if source.exists():
                    os.replace(source, target)
                if target.exists():
                    shutil.rmtree(target)
                recovered.append(source.name)
            journal.unlink()
            progress((index + 1) / len(journals))
    return {'ok': True, 'recovered_deleted_ids': recovered}
