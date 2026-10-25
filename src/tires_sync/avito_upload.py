"""Start an Avito autoload upload for one account (the only place where the panel writes to the Avito API)."""
from __future__ import annotations

import time
from typing import Callable, Optional, Tuple

from . import connections, store
from .avito import MIN_UPLOAD_INTERVAL, AvitoClient, AvitoError
from .publish import PublishError, upload_now
from .secretbox import SecretKeyError


def last_upload_key(account_id: int) -> str:
    return "avito_last_upload_%d" % account_id


def _last(conn, key: str) -> Optional[float]:
    raw = store.get_state(conn, key)
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _scrub(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text[:200]


def start_upload(conn, box, account_id: int, http, now: Callable[[], float] = time.time) -> Tuple[bool, str]:
    """(started, text for the owner). Never raises: a refused or failed upload must not hide the already published feed.
    The hour limit is reserved before the request goes out (so two parallel confirmations cannot both start) and given back on failure."""
    try:
        client_id = connections.get_secret(conn, box, "avito", account_id, "client_id")
        client_secret = connections.get_secret(conn, box, "avito", account_id, "client_secret")
    except SecretKeyError:
        return False, "ключи Авито недоступны (проверьте мастер-ключ)"
    if not client_id or not client_secret:
        return False, "ключи доступа Авито не заданы на странице «Подключения»"
    client = AvitoClient(client_id, client_secret, http=http, now=now, allow_write=True)
    key = last_upload_key(account_id)
    previous = store.get_state(conn, key)
    reserved = False
    try:
        last = _last(conn, key)
        if last is not None and now() - last < MIN_UPLOAD_INTERVAL:
            raise PublishError("Загрузку можно запускать не чаще раза в час: прошлая была совсем недавно.")
        store.set_state(conn, key, repr(now()))
        reserved = True
        upload_now(client, None)
    except PublishError as err:
        text = "%s Фид у нас обновлён, Авито заберёт его по своему расписанию." % err
    except AvitoError as err:
        text = "Авито отклонило запуск: %s" % _scrub(str(err), client_id, client_secret)
    except OSError as err:
        text = "Авито недоступно (%s). Фид у нас обновлён, попробуйте запустить загрузку позже." % _scrub(str(err), client_id, client_secret)
    except Exception as err:  # noqa: BLE001 - the owner must still see the published feed and the audit entry
        text = "неожиданная ошибка при запуске загрузки (%s)" % type(err).__name__
    else:
        return True, "загрузка на Авито запущена"
    if reserved:
        store.set_state(conn, key, previous or "")
    return False, text
