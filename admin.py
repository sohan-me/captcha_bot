#!/usr/bin/env python3
"""Create or overwrite admin credentials for the dashboard (writes admin.json next to this file)."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from werkzeug.security import generate_password_hash

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ADMIN_PATH = os.path.join(APP_DIR, "admin.json")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create admin account for the Cloudflare Turnstile API dashboard")
    parser.add_argument("-u", "--username", default="admin", help="Admin username (default: admin)")
    args = parser.parse_args()
    username = (args.username or "").strip()
    if not username:
        print("Username cannot be empty.", file=sys.stderr)
        return 1

    password = getpass.getpass("New password: ")
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        return 1
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.", file=sys.stderr)
        return 1

    record = {"username": username, "password_hash": generate_password_hash(password)}
    with open(ADMIN_PATH, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)

    print(f"Wrote {ADMIN_PATH}")
    print("Start the server, then open http://127.0.0.1:5000/ and sign in (or go to /login).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
