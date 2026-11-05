from app.db import Database


def test_link_code_is_one_time(tmp_path):
    database = Database(str(tmp_path / "test.sqlite3"))
    database.initialize()
    code = database.create_link_code("plane-user-1", 60, "@Alice Smith")

    linked = database.redeem_link_code(code, 100, 200, "alice")
    assert linked == "plane-user-1"
    assert database.redeem_link_code(code, 101, 201, "mallory") is None

    row = database.get_active_link("plane-user-1")
    assert row is not None
    assert row["telegram_chat_id"] == 100
    assert row["plane_mention_name"] == "alice smith"


def test_initialize_migrates_existing_database(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.sqlite3")
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE user_links (
        plane_user_id TEXT PRIMARY KEY, telegram_chat_id INTEGER NOT NULL UNIQUE,
        telegram_user_id INTEGER NOT NULL, telegram_username TEXT,
        active INTEGER NOT NULL DEFAULT 1, linked_at TEXT NOT NULL)"""
    )
    connection.execute(
        """CREATE TABLE link_codes (
        code_hash TEXT PRIMARY KEY, plane_user_id TEXT NOT NULL,
        expires_at TEXT NOT NULL, used_at TEXT)"""
    )
    connection.commit()
    connection.close()

    database = Database(path)
    database.initialize()
    code = database.create_link_code("u1", 60, "alice")
    assert database.redeem_link_code(code, 1, 2, "alice") == "u1"
