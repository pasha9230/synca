from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import connections
from .secretbox import SecretBox
from .settings_store import active_settings
from .transport import Transport, urllib_transport
from .webmim_client import WebmimClient, WebmimError, download_unload


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
