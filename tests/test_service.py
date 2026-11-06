import asyncio

from app.config import Settings
from app.db import Database
from app.service import (
    AssigneeNotifier,
    mentioned_user_ids,
    new_assignee_ids,
    normalize_legacy_payload,
)


class FakeTelegram:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, *, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))


class FakePlaneClient:
    def __init__(self, enrichment):
        self.enrichment = enrichment
        self.calls = []

    async def enrich_work_item(self, data):
        self.calls.append(data["id"])
        return {**data, **self.enrichment}


def make_payload(event, current, previous=None, event_id="event-1"):
    return {
        "version": "v2",
        "event_id": event_id,
        "event": event,
        "data": {
            "id": "work-item-1",
            "name": "Test task",
            "project_id": "project-1",
            "sequence_id": 42,
            "assignee_ids": current,
            "priority": "high",
        },
        "previous_attributes": previous or {},
    }


def test_created_notifies_all_assignees():
    payload = make_payload("workitem.created", ["u1", "u2"])
    assert new_assignee_ids(payload, "workitem.created") == {"u1", "u2"}


def test_updated_notifies_only_new_assignee():
    payload = make_payload("workitem.updated", ["u1", "u2"], {"assignee_ids": ["u1"]})
    assert new_assignee_ids(payload, "workitem.updated") == {"u2"}


def test_unrelated_update_notifies_nobody():
    payload = make_payload("workitem.updated", ["u1"], {"priority": "medium"})
    assert new_assignee_ids(payload, "workitem.updated") == set()


def test_delivery_is_deduplicated(tmp_path):
    settings = Settings(
        telegram_bot_token="token",
        telegram_webhook_secret="telegram-secret-long",
        plane_webhook_secret="plane-secret-long-enough",
        plane_app_url="https://plane.example.com",
        plane_workspace_slug="acme",
        database_path=str(tmp_path / "test.sqlite3"),
    )
    database = Database(settings.database_path)
    database.initialize()
    code = database.create_link_code("u1", 60)
    assert database.redeem_link_code(code, 123, 456, "alice") == "u1"
    telegram = FakeTelegram()
    notifier = AssigneeNotifier(settings, database, telegram)
    payload = make_payload("workitem.created", ["u1"])

    assert asyncio.run(notifier.process(payload)) == 1
    assert asyncio.run(notifier.process(payload)) == 0
    assert len(telegram.messages) == 1
    assert telegram.messages[0][0] == 123


def test_assignment_message_contains_project_labels_deadline_and_description(
    tmp_path,
):
    settings = Settings(
        telegram_bot_token="token",
        plane_webhook_secret="plane-secret-long-enough",
        plane_app_url="https://plane.example.com",
        plane_workspace_slug="acme",
        database_path=str(tmp_path / "test.sqlite3"),
    )
    database = Database(settings.database_path)
    database.initialize()
    code = database.create_link_code("u1", 60)
    database.redeem_link_code(code, 123, 456, "alice")
    telegram = FakeTelegram()
    payload = make_payload("workitem.created", ["u1"])
    payload["data"].update(
        {
            "project_name": "Website",
            "labels": [{"id": "label-1", "name": "Urgent"}],
            "target_date": "2026-12-01",
            "description_html": "<p>Fix the <strong>login</strong> form.</p>",
        }
    )

    notifier = AssigneeNotifier(settings, database, telegram)
    assert asyncio.run(notifier.process(payload)) == 1
    message = telegram.messages[0][1]
    assert "Проект: Website" in message
    assert "Теги: Urgent" in message
    assert "Дедлайн: 2026-12-01" in message
    assert "<b>Описание:</b>\nFix the login form." in message


def test_detailed_result_explains_unlinked_recipient(tmp_path):
    settings = Settings(
        telegram_bot_token="token",
        plane_webhook_secret="plane-secret-long-enough",
        plane_app_url="https://plane.example.com",
        plane_workspace_slug="acme",
        database_path=str(tmp_path / "test.sqlite3"),
    )
    database = Database(settings.database_path)
    database.initialize()
    notifier = AssigneeNotifier(settings, database, FakeTelegram())

    result = asyncio.run(
        notifier.process_detailed(make_payload("workitem.created", ["u1"]))
    )

    assert result.status == "processed_without_delivery"
    assert result.recipients == ["u1"]
    assert result.skipped_unlinked == ["u1"]
    assert result.sent == 0


def test_detailed_result_explains_unsupported_event(tmp_path):
    settings = Settings(
        telegram_bot_token="token",
        plane_webhook_secret="plane-secret-long-enough",
        plane_app_url="https://plane.example.com",
        plane_workspace_slug="acme",
        database_path=str(tmp_path / "test.sqlite3"),
    )
    database = Database(settings.database_path)
    database.initialize()
    notifier = AssigneeNotifier(settings, database, FakeTelegram())

    result = asyncio.run(notifier.process_detailed(make_payload("project.created", [])))

    assert result.status == "ignored_unsupported_event"
    assert result.sent == 0


