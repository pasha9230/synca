from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import connections
from .secretbox import SecretBox
from .settings_store import active_settings
from .transport import Transport, urllib_transport
from .webmim_client import WebmimClient, WebmimError, download_unload, download_wheels


def fetch_for_connection(conn, box: Optional[SecretBox], dest: Path, now: str, http: Transport = urllib_transport,
                         supplier_id: int = 1) -> dict:
    owner = connections.supplier_owner(supplier_id)
    config = connections.get_config(conn, "webmim", owner)
    if not config.get("base_url") or not config.get("mode"):
        raise WebmimError("Подключение WebMIM не настроено.")
    client = WebmimClient(
        config["base_url"], mode=config["mode"], login=config.get("login", ""),
        password=connections.get_secret(conn, box, "webmim", owner, "password") or "",
        api_key=connections.get_secret(conn, box, "webmim", owner, "api_key") or "",
        http=http,
    )
    return download_unload(client, connections.supplier_territory(conn, supplier_id, active_settings(conn)), Path(dest), now)


def wheel_snapshot_path(unload_path: Path) -> Path:
    path = Path(unload_path)
    return path.with_name(path.name + ".wheels.xml")


def fetch_wheels_for_connection(conn, box: Optional[SecretBox], dest: Path, now: str, http: Transport = urllib_transport) -> dict:
    """Use a wheel-only lock shared by the timer and manual refresh."""
    import fcntl
    lock_path = Path(dest).with_name(Path(dest).name + ".wheels.lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WebmimError("Выгрузка дисков уже обновляется.") from None
        try:
            return _fetch_wheels_locked(conn, box, dest, now, http)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _fetch_wheels_locked(conn, box: Optional[SecretBox], dest: Path, now: str, http: Transport) -> dict:
    owner = connections.supplier_owner(1)
    config = connections.get_config(conn, "webmim", owner)
    if not config.get("base_url") or not config.get("mode"):
        raise WebmimError("Подключение WebMIM не настроено.")
    from urllib.parse import urlsplit
    wheel_service = "webmim-wheels:" + (urlsplit(config["base_url"]).hostname or "unknown")
    client = WebmimClient(config["base_url"], mode=config["mode"], login=config.get("login", ""),
                          password=connections.get_secret(conn, box, "webmim", owner, "password") or "",
                          api_key=connections.get_secret(conn, box, "webmim", owner, "api_key") or "", http=http,
                          service_id=wheel_service)
    territory = connections.supplier_territory(conn, 1, active_settings(conn))
    from . import wheel_archive
    from .webmim_wheels import parse_unload as parse_wheels
    import xml.etree.ElementTree as ET
    path = wheel_snapshot_path(dest)
    try:
        previous = parse_wheels(path.read_bytes(), territory)
        if previous.offers and not previous.issues:
            wheel_archive.remember(conn, previous.offers, now)
    except (OSError, ET.ParseError):
        pass
    result = download_wheels(client, territory, path, now)
    current = parse_wheels(path.read_bytes(), territory)
    wheel_archive.remember(conn, current.offers, now)
    return result
