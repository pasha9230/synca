"""The contact person's name of an account goes into `ManagerName` of its ads; empty means the element is not written."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

import pytest

from helpers import catalog_xml, commodity_xml, settings_dict
from panel_helpers import login, make_app
from tires_sync import connections, cycle, store
from tires_sync.catalog import parse_reference
from tires_sync.settings_store import init_settings

NOW = "2026-10-14T12:00:00"
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
UNLOAD = catalog_xml(commodity_xml(NNOMMODIF="100"))


def feed(tmp_path, conn, account):
    token = connections.get_account(conn, account)["feed_token"]
    return list(ET.parse(tmp_path / "feed" / token / "next" / "avito_feed.xml").getroot())


def test_the_name_is_in_every_ad_of_that_account_only(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    first = connections.create_account(conn, "Основной", NOW, "o")
    second = connections.create_account(conn, "Партнёр-4", NOW, "o")
    connections.set_contact_name(conn, second, "  Паша ", NOW, "o")
    results = cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in results] == [True, True], [r.error for r in results]
    assert [a.findtext("ManagerName") for a in feed(tmp_path, conn, second)] == ["Паша"]
    assert [a.find("ManagerName") for a in feed(tmp_path, conn, first)] == [None]            # not set: the element is not written


def test_the_name_is_validated_and_can_be_cleared(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "A", NOW, "o")
    with pytest.raises(ValueError):
        connections.set_contact_name(conn, account, "я" * 41, NOW, "o")
    with pytest.raises(ValueError):
        connections.set_contact_name(conn, account, "<b>x</b>", NOW, "o")
    connections.set_contact_name(conn, account, "Паша", NOW, "o")
    assert connections.get_contact_name(conn, account) == "Паша"
    connections.set_contact_name(conn, account, "", NOW, "o")
    assert connections.get_contact_name(conn, account) == ""


def test_the_panel_form_saves_the_name(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    account = connections.create_account(conn, "A", NOW, "o")
    client = app.test_client()
    tok = login(client)
    assert 'name="contact_name"' in client.get("/connections/avito/%d" % account).get_data(as_text=True)
    assert client.post("/connections/accounts/%d/contact-name" % account, data={"csrf": tok, "contact_name": "Паша"}).status_code == 302
    assert connections.get_contact_name(conn, account) == "Паша"
