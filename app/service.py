from __future__ import annotations

import re
from html import escape

from app.config import Settings
from app.db import Database


class NotificationDeliveryError(RuntimeError):
    pass


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

    async def process(self, payload: dict, header_event: str | None = None) -> int:
        # The signed Plane header is authoritative and also works around older
        # v2 builds that serialized an internal enum name in payload.event.
        event_name = str(header_event or payload.get("event") or "")
        if event_name not in {
            "workitem.created",
            "workitem.updated",
            "workitem.comment.created",
        }:
            return 0
        if payload.get("version") != "v2":
            return 0

        event_id = str(payload.get("event_id") or "")
        if not event_id:
            raise ValueError("Plane v2 payload has no event_id")

        data = payload.get("data") or {}
        if event_name == "workitem.comment.created":
            recipients = self._comment_recipients(payload)
            message, markup = self._comment_message(data)
        else:
            recipients = new_assignee_ids(payload, event_name)
            message, markup = self._message(data, event_name)
        sent = 0
        failures: list[str] = []

        for plane_user_id in recipients:
            if self.database.notification_was_sent(event_id, plane_user_id):
                continue
            link = self.database.get_active_link(plane_user_id)
            if not link:
                continue
            try:
                await self.telegram.send_message(
                    int(link["telegram_chat_id"]), message, reply_markup=markup
                )
            except Exception as exc:  # Telegram errors must trigger a Plane retry.
                failures.append(f"{plane_user_id}: {exc}")
                continue
            self.database.mark_notification_sent(event_id, plane_user_id)
            sent += 1

        if failures:
            raise NotificationDeliveryError("; ".join(failures))
        return sent
