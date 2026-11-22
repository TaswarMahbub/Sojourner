#!/usr/bin/env python3
"""Create or revoke a device token. Admin task: connects with DATABASE_URL_ADMIN, not the API.

    python scripts/create_token.py create --name bailey-laptop \
        --scopes profiles:write sessions:write aggregates:read
    python scripts/create_token.py revoke --name bailey-laptop

Needs DATABASE_URL_ADMIN and the same TOKEN_PEPPER the API uses. The token prints once.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.store import SCOPES, hash_token, new_token  # noqa: E402


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Set {name} first.")
    return value


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--name", required=True)
    c.add_argument("--scopes", nargs="+", required=True, choices=SCOPES)
    r = sub.add_parser("revoke")
    r.add_argument("--name", required=True)
    args = ap.parse_args()

    import psycopg

    with psycopg.connect(_env("DATABASE_URL_ADMIN")) as conn:
        if args.cmd == "create":
            token = new_token()
            conn.execute("INSERT INTO api_clients (name, token_hash, scopes) VALUES (%s, %s, %s)",
                         (args.name, hash_token(token, _env("TOKEN_PEPPER")), sorted(set(args.scopes))))
            print(f"token for {args.name} (shown once; store it privately):\n{token}")
        else:
            n = conn.execute("UPDATE api_clients SET revoked_at = now() "
                             "WHERE name = %s AND revoked_at IS NULL", (args.name,)).rowcount
            print(f"revoked {n} token(s) named {args.name}")


if __name__ == "__main__":
    main()
