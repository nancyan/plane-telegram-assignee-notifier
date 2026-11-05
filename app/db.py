from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.security import hash_link_code


class Database:
    def __init__(self, path: str):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS user_links (
                    plane_user_id TEXT PRIMARY KEY,
                    telegram_chat_id INTEGER NOT NULL UNIQUE,
                    telegram_user_id INTEGER NOT NULL,
                    telegram_username TEXT,
                    plane_mention_name TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    linked_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS link_codes (
                    code_hash TEXT PRIMARY KEY,
                    plane_user_id TEXT NOT NULL,
                    plane_mention_name TEXT,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );

                CREATE TABLE IF NOT EXISTS sent_notifications (
                    event_id TEXT NOT NULL,
                    plane_user_id TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (event_id, plane_user_id)
                );
                """
            )
            self._ensure_column(
                connection, "user_links", "plane_mention_name", "TEXT"
            )
            self._ensure_column(
                connection, "link_codes", "plane_mention_name", "TEXT"
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, column_type: str
    ) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    def create_link_code(
        self,
        plane_user_id: str,
        ttl_minutes: int,
        plane_mention_name: str | None = None,
    ) -> str:
        code = secrets.token_urlsafe(9)
        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=ttl_minutes)
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM link_codes WHERE plane_user_id = ? AND used_at IS NULL",
                (plane_user_id,),
            )
            connection.execute(
                """
                INSERT INTO link_codes(
                    code_hash, plane_user_id, plane_mention_name, expires_at
                ) VALUES(?, ?, ?, ?)
                """,
                (
                    hash_link_code(code),
                    plane_user_id,
                    self.normalize_mention_name(plane_mention_name),
                    expires_at.isoformat(),
                ),
            )
        return code

    def redeem_link_code(
        self,
        code: str,
        telegram_chat_id: int,
        telegram_user_id: int,
        telegram_username: str | None,
    ) -> str | None:
        now = datetime.now(UTC)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT plane_user_id, plane_mention_name, expires_at
                FROM link_codes
                WHERE code_hash = ? AND used_at IS NULL
                """,
                (hash_link_code(code),),
            ).fetchone()
            if not row or datetime.fromisoformat(row["expires_at"]) <= now:
                return None

            plane_user_id = str(row["plane_user_id"])
            connection.execute(
                """
                DELETE FROM user_links
                WHERE telegram_chat_id = ? AND plane_user_id != ?
                """,
                (telegram_chat_id, plane_user_id),
            )
            connection.execute(
                """
                INSERT INTO user_links(
                    plane_user_id, telegram_chat_id, telegram_user_id,
                    telegram_username, plane_mention_name, active, linked_at
                ) VALUES(?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(plane_user_id) DO UPDATE SET
                    telegram_chat_id = excluded.telegram_chat_id,
                    telegram_user_id = excluded.telegram_user_id,
                    telegram_username = excluded.telegram_username,
                    plane_mention_name = COALESCE(
                        excluded.plane_mention_name, user_links.plane_mention_name
                    ),
                    active = 1,
                    linked_at = excluded.linked_at
                """,
                (
                    plane_user_id,
                    telegram_chat_id,
                    telegram_user_id,
                    telegram_username,
                    row["plane_mention_name"],
                    now.isoformat(),
                ),
            )
            connection.execute(
                "UPDATE link_codes SET used_at = ? WHERE code_hash = ?",
                (now.isoformat(), hash_link_code(code)),
            )
            return plane_user_id

    @staticmethod
    def normalize_mention_name(value: str | None) -> str | None:
        if not value:
            return None
        normalized = " ".join(value.strip().lstrip("@").split()).casefold()
        return normalized or None

    def get_active_mention_links(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT * FROM user_links
                WHERE active = 1 AND plane_mention_name IS NOT NULL
                """
            ).fetchall()

    def get_link_by_chat(self, telegram_chat_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM user_links WHERE telegram_chat_id = ?",
                (telegram_chat_id,),
            ).fetchone()

    def get_active_link(self, plane_user_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM user_links WHERE plane_user_id = ? AND active = 1",
                (plane_user_id,),
            ).fetchone()

    def set_active_by_chat(self, telegram_chat_id: int, active: bool) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE user_links SET active = ? WHERE telegram_chat_id = ?",
                (int(active), telegram_chat_id),
            )
            return cursor.rowcount > 0

    def notification_was_sent(self, event_id: str, plane_user_id: str) -> bool:
        with self.connect() as connection:
            return (
                connection.execute(
                    """
                    SELECT 1 FROM sent_notifications
                    WHERE event_id = ? AND plane_user_id = ?
                    """,
                    (event_id, plane_user_id),
                ).fetchone()
                is not None
            )

    def mark_notification_sent(self, event_id: str, plane_user_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO sent_notifications(
                    event_id, plane_user_id, sent_at
                )
                VALUES(?, ?, ?)
                """,
                (event_id, plane_user_id, datetime.now(UTC).isoformat()),
            )
