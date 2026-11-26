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
    """Download MIM rims into their own guarded snapshot, independently of tires."""
    owner = connections.supplier_owner(1)
    config = connections.get_config(conn, "webmim", owner)
    if not config.get("base_url") or not config.get("mode"):
        raise WebmimError("Подключение WebMIM не настроено.")
    client = WebmimClient(config["base_url"], mode=config["mode"], login=config.get("login", ""),
                          password=connections.get_secret(conn, box, "webmim", owner, "password") or "",
                          api_key=connections.get_secret(conn, box, "webmim", owner, "api_key") or "", http=http)
    territory = connections.supplier_territory(conn, 1, active_settings(conn))
    return download_wheels(client, territory, wheel_snapshot_path(dest), now)
