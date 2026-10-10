import json
import threading
import time
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import server
from backup_service import BackupService
from backup_v2 import BackupV2
from maintenance import JobStore


def _serve(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    jobs = JobStore(tmp_path / 'jobs.sqlite3')
    seen = []
    service = SimpleNamespace(create=lambda password, progress=None, full=True:
                              seen.append((password, full)) or {'complete': full})
    engine = SimpleNamespace(_system_jobs=lambda: jobs, _backup_v2_service=lambda: service)
    for name in ('backup_v2_list', 'backup_v2_start', 'backup_indexeddb_save'):
        setattr(engine, name, getattr(server.ReplyEngine, name).__get__(engine))

    class Handler(server.Handler):
        pass
    Handler.engine = engine
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=http.serve_forever, kwargs={'poll_interval': .05}, daemon=True).start()
    origin = f'http://127.0.0.1:{http.server_port}'

    def call(path, payload=None, from_origin=origin):
        req = Request(origin + path, data=None if payload is None else json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json', 'Origin': from_origin})
        with urlopen(req, timeout=3) as response:
            return response.status, json.loads(response.read())
    return http, jobs, seen, call


def test_list_create_and_indexeddb_routes(tmp_path, monkeypatch):
    store = BackupV2(tmp_path / 'backups' / 'v2')
    src = tmp_path / 'a.json'
    src.write_text('{"a":1}')
    store.create({'a.json': {'source': src, 'kind': 'json'}}, metadata={
        'app_version': '1', 'schema_version': 2, 'solver_hash': '', 'coverage': ['x']}, full=False)
    http, jobs, seen, call = _serve(tmp_path, monkeypatch)
    try:
        _, listing = call('/api/backup/v2/list')
        assert len(listing['items']) == 1
        item = listing['items'][0]
        assert item['kind'] == 'partial' and item['verified'] is False and item['size'] > 0
        assert set(item) == {'id', 'kind', 'created_at', 'size', 'verified'}
        with pytest.raises(HTTPError) as err:
            call('/api/backup/v2/list', None, 'https://other.example')
        assert err.value.code == 403
        with pytest.raises(HTTPError) as err:
            call('/api/backup/v2', {'full': True, 'password': 'long-password'}, 'chrome-extension://abc')
        assert err.value.code == 403
        _, started = call('/api/backup/v2', {'full': False, 'password': 'long-password'})
        assert started['job_id']
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not seen:
            time.sleep(.02)
        assert seen == [('long-password', False)]
        with pytest.raises(HTTPError) as err:
            call('/api/backup/v2', {'full': True, 'password': ''})
        assert err.value.code == 400
        _, saved = call('/api/backup/indexeddb', {'export': {'stores': {'snapshots': [1]}}})
        files = list((tmp_path / 'backups' / 'indexeddb').glob('*.json'))
        assert [f.name for f in files] == [saved['saved']]
        assert json.loads(files[0].read_text())['stores']['snapshots'] == [1]
        with pytest.raises(HTTPError) as err:
            call('/api/backup/indexeddb', {'export': 'nope'})
        assert err.value.code == 400
    finally:
        http.shutdown()
        http.server_close()
        jobs.close()


def test_indexeddb_export_becomes_backup_source(tmp_path):
    folder = tmp_path / 'backups' / 'indexeddb'
    folder.mkdir(parents=True)
    (folder / '20261001T000000Z.json').write_text('{"stores":{}}')
    service = BackupService(tmp_path, app_version='1', schema_version=2, solver_hash='h',
                            reference_provider=lambda: [], dpapi_decrypt=lambda b: b)
    result = service.create('long-password', full=False)
    assert 'browser_indexeddb_recovery_not_exported' not in result['missing']
    manifest = BackupV2(tmp_path / 'backups' / 'v2').verify(result['backup_id'])
    assert any(f['path'] == 'indexeddb/20261001T000000Z.json' for f in manifest['files'])
