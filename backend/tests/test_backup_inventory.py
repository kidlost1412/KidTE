import base64
import hashlib
import json
import sqlite3

import pytest

from backup import BackupError
from backup_inventory import collect_inventory
from backup_v2 import BackupV2
from backup_crypto import decrypt_secrets


def fixture_root(tmp_path):
    root = tmp_path / 'runtime'
    (root / 'data').mkdir(parents=True)
    (root / 'logs' / 'raw_stream').mkdir(parents=True)
    (root / 'models' / 'installed').mkdir(parents=True)
    (root / 'captcha').mkdir()
    (root / 'backups' / 'old').mkdir(parents=True)
    (root / 'backups' / 'old' / 'excluded.txt').write_text('not included')
    (root / '.env').write_text('DEEPSEEK_API_KEY=fixture-key\n')
    (root / 'data' / 'live_sources.json').write_text(json.dumps({'shopee': {'password': 'fixture-obs'}}))
    (root / 'data' / 'profile.json').write_text('{"dose":20}')
    (root / 'logs' / 'raw_stream' / 'stream.jsonl').write_text('{"fixture":true}\n')
    (root / 'models' / 'installed' / 'model.bin').write_bytes(b'fixture model')
    (root / 'captcha' / 'telegram-token.dpapi').write_bytes(b'fixture protected')
    (root / 'captcha' / 'solver.pid').write_text('123')
    with sqlite3.connect(root / 'data' / 'delivery.sqlite3') as db:
        db.execute('CREATE TABLE fixture(id INTEGER)')
    return root


def test_inventory_covers_managed_data_external_video_and_encrypts_portable_secrets(tmp_path):
    root = fixture_root(tmp_path)
    external = tmp_path / 'video original.mp4'
    external.write_bytes(b'fixture external original')
    digest = hashlib.sha256(external.read_bytes()).hexdigest()
    inventory = collect_inventory(root, external_references=[{'path':external,'video_id':'v','sha256':digest}],
        dpapi_decrypt=lambda payload: b'fixture telegram' if payload == b'fixture protected' else None,
        browser_recovery_exported=True)
    assert not inventory['missing']
    assert set(inventory['coverage']) == {'data','logs','models','captcha'}
    assert '.env' not in inventory['sources'] and 'data/live_sources.json' not in inventory['sources']
    assert not any(path.startswith('backups/') or path.endswith('.dpapi') for path in inventory['sources'])
    assert 'captcha/solver.pid' not in inventory['sources']
    assert 'models/installed/model.bin' in inventory['sources']
    metadata = {'app_version':'fixture','schema_version':2,'solver_hash':'fixture',
                'coverage':inventory['coverage'],'external_references':inventory['external_references']}
    store = BackupV2(tmp_path / 'snapshots')
    manifest = store.create(inventory['sources'], metadata=metadata, full=True, secrets=inventory['secrets'],
                            password='fixture password', missing=inventory['missing'], exclusions=inventory['exclusions'])
    assert manifest['complete']
    secret = next(row for row in manifest['files'] if row['kind'] == 'secrets')
    decoded = json.loads(decrypt_secrets(store._object_path(secret['sha256']).read_bytes(),
                                       'fixture password', backup_id=manifest['id']))
    rows = {row['path']:row for row in decoded['files']}
    assert base64.b64decode(rows['captcha/telegram-token.dpapi']['payload']) == b'fixture telegram'
    assert rows['captcha/telegram-token.dpapi']['protection'] == 'dpapi'
    for row in manifest['files']:
        assert b'fixture-key' not in store._object_path(row['sha256']).read_bytes()
        assert b'fixture-obs' not in store._object_path(row['sha256']).read_bytes()


def test_inventory_cannot_claim_full_without_browser_export_or_missing_external_source(tmp_path):
    root = fixture_root(tmp_path)
    inventory = collect_inventory(root, external_references=[{'path':tmp_path/'missing.mp4'}], dpapi_decrypt=lambda _: b'x')
    assert 'browser_indexeddb_recovery_not_exported' in inventory['missing']
    assert any(path.endswith('/missing.mp4') for path in inventory['missing'])


def test_dpapi_failure_does_not_publish_machine_bound_ciphertext_as_portable_secret(tmp_path):
    root = fixture_root(tmp_path)
    with pytest.raises(BackupError, match='dpapi_decryption_required'):
        collect_inventory(root)
    with pytest.raises(BackupError, match='dpapi_decryption_failed'):
        collect_inventory(root, dpapi_decrypt=lambda _: (_ for _ in ()).throw(OSError('sensitive fixture')))


def test_external_video_identity_mismatch_prevents_publishing_manifest(tmp_path):
    root = fixture_root(tmp_path)
    external = tmp_path / 'changed.mp4'
    external.write_bytes(b'changed')
    inventory = collect_inventory(root, external_references=[{'path':external,'sha256':'0'*64}], dpapi_decrypt=lambda _: b'x')
    store = BackupV2(tmp_path / 'snapshots')
    with pytest.raises(BackupError, match='source_identity_mismatch'):
        store.create(inventory['sources'], metadata={'app_version':'fixture','schema_version':2,
            'solver_hash':'fixture','coverage':inventory['coverage']}, secrets=inventory['secrets'], password='fixture')
    assert not list(store.manifests.glob('*.json'))
