import json
import threading
import time
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import server
from backup_v2 import BackupV2
from maintenance import JobStore

META = {'app_version': 'f', 'schema_version': 2, 'solver_hash': 'f', 'coverage': ['x']}


def make_backups(root, count):
    store = BackupV2(root / 'backups' / 'v2')
    ids = []
    for index in range(count):
        source = root / f'src{index}.txt'
        source.write_text('payload %d' % index)
        ids.append(store.create({f'data/f{index}.txt': {'source': source, 'kind': 'text'}}, metadata=META)['id'])
    return ids


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    jobs = JobStore(tmp_path / 'jobs.sqlite3')
    engine = SimpleNamespace(_system_jobs=lambda: jobs)
    for name in ('backup_v2_delete_preview', 'backup_v2_delete', 'backup_v2_start',
                 '_guarded_v2_create', '_v2_delete_ids'):
        setattr(engine, name, getattr(server.ReplyEngine, name).__get__(engine)
                if name != '_v2_delete_ids' else server.ReplyEngine._v2_delete_ids)
    engine.release = threading.Event()
    class Fake:
        def create(self, password, progress=None, full=True):
            engine.release.wait(5)
            return {'created': True}
    engine._backup_v2_service = lambda: Fake()
    class Handler(server.Handler):
        pass
    Handler.engine = engine
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=http.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    worker.start()
    origin = f'http://127.0.0.1:{http.server_port}'
    def request(path, payload, from_origin=origin):
        req = Request(origin + path, data=json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json', 'Origin': from_origin})
        with urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    yield SimpleNamespace(request=request, engine=engine, tmp=tmp_path)
    engine.release.set()
    http.shutdown(); http.server_close(); worker.join()
    deadline = time.monotonic() + 3
    while server._V2_WRITERS._creating and time.monotonic() < deadline:
        time.sleep(.01)
    jobs.close()


def manifests(tmp):
    return sorted(p.stem for p in (tmp / 'backups' / 'v2' / 'manifests').glob('*.json'))


def test_v2_preview_then_delete(api):
    ids = make_backups(api.tmp, 3)
    with pytest.raises(HTTPError) as error:
        api.request('/api/backup/v2/delete/preview', {'ids': ids[:1]}, 'https://other.example')
    assert error.value.code == 403
    _, preview = api.request('/api/backup/v2/delete/preview', {'ids': ids[:2]})
    assert preview['ids'] == ids[:2] and preview['reclaim_bytes'] > 0 and preview['token']
    assert manifests(api.tmp) == sorted(ids)
    _, done = api.request('/api/backup/v2/delete', {'ids': ids[:2], 'token': preview['token']})
    assert sorted(done['deleted_ids']) == sorted(ids[:2])
    assert manifests(api.tmp) == [ids[2]]
    _, all_preview = api.request('/api/backup/v2/delete/preview', {'all': True})
    assert all_preview['ids'] == [ids[2]]
    api.request('/api/backup/v2/delete', {'all': True, 'token': all_preview['token']})
    assert manifests(api.tmp) == []


def test_409_while_create_running(api):
    ids = make_backups(api.tmp, 1)
    _, preview = api.request('/api/backup/v2/delete/preview', {'ids': ids})
    status, started = api.request('/api/backup/v2', {'password': 'long-enough-1'})
    assert status == 200 and started['job_id']
    with pytest.raises(HTTPError) as error:
        api.request('/api/backup/v2/delete', {'ids': ids, 'token': preview['token']})
    assert error.value.code == 409 and json.loads(error.value.read())['error'] == 'backup_busy'
    assert manifests(api.tmp) == ids
    api.engine.release.set()
    deadline = time.monotonic() + 3
    while server._V2_WRITERS._creating and time.monotonic() < deadline:
        time.sleep(.01)
    server._V2_WRITERS.begin_delete()          # and create cannot run during delete
    try:
        with pytest.raises(HTTPError) as error:
            api.request('/api/backup/v2', {'password': 'long-enough-1'})
        assert error.value.code == 409
    finally:
        server._V2_WRITERS.end_delete()


def test_bad_token_rejected(api):
    ids = make_backups(api.tmp, 2)
    for token in ('0' * 64, '', None):
        with pytest.raises(HTTPError) as error:
            api.request('/api/backup/v2/delete', {'ids': ids[:1], 'token': token})
        assert error.value.code == 400
    # a token from an older state (set of manifests changed) is expired
    _, preview = api.request('/api/backup/v2/delete/preview', {'ids': ids[:1]})
    make_backups(api.tmp, 1)
    with pytest.raises(HTTPError) as error:
        api.request('/api/backup/v2/delete', {'ids': ids[:1], 'token': preview['token']})
    assert error.value.code == 400
    assert set(ids).issubset(manifests(api.tmp))
