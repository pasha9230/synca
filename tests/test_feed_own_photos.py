"""Own photos in `Images` (spec 2026-10-20): three_quarter, set4, card, crop, then the supplier's link; every account its own files.
With the switch `own_photos` off the feed is exactly as before."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

from helpers import catalog_xml, commodity_xml, settings_dict
from tires_sync import connections, cycle, mirror_feed, photo_library, publishing, store
from tires_sync.catalog import parse_reference
from tires_sync.settings_store import active_settings, init_settings
from tires_sync.webmim import parse_unload

NOW = "2026-10-20T12:00:00"
TODAY = date(2026, 10, 20)
BASE = "https://panel.example"
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
UNLOAD = catalog_xml(commodity_xml(NNOMMODIF="100"))
OFFER = parse_unload(UNLOAD, "T1").offers["100"]
KEY = photo_library.set_key_for(OFFER).text()
X1 = photo_library.card_variant(OFFER, False)
X4 = photo_library.card_variant(OFFER, True)


def env(tmp_path, switch="1", **settings):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(**settings), NOW)
    first = connections.create_account(conn, "Основной", NOW, "o")
    second = connections.create_account(conn, "Партнёр-4", NOW, "o")
    for n in (first, second):
        connections.save_addresses(conn, n, "percent", None, [{"name": "Точка", "address_id": "10%d" % n, "enabled": True, "share": 100, "address_text": ""}], NOW, "o")
    store.set_state(conn, "public_base", BASE)
    if switch is not None:
        store.set_state(conn, "own_photos", switch)
    conn.commit()
    return conn, first, second


def photo(conn, n):
    sha = "%064x" % n
    cur = conn.execute("INSERT INTO photo(sha256, ext, width, height, bytes, source, created) VALUES (?,?,?,?,?,?,?)",
                       (sha, "jpg", 1024, 1536, 1, "test", NOW))
    return cur.lastrowid, photo_library.photo_url(BASE, sha, "jpg")


def frame(conn, kind, n, status="approved"):
    conn.execute("INSERT OR IGNORE INTO photo_set (key, state, in_feed) VALUES (?, 'needed', 1)", (KEY,))   # the owner sent the set to the feed
    pid, url = photo(conn, n)
    conn.execute("INSERT INTO set_frame(set_key, kind, photo_id, status, created) VALUES (?,?,?,?,?)", (KEY, kind, pid, status, NOW))
    return pid, url


def account_photo(conn, account, source, kind, n, variant=""):
    pid, url = photo(conn, n)
    conn.execute("INSERT INTO account_photo(account_id, source_photo_id, kind, variant, photo_id) VALUES (?,?,?,?,?)",
                 (account, source, kind, variant, pid))
    return url


def library(conn, first, second):
    """An approved set with every file of both accounts: {account: {name: link}}."""
    tq, tq_url = frame(conn, "three_quarter", 1)
    s4, s4_url = frame(conn, "set4", 2)
    out = {first: {"tq": tq_url, "set4": s4_url}, second: {}}
    n = 10
    for account in (first, second):
        if account != first:
            out[account]["tq"] = account_photo(conn, account, tq, "copy", n)
            out[account]["set4"] = account_photo(conn, account, s4, "copy", n + 1)
        out[account]["crop"] = account_photo(conn, account, tq, "crop", n + 2)
        out[account]["x1"] = account_photo(conn, account, tq, "card", n + 3, X1)
        out[account]["x4"] = account_photo(conn, account, tq, "card", n + 4, X4)
        n += 10
    conn.commit()
    return out


def feed_bytes(tmp_path, conn, account):
    token = connections.get_account(conn, account)["feed_token"]
    return (tmp_path / "feed" / token / "next" / "avito_feed.xml").read_bytes().replace(token.encode(), b"TOKEN")


def images(tmp_path, conn, account):
    token = connections.get_account(conn, account)["feed_token"]
    root = ET.parse(tmp_path / "feed" / token / "next" / "avito_feed.xml").getroot()
    return {a.findtext("Id"): [i.get("url") for i in a.findall("Images/Image")] for a in root}


def run(tmp_path, conn):
    results = cycle.run_for_accounts(conn, UNLOAD, REF, TODAY, tmp_path / "feed")
    assert [r.ok for r in results] == [True] * len(results), [r.error for r in results]
    return results


def supplier(conn, account, code="100"):
    return "%s/img/%s/%s.jpg" % (BASE, connections.get_account(conn, account)["feed_token"], code)


GOLDEN = Path(__file__).parent / "fixtures" / "own_photos_switch_off.xml"     # made by the code before own photos (f4460a4; re-made at master 9cb239e: identical)


def test_switch_off_feed_unchanged(tmp_path):
    for name, switch in (("unset", None), ("off", "0")):
        folder = tmp_path / name
        folder.mkdir()
        conn, first, second = env(folder, switch, pack4=True)
        library(conn, first, second)
        run(folder, conn)
        assert feed_bytes(folder, conn, first) + b"\n----\n" + feed_bytes(folder, conn, second) == GOLDEN.read_bytes()


def test_order_three_quarter_set4_card_crop_supplier(tmp_path):
    conn, first, second = env(tmp_path)
    links = library(conn, first, second)[first]
    run(tmp_path, conn)
    assert images(tmp_path, conn, first)["100"] == [links["tq"], links["set4"], links["x1"], links["crop"], supplier(conn, first)]


def test_second_account_gets_copies(tmp_path):
    conn, first, second = env(tmp_path)
    links = library(conn, first, second)
    run(tmp_path, conn)
    got = images(tmp_path, conn, second)["100_a%d" % second]
    assert got == [links[second]["tq"], links[second]["set4"], links[second]["x1"], links[second]["crop"], supplier(conn, second)]
    assert not set(got) & set(links[first].values())                                     # never the files of account 1


def test_second_account_without_its_copies_keeps_the_supplier_photo(tmp_path):
    conn, first, second = env(tmp_path)
    frame(conn, "three_quarter", 1)
    frame(conn, "set4", 2)
    conn.commit()
    run(tmp_path, conn)
    assert images(tmp_path, conn, second)["100_a%d" % second] == [supplier(conn, second)]
    assert images(tmp_path, conn, first)["100"][-1] == supplier(conn, first) and len(images(tmp_path, conn, first)["100"]) == 3


def test_half_approved_set_keeps_supplier_photo_only(tmp_path):
    conn, first, second = env(tmp_path)
    tq, _url = frame(conn, "three_quarter", 1)
    frame(conn, "set4", 2, status="pending")
    account_photo(conn, first, tq, "crop", 3)
    account_photo(conn, first, tq, "card", 4, X1)
    conn.commit()
    run(tmp_path, conn)
    assert images(tmp_path, conn, first) == {"100": [supplier(conn, first)]}


def test_single_ad_card_has_no_set_line(tmp_path):
    conn, first, second = env(tmp_path, pack4=True)
    links = library(conn, first, second)[first]
    run(tmp_path, conn)
    got = images(tmp_path, conn, first)
    assert X1.endswith("|x1") and X4.endswith("|x4")
    assert got["100"][2] == links["x1"] and links["x4"] not in got["100"]
    assert got["100-x4"][2] == links["x4"] and links["x1"] not in got["100-x4"]


def test_mirror_account_ours_then_file_photo(tmp_path):
    conn, first, second = env(tmp_path)
    file = ('<Ads><Ad><Id>100_20260815153645</Id><Title>T</Title><Price>1</Price>'
            '<Images><Image url="https://old.example/100.jpg"/></Images></Ad></Ads>').encode()
    mirror_feed.import_feed(conn, second, file, NOW)
    connections.set_avito_identity(conn, second, 2, "Партнёр-4", "79000000012")
    connections.set_phone_source(conn, second, "file", NOW, "o")
    tq, _ = frame(conn, "three_quarter", 1)
    s4, _ = frame(conn, "set4", 2)
    ours = [account_photo(conn, second, tq, "copy", 10), account_photo(conn, second, s4, "copy", 11)]
    conn.commit()
    run(tmp_path, conn)
    assert images(tmp_path, conn, second)["100_20260815153645"] == ours + ["https://old.example/100.jpg"]


def test_mirror_account_with_the_switch_off_keeps_the_file_as_it_is(tmp_path):
    conn, first, second = env(tmp_path, switch="0")
    file = ('<Ads><Ad><Id>100_20260815153645</Id><Title>T</Title><Price>1</Price>'
            '<Images><Image url="https://old.example/100.jpg"/></Images></Ad></Ads>').encode()
    mirror_feed.import_feed(conn, second, file, NOW)
    connections.set_avito_identity(conn, second, 2, "Партнёр-4", "79000000012")
    connections.set_phone_source(conn, second, "file", NOW, "o")
    library(conn, first, second)
    run(tmp_path, conn)
    assert images(tmp_path, conn, second)["100_20260815153645"] == ["https://old.example/100.jpg"]


def test_without_a_photo_address_the_account_keeps_the_single_link(tmp_path):
    conn, first, second = env(tmp_path)
    library(conn, first, second)
    store.set_state(conn, "public_base", "")
    conn.commit()
    assert photo_library.own_photos_for(conn, first, "") is None
    run(tmp_path, conn)
    assert images(tmp_path, conn, first) == {"100": ["https://example.test/1.jpg"]}


def test_image_urls_has_no_repeats_and_at_most_ten():
    own = photo_library.OwnPhotos({KEY: ["a", "b", "c"]}, {"%s#%s" % (KEY, X1): "a"}, frozenset("abc"))
    assert photo_library.image_urls(own, OFFER, False, "b") == ["a", "b", "c"]
    assert photo_library.image_urls(None, OFFER, False, "s") == ["s"]
    many = photo_library.OwnPhotos({KEY: [str(i) for i in range(12)]}, {}, frozenset())
    assert len(photo_library.image_urls(many, OFFER, False, "s")) == 10


def publish_and_save_as_own(tmp_path, conn, accounts):
    """Publish the drafts (the snapshots keep the ads as they were) and make the position a saved «own» one: built from its snapshot."""
    for n in accounts:
        connections.set_live(conn, n, True, NOW, "o")
    conn.commit()
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    conn.execute("UPDATE published_state SET location = 'own'")
    conn.commit()


def foreign_findings(tmp_path, conn, account, mode="generated"):
    acct = connections.get_account(conn, account)
    found = cycle.gate(conn, acct, mode, connections.effective_settings(conn, account, active_settings(conn)), parse_unload(UNLOAD, "T1"),
                       REF, tmp_path / "feed", TODAY)
    return [f for f in found if f.rule == "own_photo_foreign"]


def test_turning_the_switch_off_rebuilds_a_saved_ad_with_one_link(tmp_path):
    conn, first, second = env(tmp_path)
    links = library(conn, first, second)
    run(tmp_path, conn)
    publish_and_save_as_own(tmp_path, conn, (first, second))
    assert "/photos/" in store.get_snapshot(conn, "100", first)
    run(tmp_path, conn)                                                                   # switch on: the saved ad shows our frames
    assert images(tmp_path, conn, first)["100"][:2] == [links[first]["tq"], links[first]["set4"]]
    store.set_state(conn, "own_photos", "0")
    conn.commit()
    run(tmp_path, conn)
    assert images(tmp_path, conn, first) == {"100": [supplier(conn, first)]}
    assert images(tmp_path, conn, second) == {"100_a%d" % second: [supplier(conn, second)]}
    assert foreign_findings(tmp_path, conn, first) == [] and foreign_findings(tmp_path, conn, second) == []


def test_a_saved_ad_without_a_photo_address_falls_back_to_the_supplier_photo(tmp_path):
    conn, first, second = env(tmp_path)
    links = library(conn, first, second)
    run(tmp_path, conn)
    publish_and_save_as_own(tmp_path, conn, (first, second))
    assert ET.fromstring(store.get_snapshot(conn, "100", first)).find("Images/Image").get("url") == links[first]["tq"]
    store.set_state(conn, "public_base", "")                                              # no address: no own photos and no account link
    conn.commit()
    run(tmp_path, conn)
    assert images(tmp_path, conn, first) == {"100": ["https://example.test/1.jpg"]}
    assert foreign_findings(tmp_path, conn, first) == []


def test_a_library_link_with_nothing_to_fall_back_to_is_dropped():
    from tires_sync import sync
    ad = ET.fromstring('<Ad><Id>100</Id><Images><Image url="%s/photos/%s.jpg"/></Images></Ad>' % (BASE, "a" * 64))
    sync._set_images(ad, [])
    assert ad.find("Images") is None
