"""Where the copies of the supplier's photos are kept: `<dir of the base>/img/<code>.<jpg|png|webp>` (or the panel's IMG_ROOT). The panel
serves them as /img/<feed token>/<code>.jpg (panel/routes_img.py); the build of a feed asks whether one is kept (sync.build_ads)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

KINDS = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
CODE = re.compile(r"^[0-9A-Za-z.\-]{1,60}$")


def root(db_path, configured=None) -> Path:
    """The folder of the kept photos: the configured one (the panel's IMG_ROOT) or `img` next to the base."""
    return Path(configured or Path(db_path).resolve().parent / "img")


def root_for(conn) -> Optional[Path]:
    """The folder of the kept photos of the base `conn` is open on (as the panel finds it); None for a base in memory."""
    row = conn.execute("PRAGMA database_list").fetchone()
    return root(row[2]) if row and row[2] else None


def cached(folder, code: str) -> Optional[Path]:
    """The kept original of `code`, or None."""
    if folder is None or not CODE.match(str(code)) or str(code).startswith("."):
        return None
    for kind in KINDS:
        path = Path(folder) / ("%s.%s" % (code, kind))
        if path.is_file():
            return path
    return None
