from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response

from app.config import get_settings
from app.db import Database
from app.security import constant_time_equal, verify_plane_signature
from app.service import AssigneeNotifier, NotificationDeliveryError
from app.telegram import TelegramClient, TelegramError

settings = get_settings()
logging.basicConfig(level=settings.log_level.upper())
logger = logging.getLogger(__name__)

database = Database(settings.database_path)
telegram = TelegramClient(settings.telegram_bot_token)
notifier = AssigneeNotifier(settings, database, telegram)


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.initialize()
    if settings.telegram_webhook_url:
        await telegram.set_webhook(
            settings.telegram_webhook_url, settings.telegram_webhook_secret
        )
        logger.info("Telegram webhook configured")
    else:
        logger.warning("PUBLIC_BASE_URL is empty; Telegram commands will not arrive")
    yield
    await telegram.close()


app = FastAPI(
    title="Plane Telegram Assignee Notifier", version="0.1.0", lifespan=lifespan
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/webhooks/plane")
async def plane_webhook(
    request: Request,
    x_plane_signature: str | None = Header(default=None),
    x_plane_event: str | None = Header(default=None),
) -> dict:
    raw_body = await request.body()
    if not verify_plane_signature(
        raw_body, settings.plane_webhook_secret, x_plane_signature
    ):
        raise HTTPException(status_code=401, detail="Invalid Plane signature")
    try:
        payload = await request.json()
        sent = await notifier.process(payload, x_plane_event)
    except NotificationDeliveryError as exc:
        logger.warning("Telegram delivery failed: %s", exc)
        raise HTTPException(status_code=502, detail="Telegram delivery failed") from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "sent": sent}


def _command_parts(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    if not parts:
        return "", []
    command = parts[0].split("@", 1)[0].lower()
    return command, parts[1:]


async def _connect(chat: dict, sender: dict, code: str) -> None:
    if chat.get("type") != "private":
        await telegram.send_message(
            int(chat["id"]), "Привязку аккаунта нужно выполнить в личном чате с ботом."
        )
        return
    plane_user_id = database.redeem_link_code(
        code,
        int(chat["id"]),
        int(sender["id"]),
        sender.get("username"),
    )
    if not plane_user_id:
        await telegram.send_message(
            int(chat["id"]), "Код недействителен, уже использован или истёк."
        )
        return
    await telegram.send_message(
        int(chat["id"]),
        "✅ Аккаунт подключён. Теперь новые задачи, назначенные вам в Plane, "
        "будут приходить сюда.",
    )


async def _handle_message(message: dict) -> None:
    text = message.get("text")
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if not text or not chat.get("id") or not sender.get("id"):
        return
    command, arguments = _command_parts(str(text))
    chat_id = int(chat["id"])

    if command == "/start":
        if arguments:
            await _connect(chat, sender, arguments[0])
            return
        link = database.get_link_by_chat(chat_id)
        if link:
            database.set_active_by_chat(chat_id, True)
            await telegram.send_message(
                chat_id, "✅ Уведомления включены. Команда /status покажет состояние."
            )
        else:
            await telegram.send_message(
                chat_id,
                "Этот бот присылает личные уведомления о задачах Plane. "
                "Получите одноразовую ссылку у администратора или используйте "
                "<code>/connect КОД</code>.",
            )
        return

    if command == "/connect":
        if not arguments:
            await telegram.send_message(
                chat_id, "Использование: <code>/connect КОД</code>"
            )
        else:
            await _connect(chat, sender, arguments[0])
        return

    if command == "/status":
        link = database.get_link_by_chat(chat_id)
        if not link:
            await telegram.send_message(chat_id, "Аккаунт Plane ещё не подключён.")
        else:
            state = "включены" if link["active"] else "отключены"
            await telegram.send_message(
                chat_id,
                f"Уведомления: <b>{state}</b>\nPlane user ID: "
                f"<code>{link['plane_user_id']}</code>",
            )
        return

    if command == "/stop":
        if database.set_active_by_chat(chat_id, False):
            await telegram.send_message(
                chat_id,
                "Уведомления отключены. Чтобы включить их снова, отправьте /start.",
            )
        else:
            await telegram.send_message(chat_id, "Аккаунт Plane ещё не подключён.")
        return

    if command == "/linkcode":
        if int(sender["id"]) not in settings.admin_ids:
            await telegram.send_message(
                chat_id, "Команда доступна только администратору."
            )
            return
        if not arguments:
            await telegram.send_message(
                chat_id, "Использование: <code>/linkcode PLANE_USER_UUID</code>"
            )
            return
        code = database.create_link_code(
            arguments[0], settings.link_code_ttl_minutes
        )
        if settings.telegram_bot_username:
            link = f"https://t.me/{settings.telegram_bot_username}?start={code}"
            answer = f"Одноразовая ссылка:\n{link}"
        else:
            answer = f"Одноразовый код: <code>{code}</code>"
        await telegram.send_message(chat_id, answer)
        return

    if command in {"/help", "/settings"}:
        await telegram.send_message(
            chat_id,
            "Команды:\n/start — включить уведомления\n"
            "/connect КОД — подключить Plane\n/status — состояние\n"
            "/stop — отключить уведомления",
        )


@app.post("/webhooks/telegram", status_code=204)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    if not constant_time_equal(
        x_telegram_bot_api_secret_token, settings.telegram_webhook_secret
    ):
        raise HTTPException(status_code=401, detail="Invalid Telegram secret")
    update = await request.json()
    try:
        if update.get("message"):
            await _handle_message(update["message"])
    except TelegramError as exc:
        logger.warning("Telegram command response failed: %s", exc)
        raise HTTPException(status_code=502, detail="Telegram delivery failed") from exc
    return Response(status_code=204)
