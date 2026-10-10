import os
import pytest
from captcha_upload import CaptchaUploads, MAX_AGE_S, UNSENT_MAX_AGE_S


def recording(tmp_path, name='screen-test.mp4', created=100):
    folder = tmp_path / 'screen-recordings'
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_bytes(b'fake-video')
    os.utime(path, (created, created))
    return path


def test_failed_upload_retries_durably_and_deletes_only_after_true_receipt(tmp_path):
    now = [100.0]
    path = recording(tmp_path)
    queue = CaptchaUploads(tmp_path, clock=lambda: now[0])
    calls = []
    def upload(file):
        calls.append(file)
        return {'ok': False}
    queue.tick(upload)
    assert path.exists()
    assert queue.db.execute('SELECT next_attempt FROM uploads').fetchone()[0] == 130
    queue.close()
    queue = CaptchaUploads(tmp_path, clock=lambda: now[0])
    assert queue.tick(upload) is False
    now[0] = 130
    queue.tick(upload)
    assert queue.db.execute('SELECT next_attempt FROM uploads').fetchone()[0] == 250
    now[0] = 250
    queue.tick(lambda file: {'ok': True, 'result': {'message_id': 42}})
    assert not path.exists()
    assert queue.db.execute('SELECT message_id FROM uploads').fetchone()[0] == '42'
    queue.close()


def test_success_persisted_before_file_deletion_failure_never_uploads_again(tmp_path, monkeypatch):
    path = recording(tmp_path)
    queue = CaptchaUploads(tmp_path, clock=lambda: 100)
    unlink = type(path).unlink
    def locked(self, *args, **kwargs):
        if self == path: raise PermissionError('busy')
        return unlink(self, *args, **kwargs)
    monkeypatch.setattr(type(path), 'unlink', locked)
    queue.tick(lambda file: {'ok': True})
    assert queue.db.execute('SELECT state FROM uploads').fetchone()[0] == 'sent'
    queue.close()
    monkeypatch.setattr(type(path), 'unlink', unlink)
    queue = CaptchaUploads(tmp_path, clock=lambda: 101)
    assert queue.tick(lambda file: pytest.fail('duplicate HTTP')) is False
    assert not path.exists()
    queue.close()


def test_in_progress_capture_is_not_uploaded_and_old_failed_file_expires(tmp_path):
    partial = recording(tmp_path, 'screen-active.recording.mp4')
    expired = recording(tmp_path, created=1)
    queue = CaptchaUploads(tmp_path, clock=lambda: MAX_AGE_S + 2)
    assert queue.tick(lambda file: pytest.fail('expired/partial upload')) is False
    assert partial.exists()
    assert not expired.exists()
    with pytest.raises(ValueError): queue.file('../outside.mp4')
    queue.close()


@pytest.mark.parametrize('ok', [False, None, '', 1, 'true'])
def test_non_boolean_receipt_is_not_delivery_proof(tmp_path, ok):
    path = recording(tmp_path)
    queue = CaptchaUploads(tmp_path, clock=lambda: 100)
    queue.tick(lambda file: {'ok': ok})
    assert path.exists()
    assert queue.db.execute('SELECT state FROM uploads').fetchone()[0] == 'pending'
    queue.close()


def test_retry_schedule_and_crash_during_http(tmp_path):
    now = [100.0]
    recording(tmp_path)
    queue = CaptchaUploads(tmp_path, clock=lambda: now[0])
    for delay in [30, 120, 600, 1800, 1800]:
        with queue.db:  # keep the row fresh: this test is about the retry schedule, not expiry
            queue.db.execute('UPDATE uploads SET created_at=?', (now[0],))
        queue.tick(lambda file: {'ok': False})
        next_attempt = queue.db.execute('SELECT next_attempt FROM uploads').fetchone()[0]
        assert next_attempt == now[0] + delay
        now[0] = next_attempt
    with queue.db:
        queue.db.execute("UPDATE uploads SET state='sending'")
    queue.close()
    queue = CaptchaUploads(tmp_path, clock=lambda: now[0])
    row = queue.db.execute('SELECT state,next_attempt FROM uploads').fetchone()
    assert row['state'] == 'pending'
    assert row['next_attempt'] == now[0] + 30
    queue.close()


def test_http_error_never_returns_success_body_to_queue(tmp_path, monkeypatch):
    from captcha_upload import telegram_upload
    import captcha_secret
    import requests
    from types import SimpleNamespace
    path = recording(tmp_path)
    monkeypatch.setattr(captcha_secret, 'load_telegram', lambda folder: ('mock-token', 'mock-chat'))
    def reject(): raise requests.HTTPError('mock HTTP 500')
    monkeypatch.setattr(requests, 'post', lambda *args, **kwargs: SimpleNamespace(
        raise_for_status=reject, json=lambda: pytest.fail('HTTP error body is not success')))
    with pytest.raises(requests.HTTPError): telegram_upload(tmp_path, path)


