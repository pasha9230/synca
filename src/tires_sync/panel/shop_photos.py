"""The photos of the client's catalog through our server: the client sees /shop/photo/<our opaque name>, never the supplier's address,
its host, a goods id or a code. Only the supplier's photo of the position (the owner 2026-11-09, replacing 2026-10-29 В6: our own
generated frames of the sets are not shown in /shop; the supplier's watermarks on its photo are acceptable): the copy the feed keeps in
photo_cache (`<img>/<code>.<kind>`) if there is one, else the picture of the position in the unload (`offer.image`), fetched in the
background only (share_photos.fetch: a public host, its own timeouts) and only once per position and picture (a made picture is kept; a
failure is not tried again for FAIL_SECONDS). A position without a supplier's photo, or whose photo could not be made, shows a
placeholder. Every picture is drawn anew as a small JPEG (MAX_SIDE) without any metadata (EXIF/XMP/IPTC of the supplier never reach the
client) into its own folder `<img>/shop` under a name of a hash (no code, no address): one file per position and picture, so the folder
is bounded by the catalog (about 50-150 KB a file). The pictures are made in the background, so that a page of 50 positions never holds
the panel's few workers; the row gets its photo link once its picture is made."""
from __future__ import annotations

import hashlib
import io
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Optional, Tuple

from .. import photo_cache, share_photos

KINDS = photo_cache.KINDS
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
    """The photos of one request: the supplier's photos of the positions (a kept copy, or its address fetched in the background)."""

    def __init__(self, conn, photos_root, cache_folder, fetch=None):
        self.cache = Path(cache_folder)
        self.made = self.cache / "shop"
        self.fetch = fetch or share_photos.fetch

    def _source(self, offer) -> Optional[Tuple[str, str, object]]:
        """(the name of the made picture, "file" | "url", where), or None: only the supplier's photo of the position reaches the client
        (the owner 2026-11-09). The copy photo_cache keeps comes first (nobody is asked); else the address of the unload. The name is a
        hash of the position and its picture: a new picture of the supplier is a new name, and the name tells nothing of either."""
        code = str(getattr(offer, "goods_id", "") or "")
        url = str(getattr(offer, "image", "") or "").strip()
        kept = photo_cache.cached(self.cache, code) if code else None
        if kept is None and not url.lower().startswith("https://"):
            return None
        name = "sup-" + hashlib.sha256(("shop-supplier\0%s\0%s" % (code, url)).encode("utf-8")).hexdigest()
        return (name, "file", kept) if kept is not None else (name, "url", url)

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
        if kind == "file":                                    # the copy photo_cache keeps: nobody is asked
            data = Path(where).read_bytes()
        else:                                                 # the supplier's address: here, in the background, with fetch's timeouts
            data = fetch(where)[0]
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
