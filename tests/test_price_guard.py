"""«Защита от снижения цены поставщика» (the owner 2026-10-30, plan docs/runs/2026-10-30-price-guard): a fall of the supplier's price by 5 % and
more keeps our price on Avito, counted from the last accepted purchase (the anchor), until the owner accepts it; a rise of 5 % and more goes at
once and is told; less than 5 % goes as before, but small falls add up against the anchor."""
from __future__ import annotations

from datetime import date

import pytest

from test_stock_fresh import REF, cycle, prepared, published, rows
from tires_sync import connections, mirror_feed, price_guard, store, sync
from tires_sync.catalog import parse_reference
from tires_sync.settings_store import active_settings
from tires_sync.webmim import parse_unload

NOW = "2026-10-30T10:00:00"


def price(base):
    """Our price of a tyre of the test brand at `base` (-3.82 %, up to tens)."""
    import math
    return int(math.ceil(base * (1 - 0.0382) / 10.0)) * 10


def at(tmp_path, base, *more):
    return cycle(tmp_path, rows(dict(NNOMMODIF="101", NPRICE_DELAYED=str(base)), *more))


def our(tmp_path, gid="101"):
    return published(tmp_path)[gid].price


def anchor(tmp_path, gid="101"):
    conn = store.connect(tmp_path / "t.db")
    try:
        return price_guard.load(conn)["anchors"].get(gid)
    finally:
        conn.close()


def decide_on(tmp_path, base):
    conn = store.connect(tmp_path / "t.db")
    try:
        settings = connections.effective_settings(conn, 1, active_settings(conn))
        parsed = parse_unload(rows(dict(NNOMMODIF="101", NPRICE_DELAYED=str(base))), "T1")
        return sync.plan_decisions(conn, parsed, parse_reference(REF), settings, 1, date(2026, 10, 30))[1]
    finally:
        conn.close()


def test_a_fall_of_five_percent_and_more_keeps_our_price_and_waits(tmp_path):
    prepared(tmp_path)
    at(tmp_path, 5000)
    assert our(tmp_path) == price(5000)
    at(tmp_path, 3850)                                                           # Gislaved: −23 %
    assert our(tmp_path) == price(5000)
    got = anchor(tmp_path)
    assert (got["base_price"], got["pending"]) == (5000.0, 3850.0)


def test_a_fall_below_five_percent_goes_as_before(tmp_path):
    prepared(tmp_path)
    at(tmp_path, 5000)
    at(tmp_path, 4760)                                                           # −4.8 %
    assert our(tmp_path) == price(4760) and anchor(tmp_path)["pending"] == 0


def test_a_rise_goes_at_once_and_moves_the_anchor(tmp_path):
    prepared(tmp_path)
    at(tmp_path, 5000)
    at(tmp_path, 5400)                                                           # +8 %
    assert our(tmp_path) == price(5400)
    got = anchor(tmp_path)
    assert (got["base_price"], got["how"], got["prev"]) == (5400.0, "rise", 5000.0)
    at(tmp_path, 5500)                                                           # +1.9 %: at once too, the anchor stays
    assert our(tmp_path) == price(5500) and anchor(tmp_path)["base_price"] == 5400.0


def test_small_falls_add_up_against_the_anchor(tmp_path):
    prepared(tmp_path)
    at(tmp_path, 5000)
    at(tmp_path, 4850)                                                           # −3 %: goes
    assert our(tmp_path) == price(4850)
    at(tmp_path, 4700)                                                           # −6 % from the anchor: held at the anchor's price
    assert our(tmp_path) == price(5000) and anchor(tmp_path)["pending"] == 4700.0


def test_accepted_goes_in_the_next_cycle_and_becomes_the_anchor(tmp_path):
    conn = prepared(tmp_path)
    at(tmp_path, 5000)
    at(tmp_path, 3850)
    assert price_guard.accept(conn, ["101"], NOW, "owner") == 1
    at(tmp_path, 3850)
    assert our(tmp_path) == price(3850)
    got = anchor(tmp_path)
    assert (got["base_price"], got["how"], got["pending"]) == (3850.0, "accepted", 0)