def test_comment_mention_by_structured_user_id(tmp_path):
    settings = Settings(
        telegram_bot_token="token",
        plane_webhook_secret="plane-secret-long-enough",
        plane_app_url="https://plane.example.com",
        plane_workspace_slug="acme",
        database_path=str(tmp_path / "test.sqlite3"),
    )
    database = Database(settings.database_path)
    database.initialize()
    code = database.create_link_code("u1", 60)
    database.redeem_link_code(code, 123, 456, "alice")
    telegram = FakeTelegram()
    payload = make_payload("workitem.comment.created", [])
    payload["data"]["comment"] = {
        "actor_id": "u2",
        "comment_stripped": "hello",
        "mentioned_user_ids": ["u1"],
    }

    assert mentioned_user_ids(payload) == {"u1"}
    notifier = AssigneeNotifier(settings, database, telegram)
    assert asyncio.run(notifier.process(payload)) == 1
    assert "упомянули" in telegram.messages[0][1]


def test_comment_mention_by_plane_html_entity_identifier(tmp_path):
    settings = Settings(
        telegram_bot_token="token",
        plane_webhook_secret="plane-secret-long-enough",
        plane_app_url="https://plane.example.com",
        plane_workspace_slug="acme",
        database_path=str(tmp_path / "test.sqlite3"),
    )
    database = Database(settings.database_path)
    database.initialize()
    code = database.create_link_code("u1", 60, "Alice")
    database.redeem_link_code(code, 123, 456, "alice")
    telegram = FakeTelegram()
    payload = make_payload("workitem.comment.created", [])
    payload["data"]["comment"] = {
        "actor_id": "u2",
        "comment_stripped": "Проверьте, пожалуйста, Alice.",
        "comment_html": (
            '<p>Проверьте <mention-component id="transaction-1" '
            'entity_identifier="u1" entity_name="Alice">'
            "</mention-component></p>"
        ),
    }

    assert mentioned_user_ids(payload) == {"u1"}
    notifier = AssigneeNotifier(settings, database, telegram)
    assert asyncio.run(notifier.process(payload)) == 1


def test_comment_mention_by_plane_html_name_fallback(tmp_path):
    settings = Settings(
        telegram_bot_token="token",
        plane_webhook_secret="plane-secret-long-enough",
        plane_app_url="https://plane.example.com",
        plane_workspace_slug="acme",
        database_path=str(tmp_path / "test.sqlite3"),
    )
    database = Database(settings.database_path)
    database.initialize()
    code = database.create_link_code("u1", 60, "Alice Smith")
    database.redeem_link_code(code, 123, 456, "alice")
    telegram = FakeTelegram()
    payload = make_payload("workitem.comment.created", [])
    payload["data"]["comment"] = {
        "actor_id": "u2",
        "comment_stripped": "Проверьте, пожалуйста, Alice Smith.",
        "comment_html": (
            '<p><mention-component entity_name="Alice Smith"></mention-component></p>'
        ),
    }

    notifier = AssigneeNotifier(settings, database, telegram)
    assert asyncio.run(notifier.process(payload)) == 1


def test_comment_mention_by_configured_name_and_excludes_actor(tmp_path):
    settings = Settings(
        telegram_bot_token="token",
        plane_webhook_secret="plane-secret-long-enough",
        plane_app_url="https://plane.example.com",
        plane_workspace_slug="acme",
        database_path=str(tmp_path / "test.sqlite3"),
    )
    database = Database(settings.database_path)
    database.initialize()
    for user_id, chat_id, alias in (
        ("u1", 101, "Alice Smith"),
        ("u2", 102, "bob"),
    ):
        code = database.create_link_code(user_id, 60, alias)
        database.redeem_link_code(code, chat_id, chat_id, alias)
    telegram = FakeTelegram()
    payload = make_payload("workitem.comment.created", [])
    payload["data"]["comment"] = {
        "actor_id": "u2",
        "comment_stripped": "Посмотрите, пожалуйста, @Alice Smith и @bob.",
    }

    notifier = AssigneeNotifier(settings, database, telegram)
    assert asyncio.run(notifier.process(payload)) == 1
    assert [message[0] for message in telegram.messages] == [101]


