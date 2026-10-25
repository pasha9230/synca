"""The photo of a position under the link of one account: `/img/<feed token>/<code>.jpg`.

Every account of ours gives Avito its own photo links, so the same position does not carry one and the same address in two accounts. The picture
itself is the supplier's: it is copied once (https only, public hosts only, small, really an image) and served from our disk. No login: the feed
token of the account is the key, as it is for the feed itself."""
from __future__ import annotations

import hmac
import os
import re
import tempfile
from pathlib import Path

from flask import abort, send_file

from .. import connections, photo_cache, share_photos, store
from ..settings_store import active_settings
from ..suppliers import supplier_of_id
from .common import get_conn
from .data import load_merged, stale_unload

NAME = re.compile(r"^([0-9A-Za-z.\-]{1,60})\.jpg$")
TOKEN = re.compile(r"^[0-9a-f]{32}$")
KINDS = photo_cache.KINDS


def cache_dir(app) -> Path:
    return photo_cache.root(app.config["DB_PATH"], app.config.get("IMG_ROOT"))


def _processed(folder: Path, code: str, style: dict):
    """The path of the picture of `code` with the account's look (made from the stored original once), or None when it cannot be made."""
    from .. import photo_style
    target = folder / "styled" / ("%s.%s.jpg" % (code, photo_style.version(style)))
    if target.is_file():
        return target
    for kind in KINDS:
        original = folder / ("%s.%s" % (code, kind))
        if original.is_file():
            data = photo_style.process(original.read_bytes(), style)
            if data is None:
                return None
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=code + ".", suffix=".part")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.chmod(tmp, 0o644)
                os.replace(tmp, str(target))
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
            return target
    return None


def _held_offer(conn, settings, base, parsed, account_id: int, code: str):
    """The position of an ad held on an old secondary WebMIM unload (the cycle keeps it as it was, its photo link stays) from the raw parse
    of that file, as before run tails4, so that Avito does not lose its photo while it is not in our cache. Only a code that the panel's
    set lacks because its supplier is held, that is in the file and that this account has published (a set of four by its single code)."""
    sid = supplier_of_id(code)
    if code in parsed.offers or sid not in parsed.unavailable:
        return None
    if not any(re.sub(r"-x\d+$", "", gid) == code for gid in store.load_published(conn, account_id)):
        return None
    held = stale_unload(conn, settings, base, sid)
    return held[0].offers.get(code) if held is not None else None


def register(app) -> None:
    @app.route("/img/<token>/<name>")
    def account_photo(token, name):
        found = NAME.match(name)
        if not TOKEN.match(token) or not found:
            abort(404)
        conn = get_conn()
        account = next((a for a in connections.list_accounts(conn) if hmac.compare_digest(str(a["feed_token"] or ""), token)), None)
        if account is None:
            abort(404)
        code = found.group(1)
        folder = cache_dir(app)
        style = connections.get_photo_style(conn, account["id"])
        if style:                                                   # the account has its own look: its processed copy is served (made once per style)
            processed = _processed(folder, code, style)
            if processed is not None:
                return send_file(str(processed), mimetype="image/jpeg", max_age=86400)
        for kind, mimetype in KINDS.items():
            path = folder / ("%s.%s" % (code, kind))
            if path.is_file():
                return send_file(str(path), mimetype=mimetype, max_age=86400)
        settings = active_settings(conn)
        parsed = load_merged(conn, settings, app.config["UNLOAD_PATH"])
        offer = parsed.offers.get(code) or _held_offer(conn, settings, app.config["UNLOAD_PATH"], parsed, account["id"], code)
        if offer is None or not offer.image:
            abort(404)
        fetch = app.config.get("PHOTO_FETCH") or share_photos.fetch
        try:
            data, kind = fetch(offer.image)
        except share_photos.PhotoError:
            abort(404)
        if kind not in KINDS:
            abort(404)
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(folder), prefix=code + ".", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, str(folder / ("%s.%s" % (code, kind))))
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        if style:
            processed = _processed(folder, code, style)
            if processed is not None:
                return send_file(str(processed), mimetype="image/jpeg", max_age=86400)
        return send_file(str(folder / ("%s.%s" % (code, kind))), mimetype=KINDS[kind], max_age=86400)
