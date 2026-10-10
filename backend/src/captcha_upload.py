"""Durable CAPTCHA video uploads, separate from recording and mouse solving."""
from __future__ import annotations

import sqlite3
import os
from contextlib import contextmanager
import time
from pathlib import Path

MAX_AGE_S = 7 * 86400  # purge window for old sent/deleted rows and stale partial recordings
UNSENT_MAX_AGE_S = 30 * 60  # unsent recordings are abandoned after this
MAX_BYTES = 5 * 1024 ** 3
RETRY_S = (30, 120, 600, 1800)


@contextmanager
def recorder_idle():
    """Only clean abandoned partial files while holding the recorder mutex."""
    if os.name != 'nt':
        yield True
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateMutexW(None, False, 'Local\\KidTECaptchaRecorder')
    acquired = bool(handle) and kernel.WaitForSingleObject(handle, 0) in (0, 0x80)
    try:
        yield acquired
    finally:
        if acquired: kernel.ReleaseMutex(handle)
        if handle: kernel.CloseHandle(handle)


class CaptchaUploads:
    def __init__(self, data_dir, *, clock=time.time):
        self.root = Path(data_dir).resolve()
        self.folder = self.root / 'screen-recordings'
        self.folder.mkdir(parents=True, exist_ok=True)
        if self.folder.resolve().parent != self.root:
            raise ValueError('captcha_recording_directory_invalid')
        self.clock = clock
        self.db = sqlite3.connect(self.root / 'captcha-uploads.sqlite3', timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS uploads (
                filename TEXT PRIMARY KEY, created_at REAL NOT NULL, state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL,
                message_id TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '')''')
            # A worker may have died during HTTP; retry upload, never rerun solver.
            self.db.execute("UPDATE uploads SET state='pending',next_attempt=? WHERE state='sending'", (clock() + 30,))

    def close(self):
        self.db.close()

    def file(self, filename):
        if Path(filename).name != filename or not filename.startswith('screen-') or not filename.endswith('.mp4'):
            raise ValueError('captcha_upload_path_invalid')
        path = self.folder / filename
        if path.is_symlink() or path.resolve().parent != self.folder.resolve():
            raise ValueError('captcha_upload_path_invalid')
        return path

    def discover(self):
        with self.db:
            for path in self.folder.glob('screen-*.mp4'):
                if path.name.endswith('.recording.mp4'):
                    continue
                try:
                    path = self.file(path.name)
                except ValueError:
                    continue
                self.db.execute('INSERT OR IGNORE INTO uploads(filename,created_at,state,next_attempt) VALUES (?,?,?,?)',
                    (path.name, path.stat().st_mtime, 'pending', self.clock()))

    def cleanup(self):
        with recorder_idle() as idle:
            if idle:
                for path in self.folder.glob('screen-*.recording.mp4'):
                    try:
                        path = self.file(path.name)
                        if path.stat().st_mtime <= self.clock() - MAX_AGE_S:
                            path.unlink()
                    except (OSError, ValueError):
                        continue
        now = self.clock()
        # Unsent recordings are abandoned after UNSENT_MAX_AGE_S; a file being sent is never touched.
        with self.db:
            self.db.execute("UPDATE uploads SET state='deleted',error_code='expired_unsent' WHERE state='pending' AND created_at<=?",
                (now - UNSENT_MAX_AGE_S,))
        # Sent/deleted rows: (re)try removing the file until it is really gone.
        for row in self.db.execute("SELECT filename,state FROM uploads WHERE state IN ('sent','deleted')").fetchall():
            try:
                path = self.file(row['filename'])
                path.unlink(missing_ok=True)
            except ValueError:
                pass
            except OSError:
                continue  # retried on the next cleanup pass
            if row['state'] == 'sent':
                with self.db:
                    self.db.execute("UPDATE uploads SET state='deleted' WHERE filename=?", (row['filename'],))
        with self.db:
            self.db.execute("DELETE FROM uploads WHERE state IN ('sent','deleted') AND created_at<=?", (now - MAX_AGE_S,))

    def tick(self, upload):
        self.discover()
        self.cleanup()
        row = self.db.execute("SELECT * FROM uploads WHERE state='pending' AND next_attempt<=? AND created_at>? ORDER BY created_at,filename LIMIT 1", (self.clock(), self.clock() - UNSENT_MAX_AGE_S)).fetchone()
        if row is None:
            return False
        path = self.file(row['filename'])
        with self.db:
            self.db.execute("UPDATE uploads SET state='sending',attempts=attempts+1 WHERE filename=?", (row['filename'],))
        try:
            receipt = upload(path)
            if not isinstance(receipt, dict) or receipt.get('ok') is not True:
                raise ValueError('telegram_receipt_invalid')
            message_id = str((receipt.get('result') or {}).get('message_id') or '')
            with self.db:
                self.db.execute("UPDATE uploads SET state='sent',message_id=?,error_code='' WHERE filename=?", (message_id, row['filename']))
            # Persist success first. A deletion failure must never repeat HTTP.
            self.cleanup()
        except Exception as error:
            current = self.db.execute('SELECT state FROM uploads WHERE filename=?', (row['filename'],)).fetchone()[0]
            if current not in {'sent', 'deleted'}:
                delay = RETRY_S[min(row['attempts'], len(RETRY_S) - 1)]
                with self.db:
                    self.db.execute("UPDATE uploads SET state='pending',next_attempt=?,error_code=? WHERE filename=?",
                        (self.clock() + delay, type(error).__name__, row['filename']))
        return True


def telegram_upload(data_dir, path):
    import requests
    from captcha_secret import load_telegram
    token, chat_id = load_telegram(Path(data_dir))
    with path.open('rb') as video:
        response = requests.post(f'https://api.telegram.org/bot{token}/sendVideo',
            data={'chat_id': chat_id, 'caption': 'Kid TE: Video CAPTCHA'},
            files={'video': (path.name, video, 'video/mp4')}, timeout=(15, 300))
        response.raise_for_status()
        return response.json()


def upload_worker(data_dir):
    queue = CaptchaUploads(data_dir)
    try:
        while True:
            try:
                worked = queue.tick(lambda path: telegram_upload(data_dir, path))
            except Exception:
                worked = False
            time.sleep(1 if worked else 30)
    finally:
        queue.close()
