from types import SimpleNamespace

import pytest

import initial_components as setup


@pytest.mark.parametrize("output,eligible", [
    ("RTX 4060, 581.80, 8.9", True),
    ("GTX 970, 526.00, 5.2", False),
    ("Old GPU, 581.80, 3.0", False),
    ("bad output", False),
    ("Old GPU, 526.00, 3.0\nRTX 4060, 581.80, 8.9", True),
])
def test_gpu_detection_checks_driver_and_all_cards(monkeypatch, output, eligible):
    def run(command, **kwargs):
        assert kwargs["timeout"] == 8
        assert "--query-gpu=name,driver_version,compute_cap" in command
        return SimpleNamespace(returncode=0, stdout=output)
    monkeypatch.setattr(setup.subprocess, "run", run)
    assert setup.detect_gpu()["eligible"] is eligible


def test_missing_nvidia_driver_uses_cpu(monkeypatch):
    def run(*args, **kwargs):
        raise FileNotFoundError
    monkeypatch.setattr(setup.subprocess, "run", run)
    assert setup.detect_gpu()["eligible"] is False


def test_new_machine_selects_all_features_and_gpu_only_when_supported():
    catalog = {"components": [{"id": k, "version": "1.0.0"} for k in setup.FEATURES | {"gpu-cuda12"}]}
    assert set(setup.selection(catalog, {}, {"eligible": False})) == setup.FEATURES
    assert set(setup.selection(catalog, {}, {"eligible": True})) == setup.FEATURES | {"gpu-cuda12"}
    assert not setup.selection(catalog, {p["id"]: "2.0.0" for p in catalog["components"]}, {"eligible": True})


def test_initial_setup_marker_is_explicit_local_request_only(tmp_path):
    assert setup.read_request(tmp_path) is None
    marker = tmp_path / setup.MARKER
    for text in ('invalid', '[]', '{"schema_version":2}', 'x' * 20000):
        marker.write_text(text)
        assert setup.read_request(tmp_path) is None
    marker.write_text('{"schema_version":1,"status":"requested"}')
    assert setup.read_request(tmp_path)["status"] == "requested"
