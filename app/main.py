from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Header, HTTPException, Request, Response

from app.config import get_settings
from app.db import Database
from app.plane import PlaneClient
from app.security import constant_time_equal, verify_plane_signature
from app.service import AssigneeNotifier, NotificationDeliveryError
from app.telegram import TelegramClient, TelegramError

settings = get_settings()
configured_log_level = settings.log_level.upper()
logging.basicConfig(level=configured_log_level)
# Uvicorn may configure handlers before importing the app, making basicConfig a
# no-op. Set the root level explicitly so LOG_LEVEL always takes effect.
logging.getLogger().setLevel(configured_log_level)
# httpx includes the bot token in its request URL. Keep its own access log off;
# app.telegram emits safe method/status diagnostics instead.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

database = Database(settings.database_path)
telegram = TelegramClient(
    settings.telegram_bot_token, proxy_url=settings.telegram_proxy_url
)
plane = (
    PlaneClient(
        settings.plane_app_url,
        settings.plane_workspace_slug,
        settings.plane_api_key,
    )
    if settings.plane_api_key
    else None
)
notifier = AssigneeNotifier(settings, database, telegram, plane_client=plane)


async def _poll_telegram_updates() -> None:
    offset: int | None = None
    while True:
        try:
            updates = await telegram.get_updates(offset=offset, timeout=30)
            if updates:
                logger.info("Telegram polling received updates count=%d", len(updates))
            for update in updates:
                try:
                    if update.get("message"):
                        await _handle_message(update["message"])
                except Exception:
                    logger.exception(
                        "Failed to process Telegram update %s", update.get("update_id")
                    )
                finally:
                    if update.get("update_id") is not None:
                        offset = int(update["update_id"]) + 1
        except asyncio.CancelledError:
            raise
        except TelegramError as exc:
            logger.warning("Telegram polling failed: %s; retrying", exc)
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.initialize()
    logger.info(
        "Notifier starting telegram_mode=%s proxy_configured=%s "
        "plane_workspace=%s plane_api_configured=%s database=%s log_level=%s",
        settings.telegram_update_mode,
        bool(settings.telegram_proxy_url),
        settings.plane_workspace_slug,
        bool(settings.plane_api_key),
        settings.database_path,
        settings.log_level.upper(),
    )
    polling_task: asyncio.Task | None = None
    if settings.telegram_update_mode == "webhook":
        assert settings.telegram_webhook_url
        assert settings.telegram_webhook_secret
        await telegram.set_webhook(
            settings.telegram_webhook_url, settings.telegram_webhook_secret
        )
        logger.info("Telegram webhook configured")
    else:
        await telegram.delete_webhook()
        polling_task = asyncio.create_task(_poll_telegram_updates())
        logger.info("Telegram long polling started")
    yield
    if polling_task:
        polling_task.cancel()
        with suppress(asyncio.CancelledError):
            await polling_task
    await telegram.close()
    if plane:
        await plane.close()


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
    client = request.client.host if request.client else "unknown"
    logger.info(
        "Plane webhook request received client=%s bytes=%d header_event=%s "
        "signature_present=%s",
        client,
        len(raw_body),
        x_plane_event or "<missing>",
        bool(x_plane_signature),
    )
    if not verify_plane_signature(
        raw_body, settings.plane_webhook_secret, x_plane_signature
    ):
        logger.warning(
            "Plane webhook signature rejected client=%s header_event=%s",
            client,
            x_plane_event or "<missing>",
        )
        raise HTTPException(status_code=401, detail="Invalid Plane signature")
    try:
        payload = await request.json()
        result = await notifier.process_detailed(payload, x_plane_event)
    except NotificationDeliveryError as exc:
        logger.warning("Telegram delivery failed: %s", exc)
        raise HTTPException(status_code=502, detail="Telegram delivery failed") from exc
    except (ValueError, TypeError) as exc:
        logger.warning("Invalid Plane webhook payload: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response = {"ok": True, **result.as_dict()}
    logger.info(
        "Plane webhook response event=%s event_id=%s status=%s sent=%d",
        result.event,
        result.event_id or "<missing>",
        result.status,
        result.sent,
    )
    return response


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
        logger.warning(
            "Telegram account link rejected telegram_user_id=%s chat_id=%s",
            sender.get("id"),
            chat.get("id"),
        )
        await telegram.send_message(
            int(chat["id"]), "Код недействителен, уже использован или истёк."
        )
        return
    logger.info(
        "Telegram account linked plane_user_id=%s telegram_user_id=%s chat_id=%s",
        plane_user_id,
        sender.get("id"),
        chat.get("id"),
    )
    await telegram.send_message(
        int(chat["id"]),
        "✅ Аккаунт подключён. Теперь новые задачи, назначенные вам в Plane, "
        "и упоминания в комментариях будут приходить сюда.",
    )


async def _handle_message(message: dict) -> None:
    text = message.get("text")
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if not text or not chat.get("id") or not sender.get("id"):
        return
    command, arguments = _command_parts(str(text))
    chat_id = int(chat["id"])
    logger.info(
        "Telegram command received command=%s telegram_user_id=%s chat_id=%s",
        command or "<none>",
        sender.get("id"),
        chat_id,
    )

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
                f"<code>{link['plane_user_id']}</code>\n"
                f"Имя для упоминаний: "
                f"<code>{link['plane_mention_name'] or 'не задано'}</code>",
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
                chat_id,
                "Использование: <code>/linkcode PLANE_USER_UUID @ИМЯ_В_PLANE</code>\n"
                "Имя необязательно, но нужно для уведомлений об упоминаниях.",
            )
            return
        code = database.create_link_code(
            arguments[0],
            settings.link_code_ttl_minutes,
            " ".join(arguments[1:]) if len(arguments) > 1 else None,
        )
        logger.info(
            "Admin created link code plane_user_id=%s mention_name_configured=%s",
            arguments[0],
            len(arguments) > 1,
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
    if settings.telegram_update_mode != "webhook":
        raise HTTPException(status_code=409, detail="Telegram polling mode is active")
    assert settings.telegram_webhook_secret
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
