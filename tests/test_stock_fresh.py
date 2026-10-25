"""The feed follows a fall of the supplier's stock (owner, 2026-10-24, «вариант Б»): a known fall below the threshold takes the ad off
even when the row is broken elsewhere or the supplier's file is stale."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from helpers import catalog_xml, commodity_xml, settings_dict
from tires_sync import connections, store, suppliers
from tires_sync.cli import main
from tires_sync.settings_store import active_settings, init_settings
from tires_sync.webmim import merge_parsed, parse_unload

NOW = "2026-10-24T10:00:00"
REF = '<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>'


def rows(*items) -> bytes:
    return catalog_xml(*(commodity_xml(TERRITORY_RN="T1", **kw) for kw in items))


# --- the stock of a broken row -------------------------------------------------------------------------------------------------


def test_a_row_broken_elsewhere_keeps_its_stock_a_broken_stock_does_not():
    parsed = parse_unload(rows(dict(NNOMMODIF="101", NPRICE_DELAYED="abc", NREST="2"),
                               dict(NNOMMODIF="102", SDIAMETR="x", NREST="7"),
                               dict(NNOMMODIF="103", NREST="много"),
                               dict(NNOMMODIF="104", NREST="9")), "T1")
    assert set(parsed.issues) == {"101", "102", "103"}
    assert parsed.stocks == {"101": 2, "102": 7}             # an offer's stock is in the offer; a broken NREST is not known
    assert parsed.offers["104"].stock == 9


def test_two_rows_under_one_number_have_no_known_stock():
    parsed = parse_unload(rows(dict(NNOMMODIF="101", NPRICE_DELAYED="abc", NREST="2"),
                               dict(NNOMMODIF="101", NREST="9")), "T1")
    assert "duplicate_in_territory" in parsed.issues["101"] and parsed.stocks == {}


def test_merge_carries_the_known_stocks_of_every_supplier():
    one = parse_unload(rows(dict(NNOMMODIF="101", NPRICE_DELAYED="abc", NREST="2")), "T1")
    two = parse_unload(rows(dict(NNOMMODIF="5", NPRICE_DELAYED="abc", NREST="1")), "T1", supplier=2)
    assert merge_parsed([one, two]).stocks == {"101": 2, "s2-5": 1}


# --- the cycle ------------------------------------------------------------------------------------------------------------------


def prepared(tmp_path, **settings):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(**settings), NOW)
    connections.create_account(conn, "A", NOW, "owner")
    connections.set_live(conn, 1, True, NOW, "owner")
    (tmp_path / "reference.txt").write_text(REF, encoding="utf-8")
    return conn


def args_of(tmp_path):
    return ["--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"),
            "--reference", str(tmp_path / "reference.txt"), "--feed-root", str(tmp_path / "feed")]


def cycle(tmp_path, unload: bytes, *extra) -> int:
    (tmp_path / "unload.xml").write_bytes(unload)
    return main(["run-cycle", "--publish"] + list(extra) + args_of(tmp_path))


def published(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    try:
        return store.load_published(conn, 1)
    finally:
        conn.close()


def last_reasons(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    try:
        return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT goods_id, event, reason FROM ad_event ORDER BY id")}
    finally:
        conn.close()


def test_a_published_row_that_breaks_with_a_known_low_stock_is_taken_off_at_once(tmp_path):
    prepared(tmp_path)
    assert cycle(tmp_path, rows(dict(NNOMMODIF="101"), dict(NNOMMODIF="102"))) == 0
    assert set(published(tmp_path)) == {"101", "102"}
    cycle(tmp_path, rows(dict(NNOMMODIF="101", NPRICE_DELAYED="abc", NREST="2"), dict(NNOMMODIF="102")))
    assert set(published(tmp_path)) == {"102"}
    assert last_reasons(tmp_path)["101"] == ("removed", "low_stock")


def test_a_published_row_that_breaks_with_enough_stock_or_a_broken_stock_is_held_as_before(tmp_path):
    prepared(tmp_path)
    cycle(tmp_path, rows(dict(NNOMMODIF="101"), dict(NNOMMODIF="102"), dict(NNOMMODIF="103")))
    cycle(tmp_path, rows(dict(NNOMMODIF="101", NPRICE_DELAYED="abc", NREST="8"), dict(NNOMMODIF="102", NREST="x"),
                         dict(NNOMMODIF="103")))
    now = published(tmp_path)
    assert set(now) == {"101", "102", "103"}
    assert now["101"].first_hold is not None and now["102"].first_hold is not None and now["103"].first_hold is None


def test_the_hourly_cycle_gives_the_same_decisions_on_the_same_good_data(tmp_path):
    """Nothing broken, nothing stale: `stocks` is empty and the decisions are those of before (the owner asked for this guard)."""
    from tires_sync import sync
    from tires_sync.catalog import parse_reference
    conn = prepared(tmp_path)
    good = rows(dict(NNOMMODIF="101"), dict(NNOMMODIF="102", NREST="3"), dict(NNOMMODIF="103", NREST="12", NPRICE_DELAYED="6000"))
    cycle(tmp_path, rows(dict(NNOMMODIF="101"), dict(NNOMMODIF="102"), dict(NNOMMODIF="103")))
    parsed = parse_unload(good, "T1")
    assert parsed.stocks == {}
    settings = connections.effective_settings(conn, 1, active_settings(conn))
    ref = parse_reference(REF)
    _pub, ev, ds = sync.plan_decisions(conn, parsed, ref, settings, 1, date(2026, 10, 24))
    parsed.stocks = {"999": 1}                                   # a stock of a position that is neither published nor offered changes nothing
    _pub, ev2, ds2 = sync.plan_decisions(conn, parsed, ref, settings, 1, date(2026, 10, 24))
    shape = lambda d: [(x.goods_id, x.action.value, x.reason, x.price) for x in d.decisions]
    assert shape(ds) == shape(ds2)
    assert shape(ds) == [("101", "keep", "unchanged", ds.decisions[0].price), ("102", "remove", "low_stock", None),
                         ("103", "update", "price_changed", ds.decisions[2].price)]


# --- a stale secondary supplier ---------------------------------------------------------------------------------------------------


BOTH = dict(enabled=[["IKON TYRES", "winter"], ["S2:IKON TYRES", "winter"]],
            pricing={"IKON TYRES|winter": {"pct_small": -3.0, "pct_large": -3.0},
                     "S2:IKON TYRES|winter": {"pct_small": 5.0, "pct_large": 5.0}})


def second_supplier(conn):
    two = connections.create_supplier(conn, "Второй", NOW, "owner")
    connections.set_config(conn, "webmim", connections.supplier_owner(two),
                           {"base_url": "https://b.example", "mode": "apikey", "territory_rn": "T2"}, NOW, "owner")
    return two


def second_file(tmp_path, *items, fetched=None):
    (tmp_path / "unload-2.xml").write_bytes(catalog_xml(*(commodity_xml(TERRITORY_RN="T2", **kw) for kw in items)))
    (tmp_path / "unload-2.xml.meta.json").write_text(json.dumps({"fetched_at": fetched or datetime.utcnow().isoformat(), "items": 1}))


def test_a_stale_secondary_unload_gives_only_its_last_known_stock(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(**BOTH), NOW)
    second_supplier(conn)
    (tmp_path / "unload.xml").write_bytes(rows(dict(NNOMMODIF="101")))
    old = (datetime.utcnow() - timedelta(days=10)).isoformat()
    second_file(tmp_path, dict(NNOMMODIF="5", NREST="2"), dict(NNOMMODIF="6", NREST="9"), fetched=old)
    parsed, _sha, notes = suppliers.load_all_parsed(conn, active_settings(conn), tmp_path / "unload.xml")
    assert 2 in parsed.unavailable and "устарела" in notes[0]
    assert set(parsed.offers) == {"101"}                         # no positions and no prices from a stale file
    assert parsed.stocks == {"s2-5": 2, "s2-6": 9}


def test_a_stale_secondary_supplier_takes_off_a_known_fall_and_holds_the_rest(tmp_path):
    conn = prepared(tmp_path, **BOTH)
    second_supplier(conn)
    conn.close()
    second_file(tmp_path, dict(NNOMMODIF="5", NPRICE_DELAYED="5000"), dict(NNOMMODIF="6", NPRICE_DELAYED="5000"),
                dict(NNOMMODIF="7", NPRICE_DELAYED="5000"))
    cycle(tmp_path, rows(dict(NNOMMODIF="101")))
    assert set(published(tmp_path)) == {"101", "s2-5", "s2-6", "s2-7"}
    old = (datetime.utcnow() - timedelta(days=10)).isoformat()
    second_file(tmp_path, dict(NNOMMODIF="5", NREST="2", NPRICE_DELAYED="5000"), dict(NNOMMODIF="6", NPRICE_DELAYED="5000"), fetched=old)
    cycle(tmp_path, rows(dict(NNOMMODIF="101")))
    now = published(tmp_path)
    assert set(now) == {"101", "s2-6", "s2-7"}                   # s2-5 fell below the threshold before the file went stale
    assert now["s2-6"].first_hold is not None and now["s2-7"].first_hold is not None
    assert last_reasons(tmp_path)["s2-5"] == ("removed", "low_stock")


# --- the guard against mass removal: held as before, the alert names the positions -------------------------------------------------


def alerts(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    try:
        return [json.loads(r[0])["text"] for r in conn.execute("SELECT detail FROM audit WHERE action = 'alert' ORDER BY id")]
    finally:
        conn.close()


def test_the_guard_still_holds_and_its_alert_lists_the_positions_with_their_reason(tmp_path, capsys):
    prepared(tmp_path, guard_min=2)
    cycle(tmp_path, rows(*(dict(NNOMMODIF=str(n)) for n in range(101, 106))))
    assert len(published(tmp_path)) == 5
    code = cycle(tmp_path, rows(dict(NNOMMODIF="101", NREST="2"), dict(NNOMMODIF="102", NREST="1"),
                                dict(NNOMMODIF="103", NPRICE_DELAYED="abc", NREST="0"), dict(NNOMMODIF="105")))
    out = capsys.readouterr()
    assert code == 3 and len(published(tmp_path)) == 5                    # held, as before
    assert all(p.first_hold is not None for g, p in published(tmp_path).items() if g != "105")
    text = [a for a in alerts(tmp_path) if "защита" in a][-1]
    assert "A, удерживается 4:" in text
    assert "101 IKON 205/55/16 T 94 Test Model — остаток 2" in text and "102 IKON 205/55/16 T 94 Test Model — остаток 1" in text
    assert "103 Ikon 205 55 R16 T 94 Test Model в наличии — остаток 0" in text   # a broken row: its known stock, the title of its last ad
    assert "104 Ikon 205 55 R16 T 94 Test Model в наличии — нет в выгрузке" in text   # gone: the name of its last ad
    assert "held_low_stock" in out.err                                     # the check of the feed reports what the guard keeps


def test_the_guard_alert_names_at_most_twenty_positions(tmp_path):
    from tires_sync import cli
    from tires_sync.cycle import AccountResult
    conn = prepared(tmp_path)
    r = AccountResult(1, "A", True, guarded=tuple(("%d" % n, "absent_from_supplier") for n in range(130, 100, -1)))
    text = cli._guard_alert_text(conn, [r], None)
    assert "A, удерживается 30:" in text and "  101 — нет в выгрузке" in text and "  121 —" not in text and "… и ещё 10" in text
    assert text == cli._guard_alert_text(conn, [r], None)                # the same list, the same text: not repeated within six hours
