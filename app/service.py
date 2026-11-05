from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from html import escape

from app.config import Settings
from app.db import Database

logger = logging.getLogger(__name__)


class NotificationDeliveryError(RuntimeError):
    pass


@dataclass
class ProcessingResult:
    event: str
    event_id: str
    work_item_id: str
    status: str
    recipients: list[str] = field(default_factory=list)
    sent: int = 0
    delivered_to: list[str] = field(default_factory=list)
    skipped_unlinked: list[str] = field(default_factory=list)
    skipped_duplicate: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def new_assignee_ids(payload: dict, event_name: str) -> set[str]:
    data = payload.get("data") or {}
    current = {str(value) for value in (data.get("assignee_ids") or []) if value}
    if event_name == "workitem.created":
        return current
    if event_name == "workitem.updated":
        previous_attributes = payload.get("previous_attributes") or {}
        if "assignee_ids" not in previous_attributes:
            return set()
        previous = {
            str(value)
            for value in (previous_attributes.get("assignee_ids") or [])
            if value
        }
        return current - previous
    return set()


def mentioned_user_ids(payload: dict) -> set[str]:
    """Read mention IDs exposed by some Plane builds and future payload versions."""
    data = payload.get("data") or {}
    comment = data.get("comment") or {}
    result: set[str] = set()
    for container in (data, comment):
        for key in (
            "mentioned_user_ids",
            "mention_ids",
            "mentions",
            "mentioned_users",
        ):
            values = container.get(key) or []
            if isinstance(values, str | dict):
                values = [values]
            for value in values:
                if isinstance(value, dict):
                    value = value.get("user_id") or value.get("id")
                if value:
                    result.add(str(value))
    return result


