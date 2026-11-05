from __future__ import annotations

import httpx


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, bot_token: str):
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.http = httpx.AsyncClient(timeout=15)

    async def close(self) -> None:
        await self.http.aclose()

    async def _call(self, method: str, payload: dict) -> dict:
        response = await self.http.post(f"{self.base_url}/{method}", json=payload)
        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramError(
                f"Telegram returned HTTP {response.status_code}"
            ) from exc
        if not response.is_success or not body.get("ok"):
            description = body.get("description", f"HTTP {response.status_code}")
            raise TelegramError(str(description))
        return body

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
    ) -> None:
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self._call("sendMessage", payload)

    async def set_webhook(self, url: str, secret_token: str) -> None:
        await self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": ["message"],
                "drop_pending_updates": False,
            },
        )
