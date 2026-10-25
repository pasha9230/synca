"""Every confirmed change that touches the feed is published for Avito at once (no second «publish» button to forget)."""
from __future__ import annotations

from pathlib import Path

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app, post
from tires_sync import connections, store

NOW = "2026-10-07T10:00:00"


def setup(tmp_path, live=True):
    app = make_app(tmp_path, FEED_ROOT=str(tmp_path / "feed"))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    account = connections.create_account(conn, "Основной", NOW, "owner")
    connections.set_live(conn, account, live, NOW, "owner")
    conn.commit()
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*[commodity_xml(NNOMMODIF=str(i), SMNFCODE="C%d" % i, NREST="12") for i in (1, 2, 3)]))
    token = connections.get_account(conn, account)["feed_token"]
    return app, client, tok, conn, account, Path(app.config["FEED_ROOT"]) / token / "avito_feed.xml"


def test_confirming_a_draft_publishes_the_feed_even_without_the_publish_button(tmp_path):
    app, client, tok, conn, account, public = setup(tmp_path)
    assert not public.exists()
    post(client, "/settings/draft", tok, min_stock="5", min_diameter="15", portion_limit="100", stale_days="3", date_end_days="29",
         point_mode="percent", **{"pv|P1": "40", "pv|P2": "40", "pv|P3": "20"}, point_rest="")
    r = post(client, "/draft/1/confirm", tok)                                          # the plain «Подтвердить»: no publish=1
    assert r.status_code == 302 and r.headers["Location"].endswith("/publish/result")
    assert public.exists() and public.read_text(encoding="utf-8").count("<Ad>") == 3


def test_switching_publication_on_publishes_at_once(tmp_path):
    app, client, tok, conn, account, public = setup(tmp_path, live=False)
    r = post(client, "/connections/accounts/%d/live" % account, tok, action="enable", understand="1")
    assert r.status_code == 302 and public.exists() and public.read_text(encoding="utf-8").count("<Ad>") == 3


def test_a_confirmed_model_mapping_publishes_at_once(tmp_path):
    app, client, tok, conn, account, public = setup(tmp_path)
    post(client, "/mappings/model", tok, brand="Ikon Tyres", model_raw="Test Model 2", avito_model="Test Model")
    assert public.exists()


def test_nothing_is_published_without_a_feed_folder_and_the_old_flow_still_works(tmp_path):
    app = make_app(tmp_path)                                                          # no TIRES_FEED_ROOT, no account
    client = app.test_client()
    tok = login(client)
    post(client, "/settings/draft", tok, min_stock="5", min_diameter="15", portion_limit="100", stale_days="3", date_end_days="29",
         point_mode="percent", **{"pv|P1": "40", "pv|P2": "40", "pv|P3": "20"}, point_rest="")
    r = post(client, "/draft/1/confirm", tok)
    assert r.status_code == 302 and not r.headers["Location"].endswith("/publish/result")


def test_the_summary_after_an_automatic_publication_keeps_the_failures_of_other_accounts():
    from tires_sync.panel.routes_drafts import publish_summary
    text = publish_summary(["Опубликовано: A, объявлений 17 (было 120)", "B: фид не собран (feed shrank from 100 to 10)",
                            "Не публикуется: C, публикация выключена, фид у нас не обновлён"])
    assert "A, объявлений 17" in text and "B: фид не собран" in text and "C" in text
    assert publish_summary(["Каталог фидов не задан (TIRES_FEED_ROOT): фид не опубликован."]).startswith("Фид для Авито не обновлён")
