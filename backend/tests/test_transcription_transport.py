import sys

import pytest
import transcription_client


def test_worker_transport_caps_actual_child_stdout(monkeypatch):
    monkeypatch.setattr(transcription_client, "MAX_RESPONSE_BYTES", 128)
    with pytest.raises(RuntimeError, match="result_too_large"):
        transcription_client._run_worker(sys.executable, b"{}", {},
                                        ("-c", "import sys; sys.stdout.buffer.write(b'x'*200000); sys.stdout.flush()"))


def test_worker_transport_drains_stderr_without_overwriting_json(monkeypatch):
    monkeypatch.setattr(transcription_client, "MAX_STDERR_BYTES", 128)
    return_code, raw = transcription_client._run_worker(sys.executable, b"{}", {},
        ("-c", "import sys; sys.stderr.buffer.write(b'x'*200000); sys.stderr.flush(); sys.stdout.buffer.write(b'{\"ok\":true}')"))
    assert return_code == 0
    assert raw == b'{"ok":true}'