def test_kept_is_not_asked_again_until_the_price_falls_further(tmp_path):
    conn = prepared(tmp_path)
    at(tmp_path, 5000)
    at(tmp_path, 3850)
    assert price_guard.keep(conn, ["101"], NOW, "owner") == 1
    at(tmp_path, 3850)
    assert our(tmp_path) == price(5000)
    got = anchor(tmp_path)
    assert (got["pending"], got["kept"]) == (0, 3850.0)
    at(tmp_path, 3700)                                                           # −3.9 % from the kept value: still quiet
    assert anchor(tmp_path)["pending"] == 0
    at(tmp_path, 3600)                                                           # −6.5 %: asked again
    assert anchor(tmp_path)["pending"] == 3600.0 and our(tmp_path) == price(5000)


def test_a_price_back_within_five_percent_ends_the_question(tmp_path):
    prepared(tmp_path)
    at(tmp_path, 5000)
    at(tmp_path, 3850)
    at(tmp_path, 4900)
    assert our(tmp_path) == price(4900) and anchor(tmp_path)["pending"] == 0


def test_a_new_position_is_published_at_its_price(tmp_path):
    prepared(tmp_path)
    at(tmp_path, 5000)
    at(tmp_path, 5000, dict(NNOMMODIF="102", NPRICE_DELAYED="3000"))
    assert our(tmp_path, "102") == price(3000) and anchor(tmp_path, "102")["base_price"] == 3000.0


def test_the_anchor_outlives_the_ad(tmp_path):
    prepared(tmp_path)
    at(tmp_path, 5000, dict(NNOMMODIF="102"))
    cycle(tmp_path, rows(dict(NNOMMODIF="102")))                                 # 101 is gone: its ad is taken off
    assert "101" not in published(tmp_path)
    at(tmp_path, 3850, dict(NNOMMODIF="102"))                                    # back, cheaper
    assert our(tmp_path) == price(5000)


def test_the_first_run_changes_no_price(tmp_path):
    """The review of 2026-10-30: an old purchase of another account must never lift a price on the day of the deploy — the anchor of the
    first sight is the price of today."""
    conn = prepared(tmp_path)
    at(tmp_path, 5000)
    conn.execute("DELETE FROM price_anchor")                                     # as before this feature
    conn.execute("UPDATE published_state SET base_price = 6000")                 # a purchase of long ago (a KEEP never rewrote it)
    conn.commit()
    at(tmp_path, 5000)
    assert our(tmp_path) == price(5000) and anchor(tmp_path)["base_price"] == 5000.0 and anchor(tmp_path)["pending"] == 0


def test_a_change_of_the_markup_at_the_same_purchase_is_never_held(tmp_path):
    conn = prepared(tmp_path)
    at(tmp_path, 5000)
    settings = connections.effective_settings(conn, 1, active_settings(conn))
    import dataclasses
    from tires_sync.models import PricingRule
    cheaper = dataclasses.replace(settings, pricing={k: (PricingRule(-20.0, -20.0) if k[0] == "IKON TYRES" else v) for k, v in settings.pricing.items()})
    parsed = parse_unload(rows(dict(NNOMMODIF="101", NPRICE_DELAYED="5000")), "T1")
    ev = sync.plan_decisions(conn, parsed, parse_reference(REF), cheaper, 1, date(2026, 10, 30))[1]
    assert ev.ok["101"].price == 4000 and ev.held == {}


def test_held_price_follows_a_new_markup(tmp_path):
    conn = prepared(tmp_path)
    at(tmp_path, 5000)
    settings = connections.effective_settings(conn, 1, active_settings(conn))
    import dataclasses
    from tires_sync.models import PricingRule
    more = dataclasses.replace(settings, pricing={k: (PricingRule(10.0, 10.0) if k[0] == "IKON TYRES" else v) for k, v in settings.pricing.items()})
    parsed = parse_unload(rows(dict(NNOMMODIF="101", NPRICE_DELAYED="3850")), "T1")
    ev = sync.plan_decisions(conn, parsed, parse_reference(REF), more, 1, date(2026, 10, 30))[1]
    assert ev.ok["101"].price == 5500 and ev.held["101"][:2] == (5000.0, 3850.0)


def test_planning_writes_nothing(tmp_path):
    conn = prepared(tmp_path)
    at(tmp_path, 5000)
    before = conn.execute("SELECT * FROM price_anchor").fetchall()
    ev = decide_on(tmp_path, 3850)
    assert "101" in ev.held
    assert conn.execute("SELECT * FROM price_anchor").fetchall() == before


