"""Owner, 2026-10-15: every account has its own look of the photos (caption, frame, crop, quality); the picture is the same tire."""
from __future__ import annotations

import io

import pytest

from panel_helpers import make_app
from tires_sync import connections, photo_style, store

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

NOW = "2026-10-15T12:00:00"


def sample(color=(200, 30, 30), size=(640, 480)):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG", quality=90)
    return out.getvalue()


def test_the_style_is_checked_and_an_empty_one_means_no_processing():
    assert photo_style.normalize({}) == {} and photo_style.normalize({"label": "", "crop": "", "quality": ""}) == {}
    assert photo_style.normalize({"label": "  Партнёр-2 ", "border": "#aabbcc", "crop": "2,5", "quality": "80"}) == {
        "label": "Партнёр-2", "corner": "bottom-right", "border": "#AABBCC", "crop": 2.5, "quality": 80}
    for bad in ({"border": "red"}, {"crop": "9"}, {"crop": "x"}, {"quality": "10"}, {"quality": "abc"}):
        with pytest.raises(ValueError):
            photo_style.normalize(bad)


def test_the_processed_picture_is_a_jpeg_of_the_same_tire_with_the_look_of_the_account():
    original = sample()
    style = photo_style.normalize({"label": "Партнёр-2", "border": "#0000FF", "crop": "4", "quality": "85"})
    out = photo_style.process(original, style)
    assert out and out != original
    image = Image.open(io.BytesIO(out))
    assert image.format == "JPEG" and image.size == (640 - 2 * int(640 * 0.04), 480 - 2 * int(480 * 0.04))      # the edges are cropped
    assert image.getpixel((1, 1))[2] > 150                                                                      # the frame is blue
    assert photo_style.process(original, {}) is None and photo_style.process(b"not an image", style) is None


def test_two_styles_give_two_different_pictures():
    a = photo_style.process(sample(), photo_style.normalize({"label": "A", "border": "#FF0000"}))
    b = photo_style.process(sample(), photo_style.normalize({"label": "B", "crop": "3"}))
    assert a != b and photo_style.version({"label": "A"}) != photo_style.version({"label": "B"})


def test_the_route_serves_the_style_of_its_account_and_the_original_to_the_others(tmp_path):
    original = sample()
    app = make_app(tmp_path, PHOTO_FETCH=lambda url: (original, "jpg"), IMG_ROOT=str(tmp_path / "img"))
    conn = store.connect(app.config["DB_PATH"])
    one = connections.create_account(conn, "Один", NOW, "o")
    two = connections.create_account(conn, "Два", NOW, "o")
    connections.set_photo_style(conn, two, {"label": "Два", "border": "#00FF00"}, NOW, "t")
    conn.commit()
    t1, t2 = (connections.get_account(conn, n)["feed_token"] for n in (one, two))
    client = app.test_client()
    first = client.get("/img/%s/100.jpg" % t1)
    second = client.get("/img/%s/100.jpg" % t2)
    assert first.status_code == second.status_code == 200 and first.data == original and second.data != original
    assert Image.open(io.BytesIO(second.data)).format == "JPEG"
    assert client.get("/img/%s/100.jpg" % t2).data == second.data                              # made once, then from the disk
    connections.set_photo_style(conn, two, {}, NOW, "t")
    conn.commit()
    assert client.get("/img/%s/100.jpg" % t2).data == original                                # style off: the supplier's picture again
