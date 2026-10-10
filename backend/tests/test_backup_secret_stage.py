import base64
import json

import pytest

from backup import BackupError
from backup_secret_stage import stage_portable_secrets


def bundle(rows):
    return json.dumps({'schema_version':1, 'files':rows}).encode()


def row(path, payload=b'fixture', protection='plain'):
    return {'path':path,'payload':base64.b64encode(payload).decode(),'protection':protection}


def test_target_dpapi_rewrap_and_plain_configuration_are_staged_only(tmp_path):
    target = tmp_path / 'fresh-stage'
    encrypted = []
    def protect(payload):
        encrypted.append(payload)
        return b'fixture target user encrypted:' + payload
    result = stage_portable_secrets(bundle([row('.env', b'API_KEY=fixture\n'),
        row('captcha/telegram-token.dpapi', b'fixture telegram', 'dpapi')]), target, dpapi_encrypt=protect)
    assert result['files'] == ['.env','captcha/telegram-token.dpapi']
    assert encrypted == [b'fixture telegram']
    assert (target / '.env').read_bytes() == b'API_KEY=fixture\n'
    assert (target / 'captcha' / 'telegram-token.dpapi').read_bytes().startswith(b'fixture target user encrypted:')


@pytest.mark.parametrize('rows', [
    [row('../outside')], [row('C:/outside')], [row('CON')],
    [row('.env'), row('.ENV')], [dict(row('.env'), payload='!not-base64')],
    [dict(row('.env'), protection='unsupported')],
])
def test_all_secret_records_are_validated_before_creating_destination(tmp_path, rows):
    target = tmp_path / 'stage'
    with pytest.raises(BackupError):
        stage_portable_secrets(bundle(rows), target)
    assert not target.exists()


def test_missing_or_failed_target_dpapi_leaves_no_partial_plaintext(tmp_path):
    target = tmp_path / 'stage'
    payload = bundle([row('.env'),row('captcha/key.dpapi', protection='dpapi')])
    with pytest.raises(BackupError, match='encryption_required'):
        stage_portable_secrets(payload, target)
    assert not target.exists()
    with pytest.raises(BackupError, match='encryption_failed'):
        stage_portable_secrets(payload, target, dpapi_encrypt=lambda _: (_ for _ in ()).throw(OSError('fixture secret')))
    assert not target.exists()


def test_existing_destination_is_never_overwritten(tmp_path):
    target = tmp_path / 'existing'
    target.mkdir()
    (target / '.env').write_bytes(b'existing fixture')
    with pytest.raises(BackupError, match='stage_exists'):
        stage_portable_secrets(bundle([row('.env')]), target)
    assert (target / '.env').read_bytes() == b'existing fixture'
