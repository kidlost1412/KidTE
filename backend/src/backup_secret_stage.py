"""Prepare portable secrets for the target user, in a new staging directory.

No live configuration is replaced here. A maintenance coordinator must validate
and publish the staged tree with the rest of the backup transaction.
"""
import base64
import os
from pathlib import Path
import shutil

from backup import BackupError, _reject_links, _safe_relative, _strict_json_loads


def stage_portable_secrets(bundle, destination, *, dpapi_encrypt=None):
    if not isinstance(bundle, bytes) or len(bundle) > 2 * 1024 * 1024:
        raise BackupError('backup_secret_bundle_invalid')
    try:
        document = _strict_json_loads(bundle.decode('utf-8'))
    except (UnicodeError, ValueError):
        raise BackupError('backup_secret_bundle_invalid') from None
    if (not isinstance(document, dict) or type(document.get('schema_version')) is not int or document['schema_version'] != 1 or
            not isinstance(document.get('files'), list) or len(document['files']) > 1000):
        raise BackupError('backup_secret_bundle_invalid')
    prepared, seen = [], set()
    for record in document['files']:
        if not isinstance(record, dict) or record.get('protection') not in {'plain', 'dpapi'}:
            raise BackupError('backup_secret_record_invalid')
        relative = record.get('path')
        safe = _safe_relative(relative)
        if relative.casefold() in seen or not isinstance(record.get('payload'), str):
            raise BackupError('backup_secret_record_invalid')
        seen.add(relative.casefold())
        try:
            payload = base64.b64decode(record['payload'], validate=True)
        except (ValueError, UnicodeError):
            raise BackupError('backup_secret_record_invalid') from None
        if record['protection'] == 'dpapi':
            if dpapi_encrypt is None:
                raise BackupError('backup_dpapi_encryption_required')
            try:
                payload = dpapi_encrypt(payload)
            except Exception:
                raise BackupError('backup_dpapi_encryption_failed') from None
            if not isinstance(payload, bytes):
                raise BackupError('backup_dpapi_encryption_failed')
        prepared.append((safe, payload))
    # Validate/rewrap ALL records before creating or touching a destination.
    destination = Path(destination).absolute()
    if destination.exists():
        raise BackupError('backup_secret_stage_exists')
    _reject_links(destination.parent, Path(destination.anchor))
    destination.mkdir(parents=True)
    try:
        for safe, payload in prepared:
            target = destination / safe
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
    except Exception:
        shutil.rmtree(destination)
        raise
    return {'files': [path.as_posix() for path, _ in prepared]}
