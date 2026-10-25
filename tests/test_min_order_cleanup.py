"""Dead minimum-order code removed (owner's terms of sale, 2026-10-20): the feeds must stay byte for byte what they were.

The golden file was taken on `origin/master` before the clean-up (set REGENERATE_MIN_ORDER_GOLDEN=1 to retake it, only on a known-good tree).
Scenarios: our own build with different stock, and the mirror (ads of the file with Quantity / MinimumOrderQuantity of every kind, the first ad of
the file as the pattern for new positions: with a minimum order, with `Quantity` only, with neither)."""
from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

from helpers import catalog_xml, commodity_xml, settings_dict
from tires_sync import connections, cycle, mirror_feed, store
from tires_sync.catalog import parse_reference
from tires_sync.settings_store import init_settings

NOW = "2026-10-14T10:00:00"
TODAY = date(2026, 10, 14)
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
GOLDEN = Path(__file__).with_name("test_min_order_cleanup_golden.json")


def _stock(*rests, file_codes=()):
    items = [commodity_xml(NNOMMODIF="100")] + [commodity_xml(NNOMMODIF=c, SMNFCODE="F" + c) for c in file_codes]
    for n, rest in enumerate(rests, start=1):
        items.append(commodity_xml(NNOMMODIF=str(200 + n), SMNFCODE="N%d" % n, NREST=str(rest)))
    return catalog_xml(*items)


def _conn(tmp_path, name):
    conn = store.connect(tmp_path / (name + ".db"))
    init_settings(conn, settings_dict(), NOW)
    return conn


def _feed_text(tmp_path, conn, account) -> str:
    token = connections.get_account(conn, account)["feed_token"]
    return (tmp_path / "feed" / token / "next" / "avito_feed.xml").read_text(encoding="utf-8").replace(token, "TOKEN")


def _own(tmp_path):
    conn = _conn(tmp_path, "own")
    account = connections.create_account(conn, "Основной", NOW, "o")
    cycle.run_for_accounts(conn, _stock(12, 8, 5, 4, 3, 2, 1), REF, TODAY, tmp_path / "feed")
    return _feed_text(tmp_path, conn, account)


FILE_ADS = ('<Ad><Id>100_20260801</Id><Price>4000</Price><Title>A</Title><Description>D. Цена: 4000 руб.</Description>%s</Ad>'
            '<Ad><Id>1_1</Id><Price>1</Price><Quantity>за 1 шт.</Quantity><TargetAudience>Частные лица</TargetAudience></Ad>'
            '<Ad><Id>2_1</Id><Price>1</Price><Quantity>за 4 шт.</Quantity><MultiItem>Нет</MultiItem></Ad>'
            '<Ad><Id>3_1</Id><Price>1</Price><Quantity>за 2 шт.</Quantity><MinimumOrderQuantity>по 4 шт.</MinimumOrderQuantity>'
            '<WholesaleType>Да</WholesaleType></Ad>'
            '<Ad><Id>4_1</Id><Price>1</Price><MinimumOrderQuantity>по 2 шт.</MinimumOrderQuantity></Ad>')


def _mirror(tmp_path, name, first_ad_tags, pricing_from):
    conn = _conn(tmp_path, name)
    account = connections.create_account(conn, "Чужой", NOW, "o")
    mirror_feed.import_feed(conn, account, ("<Ads>" + FILE_ADS % first_ad_tags + "</Ads>").encode(), NOW)
    connections.set_account_pricing(conn, account, 0.0, pricing_from, NOW, "o")
    cycle.run_for_accounts(conn, _stock(12, 5, 2, 1, file_codes=("1", "2", "3", "4")), REF, TODAY, tmp_path / "feed")
    return _feed_text(tmp_path, conn, account)


def build_all(tmp_path) -> dict:
    return {
        "own": _own(tmp_path),
        "mirror_pattern_with_minimum": _mirror(tmp_path, "m1", "<MinimumOrderQuantity>по 4 шт.</MinimumOrderQuantity><MultiItem>Нет</MultiItem>", 2),
        "mirror_pattern_quantity_only": _mirror(tmp_path, "m2", "<Quantity>за 4 шт.</Quantity><WholesaleType>Да</WholesaleType>", 2),
        "mirror_pattern_quantity_one": _mirror(tmp_path, "m3", "<Quantity>за 1 шт.</Quantity>", 4),
        "mirror_pattern_bare": _mirror(tmp_path, "m4", "", 4),
    }


def test_feeds_are_unchanged_by_the_clean_up(tmp_path):
    built = build_all(tmp_path)
    if os.environ.get("REGENERATE_MIN_ORDER_GOLDEN"):
        GOLDEN.write_text(json.dumps(built, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert set(built) == set(golden)
    for key in golden:
        assert built[key] == golden[key], key


def test_the_scenarios_really_exercise_the_mirror_and_the_own_build(tmp_path):
    built = build_all(tmp_path)
    own = ET.fromstring(built["own"])
    assert len(own.findall("Ad")) >= 4
    for key, text in built.items():
        assert "MinimumOrderQuantity" not in text and "<Quantity>" not in text and "TargetAudience" not in text, key
    for key in ("mirror_pattern_with_minimum", "mirror_pattern_quantity_only"):
        ids = [a.findtext("Id") for a in ET.fromstring(built[key]).findall("Ad")]
        assert {"100_20260801", "1_1", "2_1", "3_1", "4_1"} <= set(ids) and len(ids) > 5, key       # file ads + new positions
