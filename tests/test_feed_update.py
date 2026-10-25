"""Manual feed update: a preview of what changes against the published feed, then a confirmed rebuild and publication."""
from __future__ import annotations

import re

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app, post
from tires_sync import connections, store
import pytest

pytestmark = pytest.mark.usefixtures("morning_wait")  # these tests describe the wait for the morning refill, which is switched off by default

NOW = "2026-10-07T10:00:00"


def goods(*ids, price="5000"):
    return catalog_xml(*[commodity_xml(NNOMMODIF=str(i), SMNFCODE="C%d" % i, NREST="12", NPRICE_DELAYED=price) for i in ids])


def setup(tmp_path, live=True):
    app = make_app(tmp_path, FEED_ROOT=str(tmp_path / "feed"))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    account = connections.create_account(conn, "Основной", NOW, "owner")
    connections.set_live(conn, account, live, NOW, "owner")
    conn.commit()
    return app, client, tok, conn, account


def next_morning(app):
    """The panel clock is UTC: 04:30 UTC of the next day is 07:30 Moscow, after the 07:00 morning window when the supplier's catalog is full."""
    from datetime import datetime
    app.config["NOW"] = lambda: datetime(2026, 10, 8, 4, 30, 0)


def unload(app, xml):
    open(app.config["UNLOAD_PATH"], "wb").write(xml)


def rev_of(client):
    return re.search(r'name="rev" value="([0-9a-f]+)"', client.get("/feed/update").get_data(as_text=True)).group(1)


def public_feed(app, conn, account):
    from pathlib import Path
    path = Path(app.config["FEED_ROOT"]) / connections.get_account(conn, account)["feed_token"] / "avito_feed.xml"
    return path.read_text(encoding="utf-8") if path.exists() else None


def first_publication(app, client, tok, ids=(1, 2, 3, 4)):
    unload(app, goods(*ids))
    post(client, "/feed/update/confirm", tok, rev=rev_of(client))


def tables(conn):
    return [conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("run", "published_state", "pending_state", "known_point")]


