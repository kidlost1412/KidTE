"""Link ho tro trang CAPTCHA chi mo cac URL https co trong allowlist OPEN_TARGETS."""
import sys
from pathlib import Path
from unittest import mock

import pytest


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from server import ReplyEngine  # noqa: E402

CAPTCHA_TARGETS = {
    "sadcaptcha_dashboard": "https://www.sadcaptcha.com/dashboard",
    "telegram_botfather": "https://t.me/BotFather",
    "telegram_userinfobot": "https://t.me/userinfobot",
}


@pytest.mark.parametrize("target,url", sorted(CAPTCHA_TARGETS.items()))
def test_captcha_helper_targets_open_in_default_browser(target, url):
    fake = type("Fake", (), {"OPEN_TARGETS": ReplyEngine.OPEN_TARGETS})()
    with mock.patch("webbrowser.open") as opened:
        result = ReplyEngine.open_target(fake, target)
    opened.assert_called_once_with(url)
    assert result == {"ok": True, "opened": url}


def test_unknown_target_is_rejected_and_nothing_opens():
    fake = type("Fake", (), {"OPEN_TARGETS": ReplyEngine.OPEN_TARGETS})()
    with mock.patch("webbrowser.open") as opened:
        with pytest.raises(ValueError):
            ReplyEngine.open_target(fake, "https://evil.example/")
    opened.assert_not_called()


def test_helper_urls_are_https_only():
    for target, url in CAPTCHA_TARGETS.items():
        assert ReplyEngine.OPEN_TARGETS[target] == url
        assert url.startswith("https://")
