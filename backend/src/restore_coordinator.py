"""Restore maintenance coordinator (v2 backups).

Phases: refuse if LIVE -> maintenance -> close writers -> stage (privacy filter)
-> migrate staged copy -> verify -> atomic directory swap (old dir kept as
rollback, never deleted) -> reinit services -> leave maintenance with AI and
auto-pin still disabled. Any failure rolls back to the old directory.

Everything is injected; the caller decides which directory this operates on.
"""
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from swallowed import note as _note_swallowed


class RestoreError(RuntimeError):
    def __init__(self, code, phase=''):
        super().__init__(code)
        self.code = code
        self.phase = phase


def restore_v2_enabled(flag_path):
    """Feature flag; default OFF when the file is absent or invalid."""
    try:
        data = json.loads(Path(flag_path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get('restore_v2_enabled') is True


class WriterRegistry:
    def __init__(self):
        self._items = []

    def register(self, name, closer):
        self._items.append((name, closer))

    def close_all(self):
        closed, failed = [], []
        for name, closer in self._items:
            try:
                closer()
                closed.append(name)
            except Exception:
                failed.append(name)
        if failed:
            raise RestoreError('writers_close_failed/' + ','.join(failed), 'close')
        return closed


def sqlite_verify(staged):
    """Default verification: every sqlite file in the staged tree passes integrity_check."""
    for path in Path(staged).rglob('*'):
        if path.is_file() and path.suffix in ('.sqlite3', '.sqlite', '.db'):
            db = sqlite3.connect(path)
            try:
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RestoreError('restore_verify_failed', 'verify')
            except sqlite3.DatabaseError:
                raise RestoreError('restore_verify_failed', 'verify') from None
            finally:
                db.close()


class RestoreCoordinator:
    def __init__(self, *, data_dir, store, maintenance, is_live, writers, reinit,
                 migrate=None, verify=sqlite_verify, privacy_db=None, privacy_key_path=None,
                 current_tombstones=None, rename=os.replace, clock=time.time):
        self.data_dir = Path(data_dir).absolute()
        self.store = store
        self.maintenance = maintenance
        self.is_live = is_live
        self.writers = writers
        self.reinit = reinit
        self.migrate = migrate or (lambda staged: None)
        self.verify = verify
        self.privacy_db = privacy_db
        self.privacy_key_path = privacy_key_path
        self.current_tombstones = current_tombstones
        self.rename = rename
        self.clock = clock
        self.events = []

    def restore(self, backup_id, password):
        if self.is_live():
            raise RestoreError('maintenance_required', 'guard')
        self.maintenance.enter()
        stamp = '%d' % int(self.clock() * 1000)
        parent = self.data_dir.parent
        stage_root = parent / ('.restore-stage-' + stamp)
        rollback_dir = parent / (self.data_dir.name + '.rollback-' + stamp)
        swapped = moved_old = False
        phase = 'close'
        try:
            self.events.append('close')
            self.writers.close_all()
            phase = 'stage'
            self.events.append('stage')
            tombstones = self.current_tombstones() if callable(self.current_tombstones) else self.current_tombstones
            manifest = self.store.stage_restore(
                backup_id, stage_root, password=password, current_tombstones=tombstones,
                current_privacy_db=self.privacy_db, privacy_key_path=self.privacy_key_path)
            staged = stage_root / self.data_dir.name
            if not staged.is_dir():
                raise RestoreError('restore_stage_invalid', phase)
            self._keep_current_privacy_key(staged)
            phase = 'migrate'
            self.events.append('migrate')
            self.migrate(staged)
            phase = 'verify'
            self.events.append('verify')
            self.verify(staged)
            phase = 'swap'
            self.events.append('swap')
            self.rename(self.data_dir, rollback_dir)
            moved_old = True
            self.rename(staged, self.data_dir)
            swapped = True
            phase = 'reinit'
            self.events.append('reinit')
            self.reinit()
            self.events.append('done')
            return {'ok': True, 'restored': backup_id, 'scope': manifest.get('scope'),
                    'rollback_dir': str(rollback_dir), 'ai_enabled': False, 'auto_pin_enabled': False}
        except Exception as exc:
            self.events.append('rollback')
            if swapped:
                failed = parent / (self.data_dir.name + '.failed-' + stamp)
                os.replace(self.data_dir, failed)
            if moved_old and not self.data_dir.exists():
                os.replace(rollback_dir, self.data_dir)
            try:
                self.reinit()
            except Exception:
                _note_swallowed("restore_rollback_reinit_failed", level=30)
            if isinstance(exc, RestoreError):
                raise
            raise RestoreError('restore_failed/' + type(exc).__name__, phase) from exc
        finally:
            self.maintenance.exit(keep_ai_disabled=True)

    def _keep_current_privacy_key(self, staged):
        # Tombstones are HMACs under THIS machine's key; a key restored from the
        # backup must never replace it.
        if not self.privacy_key_path:
            return
        key = Path(self.privacy_key_path).absolute()
        if key.is_file() and key.is_relative_to(self.data_dir):
            target = staged / key.relative_to(self.data_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(key, target)
