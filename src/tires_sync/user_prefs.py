"""A person's own settings of the panel (table user_pref): a value of JSON under a key, for the login signed in only. Small by design: a
key of a few letters, a value of PREF_BYTES at most; nothing here ever reads another login's settings."""
from __future__ import annotations

import json
import re
from typing import Any

KEY = re.compile(r"^[a-z][a-z0-9_.]{0,39}$")
PREF_BYTES = 4000


class PrefError(ValueError):
    """The key or the value is not one a setting may have."""


def _key(key: str) -> str:
    if not KEY.match(str(key or "")):
        raise PrefError("bad key")
    return key


def get(conn, login: str, key: str, default: Any = None) -> Any:
    """The value saved by `login` under `key`, or `default` (no login, nothing saved, or a value that no longer reads)."""
    if not login:
        return default
    row = conn.execute("SELECT value FROM user_pref WHERE login = ? AND key = ?", (login, _key(key))).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except ValueError:
        return default


def put(conn, login: str, key: str, value: Any, now: str) -> None:
    """Save `value` (JSON) for `login`; PrefError when it is larger than PREF_BYTES. Commits."""
    if not login:
        raise PrefError("no login")
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > PREF_BYTES:
        raise PrefError("too large")
    conn.execute("INSERT INTO user_pref (login, key, value, updated_at) VALUES (?, ?, ?, ?) "
                 "ON CONFLICT (login, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at", (login, _key(key), text, now))
    conn.commit()


def drop(conn, login: str, key: str) -> None:
    """Forget the setting (back to the default). Commits."""
    conn.execute("DELETE FROM user_pref WHERE login = ? AND key = ?", (login, _key(key)))
    conn.commit()
