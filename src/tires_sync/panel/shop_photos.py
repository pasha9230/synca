"""The photos of the client's catalog through our server (the owner 2026-10-29, В6): the client sees /shop/photo/<our opaque name>, never
the supplier's address, its host, a goods id or a code. Our own photo (an approved three-quarter frame of a set sent to the feed) comes
first; else the supplier's photo: the copy /img keeps (photo_cache) or a new one (share_photos.fetch: https only, public hosts only, no
redirects, at most 2 MB, only JPEG/PNG/WebP, a time limit). Every picture is drawn anew as a small JPEG (MAX_SIDE) without any metadata
(EXIF/XMP/IPTC of the supplier or of a phone never reach the client) into its own folder `<img>/shop`: one file per position at most, so
the folder is bounded by the catalog (about 50-150 KB a file). The pictures are made in the background, so that a page of 50 positions
never holds the panel's few workers; the row gets its photo link once its picture is made."""
from __future__ import annotations

import io
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Optional, Tuple

from .. import photo_cache, photo_library, share_photos

KINDS = photo_cache.KINDS
SHA = re.compile(r"^[0-9a-f]{64}$")
MAX_SIDE = 800                 # the client sees a small picture: the longer side, px
MAX_PIXELS = 40_000_000        # a bigger JPEG is not decoded at all (a JPEG is decoded already reduced, `draft`)
MAX_PIXELS_FULL = 20_000_000   # PNG and WebP are decoded whole: a lower bound keeps the memory of a worker small
QUALITY = 85
FAIL_SECONDS = 3600            # a picture that could not be made is not tried again for an hour
MAX_PENDING = 200              # pictures waiting at once (one process); more are asked for again by a later page
MAX_FAILED = 2000              # the memory of failures is bounded: the oldest are forgotten first
WORKERS = 2

_lock = threading.Lock()
_pending: Dict[str, object] = {}
_failed: Dict[str, float] = {}
_pool: Optional[ThreadPoolExecutor] = None


def clean(data: bytes) -> bytes:
    """The picture drawn anew: JPEG, at most MAX_SIDE, no metadata of any kind. Raises PhotoError for anything that is not a picture."""
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ("JPEG", "PNG", "WEBP") or \
                    image.width * image.height > (MAX_PIXELS if image.format == "JPEG" else MAX_PIXELS_FULL):
                raise share_photos.PhotoError("не картинка")
            image.draft("RGB", (MAX_SIDE, MAX_SIDE))
            image = ImageOps.exif_transpose(image).convert("RGBA")      # a phone's turn is applied before its EXIF is dropped
            image.thumbnail((MAX_SIDE, MAX_SIDE))
            flat = Image.new("RGB", image.size, (255, 255, 255))
            flat.paste(image, mask=image.getchannel("A"))
            out = io.BytesIO()
            flat.save(out, "JPEG", quality=QUALITY, optimize=True)
            return out.getvalue()
    except share_photos.PhotoError:
        raise
    except Exception:
        raise share_photos.PhotoError("не картинка")


class Photos:
    """The photos of one request: our own sets (read once) and the folders."""

    def __init__(self, conn, photos_root, cache_folder, fetch=None):
        self.cache = Path(cache_folder)
        self.made = self.cache / "shop"
        self.fetch = fetch or share_photos.fetch
        self.aliases = photo_library.aliases(conn)
        root = Path(photos_root)
        self.own = {key: (sha, root / ("%s.%s" % (sha, ext))) for key, sha, ext in conn.execute(
            "SELECT s.key, p.sha256, p.ext FROM photo_set s JOIN set_frame f ON f.set_key = s.key AND f.kind = 'three_quarter'"
            " AND f.status = 'approved' JOIN photo p ON p.id = f.photo_id WHERE s.in_feed = 1") if SHA.match(sha or "") and ext in KINDS}

    def _source(self, offer) -> Optional[Tuple[str, str, object]]:
        """(the name of the made picture, "file" or "url", the source): ours first, else the supplier's; None without a photo."""
        key = photo_library.set_key_for(offer)
        if key is not None:
            own = self.own.get(self.aliases.get(key.text(), key.text()))
            if own is not None and own[1].is_file():
                return "own-" + own[0], "file", own[1]
        code = offer.goods_id or ""
        if not photo_cache.CODE.match(code) or code.startswith("."):
            return None
        kept = photo_cache.cached(self.cache, code)
        if kept is not None:
            return code, "file", kept
        return (code, "url", offer.image) if offer.image else None

    def path(self, offer) -> Optional[Path]:
        """The made picture of the position, or None."""
        source = self._source(offer)
        path = self.made / (source[0] + ".jpg") if source else None
        return path if path is not None and path.is_file() else None

    def ready(self, offer) -> bool:
        """The picture is made; if not, it is asked for in the background."""
        source = self._source(offer)
        if source is None:
            return False
        if (self.made / (source[0] + ".jpg")).is_file():
            return True
        _ask(self.made, source, self.fetch)
        return False


def _ask(folder: Path, source, fetch) -> None:
    name = source[0]
    global _pool
    with _lock:
        if name in _pending or len(_pending) >= MAX_PENDING or time.monotonic() - _failed.get(name, -FAIL_SECONDS) < FAIL_SECONDS:
            return
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="shop-photo")
        _pending[name] = _pool.submit(_make, folder, source, fetch)


def _make(folder: Path, source, fetch) -> None:
    name, kind, where = source
    try:
        data = Path(where).read_bytes() if kind == "file" else fetch(where)[0]
        picture = clean(data)
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(folder), prefix=name + ".", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(picture)
            os.chmod(tmp, 0o644)
            os.replace(tmp, str(folder / (name + ".jpg")))
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except Exception:                       # any failure: no picture for an hour, the page shows the stub
        with _lock:
            _failed.pop(name, None)
            while len(_failed) >= MAX_FAILED:
                _failed.pop(next(iter(_failed)))
            _failed[name] = time.monotonic()
    finally:
        with _lock:
            _pending.pop(name, None)


def drain(timeout: float = 10.0) -> None:
    """Wait for the pictures asked for (the tests)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _lock:
            futures = list(_pending.values())
        if not futures:
            return
        for future in futures:
            future.result(timeout=max(0.0, deadline - time.monotonic()))


def forget() -> None:
    """Forget the failures (the tests)."""
    with _lock:
        _failed.clear()
