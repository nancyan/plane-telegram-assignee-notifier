import asyncio

from app.config import Settings
from app.db import Database
from app.service import AssigneeNotifier, mentioned_user_ids, new_assignee_ids


class FakeTelegram:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, *, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))


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
    payload = make_payload(
        "workitem.updated", ["u1", "u2"], {"assignee_ids": ["u1"]}
    )
    assert new_assignee_ids(payload, "workitem.updated") == {"u2"}


def test_unrelated_update_notifies_nobody():
    payload = make_payload(
        "workitem.updated", ["u1"], {"priority": "medium"}
    )
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
