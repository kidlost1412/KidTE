"""Explicit application backup inventory; never reads global runtime configuration.

The caller supplies the runtime root and every external reference. Secret files
are held in a portable encrypted bundle, not published as plaintext CAS objects.
This module does not restore files or claim browser recovery has been exported.
"""
import base64
import hashlib
import json
from pathlib import Path

from backup import BackupError, _reject_links, _safe_relative, _strict_json_loads, _walk_regular_files
from backup_crypto import MAX_SECRET_BYTES

_SECRET_KEYS = {'password', 'api_key', 'apikey', 'secret', 'token', 'bot_token',
                'access_token', 'refresh_token', 'deepseek_api_key', 'sadcaptcha_key'}
_TRANSIENT_SUFFIXES = {'.tmp', '.copying', '.recording', '.pid', '.lock', '.sock',
                       '.socket', '.wal', '.shm'}
_PROGRAM_SUFFIXES = {'.exe', '.dll', '.pyd', '.pyc'}


def _has_secret(value):
    if isinstance(value, dict):
        return any((key.casefold() in _SECRET_KEYS and item not in (None, '', False)) or
                   _has_secret(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_has_secret(item) for item in value)
    return False


def _stable_bytes(path, *, limit):
    before = path.stat()
    if before.st_size > limit:
        raise BackupError('backup_secret_bundle_too_large')
    payload = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise BackupError('backup_inventory_source_changed')
    return payload


def collect_inventory(root, *, external_references=(), additional_sources=None,
                      dpapi_decrypt=None, browser_recovery_exported=False):
    root = Path(root).absolute()
    _reject_links(root, Path(root.anchor))
    if not root.is_dir():
        raise BackupError('backup_runtime_root_missing')
    sources, secret_files, missing, exclusions = {}, [], [], []
    coverage = []

    def add(path, relative):
        _safe_relative(relative)
        _reject_links(path, Path(path.anchor))
        if path.resolve().is_relative_to((root / 'backups').resolve()):
            raise BackupError('backup_source_recursive')
        if not path.is_file():
            missing.append(relative)
            return
        name = path.name.casefold()
        if (path.suffix.casefold() in _TRANSIENT_SUFFIXES | _PROGRAM_SUFFIXES or
                name.endswith(('-wal', '-shm', '-journal')) or name.startswith('.staging-')):
            exclusions.append({'path': relative, 'reason': 'transient_or_program'})
            return
        kind = {'.json': 'json', '.jsonl': 'jsonl', '.sqlite3': 'sqlite', '.sqlite': 'sqlite',
                '.db': 'sqlite', '.txt': 'text'}.get(path.suffix.casefold(), 'file')
        payload = None
        protected = path.suffix.casefold() == '.dpapi'
        secret = name == '.env' or protected
        if kind == 'json':
            # A secret-bearing config is encrypted whole: no partial redaction
            # that would silently omit settings needed on another machine.
            payload = _stable_bytes(path, limit=16 * 1024 * 1024)
            value = _strict_json_loads(payload.decode('utf-8'))
            secret = _has_secret(value)
        if secret:
            payload = payload if payload is not None else _stable_bytes(path, limit=2 * 1024 * 1024)
            if protected:
                if dpapi_decrypt is None:
                    raise BackupError('backup_dpapi_decryption_required')
                try:
                    payload = dpapi_decrypt(payload)
                except Exception:
                    raise BackupError('backup_dpapi_decryption_failed') from None
                if not isinstance(payload, bytes):
                    raise BackupError('backup_dpapi_decryption_failed')
            secret_files.append({'path': relative, 'protection': 'dpapi' if protected else 'plain',
                                 'payload': base64.b64encode(payload).decode('ascii')})
        else:
            if relative in sources:
                raise BackupError('backup_inventory_duplicate')
            sources[relative] = {'source': path, 'kind': kind}

    # Do not walk the installation or backup store. The whole managed data,
    # journal/raw logs and installed models are covered, including unknown new
    # application files rather than an allowlist of today's database names.
    for folder in ('data', 'logs', 'models', 'captcha'):
        directory = root / folder
        if directory.exists():
            for path in _walk_regular_files(directory):
                add(path, path.relative_to(root).as_posix())
            coverage.append(folder)
        else:
            exclusions.append({'path': folder, 'reason': 'not_installed'})
    if (root / '.env').exists():
        add(root / '.env', '.env')

    references = []
    for reference in external_references:
        if not isinstance(reference, dict) or not isinstance(reference.get('path'), (str, Path)):
            raise BackupError('backup_external_reference_invalid')
        path = Path(reference['path']).absolute()
        relative = (path.relative_to(root).as_posix() if path.is_relative_to(root) else
                    'external/' + hashlib.sha256(str(path).encode('utf-8')).hexdigest() + '/' + path.name)
        _safe_relative(relative)
        if not path.exists():
            missing.append(relative)
        elif relative not in sources:
            add(path, relative)
        if relative in sources and reference.get('sha256'):
            sources[relative]['expected_sha256'] = reference['sha256']
        references.append({'path': relative, 'original_path': str(path),
                           'video_id': str(reference.get('video_id', ''))})
    for relative, path in (additional_sources or {}).items():
        add(Path(path).absolute(), relative)
    if not browser_recovery_exported:
        missing.append('browser_indexeddb_recovery_not_exported')
    secret_bundle = json.dumps({'schema_version': 1, 'files': secret_files},
                               ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(secret_bundle) > MAX_SECRET_BYTES:
        raise BackupError('backup_secret_bundle_too_large')
    return {'sources': sources, 'secrets': secret_bundle, 'missing': sorted(set(missing)),
            'exclusions': exclusions, 'coverage': coverage, 'external_references': references}
