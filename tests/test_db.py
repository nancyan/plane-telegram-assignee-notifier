from app.db import Database


def test_link_code_is_one_time(tmp_path):
    database = Database(str(tmp_path / "test.sqlite3"))
    database.initialize()
    code = database.create_link_code("plane-user-1", 60)

    linked = database.redeem_link_code(code, 100, 200, "alice")
    assert linked == "plane-user-1"
    assert database.redeem_link_code(code, 101, 201, "mallory") is None

    row = database.get_active_link("plane-user-1")
    assert row is not None
    assert row["telegram_chat_id"] == 100