def test_a_tire_that_changes_supplier_keeps_the_anchor_of_its_ad(tmp_path):
    conn = prepared(tmp_path)
    at(tmp_path, 5000)
    guard = price_guard.load(conn)
    assert price_guard.base_for(guard, "s2-55", 3850.0, inherited="101") == (5000.0, "held")
    assert price_guard.base_for(guard, "s2-55", 3850.0) == (3850.0, "")


def test_the_mirror_prices_held_positions_from_the_anchor(tmp_path, monkeypatch):
    conn = prepared(tmp_path)
    at(tmp_path, 5000)
    offers = parse_unload(rows(dict(NNOMMODIF="101", NPRICE_DELAYED="3850")), "T1").offers
    monkeypatch.setattr(mirror_feed, "stored_ids", lambda c, a: ["101"])
    monkeypatch.setattr(mirror_feed, "pack_count", lambda ad_id: 1)
    settings = connections.effective_settings(conn, 1, active_settings(conn))
    assert mirror_feed.prices_for(conn, 1, offers, settings) == {"101": price(5000)}


def test_the_summary_says_each_change_once_by_brand(tmp_path, monkeypatch):
    from tires_sync import cli
    monkeypatch.setattr(cli, "_alert", lambda *a, **kw: False)                   # Telegram refused: nothing is marked told
    conn = prepared(tmp_path)
    at(tmp_path, 5000, dict(NNOMMODIF="102", NPRICE_DELAYED="4000"))
    at(tmp_path, 3850, dict(NNOMMODIF="102", NPRICE_DELAYED="4400"))
    news = price_guard.news(conn)
    assert news["drops"] == ["Ikon Tyres: 1 поз. −23 %"] and news["rises"] == ["Ikon Tyres: 1 поз. +10 %"]
    price_guard.told(conn, news)
    assert price_guard.news(conn) == {"drops": [], "rises": [], "marks": []}


def test_the_page_is_the_owners_only(tmp_path):
    from panel_helpers import login, make_app, post
    from tires_sync import users
    app = make_app(tmp_path)
    owner = app.test_client()
    tok = login(owner)
    conn = store.connect(app.config["DB_PATH"])
    conn.execute("INSERT INTO price_anchor (goods_id, base_price, set_at, how, pending, pending_since, brand) VALUES ('101', 5000, ?, 'first', "
                 "3850, ?, 'Ikon Tyres')", (NOW, NOW))
    conn.commit()
    page = owner.get("/prices/changes").get_data(as_text=True)
    assert "Цена поставщика изменилась" in page and 'value="101"' in page and "в ближайший цикл" in page
    rev = __import__("re").search(r'name="rev" value="([0-9a-f]+)"', page).group(1)
    assert post(owner, "/prices/changes", tok, goods=["101"], action="keep", rev="0" * 16).status_code == 302
    assert price_guard.load(conn)["anchors"]["101"]["pending"] == 3850.0           # a stale page changes nothing
    users.add_user(conn, "chat1", "длинный-пароль-1", NOW, role="chats")
    other = app.test_client()
    with other.session_transaction() as session:
        session.update(user="chat1", epoch=store.get_session_epoch(conn, "chat1"), csrf="tok")
    assert other.get("/prices/changes").status_code == 403
    assert other.post("/prices/changes", data={"csrf": "tok", "goods": "101", "action": "accept", "rev": rev}).status_code == 403
    assert post(owner, "/prices/changes", tok, goods=["101"], action="accept", rev=rev).status_code == 302
    assert price_guard.load(conn)["anchors"]["101"]["base_price"] == 3850.0


def test_the_cycle_sends_the_summary_once_with_the_link(tmp_path, monkeypatch):
    from tires_sync import cli
    sent = []
    monkeypatch.setattr(cli, "_alert", lambda conn, box, text, **kw: (sent.append(text), True)[1])
    monkeypatch.setenv("TIRES_PUBLIC_BASE", "https://panel.test")
    prepared(tmp_path)
    at(tmp_path, 5000)
    sent.clear()
    at(tmp_path, 3850)
    assert len(sent) == 1 and "Ikon Tyres: 1 поз. −23 %" in sent[0] and "https://panel.test/prices/changes" in sent[0]
    at(tmp_path, 3850)
    assert len(sent) == 1                                                        # said once


