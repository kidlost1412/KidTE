import base64
import json

import pytest

from backup_crypto import encrypt_secrets, decrypt_secrets


def test_secret_roundtrip_with_portable_password_and_random_salt_nonce():
    plaintext = 'bí mật fixture không phải token thật'.encode()
    first = encrypt_secrets(plaintext, 'mật khẩu fixture', backup_id='one')
    second = encrypt_secrets(plaintext, 'mật khẩu fixture', backup_id='one')
    assert plaintext not in first
    assert first != second
    assert decrypt_secrets(first, 'mật khẩu fixture', backup_id='one') == plaintext
    header = json.loads(first)
    assert header['n'] == 32768 and header['r'] == 8 and header['p'] == 1
    assert len(base64.b64decode(header['nonce'])) == 12


def test_wrong_password_and_ciphertext_changes_are_rejected():
    blob = encrypt_secrets(b'fixture', 'password', backup_id='one')
    with pytest.raises(ValueError, match='authentication_failed'):
        decrypt_secrets(blob, 'different', backup_id='one')
    body = json.loads(blob)
    changed = bytearray(base64.b64decode(body['ciphertext']))
    changed[0] ^= 1
    body['ciphertext'] = base64.b64encode(changed).decode()
    with pytest.raises(ValueError, match='authentication_failed'):
        decrypt_secrets(json.dumps(body).encode(), 'password', backup_id='one')


@pytest.mark.parametrize('field,value', [('n', 2**30), ('n', True), ('p', 1000), ('schema_version', 2),
    ('cipher', 'plaintext'), ('nonce', 'invalid'), ('salt', 'AA=='), ('extra', 'untrusted')])
def test_invalid_headers_cannot_increase_kdf_work(monkeypatch, field, value):
    body = json.loads(encrypt_secrets(b'fixture', 'password', backup_id='one'))
    body[field] = value
    monkeypatch.setattr('backup_crypto._key', lambda *args: pytest.fail('Invalid header must not derive key'))
    with pytest.raises(ValueError, match='envelope_invalid'):
        decrypt_secrets(json.dumps(body).encode(), 'password', backup_id='one')


def test_envelope_cannot_be_moved_to_different_backup_identity():
    blob = encrypt_secrets(b'fixture', 'password', backup_id='one')
    with pytest.raises(ValueError, match='envelope_invalid'):
        decrypt_secrets(blob, 'password', backup_id='two')


def test_duplicate_header_fields_are_rejected():
    blob = encrypt_secrets(b'fixture', 'password', backup_id='one')
    duplicated = b'{"n":32768,' + blob[1:]
    with pytest.raises(ValueError, match='envelope_invalid'):
        decrypt_secrets(duplicated, 'password', backup_id='one')


@pytest.mark.parametrize('password', ['', None, 123, 'x'*4097])
def test_missing_or_invalid_password_is_rejected(password):
    with pytest.raises(ValueError, match='password_required'):
        encrypt_secrets(b'fixture', password, backup_id='one')
