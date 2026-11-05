import pytest
from pydantic import ValidationError

from app.config import Settings


def base_settings(**overrides):
    values = {
        "telegram_bot_token": "token",
        "telegram_webhook_secret": "telegram-secret-long",
        "plane_webhook_secret": "plane-secret-long-enough",
        "plane_app_url": "https://plane.example.com",
        "plane_workspace_slug": "acme",
    }
    values.update(overrides)
    return Settings(**values)


def test_socks5_proxy_is_accepted():
    settings = base_settings(telegram_proxy_url="socks5://proxy.example.com:1080")
    assert settings.telegram_proxy_url == "socks5://proxy.example.com:1080"


def test_non_socks_proxy_is_rejected():
    with pytest.raises(ValidationError):
        base_settings(telegram_proxy_url="http://proxy.example.com:8080")


def test_polling_does_not_require_public_url_or_telegram_secret():
    settings = base_settings(telegram_webhook_secret=None)
    assert settings.telegram_update_mode == "polling"


def test_webhook_mode_requires_public_url_and_secret():
    with pytest.raises(ValidationError):
        base_settings(
            telegram_update_mode="webhook",
            telegram_webhook_secret=None,
            public_base_url=None,
        )
