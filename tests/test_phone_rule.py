"""An account that is connected to Avito publishes only with the phone of its own cabinet; the general number of the settings is never a fallback."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from helpers import catalog_xml, commodity_xml, settings_dict
from tires_sync import connections, cycle, mirror_feed, store
from tires_sync.catalog import parse_reference
from tires_sync.settings_store import init_settings

NOW = "2026-10-14T12:00:00"
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
UNLOAD = catalog_xml(commodity_xml(NNOMMODIF="100"))


def account_with_keys(tmp_path, keys=True):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Кабинет", NOW, "o")
    connections.add_address(conn, account, "Точка", "123456", True, 1.0, NOW, "o") if hasattr(connections, "add_address") else None
    if keys:
        conn.execute("INSERT INTO secret (kind, owner_id, name, ciphertext, updated) VALUES ('avito', ?, 'client_id', 'x', ?)", (account, NOW))
        conn.commit()
    return conn, account


def feed_path(tmp_path, conn, account):
    return tmp_path / "feed" / connections.get_account(conn, account)["feed_token"] / "next" / "avito_feed.xml"


def test_a_connected_account_without_its_phone_is_not_built_and_says_why(tmp_path):
    conn, account = account_with_keys(tmp_path)
    assert connections.has_avito_keys(conn, account) is True
    results = cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in results] == [False] and "Проверить подключение" in results[0].error
    assert not feed_path(tmp_path, conn, account).exists()                                 # nothing is written: the published feed stays as it was


def test_after_the_check_the_feed_carries_the_phone_of_the_cabinet_not_the_general_number(tmp_path):
    conn, account = account_with_keys(tmp_path)
    connections.set_avito_identity(conn, account, 30000002, "Кабинет", "79000000012")
    results = cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in results] == [True], results
    phones = {a.findtext("ContactPhone") for a in ET.parse(feed_path(tmp_path, conn, account)).getroot()}
    assert phones == {"+79000000012"} and "+70000000000" not in phones


def test_an_account_without_keys_is_not_judged(tmp_path):
    conn, account = account_with_keys(tmp_path, keys=False)
    assert connections.has_avito_keys(conn, account) is False
    results = cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in results] == [True]


def test_a_connected_mirror_never_falls_back_to_the_phone_of_the_file(tmp_path):
    conn, account = account_with_keys(tmp_path)
    ads = '<Ads><Ad><Id>100_1</Id><Title>T</Title><Price>1</Price><ContactPhone>+7 900 000-00-13</ContactPhone></Ad></Ads>'.encode()
    mirror_feed.import_feed(conn, account, ads, NOW)
    results = cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in results] == [False] and not feed_path(tmp_path, conn, account).exists()
    connections.set_avito_identity(conn, account, 1, "n", "79000000012")
    assert [r.ok for r in cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")] == [True]
    assert {a.findtext("ContactPhone") for a in ET.parse(feed_path(tmp_path, conn, account)).getroot()} == {"+79000000012"}


def test_one_failing_account_does_not_stop_the_others(tmp_path):
    conn, first = account_with_keys(tmp_path)
    second = connections.create_account(conn, "Второй", NOW, "o")
    connections.set_avito_identity(conn, second, 2, "n", "79000000014")
    conn.execute("INSERT INTO secret (kind, owner_id, name, ciphertext, updated) VALUES ('avito', ?, 'client_id', 'x', ?)", (second, NOW))
    conn.commit()
    results = {r.account_id: r.ok for r in cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")}
    assert results == {first: False, second: True}


def test_the_owner_can_keep_the_phone_of_a_mirrored_file_and_nothing_replaces_or_blocks_it(tmp_path):
    conn, account = account_with_keys(tmp_path)
    ads = '<Ads><Ad><Id>100_1</Id><Title>T</Title><Price>1</Price><ContactPhone>+7 900 000-00-13</ContactPhone></Ad></Ads>'.encode()
    mirror_feed.import_feed(conn, account, ads, NOW)
    connections.set_phone_source(conn, account, "file", NOW, "o")
    results = cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")           # no cabinet phone known, and it is not required
    assert [r.ok for r in results] == [True], results
    assert {a.findtext("ContactPhone") for a in ET.parse(feed_path(tmp_path, conn, account)).getroot()} == {"+7 900 000-00-13"}
    connections.set_avito_identity(conn, account, 1, "n", "79000000012")                                  # a later check records the cabinet's phone: still not used
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    assert {a.findtext("ContactPhone") for a in ET.parse(feed_path(tmp_path, conn, account)).getroot()} == {"+7 900 000-00-13"}
    connections.set_phone_source(conn, account, "cabinet", NOW, "o")
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    assert {a.findtext("ContactPhone") for a in ET.parse(feed_path(tmp_path, conn, account)).getroot()} == {"+79000000012"}
