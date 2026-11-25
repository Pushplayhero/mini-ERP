"""Operator CLI for session housekeeping and incident revocation (ADR-018).

Usage examples:
    uv run python -m app.cli.manage_sessions purge-expired
    uv run python -m app.cli.manage_sessions revoke-user --email user@example.com
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.db import dispose_engine, get_session_factory
from app.platform.permissions import service as permissions_service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage_sessions",
        description="Manage and purge database-backed bearer sessions",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser(
        "purge-expired",
        help="Delete all expired or idle-timed-out sessions",
    )
    revoke_parser = subparsers.add_parser(
        "revoke-user",
        help="Revoke all sessions belonging to the user with the given email",
    )
    revoke_parser.add_argument("--email", required=True, help="Target user email")
    return parser


async def _amain(argv: list[str] | None = None, *, dispose: bool = True) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    session_factory = get_session_factory()
    try:
        async with session_factory() as session:
            if args.action == "purge-expired":
                purged = await permissions_service.purge_expired_sessions(session)
                print(f"Purged {purged} expired/idle session(s).")
                return 0
            if args.action == "revoke-user":
                user_id, revoked = await permissions_service.revoke_user_sessions_by_email(
                    session, args.email
                )
                if user_id is None:
                    print(f"Error: User '{args.email}' not found.", file=sys.stderr)
                    return 1
                print(f"Revoked {revoked} session(s) for user {user_id}.")
                return 0
        return 1
    finally:
        if dispose:
            await dispose_engine()


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
