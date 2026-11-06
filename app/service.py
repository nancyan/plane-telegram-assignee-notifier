from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from html import escape
from html.parser import HTMLParser

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


def _ids(values) -> set[str]:
    if values in (None, ""):
        return set()
    if isinstance(values, str | dict):
        values = [values]
    result: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            value = value.get("id") or value.get("user_id") or value.get("assignee_id")
        if value:
            result.add(str(value))
    return result


class _PlaneMentionParser(HTMLParser):
    """Extract Plane mention metadata from TipTap's custom HTML element."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.user_ids: set[str] = set()
        self.names: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "mention-component":
            return
        attributes = {
            key.casefold().replace("-", "_"): value for key, value in attrs if value
        }
        for key in (
            "entity_identifier",
            "data_entity_identifier",
            "user_id",
            "data_user_id",
            "member_id",
            "data_member_id",
        ):
            if attributes.get(key):
                self.user_ids.add(str(attributes[key]))
        for key in ("entity_name", "data_entity_name", "name", "data_name"):
            if attributes.get(key):
                normalized = Database.normalize_mention_name(attributes[key])
                if normalized:
                    self.names.add(normalized)


class _PlainTextParser(HTMLParser):
    """Convert the small HTML subset returned by Plane into readable text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value for key, value in attrs if value}
        if tag.casefold() == "mention-component":
            name = attributes.get("entity_name") or attributes.get("data-entity-name")
            if name:
                self.parts.append(f"@{name}")
        elif tag.casefold() == "br":
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"p", "div", "li", "blockquote", "h1", "h2", "h3"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join("".join(self.parts).split())


def _html_mentions(value: object) -> tuple[set[str], set[str]]:
    if not isinstance(value, str) or not value:
        return set(), set()
    parser = _PlaneMentionParser()
    try:
        parser.feed(value)
        parser.close()
    except (TypeError, ValueError):
        logger.warning("Could not parse Plane comment_html mention markup")
    return parser.user_ids, parser.names


def _html_to_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    parser = _PlainTextParser()
    try:
        parser.feed(value)
        parser.close()
    except (TypeError, ValueError):
        logger.warning("Could not convert Plane HTML content to text")
    return parser.text()


def _legacy_event_id(payload: dict) -> str:
    data = payload.get("data") or {}
    activity = payload.get("activity") or {}
    event = str(payload.get("event") or "")
    identity: dict = {
        "event": event,
        "action": payload.get("action"),
        "entity_id": data.get("id") or data.get("issue"),
    }
    if event == "issue":
        identity.update(
            {
                "updated_at": data.get("updated_at"),
                "field": activity.get("field"),
                "new_value": activity.get("new_value"),
            }
        )
    serialized = json.dumps(identity, sort_keys=True, default=str)
    return f"legacy-{hashlib.sha256(serialized.encode()).hexdigest()}"


def normalize_legacy_payload(payload: dict, header_event: str | None = None) -> dict:
    """Convert Plane's legacy issue webhook envelope to the internal v2 shape."""
    event = str(header_event or payload.get("event") or "")
    action = str(payload.get("action") or "").lower()
    data = payload.get("data") or {}
    activity = payload.get("activity") or {}
    event_id = _legacy_event_id({**payload, "event": event})

    if event == "issue":
        action_map = {
            "create": "workitem.created",
            "created": "workitem.created",
            "update": "workitem.updated",
            "updated": "workitem.updated",
        }
        canonical_event = action_map.get(action, f"legacy.issue.{action or 'unknown'}")
        current = _ids(data.get("assignees") or data.get("assignee_ids"))
        field_name = str(activity.get("field") or "")
        previous_attributes: dict = {}
        if canonical_event == "workitem.updated" and field_name in {
            "assignees",
            "assignee_ids",
            "assignee",
        }:
            previous_attributes["assignee_ids"] = sorted(
                _ids(activity.get("old_value"))
            )
            requested = _ids(activity.get("new_value"))
            if requested:
                current = requested

        project = data.get("project")
        if isinstance(project, dict):
            project = project.get("id")
        normalized_data = {
            **data,
            "project_id": project or data.get("project_id"),
            "assignee_ids": sorted(current),
        }
        return {
            "version": "v2",
            "event_id": event_id,
            "event": canonical_event,
            "data": normalized_data,
            "previous_attributes": previous_attributes,
            "legacy": True,
            "legacy_action": action,
        }

    if event == "issue_comment":
        canonical_event = (
            "workitem.comment.created"
            if action in {"create", "created"}
            else f"legacy.issue_comment.{action or 'unknown'}"
        )
        project = data.get("project")
        if isinstance(project, dict):
            project = project.get("id")
        actor = data.get("actor") or data.get("created_by")
        if isinstance(actor, dict):
            actor = actor.get("id")
        comment = {
            **data,
            "actor_id": actor,
            "comment_stripped": data.get("comment_stripped") or "",
        }
        return {
            "version": "v2",
            "event_id": event_id,
            "event": canonical_event,
            "data": {
                "id": data.get("issue"),
                "name": "Задача в Plane",
                "project_id": project or data.get("project_id"),
                "comment": comment,
            },
            "previous_attributes": {},
            "legacy": True,
            "legacy_action": action,
        }

    return payload


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
        html_ids, _ = _html_mentions(container.get("comment_html"))
        result.update(html_ids)
    return result


