from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    telegram_bot_token: str
    telegram_webhook_secret: str = Field(min_length=16)
    telegram_bot_username: str | None = None
    public_base_url: str | None = None

    plane_webhook_secret: str = Field(min_length=16)
    plane_app_url: str
    plane_workspace_slug: str
    work_item_url_template: str = (
        "{base}/{workspace_slug}/projects/{project_id}/issues/{work_item_id}"
    )

    database_path: str = "data/notifier.sqlite3"
    admin_telegram_user_ids: str = ""
    link_code_ttl_minutes: int = Field(default=60, ge=5, le=10080)
    log_level: str = "INFO"

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
