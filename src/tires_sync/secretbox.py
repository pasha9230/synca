from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

MASKABLE = frozenset({"client_id", "api_key"})


class SecretKeyError(Exception):
    pass


class SecretBox:
    def __init__(self, key: bytes):
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError):
            raise SecretKeyError("invalid master key") from None

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeError):
            raise SecretKeyError("cannot decrypt secret") from None


def create_key_file(path) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(Fernet.generate_key())


def load_box(path) -> Optional[SecretBox]:
    if not path:
        return None
    try:
        data = Path(path).read_bytes().strip()
    except OSError:
        return None
    try:
        return SecretBox(data)
    except SecretKeyError:
        return None


def mask(name: str, value: str) -> str:
    if name in MASKABLE and len(value) >= 8:
        return "••••" + value[-4:]
    return ""