class AssigneeNotifier:
    def __init__(self, settings: Settings, database: Database, telegram):
        self.settings = settings
        self.database = database
        self.telegram = telegram

    def _work_item_url(self, data: dict) -> str:
        if not data.get("project_id") or not data.get("id"):
            return (
                f"{self.settings.plane_app_url.rstrip('/')}"
                f"/{self.settings.plane_workspace_slug}"
            )
        return self.settings.work_item_url_template.format(
            base=self.settings.plane_app_url.rstrip("/"),
            workspace_slug=self.settings.plane_workspace_slug,
            project_id=data.get("project_id", ""),
            work_item_id=data.get("id", ""),
            sequence_id=data.get("sequence_id", ""),
        )

    def _message(self, data: dict, event_name: str) -> tuple[str, dict]:
        title = escape(str(data.get("name") or "Без названия"))
        sequence = data.get("sequence_id")
        priority = escape(str(data.get("priority") or "none"))
        target_date = data.get("target_date")
        reason = (
            "Вам назначена новая задача"
            if event_name == "workitem.created"
            else "Вас назначили исполнителем задачи"
        )
        lines = [f"<b>{escape(reason)}</b>", "", f"<b>{title}</b>"]
        if sequence not in (None, "None", ""):
            lines.append(f"Номер: #{escape(str(sequence))}")
        lines.append(f"Приоритет: {priority}")
        if target_date not in (None, "None", ""):
            lines.append(f"Срок: {escape(str(target_date))}")
        url = self._work_item_url(data)
        markup = {"inline_keyboard": [[{"text": "Открыть в Plane", "url": url}]]}
        return "\n".join(lines), markup

    def _comment_message(self, data: dict) -> tuple[str, dict]:
        title = escape(str(data.get("name") or "Без названия"))
        comment = data.get("comment") or {}
        excerpt = " ".join(str(comment.get("comment_stripped") or "").split())
        if len(excerpt) > 500:
            excerpt = f"{excerpt[:497]}..."
        lines = ["<b>Вас упомянули в комментарии</b>", "", f"<b>{title}</b>"]
        if excerpt:
            lines.extend(["", escape(excerpt)])
        markup = {
            "inline_keyboard": [
                [{"text": "Открыть в Plane", "url": self._work_item_url(data)}]
            ]
        }
        return "\n".join(lines), markup

    def _comment_recipients(self, payload: dict) -> set[str]:
        recipients = mentioned_user_ids(payload)
        data = payload.get("data") or {}
        comment = data.get("comment") or {}
        text = str(comment.get("comment_stripped") or "")

        # Documented Plane v2 comment webhooks contain only stripped text. Match
        # admin-configured Plane mention names, including display names with spaces.
        for link in self.database.get_active_mention_links():
            alias = str(link["plane_mention_name"])
            pattern = rf"(?<![\w@])@{re.escape(alias)}(?![\w@])"
            if re.search(pattern, text.casefold()):
                recipients.add(str(link["plane_user_id"]))

        actor_id = comment.get("actor_id") or data.get("actor_id")
        if actor_id:
            recipients.discard(str(actor_id))
        return recipients

    async def process_detailed(
        self, payload: dict, header_event: str | None = None
    ) -> ProcessingResult:
        # The signed Plane header is authoritative and also works around older
        # v2 builds that serialized an internal enum name in payload.event.
        event_name = str(header_event or payload.get("event") or "")
        event_id = str(payload.get("event_id") or "")
        data = payload.get("data") or {}
        work_item_id = str(data.get("id") or "")
        result = ProcessingResult(
            event=event_name,
            event_id=event_id,
            work_item_id=work_item_id,
            status="received",
        )
        logger.info(
            "Processing Plane webhook event=%s event_id=%s version=%s "
            "work_item_id=%s header_event=%s",
            event_name or "<missing>",
            event_id or "<missing>",
            payload.get("version") or "<missing>",
            work_item_id or "<missing>",
            header_event or "<missing>",
        )
        if event_name not in {
            "workitem.created",
            "workitem.updated",
            "workitem.comment.created",
        }:
            result.status = "ignored_unsupported_event"
            logger.warning(
                "Ignoring unsupported Plane event event=%s event_id=%s",
                event_name or "<missing>",
                event_id or "<missing>",
            )
            return result
        if payload.get("version") != "v2":
            result.status = "ignored_unsupported_version"
            logger.warning(
                "Ignoring Plane webhook with unsupported version=%s event=%s",
                payload.get("version") or "<missing>",
                event_name,
            )
            return result

        if not event_id:
            logger.error("Plane v2 webhook has no event_id event=%s", event_name)
            raise ValueError("Plane v2 payload has no event_id")

        if event_name == "workitem.comment.created":
            recipients = self._comment_recipients(payload)
            message, markup = self._comment_message(data)
        else:
            recipients = new_assignee_ids(payload, event_name)
            message, markup = self._message(data, event_name)
        result.recipients = sorted(recipients)
        logger.info(
            "Resolved Plane recipients event=%s event_id=%s count=%d ids=%s",
            event_name,
            event_id,
            len(recipients),
            ",".join(result.recipients) or "<none>",
        )
        if not recipients:
            result.status = "no_recipients"
            logger.warning(
                "No notification recipients resolved event=%s event_id=%s "
                "work_item_id=%s",
                event_name,
                event_id,
                work_item_id or "<missing>",
            )
            return result

        sent = 0
        failures: list[str] = []

        for plane_user_id in sorted(recipients):
            if self.database.notification_was_sent(event_id, plane_user_id):
                result.skipped_duplicate.append(plane_user_id)
                logger.info(
                    "Skipping duplicate notification event_id=%s plane_user_id=%s",
                    event_id,
                    plane_user_id,
                )
                continue
            link = self.database.get_active_link(plane_user_id)
            if not link:
                result.skipped_unlinked.append(plane_user_id)
                logger.warning(
                    "No active Telegram link event_id=%s plane_user_id=%s",
                    event_id,
                    plane_user_id,
                )
                continue
            try:
                logger.info(
                    "Sending Telegram notification event_id=%s plane_user_id=%s "
                    "telegram_chat_id=%s",
                    event_id,
                    plane_user_id,
                    link["telegram_chat_id"],
                )
                await self.telegram.send_message(
                    int(link["telegram_chat_id"]), message, reply_markup=markup
                )
            except Exception as exc:  # Telegram errors must trigger a Plane retry.
                failures.append(f"{plane_user_id}: {exc}")
                logger.exception(
                    "Telegram delivery failed event_id=%s plane_user_id=%s",
                    event_id,
                    plane_user_id,
                )
                continue
            self.database.mark_notification_sent(event_id, plane_user_id)
            sent += 1
            result.delivered_to.append(plane_user_id)
            logger.info(
                "Telegram notification delivered event_id=%s plane_user_id=%s",
                event_id,
                plane_user_id,
            )

        if failures:
            raise NotificationDeliveryError("; ".join(failures))
        result.sent = sent
        result.status = "delivered" if sent else "processed_without_delivery"
        logger.info(
            "Plane webhook processed event=%s event_id=%s status=%s sent=%d "
            "unlinked=%d duplicates=%d",
            event_name,
            event_id,
            result.status,
            sent,
            len(result.skipped_unlinked),
            len(result.skipped_duplicate),
        )
        return result

    async def process(self, payload: dict, header_event: str | None = None) -> int:
        """Compatibility wrapper returning only the number of delivered messages."""
        result = await self.process_detailed(payload, header_event)
        return result.sent
