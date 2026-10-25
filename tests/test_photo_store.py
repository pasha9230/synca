"""Content-addressed photo storage and its public route."""
from __future__ import annotations

import hashlib
import io

import pytest
from PIL import Image

from panel_helpers import make_app
from tires_sync import photo_library, store

NOW = "2026-10-20T12:00:00"


def jpeg(color=(200, 10, 10)):
    buf = io.BytesIO()
    Image.new("RGB", (8, 6), color).save(buf, "JPEG")
    return buf.getvalue()


def test_same_bytes_same_photo(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    root = tmp_path / "photos"
    data = jpeg()
    first = photo_library.save_photo(conn, root, data, "upload", NOW)
    assert photo_library.save_photo(conn, root, data, "upload", NOW) == first
    assert len(list(root.iterdir())) == 1
    sha = hashlib.sha256(data).hexdigest()
    row = conn.execute("SELECT sha256, ext, width, height, bytes FROM photo WHERE id=?", (first,)).fetchone()
    assert tuple(row) == (sha, "jpg", 8, 6, len(data))
    assert (root / (sha + ".jpg")).read_bytes() == data
    assert photo_library.photo_url("https://x", sha, "jpg") == "https://x/photos/%s.jpg" % sha
    assert photo_library.save_photo(conn, root, jpeg((1, 2, 3)), "upload", NOW) != first


def test_rejects_fake_extension(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    with pytest.raises(ValueError):
        photo_library.save_photo(conn, tmp_path / "photos", b"just text, named a.jpg", "upload", NOW)
    with pytest.raises(ValueError):
        photo_library.save_photo(conn, tmp_path / "photos", b"\xff\xd8\xff" + b"broken", "upload", NOW)


def test_rejects_over_25mb(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    with pytest.raises(ValueError):
        photo_library.save_photo(conn, tmp_path / "photos", jpeg() + b"0" * (25 * 1024 * 1024), "upload", NOW)


def test_route_serves_file_and_404s_unknown(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    data = jpeg()
    photo_library.save_photo(conn, app.config["PHOTOS_ROOT"], data, "upload", NOW)
    sha = hashlib.sha256(data).hexdigest()
    client = app.test_client()
    ok = client.get("/photos/%s.jpg" % sha)
    assert ok.status_code == 200 and ok.mimetype == "image/jpeg" and ok.data == data
    assert "Set-Cookie" not in ok.headers
    assert client.get("/photos/%s.jpg" % ("0" * 64)).status_code == 404
    assert client.get("/photos/..%2Ft.db").status_code == 404
    assert client.get("/photos/%s.png" % sha).status_code == 404
