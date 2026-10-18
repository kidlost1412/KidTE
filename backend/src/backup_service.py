"""Application backup orchestration with explicit, auditable source coverage."""
from pathlib import Path

from backup import BackupError
from backup_crypto import _password_bytes
from backup_inventory import collect_inventory
import os
import shutil
import tempfile
import secrets as _secrets

from backup_v2 import BackupV2, references_coverage

# D8: giữ nhiều bản (mỗi nhóm đầy/từng phần) để lỗi phát hiện muộn vẫn còn bản tốt.
DEFAULT_KEEP = 7


class BackupService:
    def __init__(self, root, *, app_version, schema_version, solver_hash,
                 reference_provider, dpapi_decrypt, additional_sources=None, privacy_db=None,
                 keep=DEFAULT_KEEP):
        self.root = Path(root).absolute()
        self.metadata = {'app_version': app_version, 'schema_version': schema_version,
                         'solver_hash': solver_hash}
        self.reference_provider = reference_provider
        self.dpapi_decrypt = dpapi_decrypt
        self.additional_sources = additional_sources or {}
        self.privacy_db = privacy_db
        self.keep = keep if isinstance(keep, int) and not isinstance(keep, bool) and keep >= 1 else DEFAULT_KEEP

    def latest_indexeddb_export(self):
        folder = self.root / 'backups' / 'indexeddb'
        if not folder.is_dir():
            return None
        files = sorted(p for p in folder.glob('*.json') if p.is_file() and not p.is_symlink())
        return files[-1] if files else None

    def create(self, password, *, progress=lambda value: None, full=True):
        return self._create_snapshot(password, progress=progress, full=full)

    def _create_snapshot(self, password, *, progress=lambda value: None, full=True):
        _password_bytes(password)
        latest = self.latest_indexeddb_export()
        if latest is None:
            return self._create(password, progress, full, self.additional_sources, False)
        # Inventory refuses sources inside ROOT/backups (recursion guard), so
        # hand it a stable copy outside the managed store.
        with tempfile.TemporaryDirectory(prefix='kidte-idb-') as scratch:
            copy = Path(scratch) / latest.name
            shutil.copyfile(latest, copy)
            sources = {**self.additional_sources, 'indexeddb/' + latest.name: copy}
            return self._create(password, progress, full, sources, True)

    def _create(self, password, progress, full, additional_sources, idb_exported):
        progress(.05)
        references = self.reference_provider()
        if not isinstance(references, list):
            raise BackupError('backup_reference_inventory_invalid')
        inventory = collect_inventory(self.root, external_references=references,
            additional_sources=additional_sources, dpapi_decrypt=self.dpapi_decrypt,
            browser_recovery_exported=idb_exported)
        if not self.metadata['solver_hash']:
            inventory['missing'].append('solver_version_unverified')
        inventory['missing'].append('cross_store_snapshot_not_coordinated')
        for reference in references:
            if not reference.get('sha256'):
                inventory['missing'].append('video_identity_unverified/' + str(reference.get('video_id', '')))
        metadata = {**self.metadata, 'coverage': inventory['coverage'],
                    'external_references': inventory['external_references']}
        progress(.20)
        store = BackupV2(self.root / 'backups' / 'v2')
        manifest = store.create(inventory['sources'], metadata=metadata, full=bool(full),
            secrets=inventory['secrets'], password=password,
            missing=inventory['missing'], exclusions=inventory['exclusions'],
            privacy_db=self.privacy_db)
        coverage_warnings = references_coverage(manifest, inventory)
        progress(.85)
        store.verify(manifest['id'], password=password, deep=False)
        progress(.95)
        # Retention happens only after the new backup has passed verification.
        # A retention failure must not erase knowledge of a published backup.
        try:
            retention = store.prune(keep=self.keep)
        except Exception as exc:
            retention = {'ok': False, 'error': type(exc).__name__}
        return {'created': True, 'backup_id': manifest['id'], 'schema_version': 2,
                'complete': manifest['complete'], 'scope': manifest['scope'],
                'missing': manifest['missing'], 'exclusions': manifest['exclusions'],
                'file_count': len(manifest['files']), 'retention': retention,
                'coverage_warnings': coverage_warnings}

    def auto_password_path(self):
        return self.root / 'backups' / 'auto-backup-password.dpapi'

    def auto_password(self, *, protect=None, unprotect=None):
        """Generate once, then keep only a DPAPI-protected copy on disk."""
        if protect is None or unprotect is None:
            from windows_integration import dpapi_protect, dpapi_unprotect
            protect, unprotect = protect or dpapi_protect, unprotect or dpapi_unprotect
        path = self.auto_password_path()
        if path.is_file() and path.stat().st_size > 0:
            try:
                value = unprotect(path.read_bytes()).decode('utf-8')
            except Exception:
                raise BackupError('backup_auto_password_unreadable') from None
            _password_bytes(value)
            return value
        value = _secrets.token_urlsafe(32)
        protected = protect(value.encode('utf-8'))
        if not isinstance(protected, bytes) or not protected:
            raise BackupError('backup_auto_password_protect_failed')
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(protected)
        os.replace(temporary, path)
        return value

    def create_auto(self, *, protect=None, unprotect=None, full=False,
                    progress=lambda value: None):
        """Automatic backup: always v2, partial by default, DPAPI-held password."""
        return self.create(self.auto_password(protect=protect, unprotect=unprotect),
                           progress=progress, full=full)
