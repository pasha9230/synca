from __future__ import annotations

import argparse
import getpass
import os
import sys

from werkzeug.security import generate_password_hash

from . import create_app


def build_app(env: dict, db: str, unload: str, reference: str):
    if not env.get("PANEL_PASSWORD_HASH") or not env.get("PANEL_SECRET_KEY"):
        raise RuntimeError("set PANEL_PASSWORD_HASH and PANEL_SECRET_KEY in the environment")
    return create_app({
        "SECRET_KEY": env["PANEL_SECRET_KEY"],
        "PASSWORD_HASH": env["PANEL_PASSWORD_HASH"],
        "DB_PATH": db,
        "UNLOAD_PATH": unload,
        "REFERENCE_PATH": reference,
        "SESSION_COOKIE_SECURE": env.get("PANEL_SECURE_COOKIES", "1") != "0",       # https only; «0» for a panel run locally over http
        "TRUST_PROXY": int(env.get("PANEL_TRUST_PROXY", "0") or 0),
        "SECRET_KEY_FILE": env.get("TIRES_SECRET_KEY_FILE", ""),
        "FEED_ROOT": env.get("TIRES_FEED_ROOT", ""),
        "SHARE_ROOT": env.get("TIRES_SHARE_ROOT", ""),
    })


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tires_sync.panel")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hash-password")
    run = sub.add_parser("run")
    run.add_argument("--db", required=True)
    run.add_argument("--unload", required=True)
    run.add_argument("--reference", required=True)
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if args.command == "hash-password":
        print(generate_password_hash(getpass.getpass("Пароль: "), method="pbkdf2:sha256"))
        return 0
    try:
        app = build_app(os.environ, args.db, args.unload, args.reference)
    except RuntimeError as err:
        print("error: %s" % err, file=sys.stderr)
        return 2
    app.run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
