"""Hotfix 2026-10-21: the supplier emptied SPICTURE of positions whose photo we already keep (<dir of the base>/img/<code>.jpg, served by
/img/<token>/<code>.jpg). Such an ad keeps the same link of the account, so Avito sees no change and the feed check passes; without a kept
photo the ad has no Images and the rule «photo» stops the account, as before; positions with a picture are not touched."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from helpers import catalog_xml, commodity_xml
from test_feed_account_rules import REF, feed, two_accounts
from tires_sync import connections, cycle, photo_cache

WITH = catalog_xml(commodity_xml(NNOMMODIF="100"), commodity_xml(NNOMMODIF="101", SMNFCODE="TS2"))
WITHOUT_101 = catalog_xml(commodity_xml(NNOMMODIF="100"), commodity_xml(NNOMMODIF="101", SMNFCODE="TS2", SPICTURE=""))


def _keep_photo(tmp_path, code):
    folder = tmp_path / "img"
    folder.mkdir(exist_ok=True)
    (folder / ("%s.jpg" % code)).write_bytes(b"\xff\xd8\xff\xe0jpeg")


def _urls(tmp_path, conn, account):
    return {a.findtext("Id").split("_")[0]: (a.find("Images/Image").get("url") if a.find("Images/Image") is not None else None)
            for a in feed(tmp_path, conn, account)}


def test_a_kept_photo_keeps_the_link_in_both_accounts_and_the_check_passes(tmp_path, caplog):
    conn, first, second = two_accounts(tmp_path)
    _keep_photo(tmp_path, "101")
    results = cycle.run_for_accounts(conn, WITHOUT_101, REF, date(2026, 10, 21), tmp_path / "feed")
    assert all(r.ok and "проверка фида не пройдена" not in (r.error or "") for r in results), [r.error for r in results]
    for n in (first, second):
        token = connections.get_account(conn, n)["feed_token"]
        assert _urls(tmp_path, conn, n) == {"100": "https://panel.example/img/%s/100.jpg" % token, "101": "https://panel.example/img/%s/101.jpg" % token}
    assert "фото из кэша: 1 позиций (поставщик убрал картинку)" in caplog.text


def test_without_a_kept_photo_the_rule_photo_still_stops_the_account(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    results = cycle.run_for_accounts(conn, WITHOUT_101, REF, date(2026, 10, 21), tmp_path / "feed")
    assert _urls(tmp_path, conn, first)["101"] is None
    assert any("photo" in (r.error or "") or "фото" in (r.error or "").lower() for r in results), [r.error for r in results]


def test_positions_with_a_picture_give_the_same_feed_with_or_without_kept_photos(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    cycle.run_for_accounts(conn, WITH, REF, date(2026, 10, 21), tmp_path / "feed")
    before = {n: ET.tostring(feed(tmp_path, conn, n)) for n in (first, second)}
    _keep_photo(tmp_path, "100")
    _keep_photo(tmp_path, "101")
    cycle.run_for_accounts(conn, WITH, REF, date(2026, 10, 21), tmp_path / "feed")
    assert {n: ET.tostring(feed(tmp_path, conn, n)) for n in (first, second)} == before


def test_the_cache_is_the_one_the_photo_route_serves(tmp_path):
    from panel_helpers import make_app
    app = make_app(tmp_path)
    from tires_sync.panel import routes_img
    assert routes_img.cache_dir(app) == photo_cache.root(app.config["DB_PATH"]) == tmp_path / "img"
    _keep_photo(tmp_path, "100")
    assert photo_cache.cached(tmp_path / "img", "100") == tmp_path / "img" / "100.jpg" and photo_cache.cached(tmp_path / "img", "999") is None
    assert photo_cache.cached(tmp_path / "img", "../t") is None


def test_the_account_s_look_and_an_adopted_link_apply_to_a_kept_photo_as_to_any_other(tmp_path):
    import json
    from tires_sync import store
    conn, first, second = two_accounts(tmp_path)
    _keep_photo(tmp_path, "101")
    connections.set_photo_style(conn, first, {"label": "Один", "border": "#00FF00"}, "2026-10-21T10:00:00", "t")      # ?s=<version>
    store.set_state(conn, "adopted_%d" % second, json.dumps({"ids": {"101": "77777"}, "images": {"101": "https://old.example/77777.jpg"}}))
    cycle.run_for_accounts(conn, WITHOUT_101, REF, date(2026, 10, 21), tmp_path / "feed")
    one, two = _urls(tmp_path, conn, first), {a.findtext("Id"): a.find("Images/Image").get("url") for a in feed(tmp_path, conn, second)}
    token = connections.get_account(conn, first)["feed_token"]
    assert one["101"].startswith("https://panel.example/img/%s/101.jpg?s=" % token) and one["100"].split("?")[1] == one["101"].split("?")[1]
    assert two["77777"] == "https://old.example/77777.jpg"                                         # the adopted ad keeps its photo link
