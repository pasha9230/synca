"""A mirrored account may leave out whole tire types (the owner: no summer tires in «Партнёр-2»): those ads are not published, Ids of the rest stay."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

import pytest

from helpers import catalog_xml, commodity_xml, settings_dict
from panel_helpers import login, make_app, post
from tires_sync import connections, cycle, mirror_feed, store
from tires_sync.catalog import parse_reference
from tires_sync.settings_store import init_settings

NOW = "2026-10-14T12:00:00"
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')


def car(code, tire_type):
    return '<Ad><Id>%s_1</Id><Title>Шина %s</Title><Price>5000</Price><TireType>%s</TireType></Ad>' % (code, code, tire_type)


def setup(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Партнёр-2", NOW, "o")
    file = ("<Ads>%s</Ads>" % "".join([car("100", "Летние"), car("101", "Зимние шипованные"), car("102", "Летние"), car("103", "Всесезонные")])).encode()
    mirror_feed.import_feed(conn, account, file, NOW)
    return conn, account


def ids(tmp_path, conn, account):
    token = connections.get_account(conn, account)["feed_token"]
    return [a.findtext("Id") for a in ET.parse(tmp_path / "feed" / token / "next" / "avito_feed.xml").getroot()]


UNLOAD = catalog_xml(*[commodity_xml(NNOMMODIF=str(n), SMNFCODE="C%d" % n) for n in (100, 101, 102, 103, 200)])


def test_the_types_are_kept_and_validated(tmp_path):
    conn, account = setup(tmp_path)
    assert connections.get_excluded_types(conn, account) == []
    connections.set_excluded_types(conn, account, ["Летние"], NOW, "o")
    assert connections.get_excluded_types(conn, account) == ["Летние"]
    with pytest.raises(ValueError):
        connections.set_excluded_types(conn, account, ["Какие-то"], NOW, "o")


def test_ads_of_an_excluded_type_leave_the_feed_and_the_others_keep_their_ids(tmp_path):
    conn, account = setup(tmp_path)
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    assert sorted(ids(tmp_path, conn, account)) == ["100_1", "101_1", "102_1", "103_1", "200" + connections.feed_style(conn, account)[0]]
    connections.set_excluded_types(conn, account, ["Летние"], NOW, "o")
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    assert sorted(ids(tmp_path, conn, account)) == ["101_1", "103_1", "200" + connections.feed_style(conn, account)[0]]


def test_a_new_position_of_an_excluded_type_is_not_added_either(tmp_path):
    conn, account = setup(tmp_path)
    connections.set_excluded_types(conn, account, ["Зимние нешипованные", "Зимние шипованные", "Летние", "Всесезонные"], NOW, "o")
    results = cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in results] == [False] or ids(tmp_path, conn, account) == []        # nothing is left: an empty feed is never published


def test_the_panel_form_saves_the_types_of_a_mirror(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    account = connections.create_account(conn, "Зеркало", NOW, "o")
    mirror_feed.import_feed(conn, account, ("<Ads>%s</Ads>" % car("100", "Летние")).encode(), NOW)
    client = app.test_client()
    tok = login(client)
    page = client.get("/connections/avito/%d" % account).get_data(as_text=True)
    assert 'name="exclude_types"' in page and "Летние" in page
    resp = client.post("/connections/accounts/%d/exclude-types" % account, data={"csrf": tok, "exclude_types": ["Летние"]})
    assert resp.status_code == 302
    assert connections.get_excluded_types(conn, account) == ["Летние"]
    client.post("/connections/accounts/%d/exclude-types" % account, data={"csrf": tok})                     # nothing ticked: nothing excluded
    assert connections.get_excluded_types(conn, account) == []


def test_an_account_that_builds_its_own_feed_leaves_the_excluded_types_out_too(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Партнёр-2", NOW, "o")
    unload = catalog_xml(commodity_xml(NNOMMODIF="1", SMODIFNAME="IKON 205/55/16 T 94 Test Model Ш.", SMNFCODE="S1"), commodity_xml(NNOMMODIF="2", SMNFCODE="W2"))
    cycle.run_for_accounts(conn, unload, REF, date(2026, 10, 14), tmp_path / "feed")
    assert sorted(ids(tmp_path, conn, account)) == ["1", "2"]
    connections.set_excluded_types(conn, account, ["Зимние шипованные"], NOW, "o")
    results = cycle.run_for_accounts(conn, unload, REF, date(2026, 10, 14), tmp_path / "feed")
    assert results[0].ok and sorted(ids(tmp_path, conn, account)) == ["2"]


def test_the_choice_of_tire_types_is_on_the_page_of_every_account_not_only_of_a_mirror(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    account = connections.create_account(conn, "Своя сборка", NOW, "o")                          # not a mirror
    client = app.test_client()
    login(client)
    page = client.get("/connections/avito/%d" % account).get_data(as_text=True)
    assert connections.get_account(conn, account)["feed_mode"] == "generated"
    assert 'name="exclude_types"' in page and "Какие шины не публиковать" in page
