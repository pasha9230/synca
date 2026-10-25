from __future__ import annotations

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app, post
from tires_sync import connections, staging, store
from tires_sync.settings_store import active_settings, confirm_draft, create_draft

T = "2026-10-07T10:00:00"
ROWS = tuple(commodity_xml(NNOMMODIF=str(i), NREST="12", SMNFCODE="C%d" % i) for i in range(1, 7))


def setup(tmp_path, live=True, guard_min=50):
    feed_root = tmp_path / "feed"
    app = make_app(tmp_path, FEED_ROOT=str(feed_root))
    client = app.test_client()
    tok = login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    conn = store.connect(app.config["DB_PATH"])
    confirm_draft(conn, create_draft(conn, {"guard_min": guard_min, "guard_fraction": 0.0}, T), T, "owner")
    connections.create_account(conn, "Основной", T, "owner")
    if live:
        connections.set_live(conn, 1, True, T, "owner")
    token = connections.get_account(conn, 1)["feed_token"]
    return app, client, tok, conn, feed_root / token


def draft_id(conn):
    return staging.summary(conn)["draft_id"]


def test_apply_and_publish_now_updates_the_public_feed_and_records_state(tmp_path):
    app, client, tok, conn, folder = setup(tmp_path)
    staging.set_override(conn, "1", "off", T)
    r = post(client, "/draft/%d/confirm" % draft_id(conn), tok, publish="1")
    assert r.status_code == 302
    text = (folder / "avito_feed.xml").read_text(encoding="utf-8")
    assert "<Id>1</Id>" not in text and "<Id>2</Id>" in text
    assert "1" not in store.load_published(conn, account_id=1) and "2" in store.load_published(conn, account_id=1)
    assert r.headers["Location"].endswith("/publish/result")
    result = client.get("/publish/result").get_data(as_text=True)
    assert "Результат публикации" in result and "Опубликовано" in result and "Вернуться в каталог" in result
    assert "Последняя публикация" in client.get("/history").get_data(as_text=True)          # on «История» since 2026-10-21


def test_a_confirmed_change_always_updates_the_public_feed_without_a_separate_publish_button(tmp_path):
    """Owner's decision (2026-10-09): any confirmed change goes to the feed Avito reads at once."""
    app, client, tok, conn, folder = setup(tmp_path)
    staging.set_override(conn, "1", "off", T)
    r = post(client, "/draft/%d/confirm" % draft_id(conn), tok)                      # no publish=1 in the form
    assert r.headers["Location"].endswith("/publish/result")
    text = (folder / "avito_feed.xml").read_text(encoding="utf-8")
    assert "<Id>1</Id>" not in text and "<Id>2</Id>" in text
    assert active_settings(conn).overrides == {"1:1": "off"}


def test_accounts_with_publication_off_are_not_published_and_the_user_is_told(tmp_path):
    app, client, tok, conn, folder = setup(tmp_path, live=False)
    staging.set_override(conn, "1", "off", T)
    post(client, "/draft/%d/confirm" % draft_id(conn), tok, publish="1")
    assert not (folder / "avito_feed.xml").exists()
    assert "публикация выключена" in client.get("/").get_data(as_text=True).lower()


def test_large_removal_needs_the_checkbox_for_publishing_too_and_then_goes_through(tmp_path):
    app, client, tok, conn, folder = setup(tmp_path, guard_min=0)
    first = create_draft(conn, {"min_stock": 1}, T)
    post(client, "/draft/%d/confirm" % first, tok, publish="1", understand="on")
    assert (folder / "avito_feed.xml").exists() and len(store.load_published(conn, account_id=1)) == 6
    d = create_draft(conn, {"enabled": []}, T)
    refused = post(client, "/draft/%d/confirm" % d, tok, publish="1")
    assert refused.headers["Location"].endswith("/draft/%d" % d)
    assert active_settings(conn).enabled != frozenset()
    ok = post(client, "/draft/%d/confirm" % d, tok, publish="1", understand="on")
    assert ok.headers["Location"].endswith("/publish/result")
    assert active_settings(conn).enabled == frozenset()
    # an empty feed is never published: the public feed and the recorded state stay as they were
    assert len(store.load_published(conn, account_id=1)) == 6 and (folder / "avito_feed.xml").exists()
    assert "ни одного объявления" in client.get("/").get_data(as_text=True)
