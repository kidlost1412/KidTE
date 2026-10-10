"""Versioned, authenticated secret envelope for portable backups.

Uses cryptography AESGCM/Scrypt, not an application-written cipher. Untrusted
envelopes cannot choose KDF cost parameters. No password/key is written here.
"""
import base64
import binascii
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAX_SECRET_BYTES = 2_000_000
_PARAMETERS = {'schema_version': 1, 'cipher': 'AES-256-GCM', 'kdf': 'scrypt',
               'n': 32768, 'r': 8, 'p': 1}


def _password_bytes(password):
    if not isinstance(password, str) or not password:
        raise ValueError('backup_password_required')
    try:
        encoded = password.encode('utf-8')
    except UnicodeError:
        raise ValueError('backup_password_required') from None
    if len(encoded) > 4096:
        raise ValueError('backup_password_required')
    return encoded


def _key(password, salt):
    return Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(_password_bytes(password))


def _encode(data):
    return base64.b64encode(data).decode('ascii')


def _decode(value):
    if not isinstance(value, str):
        raise ValueError('backup_secret_envelope_invalid')
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError('backup_secret_envelope_invalid') from exc


def _aad(header):
    return json.dumps(header, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def encrypt_secrets(data, password, *, backup_id):
    _password_bytes(password)
    if not isinstance(data, bytes) or len(data) > MAX_SECRET_BYTES:
        raise ValueError('backup_secret_payload_invalid')
    if not isinstance(backup_id, str) or not 1 <= len(backup_id) <= 160:
        raise ValueError('backup_secret_identity_invalid')
    salt, nonce = os.urandom(16), os.urandom(12)
    header = {**_PARAMETERS, 'backup_id': backup_id, 'salt': _encode(salt), 'nonce': _encode(nonce)}
    ciphertext = AESGCM(_key(password, salt)).encrypt(nonce, data, _aad(header))
    return json.dumps({**header, 'ciphertext': _encode(ciphertext)}, sort_keys=True, separators=(',', ':')).encode('utf-8')


def decrypt_secrets(envelope, password, *, backup_id):
    _password_bytes(password)
    if not isinstance(envelope, bytes) or len(envelope) > MAX_SECRET_BYTES * 2:
        raise ValueError('backup_secret_envelope_invalid')
    try:
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate_field')
                result[key] = value
            return result
        body = json.loads(envelope, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError) as exc:
        raise ValueError('backup_secret_envelope_invalid') from exc
    expected_keys = set(_PARAMETERS) | {'backup_id', 'salt', 'nonce', 'ciphertext'}
    if (not isinstance(body, dict) or set(body) != expected_keys or
        any(type(body[key]) is not type(value) or body[key] != value for key, value in _PARAMETERS.items()) or
        not isinstance(body.get('backup_id'), str) or body['backup_id'] != backup_id):
        raise ValueError('backup_secret_envelope_invalid')
    salt, nonce, ciphertext = (_decode(body[name]) for name in ('salt', 'nonce', 'ciphertext'))
    if len(salt) != 16 or len(nonce) != 12 or not 16 <= len(ciphertext) <= MAX_SECRET_BYTES + 16:
        raise ValueError('backup_secret_envelope_invalid')
    header = {key: value for key, value in body.items() if key != 'ciphertext'}
    try:
        return AESGCM(_key(password, salt)).decrypt(nonce, ciphertext, _aad(header))
    except InvalidTag as exc:
        # A wrong password and corrupted ciphertext are indistinguishable.
        raise ValueError('backup_secret_authentication_failed') from exc
