import argparse

from app.config import get_settings
from app.db import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Plane notifier administration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create-link", help="Create a one-time link code")
    create.add_argument("plane_user_id")
    create.add_argument(
        "--mention-name", help="Plane name used after @ in comment mentions"
    )
    create.add_argument("--ttl", type=int, default=None, help="Lifetime in minutes")
    args = parser.parse_args()

    settings = get_settings()
    database = Database(settings.database_path)
    database.initialize()
    if args.command == "create-link":
        ttl = args.ttl or settings.link_code_ttl_minutes
        code = database.create_link_code(
            args.plane_user_id, ttl, args.mention_name
        )
        if settings.telegram_bot_username:
            print(f"https://t.me/{settings.telegram_bot_username}?start={code}")
        else:
            print(code)


if __name__ == "__main__":
    main()
