from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    telegram_bot_token: str
    telegram_update_mode: Literal["polling", "webhook"] = "polling"
    telegram_webhook_secret: str | None = Field(default=None, min_length=16)
    telegram_bot_username: str | None = None
    telegram_proxy_url: str | None = None
    public_base_url: str | None = None

    plane_webhook_secret: str = Field(min_length=16)
    plane_app_url: str
    plane_workspace_slug: str
    plane_api_key: str | None = None
    work_item_url_template: str = (
        "{base}/{workspace_slug}/projects/{project_id}/issues/{work_item_id}"
    )

    database_path: str = "data/notifier.sqlite3"
    admin_telegram_user_ids: str = ""
    link_code_ttl_minutes: int = Field(default=60, ge=5, le=10080)
    log_level: str = "INFO"

    @field_validator("telegram_proxy_url")
    @classmethod
    def validate_telegram_proxy_url(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if not value.startswith("socks5://"):
            raise ValueError("TELEGRAM_PROXY_URL must start with socks5://")
        return value

    @model_validator(mode="after")
    def validate_webhook_settings(self):
        if self.telegram_update_mode == "webhook":
            if not self.public_base_url:
                raise ValueError("PUBLIC_BASE_URL is required in Telegram webhook mode")
            if not self.telegram_webhook_secret:
                raise ValueError(
                    "TELEGRAM_WEBHOOK_SECRET is required in Telegram webhook mode"
                )
        return self

    @property
    def admin_ids(self) -> set[int]:
        result: set[int] = set()
        for value in self.admin_telegram_user_ids.split(","):
            value = value.strip()
            if value:
                result.add(int(value))
        return result

    @property
    def telegram_webhook_url(self) -> str | None:
        if not self.public_base_url:
            return None
        return f"{self.public_base_url.rstrip('/')}/webhooks/telegram"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
