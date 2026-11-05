import asyncio

import httpx
import pytest

from app.telegram import TelegramClient, TelegramError


def test_network_error_does_not_expose_bot_token():
    token = "123456:very-secret-token"

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"Cannot connect to {request.url}", request=request)

    async def run() -> str:
        client = TelegramClient(token)
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(TelegramError) as error:
                await client.send_message(123, "test")
            return str(error.value)
        finally:
            await client.close()

    message = asyncio.run(run())
    assert token not in message
    assert "<redacted>" in message
