"""Durable local job receipts; interrupted work is never blindly replayed."""
import json
import sqlite3
import threading
import time
import uuid


class JobStore:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._lock = threading.RLock()
        self._threads = []
        with self._db:
            self._db.execute('''CREATE TABLE IF NOT EXISTS system_jobs (
                job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
                progress REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                result_json TEXT, error TEXT)''')
            self._db.execute("UPDATE system_jobs SET state='interrupted', error='process_interrupted', updated_at=? WHERE state IN ('queued','running')", (time.time(),))

    def get(self, job_id):
        with self._lock:
            row = self._db.execute('SELECT * FROM system_jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('job_not_found')
        return dict(zip(('job_id', 'kind', 'state', 'progress', 'created_at', 'updated_at', 'result', 'error'),
                        (*row[:6], json.loads(row[6]) if row[6] else None, row[7])))

    def start(self, kind, operation):
        job_id = uuid.uuid4().hex
        now = time.time()
        with self._lock, self._db:
            if self._db.execute("SELECT 1 FROM system_jobs WHERE state IN ('queued','running') LIMIT 1").fetchone():
                raise ValueError('maintenance_job_busy')
            self._db.execute('INSERT INTO system_jobs VALUES(?,?,?,?,?,?,?,?)',
                             (job_id, kind, 'queued', 0, now, now, None, None))
        def run():
            try:
                self._update(job_id, 'running', 0)
                result = operation(lambda progress: self._update(job_id, 'running', progress))
                encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
                self._update(job_id, 'completed', 1, result=encoded)
            except Exception as exc:
                # Do not expose paths, secrets or customer content in a job error.
                self._update(job_id, 'failed', 0, error='backup_busy' if str(exc) == 'backup_busy' else type(exc).__name__)
        thread = threading.Thread(target=run, name='system-job-' + job_id, daemon=True)
        self._threads = [item for item in self._threads if item.is_alive()]
        self._threads.append(thread)
        thread.start()
        return {'ok': True, 'accepted': True, 'job_id': job_id}

    def _update(self, job_id, state, progress, *, result=None, error=None):
        if not isinstance(progress, (float, int)) or not 0 <= progress <= 1:
            raise ValueError('job_progress_invalid')
        with self._lock, self._db:
            self._db.execute('UPDATE system_jobs SET state=?, progress=?, updated_at=?, result_json=?, error=? WHERE job_id=?',
                             (state, progress, time.time(), result, error, job_id))

    def close(self):
        # Keep the connection open if a long operation still uses its receipt.
        # Process termination leaves queued/running rows for restart diagnosis.
        for thread in self._threads:
            thread.join(timeout=1)
        if not any(thread.is_alive() for thread in self._threads):
            with self._lock:
                self._db.close()


class MaintenanceMode:
    """Process-wide maintenance flag. While active, reply paths answer
    {'ok': False, 'error': 'maintenance'}. After a restore, AI and auto-pin
    stay disabled until an operator explicitly re-enables them."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active = False
        self.ai_disabled = False
        self.auto_pin_disabled = False

    @property
    def active(self):
        return self._active

    def enter(self):
        with self._lock:
            if self._active:
                raise ValueError('maintenance_job_busy')
            self._active = True

    def exit(self, *, keep_ai_disabled=False):
        with self._lock:
            self._active = False
            if keep_ai_disabled:
                self.ai_disabled = self.auto_pin_disabled = True

    def reenable(self):
        with self._lock:
            self.ai_disabled = self.auto_pin_disabled = False

    def blocked(self):
        return {'ok': False, 'error': 'maintenance'} if self._active else None