def test_legacy_created_issue_notifies_assignee(tmp_path):
    settings = Settings(
        telegram_bot_token="token",
        plane_webhook_secret="plane-secret-long-enough",
        plane_app_url="https://plane.example.com",
        plane_workspace_slug="acme",
        database_path=str(tmp_path / "test.sqlite3"),
    )
    database = Database(settings.database_path)
    database.initialize()
    code = database.create_link_code("u1", 60)
    database.redeem_link_code(code, 123, 456, "alice")
    telegram = FakeTelegram()
    payload = {
        "event": "issue",
        "action": "created",
        "data": {
            "id": "issue-1",
            "name": "Legacy task",
            "project": "project-1",
            "assignees": [{"id": "u1", "display_name": "Alice"}],
            "updated_at": "2026-11-05T12:00:00Z",
        },
        "activity": {"field": None, "old_value": None, "new_value": None},
    }

    notifier = AssigneeNotifier(settings, database, telegram)
    result = asyncio.run(notifier.process_detailed(payload, "issue"))

    assert result.event == "workitem.created"
    assert result.sent == 1
    assert result.event_id.startswith("legacy-")


def test_legacy_issue_update_notifies_only_new_assignee():
    payload = {
        "event": "issue",
        "action": "updated",
        "data": {
            "id": "issue-1",
            "assignees": [{"id": "u1"}, {"id": "u2"}],
            "updated_at": "2026-11-05T12:00:00Z",
        },
        "activity": {
            "field": "assignees",
            "old_value": ["u1"],
            "new_value": ["u1", "u2"],
        },
    }

    normalized = normalize_legacy_payload(payload, "issue")
    assert normalized["event"] == "workitem.updated"
    assert new_assignee_ids(normalized, normalized["event"]) == {"u2"}


def test_legacy_unrelated_issue_update_has_no_recipients():
    payload = {
        "event": "issue",
        "action": "updated",
        "data": {"id": "issue-1", "assignees": [{"id": "u1"}]},
        "activity": {
            "field": "priority",
            "old_value": "low",
            "new_value": "high",
        },
    }

    normalized = normalize_legacy_payload(payload, "issue")
    assert new_assignee_ids(normalized, normalized["event"]) == set()


def test_legacy_comment_mention_is_supported(tmp_path):
    settings = Settings(
        telegram_bot_token="token",
        plane_webhook_secret="plane-secret-long-enough",
        plane_app_url="https://plane.example.com",
        plane_workspace_slug="acme",
        database_path=str(tmp_path / "test.sqlite3"),
    )
    database = Database(settings.database_path)
    database.initialize()
    code = database.create_link_code("u1", 60, "Alice")
    database.redeem_link_code(code, 123, 456, "alice")
    telegram = FakeTelegram()
    payload = {
        "event": "issue_comment",
        "action": "created",
        "data": {
            "id": "comment-1",
            "issue": "issue-1",
            "project": "project-1",
            "actor": "u2",
            "comment_stripped": "Проверьте, пожалуйста, @Alice.",
        },
        "activity": {},
    }

    notifier = AssigneeNotifier(settings, database, telegram)
    result = asyncio.run(notifier.process_detailed(payload, "issue_comment"))

    assert result.event == "workitem.comment.created"
    assert result.sent == 1


def test_legacy_comment_html_mention_is_supported(tmp_path):
    settings = Settings(
        telegram_bot_token="token",
        plane_webhook_secret="plane-secret-long-enough",
        plane_app_url="https://plane.example.com",
        plane_workspace_slug="acme",
        database_path=str(tmp_path / "test.sqlite3"),
    )
    database = Database(settings.database_path)
    database.initialize()
    code = database.create_link_code("u1", 60, "Alice")
    database.redeem_link_code(code, 123, 456, "alice")
    telegram = FakeTelegram()
    payload = {
        "event": "issue_comment",
        "action": "created",
        "data": {
            "id": "comment-1",
            "issue": "issue-1",
            "project": "project-1",
            "actor": "u2",
            "comment_stripped": "Проверьте, пожалуйста, Alice.",
            "comment_html": (
                '<p><mention-component entity_identifier="u1" '
                'entity_name="Alice"></mention-component></p>'
            ),
        },
        "activity": {},
    }

    plane = FakePlaneClient(
        {
            "name": "Login bug",
            "project_name": "Website",
            "target_date": "2026-12-01",
            "labels": [{"name": "Bug"}],
            "description_stripped": "The login form does not submit.",
        }
    )
    notifier = AssigneeNotifier(settings, database, telegram, plane_client=plane)
    result = asyncio.run(notifier.process_detailed(payload, "issue_comment"))

    assert result.event == "workitem.comment.created"
    assert result.recipients == ["u1"]
    assert result.sent == 1
    assert plane.calls == ["issue-1"]
    message = telegram.messages[0][1]
    assert "<b>Login bug</b>" in message
    assert "Проект: Website" in message
    assert "Теги: Bug" in message
    assert "Дедлайн: 2026-12-01" in message
    assert "<b>Описание:</b>\nThe login form does not submit." in message
    assert "<b>Комментарий:</b>\nПроверьте, пожалуйста, Alice." in message
