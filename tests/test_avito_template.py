"""Avito's own template of the category (14-10-2026, saved in src/tires_sync/data/avito-templates): a feed may carry only the tags the template has."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

from helpers import make_offer, make_settings
from tires_sync import connections, mirror_feed, store
from tires_sync.candidates import Candidate
from tires_sync.feed import render_ad, validate_ad
from tires_sync.settings_store import init_settings
from helpers import settings_dict

TEMPLATE = Path(__file__).resolve().parent.parent / "src" / "tires_sync" / "data" / "avito-templates" / "legkovye-shiny-2026-10-14.xml"
NOW = "2026-10-14T12:00:00"


def template_tags():
    return {child.tag for child in ET.parse(TEMPLATE).getroot().find("Ad")}


def test_our_own_ad_has_only_tags_of_the_template():
    cand = Candidate("100", make_offer(goods_id="100", stock=12, runflat=True, mnf_year="2024"), 4810, "Ikon Tyres", "Test Model")
    for pack in (1, 4):
        ad = render_ad(cand, 4810, "P1", make_settings(), date(2026, 10, 14), pack=pack)
        assert {c.tag for c in ad} <= template_tags() | {"Images"}, {c.tag for c in ad} - template_tags()
        assert validate_ad(ad) == []


def test_a_mirrored_ad_loses_the_tags_the_template_no_longer_has(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Партнёр", NOW, "o")
    file = ('<Ads><Ad><Id>1_1</Id><Title>T</Title><Price>1</Price><TargetAudience>Частные лица и бизнес</TargetAudience><NDS>Без НДС</NDS>'
            '<WholesaleType>Нет</WholesaleType><MultiItem>Да</MultiItem><ContactPhone>+79990000001</ContactPhone></Ad></Ads>').encode()
    mirror_feed.import_feed(conn, account, file, NOW)
    from tires_sync.config import settings_from_dict
    out = ET.fromstring(mirror_feed.render(conn, account, date(2026, 10, 14), settings_from_dict(settings_dict(), enforce_pct_limit=False)))
    tags = {c.tag for ad in out for c in ad}
    assert "TargetAudience" not in tags
    assert tags <= template_tags() | {"Images"}, tags - template_tags()
    assert {"NDS", "WholesaleType", "MultiItem"} <= tags                         # these are in the template: kept as the file has them
