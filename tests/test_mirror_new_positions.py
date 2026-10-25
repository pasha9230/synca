"""A mirrored account: the ads of the file keep their Ids and texts; positions the file did not have are added by our settings with the account's own text template."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from helpers import catalog_xml, commodity_xml, settings_dict
from tires_sync import connections, cycle, mirror_feed, publishing, store
from tires_sync.catalog import parse_reference
from tires_sync.settings_store import init_settings

NOW = "2026-10-14T10:00:00"
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
STORED = ('<Ads><Ad><Id>100_20260801</Id><Price>4000</Price><Title>Заголовок Партнёра</Title>'
          '<Description>Текст Партнёра. Цена: 4000 руб.</Description></Ad></Ads>').encode()


def setup(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Чужой", NOW, "o")
    mirror_feed.import_feed(conn, account, STORED, NOW)
    connections.set_description_template(conn, account, "ШАБЛОН ПАРТНЁРА {title} / {code} / {price} руб.", NOW, "o")
    return conn, account


def unload():
    return catalog_xml(commodity_xml(NNOMMODIF="100"), commodity_xml(NNOMMODIF="200", SMNFCODE="NEW2", SMODIFNAME_ORIG="Ikon  215/60/16  T 95 Test Model",
                                                                 SMODIFNAME="IKON 215/60/16 T 95 Test Model", SWIDTH="215", SHEIGHT="60", SLOAD="95"))


def feed_of(tmp_path, conn, account):
    token = connections.get_account(conn, account)["feed_token"]
    return ET.parse(tmp_path / "feed" / token / "next" / "avito_feed.xml").getroot()


def test_a_new_position_joins_the_mirror_with_our_settings_and_the_template_of_the_account(tmp_path):
    conn, account = setup(tmp_path)
    results = cycle.run_for_accounts(conn, unload(), REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in results] == [True], results
    ads = {a.findtext("Id"): a for a in feed_of(tmp_path, conn, account)}
    assert set(ads) == {"100_20260801", "200"}
    assert ads["100_20260801"].findtext("Title") == "Заголовок Партнёра"                       # the ad of the file: his Id and his text
    assert ads["100_20260801"].findtext("Description").startswith("Текст Партнёра")
    new = ads["200"]
    assert new.findtext("Description").startswith("ШАБЛОН ПАРТНЁРА") and "NEW2" in new.findtext("Description")
    assert new.findtext("Price") == "4810" and new.findtext("Brand")                         # priced by the settings of the account


def test_the_new_position_is_remembered_and_the_published_feed_commits_it(tmp_path):
    conn, account = setup(tmp_path)
    cycle.run_for_accounts(conn, unload(), REF, date(2026, 10, 14), tmp_path / "feed")
    connections.set_live(conn, account, True, NOW, "o")
    conn.commit()
    result = publishing.publish_live_accounts(conn, tmp_path / "feed")
    assert result[0]["ok"], result
    assert store.get_snapshot(conn, "200", account) is not None                              # known as published: the next cycle will keep it, not publish it again
    assert store.get_snapshot(conn, "100", account) is None                                  # the file's ads are not ours to track
    cycle.run_for_accounts(conn, unload(), REF, date(2026, 10, 15), tmp_path / "feed")
    assert {a.findtext("Id") for a in feed_of(tmp_path, conn, account)} == {"100_20260801", "200"}


def test_a_new_position_that_the_supplier_drops_leaves_the_feed_and_the_file_ads_stay(tmp_path):
    conn, account = setup(tmp_path)
    cycle.run_for_accounts(conn, unload(), REF, date(2026, 10, 14), tmp_path / "feed")
    connections.set_live(conn, account, True, NOW, "o")
    conn.commit()
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    only_old = catalog_xml(commodity_xml(NNOMMODIF="100"))
    cycle.run_for_accounts(conn, only_old, REF, date(2026, 10, 15), tmp_path / "feed")
    assert "200" not in {a.findtext("Id") for a in feed_of(tmp_path, conn, account)}


def test_new_positions_are_added_when_the_cycle_gets_the_unload_already_parsed(tmp_path):
    from tires_sync.webmim import parse_unload
    conn, account = setup(tmp_path)
    parsed = parse_unload(unload(), "T1")
    cycle.run_for_accounts(conn, None, REF, date(2026, 10, 14), tmp_path / "feed", parsed=parsed, input_sha="x")      # how the timer calls it
    assert {a.findtext("Id") for a in feed_of(tmp_path, conn, account)} == {"100_20260801", "200"}


def test_a_new_ad_gets_the_fields_and_the_phone_of_the_ads_of_the_file(tmp_path):
    donor = ('<Ads><Ad><Id>100_20260801</Id><Price>4000</Price><Title>T</Title><ContactPhone>+7 981 000-00-00</ContactPhone><Quantity>за 1 шт.</Quantity>'
             '<TargetAudience>Частные лица и бизнес</TargetAudience><NDS>Без НДС</NDS><WholesaleType>Нет</WholesaleType><MultiItem>Да</MultiItem>'
             '<Description>Д</Description></Ad></Ads>').encode()
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Чужой", NOW, "o")
    mirror_feed.import_feed(conn, account, donor, NOW)
    cycle.run_for_accounts(conn, unload(), REF, date(2026, 10, 14), tmp_path / "feed")
    new = {a.findtext("Id"): a for a in feed_of(tmp_path, conn, account)}["200"]
    assert new.find("Quantity") is None and new.find("TargetAudience") is None          # Avito: no Quantity (selling by one is no parameter), no TargetAudience (template of 14-10-2026)
    assert new.findtext("NDS") == "Без НДС" and new.findtext("WholesaleType") == "Нет" and new.findtext("MultiItem") == "Да"
    assert new.findtext("ContactPhone") == "+7 981 000-00-00"
    assert new.findtext("RunFlat") == "Нет"                                                    # sent explicitly
    assert new.find("MinimumOrderQuantity") is None                                          # the account sells by one, like its ads


def test_the_new_positions_of_a_mirror_are_not_held_back_by_the_shrink_check_of_their_own_small_feed(tmp_path):
    conn, account = setup(tmp_path)
    many = catalog_xml(commodity_xml(NNOMMODIF="100"), *[commodity_xml(NNOMMODIF=str(n), SMNFCODE="N%d" % n) for n in (201, 202, 203)])
    cycle.run_for_accounts(conn, many, REF, date(2026, 10, 14), tmp_path / "feed")
    connections.set_live(conn, account, True, NOW, "o")
    conn.commit()
    assert publishing.publish_live_accounts(conn, tmp_path / "feed")[0]["ok"]
    one_left = catalog_xml(commodity_xml(NNOMMODIF="100"), commodity_xml(NNOMMODIF="201", SMNFCODE="N201"))
    results = cycle.run_for_accounts(conn, one_left, REF, date(2026, 10, 15), tmp_path / "feed")
    assert [r.ok for r in results] == [True] and "shrank" not in results[0].error and "новые позиции не добавлены" not in results[0].error   # no «feed shrank» for the little feed of new positions
    assert {a.findtext("Id") for a in feed_of(tmp_path, conn, account)} == {"100_20260801", "201"}
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    assert store.get_snapshot(conn, "202", account) is None and store.get_snapshot(conn, "201", account) is not None


def test_no_new_positions_is_not_a_warning_every_cycle(tmp_path):
    conn, account = setup(tmp_path)
    results = cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NNOMMODIF="100")), REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in results] == [True] and "новые позиции не добавлены" not in results[0].error


def test_a_new_position_has_no_minimum_order_even_when_the_ads_of_the_file_have_one(tmp_path):
    donor = ('<Ads><Ad><Id>100_20260801</Id><Price>4000</Price><Title>T</Title><MinimumOrderQuantity>по 4 шт.</MinimumOrderQuantity>'
             '<WholesaleType>Да</WholesaleType><MultiItem>Нет</MultiItem>'
             '<Description>Д</Description></Ad></Ads>').encode()
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Чужой", NOW, "o")
    mirror_feed.import_feed(conn, account, donor, NOW)
    connections.set_account_pricing(conn, account, 0.0, 2, NOW, "o")                      # this account publishes from two tires
    many = commodity_xml(NNOMMODIF="300", SMNFCODE="MANY", NREST="12")
    few = commodity_xml(NNOMMODIF="301", SMNFCODE="FEW", NREST="2")
    cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NNOMMODIF="100"), many, few), REF, date(2026, 10, 14), tmp_path / "feed")
    ads = {a.findtext("Id"): a for a in feed_of(tmp_path, conn, account)}
    for code in ("100_20260801", "300", "301"):                                               # the owner, 2026-10-20: the file's own ad too
        assert ads[code].find("MinimumOrderQuantity") is None
        assert (ads[code].findtext("MultiItem"), ads[code].findtext("WholesaleType")) == ("Да", "Нет")