def test_a_tire_seen_before_takes_the_anchor_of_the_ad_it_takes_over_two_cycles(tmp_path):
    """The review of 2026-10-30: B (4 Точки) had its own first anchor long before it took A's ad."""
    conn = prepared(tmp_path)
    at(tmp_path, 5000)
    conn.execute("INSERT INTO price_anchor (goods_id, base_price, set_at, how) VALUES ('s2-55', 3850, ?, 'first')", (NOW,))
    conn.commit()

    class Ev:
        inherit = {"s2-55": "101"}
        held = {"s2-55": (5000.0, 3850.0, "held")}
    guard = price_guard.load(conn)
    assert price_guard.base_for(guard, "s2-55", 3850.0, inherited="101") == (5000.0, "held")
    offers = {"s2-55": type("O", (), {"base_price": 3850.0, "brand_raw": "Ikon Tyres"})()}
    price_guard.record(conn, offers, NOW, Ev())
    got = price_guard.load(conn)["anchors"]["s2-55"]
    assert (got["base_price"], got["how"], got["pending"]) == (5000.0, "inherited", 3850.0)
    price_guard.accept(conn, ["s2-55"], NOW, "owner")
    assert price_guard.base_for(price_guard.load(conn), "s2-55", 3850.0, inherited="101") == (3850.0, "")    # accepted: its own now


def test_a_kept_price_can_be_accepted_later(tmp_path):
    conn = prepared(tmp_path)
    at(tmp_path, 5000)
    at(tmp_path, 3850)
    price_guard.keep(conn, ["101"], NOW, "owner")
    assert price_guard.accept(conn, ["101"], NOW, "owner") == 1
    at(tmp_path, 3850)
    assert our(tmp_path) == price(3850)


def test_a_rise_is_told_once_even_after_a_fall_and_a_fall_again_only_further(tmp_path, monkeypatch):
    from tires_sync import cli
    monkeypatch.setattr(cli, "_alert", lambda *a, **kw: False)
    conn = prepared(tmp_path)
    at(tmp_path, 5000)
    at(tmp_path, 5500)                                                           # +10 %
    found = price_guard.news(conn)
    assert found["rises"] and not found["drops"]
    price_guard.told(conn, found)
    at(tmp_path, 5000)                                                           # −9 % from 5500: waits
    found = price_guard.news(conn)
    assert found["drops"] and not found["rises"]                                 # the rise is not told again
    price_guard.told(conn, found)
    at(tmp_path, 4990)                                                           # a wiggle: quiet
    assert price_guard.news(conn)["drops"] == []
    at(tmp_path, 4700)                                                           # 6 % further down: told again
    assert price_guard.news(conn)["drops"]
    price_guard.keep(conn, ["101"], NOW, "owner")
    assert price_guard.news(conn)["rises"] == []


def test_a_fall_of_a_position_not_on_avito_asks_nothing(tmp_path):
    prepared(tmp_path)
    at(tmp_path, 5000)
    cycle(tmp_path, rows(dict(NNOMMODIF="101", NPRICE_DELAYED="3850", NREST="1")))   # below the threshold: no ad
    assert anchor(tmp_path)["pending"] == 0 and anchor(tmp_path)["base_price"] == 5000.0


def test_a_fall_again_after_the_question_was_over_is_told_again(tmp_path, monkeypatch):
    from tires_sync import cli
    monkeypatch.setattr(cli, "_alert", lambda *a, **kw: False)
    conn = prepared(tmp_path)
    at(tmp_path, 5000)
    at(tmp_path, 4500)
    price_guard.told(conn, price_guard.news(conn))
    at(tmp_path, 4900)                                                           # back within 5 %: the question is over
    at(tmp_path, 4500)                                                           # the same fall a week later
    assert price_guard.news(conn)["drops"] == ["Ikon Tyres: 1 поз. −10 %"]


def test_the_ads_of_a_mirrored_account_are_asked_too(tmp_path):
    conn = prepared(tmp_path)
    at(tmp_path, 5000)
    offers = parse_unload(rows(dict(NNOMMODIF="101", NPRICE_DELAYED="3850")), "T1").offers
    price_guard.record_mirror(conn, offers, ["101"], NOW)
    assert price_guard.load(conn)["anchors"]["101"]["pending"] == 3850.0