class AssigneeNotifier:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        telegram,
        plane_client=None,
    ):
        self.settings = settings
        self.database = database
        self.telegram = telegram
        self.plane_client = plane_client

    @staticmethod
    def _project_name(data: dict) -> str | None:
        for value in (
            data.get("project_name"),
            data.get("project_detail"),
            data.get("project"),
        ):
            if isinstance(value, dict):
                value = value.get("name")
            if value and str(value) != str(data.get("project_id") or ""):
                return str(value)
        return None

    @staticmethod
    def _label_names(data: dict) -> list[str]:
        names: list[str] = []
        for key in ("labels", "label_details", "label_data"):
            values = data.get(key) or []
            if isinstance(values, dict | str):
                values = [values]
            for value in values:
                if isinstance(value, dict):
                    value = value.get("name") or value.get("label")
                # UUID-only label lists do not contain a displayable name.
                if value and not re.fullmatch(
                    r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
                    str(value),
                ):
                    name = str(value)
                    if name not in names:
                        names.append(name)
        return names

    def _metadata_lines(self, data: dict) -> list[str]:
        lines: list[str] = []
        project_name = self._project_name(data)
        if project_name:
            lines.append(f"Проект: {escape(project_name)}")
        labels = self._label_names(data)
        if labels:
            lines.append(f"Теги: {escape(', '.join(labels))}")
        target_date = data.get("target_date") or data.get("due_date")
        if target_date not in (None, "None", ""):
            lines.append(f"Дедлайн: {escape(str(target_date))}")
        return lines

    @staticmethod
    def _excerpt(value: object, limit: int = 500) -> str:
        text = " ".join(str(value or "").split())
        if len(text) > limit:
            return f"{text[: limit - 3]}..."
        return text

    def _description_excerpt(self, data: dict) -> str:
        stripped = data.get("description_stripped")
        if stripped:
            return self._excerpt(stripped)
        description = data.get("description")
        if isinstance(description, str) and "<" not in description:
            return self._excerpt(description)
        html = data.get("description_html") or description
        return self._excerpt(_html_to_text(html))

    def _description_lines(self, data: dict) -> list[str]:
        excerpt = self._description_excerpt(data)
        if not excerpt:
            return []
        return ["", "<b>Описание:</b>", escape(excerpt)]

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
        reason = (
            "Вам назначена новая задача"
            if event_name == "workitem.created"
            else "Вас назначили исполнителем задачи"
        )
        lines = [f"<b>{escape(reason)}</b>", "", f"<b>{title}</b>"]
        if sequence not in (None, "None", ""):
            lines.append(f"Номер: #{escape(str(sequence))}")
        lines.extend(self._metadata_lines(data))
        lines.append(f"Приоритет: {priority}")
        lines.extend(self._description_lines(data))
        url = self._work_item_url(data)
        markup = {"inline_keyboard": [[{"text": "Открыть в Plane", "url": url}]]}
        return "\n".join(lines), markup

    def _comment_message(self, data: dict) -> tuple[str, dict]:
        title = escape(str(data.get("name") or "Без названия"))
        comment = data.get("comment") or {}
        comment_text = comment.get("comment_stripped") or _html_to_text(
            comment.get("comment_html")
        )
        excerpt = self._excerpt(comment_text)
        lines = ["<b>Вас упомянули в комментарии</b>", "", f"<b>{title}</b>"]
        lines.extend(self._metadata_lines(data))
        lines.extend(self._description_lines(data))
        if excerpt:
            lines.extend(["", "<b>Комментарий:</b>", escape(excerpt)])
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
        html_ids, html_names = _html_mentions(comment.get("comment_html"))
        recipients.update(html_ids)

        # Documented Plane v2 comment webhooks contain only stripped text. Match
        # admin-configured Plane mention names, including display names with spaces.
        mention_links = self.database.get_active_mention_links()
        for link in mention_links:
            alias = str(link["plane_mention_name"])
            pattern = rf"(?<![\w@])@{re.escape(alias)}(?![\w@])"
            if alias in html_names or re.search(pattern, text.casefold()):
                recipients.add(str(link["plane_user_id"]))

        logger.info(
            "Comment mention diagnostics stripped_length=%d html_present=%s "
            "html_mention_ids=%d html_mention_names=%d active_aliases=%d",
            len(text),
            bool(comment.get("comment_html")),
            len(html_ids),
            len(html_names),
            len(mention_links),
        )

        actor_id = comment.get("actor_id") or data.get("actor_id")
        if actor_id:
            recipients.discard(str(actor_id))
        return recipients

    async def process_detailed(
        self, payload: dict, header_event: str | None = None
    ) -> ProcessingResult:
        # The signed Plane header is authoritative and also works around older
        # v2 builds that serialized an internal enum name in payload.event.
        incoming_event = str(header_event or payload.get("event") or "")
        if payload.get("version") != "v2" and incoming_event in {
            "issue",
            "issue_comment",
        }:
            payload = normalize_legacy_payload(payload, incoming_event)
            header_event = None
            logger.info(
                "Normalized legacy Plane webhook incoming_event=%s action=%s "
                "canonical_event=%s generated_event_id=%s",
                incoming_event,
                payload.get("legacy_action", "<missing>"),
                payload.get("event"),
                payload.get("event_id"),
            )

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
        else:
            recipients = new_assignee_ids(payload, event_name)
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

        if self.plane_client:
            try:
                data = await self.plane_client.enrich_work_item(data)
            except Exception:
                logger.exception(
                    "Unexpected Plane API enrichment failure work_item_id=%s",
                    work_item_id or "<missing>",
                )

        if event_name == "workitem.comment.created":
            message, markup = self._comment_message(data)
        else:
            message, markup = self._message(data, event_name)

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
