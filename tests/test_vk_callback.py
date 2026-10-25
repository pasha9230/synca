"""The VK community's Callback API receiver: confirms the address, records the events, and keeps strangers out."""
from __future__ import annotations

import json
from datetime import datetime

from cryptography.fernet import Fernet

from panel_helpers import login, make_app, post
from tires_sync import connections, store, vk_callback
from tires_sync.panel.gunicorn_logger import mask
from tires_sync.secretbox import SecretBox

NOW = datetime(2026, 10, 12, 18, 30, 0)
SECRET = "Sup3r-Secret_key"


def setup(tmp_path, configure=True):
    box = SecretBox(Fernet.generate_key())
    app = make_app(tmp_path, SECRET_BOX=box, VK_RUN=lambda work: work())        # the work on a message runs in place, not after the answer
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    if configure:
        post(client, "/connections/vk", tok, group_id="220945892", confirmation="3d6defda", callback_secret=SECRET, token="")
    return app, client, tok, conn, box


def hook(app, token, body, raw=None):
    data = raw if raw is not None else json.dumps(body)
    return app.test_client().post("/hook/vk/%s" % token, data=data, content_type="application/json")


def token_of(conn):
    return vk_callback.path_token(conn)


def test_the_address_is_confirmed_with_the_string_from_the_vk_window(tmp_path):
    app, client, tok, conn, box = setup(tmp_path)
    r = hook(app, token_of(conn), {"type": "confirmation", "group_id": 220945892})
    assert r.status_code == 200 and r.get_data(as_text=True) == "3d6defda" and r.mimetype == "text/plain"


def test_events_are_recorded_without_the_secret_and_a_repeat_is_ignored(tmp_path):
    app, client, tok, conn, box = setup(tmp_path)
    event = {"type": "message_new", "group_id": 220945892, "secret": SECRET, "event_id": "abc123",
             "object": {"message": {"from_id": 5, "text": "принял 12"}}}
    for _ in range(2):
        r = hook(app, token_of(conn), event)
        assert r.status_code == 200 and r.get_data(as_text=True) == "ok"
    rows = conn.execute("SELECT type, payload FROM vk_event").fetchall()
    assert len(rows) == 1 and rows[0][0] == "message_new" and "принял 12" in rows[0][1] and SECRET not in rows[0][1]
    assert "message_new" in client.get("/connections/vk").get_data(as_text=True)


def test_a_wrong_address_community_or_secret_is_refused_and_nothing_is_recorded(tmp_path):
    app, client, tok, conn, box = setup(tmp_path)
    good = {"type": "message_new", "group_id": 220945892, "secret": SECRET, "event_id": "e1"}
    assert hook(app, "wrong-token", good).status_code == 404
    assert hook(app, token_of(conn), dict(good, group_id=1)).status_code == 403
    assert hook(app, token_of(conn), dict(good, secret="nope")).status_code == 403
    no_secret = dict(good)
    del no_secret["secret"]
    assert hook(app, token_of(conn), no_secret).status_code == 403
    assert hook(app, token_of(conn), {}, raw="not json").status_code == 400
    assert hook(app, token_of(conn), {}, raw="[1,2]").status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM vk_event").fetchone()[0] == 0
    assert hook(app, token_of(conn), {"type": "confirmation", "group_id": 1}).status_code == 403    # the string only for our community


def test_nothing_is_accepted_until_the_community_is_set_up(tmp_path):
    app, client, tok, conn, box = setup(tmp_path, configure=False)
    assert hook(app, token_of(conn), {"type": "confirmation", "group_id": 220945892}).status_code == 403
    assert hook(app, token_of(conn), {"type": "message_new", "group_id": 220945892, "secret": ""}).status_code == 403


def test_the_secret_path_is_stable_unguessable_and_the_hook_sets_no_cookie(tmp_path):
    app, client, tok, conn, box = setup(tmp_path)
    token = token_of(conn)
    assert token == token_of(conn) and len(token) >= 20
    r = hook(app, token, {"type": "confirmation", "group_id": 220945892})
    assert "Set-Cookie" not in r.headers
    assert app.test_client().get("/hook/vk/%s" % token).status_code == 405                       # only POST


def test_the_settings_page_is_saved_validated_and_never_shows_the_secrets(tmp_path):
    app, client, tok, conn, box = setup(tmp_path)
    html = client.get("/connections/vk").get_data(as_text=True)
    assert "220945892" in html and "3d6defda" in html and SECRET not in html and "/hook/vk/" in html and "задан" in html
    assert connections.get_secret(conn, box, "vk", 0, "callback_secret") == SECRET
    raw = conn.execute("SELECT ciphertext FROM secret WHERE kind = 'vk' AND name = 'callback_secret'").fetchone()[0]
    assert SECRET not in raw and SECRET not in str(list(conn.execute("SELECT * FROM audit")))
    post(client, "/connections/vk", tok, group_id="12ab", confirmation="ok", callback_secret="")
    assert connections.get_config(conn, "vk", 0)["group_id"] == "220945892"                      # the bad form changed nothing
    post(client, "/connections/vk", tok, group_id="220945892", confirmation="bad string!", callback_secret="")
    assert connections.get_config(conn, "vk", 0)["confirmation"] == "3d6defda"
    post(client, "/connections/vk", tok, group_id="220945892", confirmation="3d6defda", callback_secret="")      # empty: the key stays
    assert connections.get_secret(conn, box, "vk", 0, "callback_secret") == SECRET
    assert 'href="/connections/vk"' in html and app.test_client().get("/connections/vk").status_code == 302


def test_the_access_log_hides_the_secret_path_of_the_hook():
    assert mask("POST /hook/vk/AbC_123-xyz HTTP/1.1") == "POST /hook/vk/*** HTTP/1.1"
    assert mask("GET /connections/vk HTTP/1.1") == "GET /connections/vk HTTP/1.1"
