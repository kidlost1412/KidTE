import sys
import types
import wave

import numpy as np
import pytest
import transcription_client
import transcription_worker


def _result(segments=None):
    return {"protocol": 1, "ok": True, "segments": segments or [], "info": {
        "language": "vi", "language_probability": 0.98, "device": "cpu",
        "compute_type": "int8", "model": "C:/models/local",
    }}


def _worker_setup(tmp_path, monkeypatch):
    audio = tmp_path / "speech.wav"
    with wave.open(str(audio), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(np.zeros(1600, dtype="<i2").tobytes())
    model = tmp_path / "model"
    model.mkdir()
    for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
        (model / name).write_bytes(b"local")
    return audio, model


def test_client_rejects_malformed_worker_response():
    with pytest.raises(RuntimeError, match="protocol_invalid"):
        transcription_client._validate_response({"protocol": 2, "ok": True})
    payload = _result([{"start": 1, "end": 0, "text": "bad interval"}])
    with pytest.raises(RuntimeError, match="segment_invalid"):
        transcription_client._validate_response(payload)


def test_client_reports_missing_component_and_failed_process(tmp_path, monkeypatch):
    wav = tmp_path / "input.wav"
    wav.write_bytes(b"wav")
    model = tmp_path / "model"
    model.mkdir()
    monkeypatch.setitem(sys.modules, "component_runtime", types.SimpleNamespace(
        component_path=lambda *_: None,
    ))
    with pytest.raises(RuntimeError, match="package_unavailable"):
        transcription_client.transcribe(wav, model)

    worker = tmp_path / "KidTETranscription.exe"
    worker.write_bytes(b"stub")
    monkeypatch.setitem(sys.modules, "component_runtime", types.SimpleNamespace(
        component_path=lambda *_: worker,
    ))
    monkeypatch.setattr(transcription_client, "_run_worker", lambda *a: (3, b""))
    with pytest.raises(RuntimeError, match="worker_exit:3"):
        transcription_client.transcribe(wav, model)


def test_worker_requires_explicit_local_model_and_disables_download(tmp_path, monkeypatch):
    audio, model = _worker_setup(tmp_path, monkeypatch)
    calls = {}

    class FakeModel:
        def __init__(self, model_path, **kwargs):
            calls["model_path"] = model_path
            calls.update(kwargs)

        def transcribe(self, audio_data, **kwargs):
            calls["audio"] = audio_data
            return [types.SimpleNamespace(start=0, end=0.1, text=" local ")], types.SimpleNamespace(
                language="vi", language_probability=0.99)

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(
        WhisperModel=FakeModel,
    ))
    monkeypatch.setattr("gpu_runtime.register_cuda_dll_dirs", lambda: False)
    result = transcription_worker._transcribe({
        "audio_wav": str(audio.resolve()), "model_path": str(model.resolve()),
    })
    assert result["ok"] is True
    assert result["segments"][0]["text"] == "local"
    assert calls["local_files_only"] is True
    assert calls["model_path"] == str(model.resolve())
    assert isinstance(calls["audio"], np.ndarray)


def test_worker_rejects_nonlocal_model_reference(tmp_path):
    with pytest.raises(ValueError, match="local_model_missing_or_invalid"):
        transcription_worker._validate_model(tmp_path / "model-id")