def test_abandoned_partial_is_cleaned_only_when_recorder_mutex_is_free(tmp_path, monkeypatch):
    import captcha_upload
    from contextlib import contextmanager
    partial = recording(tmp_path, 'screen-abandoned.recording.mp4', created=1)
    queue = CaptchaUploads(tmp_path, clock=lambda: MAX_AGE_S + 2)
    @contextmanager
    def active(): yield False
    monkeypatch.setattr(captcha_upload, 'recorder_idle', active)
    queue.cleanup()
    assert partial.exists()
    @contextmanager
    def idle(): yield True
    monkeypatch.setattr(captcha_upload, 'recorder_idle', idle)
    queue.cleanup()
    assert not partial.exists()
    queue.close()


def test_bridge_reports_queue_without_resetting_sending_or_creating_database(tmp_path):
    from captcha_bridge import CaptchaBridge
    bridge = CaptchaBridge(tmp_path)
    assert bridge._upload_status()['pending'] == 0
    assert not (tmp_path / 'captcha-uploads.sqlite3').exists()
    recording(tmp_path)
    queue = CaptchaUploads(tmp_path, clock=lambda: 100)
    queue.discover()
    with queue.db: queue.db.execute("UPDATE uploads SET state='sending'")
    status = bridge._upload_status()
    assert status['sending'] == 1 and status['pending'] == 0
    assert status['bytes_used'] == len(b'fake-video')
    assert queue.db.execute('SELECT state FROM uploads').fetchone()[0] == 'sending'
    queue.close()


def test_unsent_recording_expires_after_30_minutes_and_is_never_attempted(tmp_path):
    now = [100.0]
    path = recording(tmp_path, created=100)
    queue = CaptchaUploads(tmp_path, clock=lambda: now[0])
    assert UNSENT_MAX_AGE_S == 1800
    queue.tick(lambda file: {'ok': False})
    assert path.exists()
    now[0] = 100 + UNSENT_MAX_AGE_S - 1
    with queue.db: queue.db.execute("UPDATE uploads SET next_attempt=0")
    assert queue.tick(lambda file: {'ok': False}) is True
    with queue.db: queue.db.execute("UPDATE uploads SET next_attempt=0")
    now[0] = 100 + UNSENT_MAX_AGE_S
    assert queue.tick(lambda file: pytest.fail('expired upload attempted')) is False
    assert not path.exists()
    row = queue.db.execute('SELECT state,error_code FROM uploads').fetchone()
    assert (row['state'], row['error_code']) == ('deleted', 'expired_unsent')
    queue.close()


def test_expired_pending_row_is_not_picked_even_if_file_cannot_be_deleted(tmp_path, monkeypatch):
    path = recording(tmp_path, created=1)
    queue = CaptchaUploads(tmp_path, clock=lambda: 5000)
    unlink = type(path).unlink
    def locked(self, *args, **kwargs):
        if self == path: raise PermissionError('busy')
        return unlink(self, *args, **kwargs)
    monkeypatch.setattr(type(path), 'unlink', locked)
    assert queue.tick(lambda file: pytest.fail('expired upload attempted')) is False
    assert path.exists()
    assert queue.db.execute('SELECT state FROM uploads').fetchone()[0] == 'deleted'
    monkeypatch.setattr(type(path), 'unlink', unlink)
    assert queue.tick(lambda file: pytest.fail('expired upload attempted')) is False
    assert not path.exists()
    queue.close()


def test_sending_file_is_not_deleted_mid_upload(tmp_path):
    now = [100.0]
    path = recording(tmp_path, created=100)
    queue = CaptchaUploads(tmp_path, clock=lambda: now[0])
    def upload(file):
        now[0] = 100 + UNSENT_MAX_AGE_S + 60
        queue.cleanup()
        assert file.exists()
        return {'ok': False}
    queue.tick(upload)
    assert path.exists()
    queue.close()


def test_old_deleted_rows_are_purged_after_max_age(tmp_path):
    now = [100.0]
    recording(tmp_path, created=100)
    queue = CaptchaUploads(tmp_path, clock=lambda: now[0])
    now[0] = 100 + UNSENT_MAX_AGE_S
    queue.tick(lambda file: pytest.fail('expired'))
    assert queue.db.execute('SELECT COUNT(*) FROM uploads').fetchone()[0] == 1
    now[0] = 100 + MAX_AGE_S
    queue.cleanup()
    assert queue.db.execute('SELECT COUNT(*) FROM uploads').fetchone()[0] == 0
    queue.close()
