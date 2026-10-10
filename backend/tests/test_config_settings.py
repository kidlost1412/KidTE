import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from config_settings import (
    save_api_key, save_audio_device, save_knowledge_source, save_model,
    save_tiktok_identity,
)


def test_save_model_preserves_secret_without_returning_it(tmp_path):
    env = tmp_path / ".env"
    env.write_text("DEEPSEEK_API_KEY=sk-secret\nDEEPSEEK_MODEL=old\n", encoding="utf-8")
    result = save_model("deepseek-v4-pro", env)
    assert result == {"ok": True, "model": "deepseek-v4-pro", "restart_required": False}
    content = env.read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY=sk-secret" in content
    assert "DEEPSEEK_MODEL=deepseek-v4-pro" in content
    assert "sk-secret" not in str(result)


@pytest.mark.parametrize("bad", ["", "x\ny", "x=y", "x\0y"])
def test_save_model_rejects_unsafe_values(tmp_path, bad):
    with pytest.raises(ValueError):
        save_model(bad, tmp_path / ".env")


def test_save_api_key_returns_only_boolean(tmp_path):
    result = save_api_key("secret-key-value", tmp_path / ".env")
    assert result["api_key_configured"] is True
    assert "secret-key-value" not in str(result)
    assert "secret-key-value" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_save_audio_device_preserves_other_settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text("DEEPSEEK_API_KEY=keep-me\nLIVE_RE_AUDIO_DEVICE=auto\n",
                   encoding="utf-8")
    result = save_audio_device("loopback-2", env)
    content = env.read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY=keep-me" in content
    assert "KID_TE_AUDIO_DEVICE=loopback-2" in content
    assert "LIVE_RE_AUDIO_DEVICE=" not in content
    assert result["audio_device_id"] == "loopback-2"


@pytest.mark.parametrize("bad", ["", "x\ny", "x\0y"])
def test_save_audio_device_rejects_unsafe_values(tmp_path, bad):
    with pytest.raises(ValueError):
        save_audio_device(bad, tmp_path / ".env")


def test_save_tiktok_identity_is_atomic_and_normalized(tmp_path):
    env = tmp_path / ".env"
    env.write_text("DEEPSEEK_API_KEY=keep-me\n", encoding="utf-8")
    result = save_tiktok_identity(" 123, 456 ", "MyShop, shop_2", env)
    content = env.read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY=keep-me" in content
    assert "TIKTOK_HOST_USER_IDS=123,456" in content
    assert "TIKTOK_HOST_UNIQUE_IDS=MyShop,shop_2" in content
    assert result["restart_required"] is True


def test_knowledge_source_rejects_http_and_accepts_network_path(tmp_path):
    with pytest.raises(ValueError, match="HTTPS"):
        save_knowledge_source("http://unsafe.test/package.json", tmp_path / ".env")
    result = save_knowledge_source(r"\\server\share\knowledge.json", tmp_path / ".env")
    assert result["knowledge_source_saved"] is True
