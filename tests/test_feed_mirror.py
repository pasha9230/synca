"""The mirror of a feed that somebody keeps by hand: every ad is carried over as it is, with the same Id, and the dates are kept alive."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pytest

from helpers import settings_dict
from tires_sync import connections, cycle, mirror_feed, store
from tires_sync.cli import main
from tires_sync.feed import stable_date_end
from tires_sync.publish import promote_feed
from tires_sync.settings_store import init_settings

NOW = "2026-10-13T12:00:00"


def ad(ad_id, title="Ikon 185/55 R15", price="5710", date_end="2026-11-11", images=True):
    pictures = '<Images><Image url="https://img.example/%s.jpg"/></Images>' % ad_id if images else ""
    return ('<Ad><Id>%s</Id><Category>Запчасти и аксессуары</Category><DateEnd>%s</DateEnd><Title>%s</Title><Price>%s</Price>'
            '<Quantity>за 1 шт.</Quantity><Description>Строка 1\n\nСтрока 2 &amp; &lt;b&gt;</Description>%s</Ad>' % (ad_id, date_end, title, price, pictures))


def feed(*ads):
    return ('<?xml version="1.0" encoding="utf-8"?><Ads formatVersion="3" target="Avito.ru">%s</Ads>' % "".join(ads)).encode("utf-8")


def setup(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Чужой", NOW, "o")
    return conn, account


def test_the_import_keeps_every_ad_with_its_own_id_and_replaces_the_previous_import(tmp_path):
    conn, account = setup(tmp_path)
    ids = ["118649107660_20260812122712", "110650506879_20260716165744", "5"]
    result = mirror_feed.import_feed(conn, account, feed(*[ad(i) for i in ids]), NOW)
    assert result == {"ads": 3, "with_images": 3} and connections.get_account(conn, account)["feed_mode"] == "mirror"
    assert mirror_feed.stored_ids(conn, account) == ids
    mirror_feed.import_feed(conn, account, feed(ad("a_1"), ad("a_2")), NOW)
    assert mirror_feed.stored_ids(conn, account) == ["a_1", "a_2"]


@pytest.mark.parametrize("bad", [b"", b"not xml", b"<Ads></Ads>", b"<Other><Ad><Id>1</Id></Ad></Other>",
                                 feed(ad("1"), ad("1")), feed("<Ad><Title>no id</Title></Ad>"), feed("<Ad><Id>  </Id></Ad>")])
def test_a_broken_file_is_refused_and_leaves_the_account_as_it_was(tmp_path, bad):
    conn, account = setup(tmp_path)
    mirror_feed.import_feed(conn, account, feed(ad("keep")), NOW)
    with pytest.raises(mirror_feed.MirrorError):
        mirror_feed.import_feed(conn, account, bad, NOW)
    assert mirror_feed.stored_ids(conn, account) == ["keep"]


def test_the_rendered_feed_is_the_same_ads_with_only_the_date_end_moved(tmp_path):
    conn, account = setup(tmp_path)
    source = feed(ad("a_1", date_end="2026-10-20"), ad("a_2", images=False, date_end="2026-11-11"))
    mirror_feed.import_feed(conn, account, source, NOW)
    settings = settings_dict_end_days()
    out = mirror_feed.render(conn, account, date(2026, 10, 13), settings)
    root = ET.fromstring(out)
    assert root.tag == "Ads" and root.get("formatVersion") == "3" and [a.findtext("Id") for a in root] == ["a_1", "a_2"]
    old = {a.findtext("Id"): a for a in ET.fromstring(source)}
    for new in root:
        before = old[new.findtext("Id")]
        moved = ("DateEnd", "Quantity", "MultiItem", "WholesaleType")     # no Quantity in Avito's tire format; the owner's terms of sale (2026-10-20)
        assert (new.findtext("MultiItem"), new.findtext("WholesaleType")) == ("Да", "Нет")
        assert {c.tag: ET.tostring(c) for c in new if c.tag not in moved} == {c.tag: ET.tostring(c) for c in before if c.tag not in moved}
        assert new.findtext("DateEnd") == stable_date_end(date(2026, 10, 13), settings.date_end_days, settings.date_end_renew_days).isoformat()
    assert root[0].findtext("Description") == "Строка 1\n\nСтрока 2 & <b>"                               # text with markup characters comes back intact
    assert root[0].find("Images") is not None and root[1].find("Images") is None


def settings_dict_end_days():
    from tires_sync.config import settings_from_dict
    return settings_from_dict(settings_dict(), enforce_pct_limit=False)


def test_the_cycle_writes_the_mirror_for_a_mirror_account_and_still_builds_the_others(tmp_path):
    conn, account = setup(tmp_path)
    other = connections.create_account(conn, "Основной", NOW, "o")
    mirror_feed.import_feed(conn, account, feed(ad("a_1"), ad("a_2")), NOW)
    out = mirror_feed.write_candidate(conn, connections.get_account(conn, account), tmp_path / "feed", date(2026, 10, 13), settings_dict_end_days())
    path = tmp_path / "feed" / connections.get_account(conn, account)["feed_token"] / "next" / "avito_feed.xml"
    assert out == 2 and path.exists() and len(ET.fromstring(path.read_bytes())) == 2
    assert not (tmp_path / "feed" / connections.get_account(conn, other)["feed_token"]).exists()               # the other account is not touched by it


def test_the_candidate_can_be_promoted_and_nothing_is_lost_on_the_way(tmp_path):
    conn, account = setup(tmp_path)
    ids = ["a_%d" % n for n in range(10)]
    mirror_feed.import_feed(conn, account, feed(*[ad(i) for i in ids]), NOW)
    acc = connections.get_account(conn, account)
    mirror_feed.write_candidate(conn, acc, tmp_path / "feed", date(2026, 10, 13), settings_dict_end_days())
    folder = tmp_path / "feed" / acc["feed_token"]
    assert promote_feed(folder)["new"] == 10
    assert mirror_feed.missing_ids(conn, account, (folder / "avito_feed.xml").read_bytes()) == []
    assert mirror_feed.missing_ids(conn, account, feed(*[ad(i) for i in ids[:7]])) == ids[7:]


def test_the_cli_imports_a_file_and_refuses_a_bad_one(tmp_path, capsys):
    conn, account = setup(tmp_path)
    conn.close()
    good, bad = tmp_path / "good.xml", tmp_path / "bad.xml"
    good.write_bytes(feed(ad("a_1"), ad("a_2"), ad("a_3")))
    bad.write_bytes(b"<Ads><Ad></Ad></Ads>")
    db = str(tmp_path / "x.db")
    assert main(["feed-import", "--db", db, "--account", str(account), "--file", str(bad)]) != 0
    assert main(["feed-import", "--db", db, "--account", str(account), "--file", str(good)]) == 0
    assert "3" in capsys.readouterr().out and mirror_feed.stored_ids(store.connect(db), account) == ["a_1", "a_2", "a_3"]


class _Offer:
    def __init__(self, stock):
        self.stock = stock


def test_ads_the_supplier_does_not_have_or_has_too_few_of_are_left_out_of_the_mirror(tmp_path):
    conn, account = setup(tmp_path)
    mirror_feed.import_feed(conn, account, feed(ad("100_1"), ad("101_1"), ad("102_1"), ad("103_1")), NOW)
    offers = {"100": _Offer(12), "101": _Offer(2), "102": _Offer(4)}                                # 101: below the threshold of 4; 103: not in the unload
    skip = mirror_feed.unavailable(conn, account, offers, 4)
    assert skip == {"101_1": "остаток 2 меньше порога 4", "103_1": "нет у поставщика"}
    out = mirror_feed.render(conn, account, date(2026, 10, 13), settings_dict_end_days(), skip=set(skip))
    assert [a.findtext("Id") for a in ET.fromstring(out)] == ["100_1", "102_1"]
    assert mirror_feed.missing_ids(conn, account, out, skip=set(skip)) == []                          # what was left out on purpose is not a loss
    assert mirror_feed.missing_ids(conn, account, out) == ["101_1", "103_1"]
    assert mirror_feed.unavailable(conn, account, {"100": _Offer(9), "101": _Offer(9), "102": _Offer(9), "103": _Offer(9)}, 4) == {}


def test_the_pack_suffix_and_the_stamp_do_not_hide_the_supplier_code(tmp_path):
    conn, account = setup(tmp_path)
    mirror_feed.import_feed(conn, account, feed(ad("100-x4"), ad("101_20260812122712")), NOW)
    assert mirror_feed.unavailable(conn, account, {"100": _Offer(1), "101": _Offer(9)}, 4) == {"100-x4": "остаток 1 меньше порога 4"}


def test_the_cycle_leaves_unavailable_ads_out_of_a_mirror_account(tmp_path):
    conn, account = setup(tmp_path)
    mirror_feed.import_feed(conn, account, feed(ad("100_1"), ad("200_1")), NOW)

    class Parsed:
        offers = {"100": _Offer(10)}
        issues = {}
    results = cycle.run_for_accounts(conn, b"<x/>", {}, date(2026, 10, 13), tmp_path / "feed", parsed=Parsed(), input_sha="s", now=None)
    assert [(r.ok, r.feed_count) for r in results] == [(True, 1)]


def test_the_price_follows_the_supplier_and_only_the_price_of_the_ad_changes(tmp_path):
    conn, account = setup(tmp_path)
    text = "Старый текст\n💰 Цена: 5710 руб.\nПозвоните"
    source = ('<Ads><Ad><Id>100_1</Id><Price>5710</Price><Title>T</Title><Description>%s</Description></Ad>'
              '<Ad><Id>101_1</Id><Price>900</Price><Title>U</Title><Description>%s</Description></Ad>'
              '<Ad><Id>102-x4</Id><Price>2000</Price><Title>V</Title><Description>Цена: 2000 руб.</Description></Ad></Ads>' % (text, text)).encode()
    mirror_feed.import_feed(conn, account, source, NOW)
    out = ET.fromstring(mirror_feed.render(conn, account, date(2026, 10, 13), settings_dict_end_days(),
                                           prices={"100_1": 6100, "102-x4": 3000}))
    first, second, pack = list(out)
    assert first.findtext("Price") == "6100" and "Цена: 6100 руб." in first.findtext("Description")
    assert first.findtext("Description").startswith("Старый текст") and first.findtext("Title") == "T"
    assert second.findtext("Price") == "900" and "5710" in second.findtext("Description")            # no price from the supplier: left as it was
    assert pack.findtext("Price") == "3000"


def test_prices_are_counted_with_the_pricing_rule_of_the_account_and_a_pack_costs_its_count(tmp_path):
    from tires_sync.models import PricingRule
    from types import SimpleNamespace as NS
    conn, account = setup(tmp_path)
    mirror_feed.import_feed(conn, account, feed(ad("100_1"), ad("101-x4"), ad("102_1"), ad("103_1")), NOW)
    rule = PricingRule(pct_small=10.0, pct_large=20.0)
    settings = NS(pricing={("ikon", "winter"): rule})
    def offer(base, brand="ikon", season="winter", diameter=15):
        return NS(base_price=base, brand_key=brand, season=season, diameter=diameter, stock=8)
    offers = {"100": offer(5000), "101": offer(1000), "102": offer(5000, brand="other"), "103": offer(0)}
    assert mirror_feed.prices_for(conn, account, offers, settings) == {"100_1": 5500, "101-x4": 4400}      # no rule or a bad price: no entry


def test_the_old_quantity_and_any_minimum_order_are_dropped(tmp_path):
    conn, account = setup(tmp_path)
    source = ('<Ads><Ad><Id>1_1</Id><Price>1</Price><Quantity>за 1 шт.</Quantity></Ad>'
              '<Ad><Id>2_1</Id><Price>1</Price><Quantity>за 4 шт.</Quantity></Ad>'
              '<Ad><Id>3_1</Id><Price>1</Price><Quantity>за 2 шт.</Quantity><MinimumOrderQuantity>по 4 шт.</MinimumOrderQuantity></Ad></Ads>').encode()
    mirror_feed.import_feed(conn, account, source, NOW)
    one, four, both = list(ET.fromstring(mirror_feed.render(conn, account, date(2026, 10, 13), settings_dict_end_days())))
    assert one.find("Quantity") is None and one.find("MinimumOrderQuantity") is None
    assert four.find("Quantity") is None and four.find("MinimumOrderQuantity") is None                # the owner, 2026-10-20: no minimum order
    assert both.find("Quantity") is None and both.find("MinimumOrderQuantity") is None                # not even the one the file has
    assert all((a.findtext("MultiItem"), a.findtext("WholesaleType")) == ("Да", "Нет") for a in (one, four, both))


def test_runflat_is_sent_explicitly_from_the_supplier(tmp_path):
    from types import SimpleNamespace as NS
    conn, account = setup(tmp_path)
    mirror_feed.import_feed(conn, account, feed(ad("1_1"), ad("2_1"), ad("3_1")), NOW)
    offers = {"1": NS(runflat=True), "2": NS(runflat=False)}                                  # 3: not in the unload, nothing to say
    flags = mirror_feed.runflat_for(conn, account, offers)
    assert flags == {"1_1": "Да", "2_1": "Нет"}
    out = {a.findtext("Id"): a.findtext("RunFlat") for a in ET.fromstring(
        mirror_feed.render(conn, account, date(2026, 10, 13), settings_dict_end_days(), runflat=flags))}
    assert out == {"1_1": "Да", "2_1": "Нет", "3_1": None}
