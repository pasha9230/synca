"""`/img/<feed token>/<code>.jpg`: the supplier's picture under the account's own address, copied once."""
from __future__ import annotations

from panel_helpers import make_app
from tires_sync import connections, store

NOW = "2026-10-14T12:00:00"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


def setup(tmp_path, fetch):
    app = make_app(tmp_path, PHOTO_FETCH=fetch, IMG_ROOT=str(tmp_path / "img"))
    conn = store.connect(app.config["DB_PATH"])
    account = connections.create_account(conn, "Партнёр-4", NOW, "o")
    return app, app.test_client(), connections.get_account(conn, account)["feed_token"]


def test_the_picture_is_fetched_once_and_served_without_a_login(tmp_path):
    calls = []

    def fetch(url):
        calls.append(url)
        return JPEG, "jpg"
    app, client, token = setup(tmp_path, fetch)
    first = client.get("/img/%s/100.jpg" % token)                                    # 100: a position of the test unload
    assert first.status_code == 200 and first.mimetype == "image/jpeg" and first.data == JPEG
    assert client.get("/img/%s/100.jpg" % token).data == JPEG and len(calls) == 1      # the second time from our disk


def test_a_wrong_token_name_or_position_is_a_404_and_a_broken_supplier_is_too(tmp_path):
    from tires_sync.share_photos import PhotoError

    def broken(url):
        raise PhotoError("нет")
    app, client, token = setup(tmp_path, broken)
    assert client.get("/img/%s/100.jpg" % ("0" * 32)).status_code == 404               # nobody's token
    assert client.get("/img/%s/..%%2Fsecret.jpg" % token).status_code == 404
    assert client.get("/img/%s/100.png" % token).status_code == 404
    assert client.get("/img/%s/999999.jpg" % token).status_code == 404                 # not in the unload
    assert client.get("/img/%s/100.jpg" % token).status_code == 404                    # the supplier does not give it
    assert not (tmp_path / "img").exists() or not list((tmp_path / "img").glob("100.*"))
