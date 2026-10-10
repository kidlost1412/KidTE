import sqlite3
import threading
import time

import pytest

from maintenance import JobStore


def completed(store, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = store.get(job_id)
        if result['state'] not in {'queued', 'running'}:
            return result
        time.sleep(.01)
    pytest.fail('job did not settle')


def test_job_success_progress_and_persistent_result(tmp_path):
    path = tmp_path / 'jobs.sqlite3'
    store = JobStore(path)
    def operation(progress):
        progress(.5)
        return {'bytes': 42}
    job = store.start('backup', operation)
    result = completed(store, job['job_id'])
    assert result['state'] == 'completed' and result['progress'] == 1
    assert JobStore(path).get(job['job_id'])['result'] == {'bytes': 42}


def test_job_failure_never_exposes_sensitive_exception(tmp_path):
    store = JobStore(tmp_path / 'jobs.sqlite3')
    def operation(progress):
        raise RuntimeError('secret token and customer text')
    result = completed(store, store.start('backup', operation)['job_id'])
    assert result['state'] == 'failed' and result['error'] == 'RuntimeError'
    assert 'secret' not in str(result)


def test_restart_marks_interrupted_work_without_replaying(tmp_path):
    path = tmp_path / 'jobs.sqlite3'
    store = JobStore(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO system_jobs VALUES('old','restore','running',.5,1,1,NULL,NULL)")
    assert JobStore(path).get('old')['state'] == 'interrupted'
    with pytest.raises(ValueError, match='job_not_found'):
        store.get('missing')


def test_only_one_maintenance_job_can_run(tmp_path):
    store = JobStore(tmp_path / 'jobs.sqlite3')
    gate = threading.Event()
    job = store.start('backup', lambda progress: gate.wait(2))
    try:
        with pytest.raises(ValueError, match='maintenance_job_busy'):
            store.start('backup', lambda progress: {})
    finally:
        gate.set()
    assert completed(store, job['job_id'])['state'] == 'completed'