def test_the_preview_lists_new_gone_and_repriced_positions_and_writes_nothing(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    first_publication(app, client, tok)
    assert public_feed(app, conn, account).count("<Ad>") == 4
    unload(app, catalog_xml(commodity_xml(NNOMMODIF="1", SMNFCODE="C1", NREST="12", NPRICE_DELAYED="6000"),
                            commodity_xml(NNOMMODIF="2", SMNFCODE="C2", NREST="12"), commodity_xml(NNOMMODIF="3", SMNFCODE="C3", NREST="12"),
                            commodity_xml(NNOMMODIF="9", SMNFCODE="C9", NREST="12")))               # 4 is gone, 9 is new, 1 costs more
    before, feed_before = tables(conn), public_feed(app, conn, account)
    html = client.get("/feed/update").get_data(as_text=True)
    # position 4 left the unload during the day: it waits for the morning refill of the supplier's catalog instead of going at once
    assert "Появятся (1)" in html and "Ждут утра (1)" in html and "Исчезнут" not in html and "Изменится цена (1)" in html
    row = re.search(r"Изменится цена \(1\).*?<tbody>(.*?)</tbody>", html, re.S).group(1)
    assert "4" in row and "810" in row and "780" in row                                               # 4 810 became 5 780 (6000 with the brand's markdown)
    assert tables(conn) == before and public_feed(app, conn, account) == feed_before                   # reading changed nothing
    post(client, "/feed/update/confirm", tok, rev=rev_of(client))
    assert "<Id>4</Id>" in public_feed(app, conn, account)                                              # still published, waiting
    next_morning(app)                                                                                   # the catalog did not bring it back
    html = client.get("/feed/update").get_data(as_text=True)
    assert "Исчезнут (1)" in html and "пропала у поставщика" in html and "Ждут утра" not in html
    post(client, "/feed/update/confirm", tok, rev=rev_of(client))
    assert "<Id>4</Id>" not in public_feed(app, conn, account)


def test_confirming_rebuilds_and_publishes_and_a_stale_preview_is_refused(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    first_publication(app, client, tok)
    unload(app, goods(1, 2, 3, 9))
    old_rev = rev_of(client)
    unload(app, goods(1, 2, 3, 8, 9))                                                                 # the data moved on after the preview
    r = post(client, "/feed/update/confirm", tok, rev=old_rev)
    assert r.status_code == 302 and r.headers["Location"].endswith("/feed/update")
    assert "<Id>9</Id>" not in public_feed(app, conn, account)
    r = post(client, "/feed/update/confirm", tok, rev=rev_of(client))
    assert r.status_code == 302 and r.headers["Location"].endswith("/publish/result")
    feed = public_feed(app, conn, account)
    assert "<Id>9</Id>" in feed and "<Id>8</Id>" in feed and "<Id>4</Id>" in feed                    # 4 left the unload: it waits for the morning


def test_without_publication_the_feed_is_only_built_not_sent(tmp_path):
    app, client, tok, conn, account = setup(tmp_path, live=False)
    unload(app, goods(1, 2))
    html = client.get("/feed/update").get_data(as_text=True)
    assert "публикация выключена" in html and "Собрать фид (без публикации)" in html
    post(client, "/feed/update/confirm", tok, rev=rev_of(client))
    assert public_feed(app, conn, account) is None                                                    # nothing public


def test_a_feed_that_would_shrink_by_more_than_half_is_stopped_in_the_preview_and_at_confirmation(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    first_publication(app, client, tok, ids=(1, 2, 3, 4))
    unload(app, goods(1))
    assert "Публикация будет отклонена" not in client.get("/feed/update").get_data(as_text=True)     # all three wait for the morning
    post(client, "/feed/update/confirm", tok, rev=rev_of(client))
    next_morning(app)                                                                                   # and are still gone: now the shrink is real
    html = client.get("/feed/update").get_data(as_text=True)
    assert "Публикация будет отклонена" in html and "disabled" in html
    before = public_feed(app, conn, account)
    r = post(client, "/feed/update/confirm", tok, rev=rev_of(client))
    assert r.headers["Location"].endswith("/feed/update") and public_feed(app, conn, account) == before


def test_the_page_needs_a_login_and_the_fetch_says_when_the_master_key_is_missing(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    assert app.test_client().get("/feed/update").status_code == 302
    unload(app, goods(1))
    post(client, "/feed/update/fetch", tok)
    assert "Мастер-ключ недоступен" in client.get("/feed/update").get_data(as_text=True)
    assert 'href="/feed/update"' in client.get("/").get_data(as_text=True)                            # the button on the home page


def actions(conn):
    return [row[0] for row in conn.execute("SELECT action FROM audit").fetchall()]


def test_a_preview_made_before_publication_was_switched_on_cannot_be_confirmed_afterwards(tmp_path):
    app, client, tok, conn, account = setup(tmp_path, live=False)
    unload(app, goods(1, 2))
    stale = rev_of(client)                                                                           # «Собрать фид (без публикации)»
    connections.set_live(conn, account, True, NOW, "owner")
    conn.commit()
    r = post(client, "/feed/update/confirm", tok, rev=stale)
    assert r.headers["Location"].endswith("/feed/update") and public_feed(app, conn, account) is None
    assert "feed_updated_manually" not in actions(conn)


def test_an_unreadable_or_empty_supplier_file_blocks_the_update_and_nothing_is_recorded_as_done(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    first_publication(app, client, tok)
    feed_before = public_feed(app, conn, account)
    done_before = actions(conn).count("feed_updated_manually")
    unload(app, catalog_xml())                                                                       # no positions at all
    html = client.get("/feed/update").get_data(as_text=True)
    assert "недоступна или пуста" in html and "disabled" in html
    post(client, "/feed/update/confirm", tok, rev=rev_of(client))
    assert public_feed(app, conn, account) == feed_before and actions(conn).count("feed_updated_manually") == done_before


def test_the_check_runs_inside_the_feed_lock_and_stops_a_rebuild_whose_reason_it_names(tmp_path):
    from tires_sync.panel.routes_drafts import apply_publish
    app, client, tok, conn, account = setup(tmp_path)
    unload(app, goods(1, 2))
    with app.test_request_context():
        assert apply_publish(app, verify=lambda: "Данные изменились: стоп.") == ["Данные изменились: стоп."]
    assert public_feed(app, conn, account) is None                                                   # nothing was built or made public


def test_the_start_of_a_preview_without_a_feed_folder_says_so(tmp_path):
    app = make_app(tmp_path)                                                                         # TIRES_FEED_ROOT is not set
    client = app.test_client()
    login(client)
    unload(app, goods(1))
    assert "Каталог фидов не задан" in client.get("/feed/update").get_data(as_text=True)


def test_a_position_whose_ad_does_not_build_is_not_promised_in_the_preview(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    unload(app, catalog_xml(commodity_xml(NNOMMODIF="1", SMNFCODE="C1", NREST="12"),
                            commodity_xml(NNOMMODIF="7", SMNFCODE="C7", NREST="12", SMODIFNAME="", SMODIFNAME_ORIG="")))   # no title: the ad cannot be built
    html = client.get("/feed/update").get_data(as_text=True)
    assert "Появятся (1)" in html and "Станет" in html and "не соберётся" in html
    post(client, "/feed/update/confirm", tok, rev=rev_of(client))
    feed = public_feed(app, conn, account)
    assert feed.count("<Ad>") == 1 and "<Id>1</Id>" in feed and "<Id>7</Id>" not in feed                      # what the preview said


def test_many_disappearing_ads_need_the_tick_and_one_account_failing_does_not_hide_the_other(tmp_path):
    from tires_sync.settings_store import confirm_draft, create_draft
    app, client, tok, conn, account = setup(tmp_path)
    pirelli = lambda i: commodity_xml(NNOMMODIF=str(100 + i), SMARKA="Pirelli", SMODIFNAME="PIRELLI 205/55/16 T 94 Test Model",
                                      SMODIFNAME_ORIG="Pirelli 205/55/16 T 94 Test Model", SMNFCODE="P%d" % i, NREST="12")
    unload(app, catalog_xml(*[commodity_xml(NNOMMODIF=str(i), SMNFCODE="C%d" % i, NREST="12") for i in range(1, 13)],
                            *[pirelli(i) for i in range(1, 15)]))
    post(client, "/feed/update/confirm", tok, rev=rev_of(client))
    assert public_feed(app, conn, account).count("<Ad>") == 26
    confirm_draft(conn, create_draft(conn, {"excluded_brands": ["IKON TYRES"]}, NOW), NOW, "owner")        # twelve ads leave at once
    html = client.get("/feed/update").get_data(as_text=True)
    assert "Исчезнут (12)" in html and 'name="understand"' in html
    before = public_feed(app, conn, account)
    post(client, "/feed/update/confirm", tok, rev=rev_of(client))                                         # without the tick
    assert public_feed(app, conn, account) == before
    post(client, "/feed/update/confirm", tok, rev=rev_of(client), understand="1")
    assert public_feed(app, conn, account).count("<Ad>") == 14


def test_the_fetch_downloads_the_unload_and_a_second_press_during_a_download_is_told_to_wait(tmp_path):
    from cryptography.fernet import Fernet
    from http_fakes import ScriptedHttp
    from tires_sync.secretbox import SecretBox
    from tires_sync.panel import feed_update as fu
    http = ScriptedHttp((200, {"result": True, "authKey": "A"}), (200, '"%s"' % ("C" * 32)), (200, {"result": True}),
                        (200, catalog_xml(commodity_xml(NNOMMODIF="1", SMNFCODE="C1", NREST="12", TERRITORY_RN="103261489424"))))
    app = make_app(tmp_path, FEED_ROOT=str(tmp_path / "feed"), SECRET_BOX=SecretBox(Fernet.generate_key()), WEBMIM_HTTP=http)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    box = app.config["SECRET_BOX"]
    connections.set_config(conn, "webmim", 0, {"base_url": "https://mimtest.svrauto.ru", "mode": "login", "login": "user"}, NOW, "owner")
    connections.set_secret(conn, box, "webmim", 0, "password", "pw", NOW, "owner")
    from tires_sync.settings_store import confirm_draft, create_draft
    confirm_draft(conn, create_draft(conn, {"territory_rn": "103261489424"}, NOW), NOW, "owner")
    conn.commit()
    with fu.fetch_lock(tmp_path):                                                                         # a download is running
        post(client, "/feed/update/fetch", tok)
    assert "уже скачивается" in client.get("/feed/update").get_data(as_text=True) and not http.calls
    post(client, "/feed/update/fetch", tok)
    assert "Выгрузка скачана" in client.get("/feed/update").get_data(as_text=True) and len(http.calls) == 4


def test_a_position_that_left_the_unload_is_listed_by_its_title_not_by_its_number(tmp_path):
    import xml.etree.ElementTree as ET
    app, client, tok, conn, account = setup(tmp_path)
    first_publication(app, client, tok)
    title = next(a.findtext("Title") for a in ET.fromstring(public_feed(app, conn, account)).findall("Ad") if a.findtext("Id").startswith("4"))
    unload(app, catalog_xml(commodity_xml(NNOMMODIF="1", SMNFCODE="C1", NREST="12"), commodity_xml(NNOMMODIF="2", SMNFCODE="C2", NREST="12"),
                            commodity_xml(NNOMMODIF="3", SMNFCODE="C3", NREST="12")))                  # 4 is gone from the unload
    html = client.get("/feed/update").get_data(as_text=True)
    waiting = re.search(r"Ждут утра \(1\).*?<tbody>(.*?)</tbody>", html, re.S).group(1)
    assert title and title in waiting                                                                # the name of the ad we published, not «4»


def test_the_preview_builds_the_photo_links_as_the_cycle_does_for_an_account_with_a_photo_style(tmp_path):
    """The preview used to build without the photo version: its ads carried links without «s=», unlike the published ones."""
    from xml.etree import ElementTree as ET
    from tires_sync.panel import feed_update
    app, client, tok, conn, account = setup(tmp_path)
    store.set_state(conn, "public_base", "https://feeds.example.test")
    connections.set_photo_style(conn, account, {"border": "#112233"}, NOW, "owner")
    conn.commit()
    unload(app, goods(1, 2))
    seen = []
    real = feed_update.build_ads
    def spy(*args, **kwargs):
        result = real(*args, **kwargs)
        seen.extend(result[0])
        return result
    feed_update.build_ads = spy
    try:
        client.get("/feed/update")
    finally:
        feed_update.build_ads = real
    post(client, "/feed/update/confirm", tok, rev=rev_of(client))
    urls = lambda nodes: sorted(i.get("url") for n in nodes for i in n.findall("Images/Image"))
    preview = urls(seen)
    published = urls(ET.fromstring(public_feed(app, conn, account).encode("utf-8")).findall("Ad"))
    assert preview and all("?s=" in u or "&s=" in u for u in preview)
    assert preview == published


def test_the_preview_counts_what_the_stock_passes_took_off_in_the_last_hour_as_the_cycle_does(tmp_path, monkeypatch):
    """tails2 Ф4: the guard of the build counts the light passes' removals of the last hour (cycle.pass_removals_lately); the preview must
    too, or it promises removals the build holds back."""
    from tires_sync import cycle, decide
    monkeypatch.setattr(decide, "WAIT_FOR_MORNING_REFILL", False)                                         # as in production: a fall is taken off at once
    app, client, tok, conn, account = setup(tmp_path)
    first_publication(app, client, tok, ids=range(1, 13))
    assert public_feed(app, conn, account).count("<Ad>") == 12
    cycle.remember_pass_removals(conn, account, 9)                                                        # the guard's limit is 10
    unload(app, catalog_xml(*[commodity_xml(NNOMMODIF=str(i), SMNFCODE="C%d" % i, NREST="2" if i <= 2 else "12") for i in range(1, 13)]))
    html = client.get("/feed/update").get_data(as_text=True)
    assert "Сработала защита от массового снятия" in html and "Исчезнут (2)" not in html
