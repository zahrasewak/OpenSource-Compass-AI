"""Admin CLI. Issuing tokens requires shell access to the server (and its secret).

    python -m app.cli issue-token --github-id 123 --username octocat
"""

import argparse
import sys

from app.core.config import get_settings
from app.core.security import AuthConfigError, issue_token
from app.db.session import get_sessionmaker
from app.services.users import get_or_create_user


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("issue-token", help="DEPRECATED: print a legacy 12 h token that cannot be revoked (not accepted in production)")
    p.add_argument("--github-id", type=int, required=True)
    p.add_argument("--username", required=True)
    pc = sub.add_parser("issue-pairing-code", help="create/find a user and print a one-time pairing code to sign a client in")
    pc.add_argument("--github-id", type=int, required=True)
    pc.add_argument("--username", required=True)
    pc.add_argument("--ttl-minutes", type=int, default=None)
    rv = sub.add_parser("revoke-sessions", help="revoke every session of a user")
    rv.add_argument("--github-id", type=int, required=True)
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.cmd == "revoke-sessions":
        from sqlalchemy import select

        from app.db.models import User
        from app.services.sessions import revoke_all_for_user

        with get_sessionmaker()() as db:
            user = db.scalar(select(User).where(User.github_user_id == args.github_id))
            print(f"revoked {revoke_all_for_user(db, user.id, 'admin_revoke') if user else 0} session(s)")
        return 0
    if args.cmd == "issue-pairing-code":
        from datetime import timedelta

        from app.services.sessions import create_pairing_code

        with get_sessionmaker()() as db:
            user = get_or_create_user(db, args.github_id, args.username)
            ttl = timedelta(minutes=args.ttl_minutes) if args.ttl_minutes else None
            code = create_pairing_code(db, user.id, settings, ttl)
        print(code)
        print(f"(one-time code; valid {args.ttl_minutes or settings.auth_pairing_ttl_minutes} minutes)", file=sys.stderr)
        return 0
    print("warning: issue-token is deprecated (long-lived, not revocable); use issue-pairing-code", file=sys.stderr)
    with get_sessionmaker()() as db:
        user = get_or_create_user(db, args.github_id, args.username)
        try:
            token = issue_token(user.id, settings)
        except AuthConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
