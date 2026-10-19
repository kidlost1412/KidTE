import json
import threading
import time
from types import SimpleNamespace
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

import server
from maintenance import JobStore


def test_delete_job_http_202_guard_status_and_all_ids(tmp_path, monkeypatch):
    root = tmp_path / 'backups'
    for name in ('one', 'two'):
        target = root / name
        target.mkdir(parents=True)
        (target / 'manifest.json').write_text('{"schema_version":1}')
    monkeypatch.setattr(server, 'BACKUP_DIR', root)
    jobs = JobStore(tmp_path / 'jobs.sqlite3')
    engine = SimpleNamespace(_system_jobs=lambda: jobs)
    engine.system_post = lambda path, payload: server.ReplyEngine.system_post(engine, path, payload)
    engine.system_get = lambda path, query: server.ReplyEngine.system_get(engine, path, query)
    class Handler(server.Handler):
        pass
    Handler.engine = engine
    http = server.LoopbackHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=http.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    worker.start()
    origin = f'http://127.0.0.1:{http.server_port}'
    def request(path, payload=None, from_origin=origin):
        req = Request(origin + path, data=None if payload is None else json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json', 'Origin': from_origin})
        with urlopen(req, timeout=3) as response:
            return response.status, json.loads(response.read())
    try:
        with pytest.raises(HTTPError) as error:
            request('/api/system/backups/delete/preview', {'all': True}, 'https://other.example')
        assert error.value.code == 403
        _, preview = request('/api/system/backups/delete/preview', {'all': True})
        assert set(preview['ids']) == {'one', 'two'}
        status, started = request('/api/system/backups/delete', {
            'ids': preview['ids'], 'preview_token': preview['preview_token']})
        assert status == 202
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            _, job = request('/api/system/jobs/' + started['job_id'])
            if job['state'] == 'completed':
                break
            time.sleep(.01)
        assert job['state'] == 'completed'
        assert set(job['result']['deleted_ids']) == {'one', 'two'}
        assert list(root.iterdir()) == []
    finally:
        http.shutdown()
        http.server_close()
        worker.join()
        jobs.close()
