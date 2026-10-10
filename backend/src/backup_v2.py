"""Content-addressed backup core. Application coverage is supplied explicitly.

This module stages/validates snapshots and owns shared-object collection. It
does not replace live database files or infer application source coverage.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import uuid
import hashlib

from backup import (BackupError, _safe_relative, _reject_links, _copy_stable_file,
                    _backup_sqlite, _sha256, _validate_json, _validate_jsonl, _validate_sqlite, _validate_text,
                    _strict_json_loads)
import privacy
from backup_crypto import encrypt_secrets, decrypt_secrets

_HASH = re.compile(r'[a-f0-9]{64}')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}')
_KINDS = {'file', 'sqlite', 'json', 'jsonl', 'text', 'secrets'}


class BackupV2:
    @staticmethod
    def _stat_key(path):
        stat = path.stat()
        return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]

    def _source_index(self):
        path = self.root / 'source-index.sqlite3'
        _reject_links(path, self.root)
        db = sqlite3.connect(path, timeout=10)
        db.execute('CREATE TABLE IF NOT EXISTS sources (path TEXT PRIMARY KEY, source_stat TEXT, digest TEXT, object_stat TEXT, updated REAL)')
        return db

    def _cached_source(self, source, relative, kind):
        if kind != 'file' or source.stat().st_size < 1024 * 1024:
            return None
        db = self._source_index()
        try:
            row = db.execute('SELECT source_stat,digest,object_stat FROM sources WHERE path=?', (str(source),)).fetchone()
        finally:
            db.close()
        if row is None or json.loads(row[0]) != self._stat_key(source):
            return None
        obj = self._object_path(row[1])
        if not obj.is_file() or json.loads(row[2]) != self._stat_key(obj):
            return None
        return {'path':relative, 'kind':kind, 'bytes':source.stat().st_size, 'sha256':row[1]}

    def _remember_source(self, source, before, record):
        if record['kind'] != 'file' or record['bytes'] < 1024 * 1024 or before != self._stat_key(source):
            return
        obj = self._object_path(record['sha256'])
        db = self._source_index()
        try:
            with db:
                db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?,?,julianday("now"))',
                    (str(source), json.dumps(before), record['sha256'], json.dumps(self._stat_key(obj))))
                db.execute('DELETE FROM sources WHERE path IN (SELECT path FROM sources ORDER BY updated DESC LIMIT -1 OFFSET 20000)')
        finally:
            db.close()

    def __init__(self, root):
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        _reject_links(self.root, self.root)
        self.objects = self.root / 'objects'
        self.manifests = self.root / 'manifests'
        for folder in (self.objects, self.manifests):
            folder.mkdir(exist_ok=True)
            _reject_links(folder, self.root)

    @contextmanager
    def _write(self, *, recovery=False):
        lock_path = self.root / 'operations.sqlite3'
        _reject_links(lock_path, self.root)
        db = sqlite3.connect(lock_path, timeout=.2)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS lock_version (version INTEGER)')
            db.execute('BEGIN IMMEDIATE')
            if not recovery and any(self.root.glob('.delete-*.json')):
                raise BackupError('backup_recovery_required')
            yield
            db.commit()
        except sqlite3.OperationalError as exc:
            db.rollback()
            if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
                raise BackupError('backup_busy') from None
            raise
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _manifest_path(self, backup_id):
        if not isinstance(backup_id, str) or not _ID.fullmatch(backup_id):
            raise BackupError('backup_id_invalid')
        path = self.manifests / (backup_id + '.json')
        _reject_links(path, self.root)
        return path

    def _object_path(self, digest):
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise BackupError('backup_object_id_invalid')
        path = self.objects / digest[:2] / digest
        _reject_links(path, self.root)
        return path

    def _validate(self, path, kind):
        {'sqlite': _validate_sqlite, 'json': _validate_json, 'jsonl': _validate_jsonl,
         'text': _validate_text}.get(kind, lambda _: None)(path)

    def create(self, sources, *, metadata, full=False, secrets=None, password=None,
               missing=None, exclusions=None, privacy_db=None, tombstones=None):
        if not isinstance(sources, dict) or not isinstance(metadata, dict):
            raise BackupError('backup_sources_invalid')
        required = {'app_version', 'schema_version', 'solver_hash', 'coverage'}
        if (not required.issubset(metadata) or not isinstance(metadata['coverage'], list) or
            any(not isinstance(item, str) or not item for item in metadata['coverage'])):
            raise BackupError('backup_metadata_incomplete')
        if full and (not metadata['coverage'] or not sources):
            raise BackupError('backup_metadata_incomplete')
        backup_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:12]
        stage = self.root / ('.staging-' + uuid.uuid4().hex)
        with self._write():
            stage.mkdir()
            try:
                records = []
                names = set()
                for relative, spec in sorted(sources.items()):
                    safe = _safe_relative(relative)
                    if relative.casefold() in names or relative == 'secrets.enc' or not isinstance(spec, dict):
                        raise BackupError('backup_source_duplicate')
                    names.add(relative.casefold())
                    kind = spec.get('kind')
                    if kind not in _KINDS - {'secrets'}:
                        raise BackupError('backup_source_kind_invalid')
                    source = Path(spec.get('source', ''))
                    source = source.absolute()
                    _reject_links(source, Path(source.anchor))
                    if source.is_symlink():
                        raise BackupError('backup_source_link_unsafe')
                    # No source in the managed backup store: prevent recursive
                    # backup-of-backup, including aliases resolving into it.
                    if source.resolve().is_relative_to(self.root.resolve()):
                        raise BackupError('backup_source_recursive')
                    if not source.is_file():
                        raise BackupError('backup_source_missing_or_not_file')
                    destination = stage / safe
                    before = self._stat_key(source)
                    record = self._cached_source(source, relative, kind)
                    if record is None:
                        if kind == 'sqlite':
                            _backup_sqlite(source, destination)
                        else:
                            _copy_stable_file(source, destination)
                        self._validate(destination, kind)
                        record = self._publish_object(destination, relative, kind)
                        self._remember_source(source, before, record)
                    expected = spec.get('expected_sha256')
                    if expected is not None and (not isinstance(expected, str) or not _HASH.fullmatch(expected)
                                                 or record['sha256'] != expected):
                        raise BackupError('backup_source_identity_mismatch')
                    records.append(record)
                if secrets is not None:
                    secret_path = stage / 'secrets.enc'
                    secret_path.write_bytes(encrypt_secrets(secrets, password, backup_id=backup_id))
                    records.append(self._publish_object(secret_path, 'secrets.enc', 'secrets'))
                missing = list(missing or [])
                # Only HMACs (never plain customer keys) go in the manifest.
                tomb = privacy.merge_tombstones(tombstones, privacy.read_tombstones(privacy_db) if privacy_db else [])
                manifest = {'schema_version': 2, 'id': backup_id,
                    'created_at': datetime.now(timezone.utc).isoformat(),
                    'scope': 'full' if full and not missing and secrets is not None else 'partial',
                    'complete': bool(full and not missing and secrets is not None),
                    'metadata': metadata, 'missing': missing, 'exclusions': list(exclusions or []), 'files': records,
                    'privacy_tombstones': tomb}
                temporary = stage / 'manifest.json'
                with temporary.open('w', encoding='utf-8') as handle:
                    json.dump(manifest, handle, ensure_ascii=False, allow_nan=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                # Objects were verified on publish; validate the manifest and
                # all referenced objects before exposing the new snapshot.
                self._verify_manifest(manifest, password=password, deep=False)
                os.replace(temporary, self._manifest_path(backup_id))
                return manifest
            finally:
                shutil.rmtree(stage)

    def _publish_object(self, source, relative, kind):
        with source.open('r+b') as handle:
            os.fsync(handle.fileno())
        digest = _sha256(source)
        destination = self._object_path(digest)
        destination.parent.mkdir(exist_ok=True)
        _reject_links(destination, self.root)
        size = source.stat().st_size
        if destination.exists():
            if destination.stat().st_size != size or _sha256(destination) != digest:
                raise BackupError('backup_object_corrupt')
        else:
            os.replace(source, destination)
        return {'path': relative, 'kind': kind, 'bytes': size, 'sha256': digest}

    def _load(self, backup_id):
        path = self._manifest_path(backup_id)
        try:
            return _strict_json_loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise BackupError('backup_manifest_unreadable') from exc

    def _verify_manifest(self, manifest, password=None, deep=True, check_objects=True):
        if (not isinstance(manifest, dict) or manifest.get('schema_version') != 2 or
            not isinstance(manifest.get('files'), list) or not _ID.fullmatch(str(manifest.get('id', '')))):
            raise BackupError('backup_manifest_invalid')
        if (manifest.get('scope') not in {'full', 'partial'} or
            type(manifest.get('complete')) is not bool or not isinstance(manifest.get('missing'), list) or
            not isinstance(manifest.get('exclusions'), list) or not isinstance(manifest.get('metadata'), dict)):
            raise BackupError('backup_manifest_invalid')
        if (manifest['scope'] == 'full') != manifest['complete']:
            raise BackupError('backup_manifest_invalid')
        if manifest['complete'] and (manifest['missing'] or not manifest['metadata'].get('coverage')):
            raise BackupError('backup_manifest_invalid')
        seen = set()
        for record in manifest['files']:
            if not isinstance(record, dict):
                raise BackupError('backup_record_invalid')
            relative = record.get('path')
            _safe_relative(relative)
            if relative.casefold() in seen or record.get('kind') not in _KINDS:
                raise BackupError('backup_record_invalid')
            if (relative == 'secrets.enc') != (record['kind'] == 'secrets'):
                raise BackupError('backup_record_invalid')
            if type(record.get('bytes')) is not int or record['bytes'] < 0:
                raise BackupError('backup_record_invalid')
            seen.add(relative.casefold())
            path = self._object_path(record.get('sha256'))
            if not check_objects:
                continue
            if (not path.is_file() or path.stat().st_size != record.get('bytes')
                or (deep and _sha256(path) != record['sha256'])):
                raise BackupError('backup_object_corrupt_or_missing')
            self._validate(path, record['kind'])
            if record['kind'] == 'secrets' and password is not None:
                decrypt_secrets(path.read_bytes(), password, backup_id=manifest['id'])
        if manifest['complete'] and 'secrets.enc' not in seen:
            raise BackupError('backup_manifest_invalid')
        return manifest

    def verify(self, backup_id, *, password=None, deep=True):
        manifest = self._load(backup_id)
        if not isinstance(manifest, dict) or manifest.get('id') != backup_id:
            raise BackupError('backup_manifest_identity_mismatch')
        return self._verify_manifest(manifest, password=password, deep=deep)

    def stage_restore(self, backup_id, destination, *, password, current_tombstones=None,
                      current_privacy_db=None, privacy_key_path=None):
        # Stage only. The maintenance coordinator must migrate/validate and
        # swap this tree after closing writers; this never touches LIVE data.
        with self._write():
            manifest = self.verify(backup_id, password=password)
            if any(row['kind'] == 'secrets' for row in manifest['files']) and not isinstance(password, str):
                raise BackupError('backup_password_required')
            destination = Path(destination).absolute()
            if destination.exists() or destination.resolve().is_relative_to(self.root.resolve()):
                raise BackupError('backup_restore_destination_invalid')
            _reject_links(destination.parent, Path(destination.anchor))
            destination.mkdir(parents=True)
            try:
                for record in manifest['files']:
                    target = destination / _safe_relative(record['path'])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = self._object_path(record['sha256'])
                    # Keep secret blob encrypted in staging; the coordinator
                    # must re-wrap only on the destination machine.
                    _copy_stable_file(source, target)
                self._apply_privacy(manifest, destination, current_tombstones, current_privacy_db, privacy_key_path)
                return manifest
            except Exception:
                shutil.rmtree(destination)
                raise

    def _apply_privacy(self, manifest, destination, current, current_db, key_path):
        # Union of live + backup tombstones is applied to the STAGED copy only,
        # so a restore can neither resurrect forgotten customers nor drop a tombstone.
        union = privacy.merge_tombstones(manifest.get('privacy_tombstones'), current,
                                         privacy.read_tombstones(current_db) if current_db else [])
        secret = privacy.load_secret_from_path(key_path) if key_path else None
        if union and secret is None:
            raise BackupError('backup_privacy_secret_required')
        for record in manifest['files']:
            if record['kind'] == 'sqlite':
                privacy.purge_db_by_tombstones(destination / _safe_relative(record['path']), union, secret)

    def _deletion_preview(self, ids):
        if (not isinstance(ids, list) or not ids or len(ids) > 1000 or
            any(not isinstance(identity, str) or not _ID.fullmatch(identity) for identity in ids)
            or len(set(ids)) != len(ids)):
            raise BackupError('backup_ids_invalid')
        chosen = set(ids)
        manifests = {}
        signatures = []
        for path in sorted(self.manifests.glob('*.json')):
            manifest = self._load(path.stem)
            if manifest.get('id') != path.stem:
                raise BackupError('backup_manifest_identity_mismatch')
            self._verify_manifest(manifest, check_objects=False)
            manifests[path.stem] = manifest
            signatures.append((path.name, _sha256(path)))
        if not chosen.issubset(manifests):
            raise BackupError('backup_not_found')
        retained = {row['sha256'] for identity, manifest in manifests.items() if identity not in chosen
                    for row in manifest['files']}
        removed = {row['sha256'] for identity in chosen for row in manifests[identity]['files']} - retained
        size = sum(self._object_path(digest).stat().st_size for digest in removed
                   if self._object_path(digest).is_file())
        size += sum(self._manifest_path(identity).stat().st_size for identity in chosen)
        token = hashlib.sha256(json.dumps({'ids': sorted(ids), 'manifests': signatures}).encode()).hexdigest()
        return {'ok': True, 'ids': ids, 'object_ids': sorted(removed), 'reclaimable_bytes': size, 'preview_token': token}

    def preview_delete(self, ids):
        with self._write():
            return self._deletion_preview(ids)

    def delete(self, ids, *, preview_token):
        with self._write():
            preview = self._deletion_preview(ids)
            if preview['preview_token'] != preview_token:
                raise BackupError('backup_preview_changed')
            # Journal before removing visible manifests. If deletion crashes,
            # objects may be retained, but never collected while referenced.
            transaction = uuid.uuid4().hex
            journal = self.root / ('.delete-' + transaction + '.json')
            with journal.open('x', encoding='utf-8') as handle:
                json.dump({'ids': ids, 'objects': preview['object_ids'],
                           'manifest_hashes': {identity: _sha256(self._manifest_path(identity)) for identity in ids}}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            removed_manifests = []
            for identity in ids:
                source = self._manifest_path(identity)
                target = self.root / ('.deleted-' + transaction + '-' + identity + '.json')
                os.replace(source, target)
                removed_manifests.append(target)
            for digest in preview['object_ids']:
                self._object_path(digest).unlink(missing_ok=True)
            for path in removed_manifests:
                path.unlink()
            journal.unlink()
            return {'ok': True, 'deleted_ids': ids, 'reclaimed_bytes': preview['reclaimable_bytes']}

    def recover_deletions(self):
        recovered = []
        with self._write(recovery=True):
            for journal in sorted(self.root.glob('.delete-*.json')):
                match = re.fullmatch(r'\.delete-([a-f0-9]{32})\.json', journal.name)
                _reject_links(journal, self.root)
                if not match:
                    raise BackupError('backup_delete_journal_invalid')
                payload = _strict_json_loads(journal.read_text(encoding='utf-8'))
                if not isinstance(payload, dict):
                    raise BackupError('backup_delete_journal_invalid')
                ids, objects, hashes = (payload.get(key) for key in ('ids','objects','manifest_hashes'))
                if (not isinstance(ids, list) or not ids or len(ids) > 1000 or
                    any(not isinstance(identity, str) or not _ID.fullmatch(identity) for identity in ids) or
                    len(set(ids)) != len(ids) or not isinstance(objects, list) or
                    any(not isinstance(digest, str) or not _HASH.fullmatch(digest) for digest in objects) or
                    not isinstance(hashes, dict) or set(hashes) != set(ids) or
                    any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in hashes.values())):
                    raise BackupError('backup_delete_journal_invalid')
                paths = []
                for identity in ids:
                    source = self._manifest_path(identity)
                    hidden = self.root / ('.deleted-' + match[1] + '-' + identity + '.json')
                    _reject_links(hidden, self.root)
                    if source.exists() and hidden.exists():
                        raise BackupError('backup_delete_journal_conflict')
                    for path in (source, hidden):
                        if path.exists() and _sha256(path) != hashes[identity]:
                            raise BackupError('backup_delete_journal_conflict')
                    paths.append((source, hidden))
                # Read all remaining manifests before mutating anything.
                retained = set()
                for path in self.manifests.glob('*.json'):
                    if path.stem not in ids:
                        retained.update(row['sha256'] for row in self.verify(path.stem)['files'])
                if retained.intersection(objects):
                    raise BackupError('backup_delete_journal_conflict')
                for source, hidden in paths:
                    if source.exists():
                        os.replace(source, hidden)
                for digest in objects:
                    path = self._object_path(digest)
                    if path.exists():
                        path.unlink()
                for _, hidden in paths:
                    if hidden.exists():
                        hidden.unlink()
                journal.unlink()
                recovered.extend(ids)
        return {'ok': True, 'recovered_deleted_ids': recovered}

    def prune(self, *, keep=20):
        if not isinstance(keep, int) or isinstance(keep, bool) or keep < 1:
            raise BackupError('backup_retention_invalid')
        with self._write():
            groups = {'full': [], 'partial': []}
            for path in self.manifests.glob('*.json'):
                # Retained snapshots need only manifest/object metadata here.
                # Deep verification remains mandatory in deletion and restore.
                manifest = self.verify(path.stem, deep=False)
                scope = manifest.get('scope')
                if scope not in groups:
                    raise BackupError('backup_manifest_invalid')
                groups[scope].append((manifest.get('created_at', ''), path.stem))
            removed = [identity for rows in groups.values()
                       for _, identity in sorted(rows, reverse=True)[keep:]]
            checked = self._deletion_preview(removed) if removed else None
        if checked:
            # New manifests between preview and delete invalidate the token;
            # abort instead of collecting objects using obsolete references.
            return self.delete(removed, preview_token=checked['preview_token'])
        return {'ok': True, 'deleted_ids': [], 'reclaimed_bytes': 0}


def references_coverage(manifest, inventory):
    """Warning-only cross-check: manifest source paths vs the collected inventory.

    Never raises; returns a list of warning strings (empty when consistent).
    """
    warnings = []
    try:
        known = set(inventory.get('sources', {}))
        files = {record.get('path') for record in manifest.get('files', [])
                 if isinstance(record, dict)} - {'secrets.enc'}
        for path in sorted(p for p in files if p not in known):
            warnings.append('manifest_path_not_in_inventory/' + str(path))
        for reference in inventory.get('external_references', []):
            path = reference.get('path') if isinstance(reference, dict) else None
            if path not in files:
                warnings.append('reference_not_in_backup/' + str(path))
    except Exception as exc:
        warnings.append('coverage_check_failed/' + type(exc).__name__)
    return warnings
