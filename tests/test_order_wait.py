"""The stage «Ожидает наличие»: what the client wants, the responsible manager, and the check of waiting cards against the stock (owner, 2026-10-15)."""
from __future__ import annotations

import pytest

from helpers import make_offer
from tires_sync import order_wait, orders, store
from tires_sync.models import Season

NOW = "2026-10-15T12:00:00"
WANT = dict(brand="Ikon Tyres", width=205, profile=55, radius=16, season="winter", studded="no", qty=4)


def db(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    return conn


def wait_stage(conn):
    return next(s for s in orders.list_stages(conn) if s["code"] == "wait")


def waiting(conn, *wants, **fields):
    """A card in «Ожидает наличие» with the given wanted-tire lines."""
    card_id = orders.create_card(conn, {"client_name": "Иван", **fields}, NOW, "worker2")
    for want in wants:
        order_wait.add_want(conn, card_id, want, NOW, "worker2")
    orders.move_card(conn, card_id, wait_stage(conn)["id"], NOW, "worker2")
    return card_id


def tire(**changes):
    return make_offer(**{"brand_key": "IKON TYRES", "width": 205, "height": 55, "diameter": 16, "season": Season.WINTER, "studded": False,
                         "stock": 10, "code": "TS1", **changes})


def test_the_wait_stage_is_added_once_to_an_old_pipeline_it_is_a_system_stage(tmp_path):
    conn = store.connect(str(tmp_path / "old.db"))
    for position, (code, name, color, kind) in enumerate(c for c in orders.DEFAULT_STAGES if c[0] != "wait"):      # a pipeline made before the stage existed
        conn.execute("INSERT INTO order_stage (code, name, color, position, kind) VALUES (?,?,?,?,?)", (code, name, color, position, kind))
    conn.commit()
    orders.ensure_pipeline(conn, NOW)
    orders.ensure_pipeline(conn, NOW)
    stages = orders.list_stages(conn)
    assert [s["code"] for s in stages].count("wait") == 1 and [s["code"] for s in stages][:3] == ["new", "talk", "wait"]
    assert [s["position"] for s in stages] == list(range(len(stages)))
    with pytest.raises(orders.OrderError, match="Системный"):                                                   # renamed and recoloured, like the other system stages, not deleted
        orders.delete_stage(conn, wait_stage(conn)["id"])


def test_a_card_needs_a_client_and_a_wanted_tire_to_wait(tmp_path):
    conn = db(tmp_path)
    card_id = orders.create_card(conn, {"client_name": "Иван"}, NOW, "worker2")
    with pytest.raises(orders.OrderError, match="что нужно клиенту"):
        orders.move_card(conn, card_id, wait_stage(conn)["id"], NOW, "worker2")
    order_wait.add_want(conn, card_id, WANT, NOW, "worker2")
    assert orders.move_card(conn, card_id, wait_stage(conn)["id"], NOW, "worker2") is True
    assert orders.get_card(conn, card_id)["stage"]["code"] == "wait"


def test_a_wanted_tire_line_is_checked(tmp_path):
    conn = db(tmp_path)
    card_id = orders.create_card(conn, {"client_name": "Иван"}, NOW, "worker2")
    for bad in ({}, {**WANT, "qty": 0}, {**WANT, "qty": "много"}, {**WANT, "width": "x"}, {**WANT, "radius": 0}, {**WANT, "season": "осень"}, {**WANT, "studded": "maybe"}):
        with pytest.raises(orders.OrderError):
            order_wait.add_want(conn, card_id, bad, NOW, "worker2")
    order_wait.add_want(conn, card_id, {"code": "TS1", "qty": 2}, NOW, "worker2")                              # a code alone is enough
    line = order_wait.list_wants(conn, card_id)[0]
    assert line["qty"] == 2 and line["code"] == "TS1" and not line["any_brand"] and not line["any_size"]


def test_the_last_line_of_a_waiting_card_cannot_be_taken_away(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, WANT, {**WANT, "width": 215})
    first, second = [w["id"] for w in order_wait.list_wants(conn, card_id)]
    order_wait.remove_want(conn, first, NOW, "worker2")
    with pytest.raises(orders.OrderError, match="последн"):
        order_wait.remove_want(conn, second, NOW, "worker2")


def test_the_responsible_manager_is_the_one_who_made_the_card_unless_somebody_else_is_named(tmp_path):
    conn = db(tmp_path)
    own = orders.create_card(conn, {"client_name": "А"}, NOW, "worker2")
    named = orders.create_card(conn, {"client_name": "Б", "responsible": "pavel"}, NOW, "worker2")
    assert orders.get_card(conn, own)["responsible"] == "worker2" and orders.get_card(conn, named)["responsible"] == "pavel"
    orders.update_card(conn, own, {"responsible": "pavel"}, NOW, "worker2")
    assert orders.get_card(conn, own)["responsible"] == "pavel"


def test_an_exact_tire_in_enough_stock_is_found_and_too_little_stock_is_not(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, WANT)                                                                             # four tires, winter, not studded
    assert order_wait.check(conn, [tire(stock=3)], NOW) == {"exact": [], "analogs": []}                       # three are not four
    assert order_wait.check(conn, [tire(stock=4)], NOW) == {"exact": [card_id], "analogs": []}               # four are


def test_the_season_the_studs_and_the_size_must_match(tmp_path):
    conn = db(tmp_path)
    waiting(conn, WANT)
    for other in (tire(season=Season.SUMMER), tire(studded=True), tire(width=215), tire(height=60), tire(diameter=17), tire(brand_key="KUMHO")):
        assert order_wait.check(conn, [other], NOW) == {"exact": [], "analogs": []}


def test_a_code_matches_and_a_one_tire_line_needs_one_tire(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, {"code": "TS9", "qty": 1})
    assert order_wait.check(conn, [tire(code="TS1")], NOW)["exact"] == []
    assert order_wait.check(conn, [tire(code="TS9", stock=1, width=999)], NOW)["exact"] == [card_id]


def test_every_line_of_the_card_must_be_found_a_part_is_not_a_sale(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, WANT, {**WANT, "width": 215, "qty": 2})
    assert order_wait.check(conn, [tire()], NOW) == {"exact": [], "analogs": []}                              # one of two lines: nothing, no tag
    assert "Появилось" not in orders.get_card(conn, card_id)["tags"]
    assert order_wait.check(conn, [tire(), tire(width=215, code="TS2", stock=2)], NOW)["exact"] == [card_id]


def test_other_brands_are_analogs_only_when_the_line_allows_them(tmp_path):
    conn = db(tmp_path)
    strict = waiting(conn, WANT)
    open_brand = waiting(conn, {**WANT, "any_brand": True})
    found = order_wait.check(conn, [tire(brand_key="KUMHO", code="K1")], NOW)
    assert found == {"exact": [], "analogs": [open_brand]} and strict not in found["analogs"]


def test_other_sizes_are_neighbours_of_the_wanted_one(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, {**WANT, "any_size": True})
    for near in (tire(width=215, code="A"), tire(width=195, code="B"), tire(height=60, code="C"), tire(height=50, code="D")):
        assert order_wait.check(conn, [near], NOW)["analogs"] == [card_id], near
    for far in (tire(width=225), tire(width=185), tire(height=65), tire(diameter=17), tire(width=215, season=Season.SUMMER)):
        assert order_wait.check(conn, [far], NOW)["analogs"] == [], far
    assert order_wait.check(conn, [tire(width=215, brand_key="KUMHO")], NOW)["analogs"] == []                 # another size AND another brand needs both ticks


def test_both_ticks_allow_another_brand_in_another_size(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, {**WANT, "any_brand": True, "any_size": True})
    assert order_wait.check(conn, [tire(width=215, brand_key="KUMHO")], NOW)["analogs"] == [card_id]


def test_a_line_with_the_exact_tire_and_a_line_with_an_analog_is_a_card_with_analogs(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, WANT, {**WANT, "width": 215, "any_brand": True})
    found = order_wait.check(conn, [tire(), tire(width=215, brand_key="KUMHO", code="K2")], NOW)
    assert found == {"exact": [], "analogs": [card_id]}


def test_the_exact_find_tags_the_card_and_leaves_one_task_for_the_responsible_manager_once(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, WANT, responsible="pavel")
    order_wait.check(conn, [tire()], NOW)
    order_wait.check(conn, [tire()], "2026-10-15T13:00:00")                                                   # the next cycle: nothing is doubled
    card = orders.get_card(conn, card_id)
    assert "Появилось" in card["tags"] and "Есть аналоги" not in card["tags"]
    tasks = conn.execute("SELECT status, title, body FROM order_task WHERE order_id = ?", (card_id,)).fetchall()
    assert len(tasks) == 1 and tasks[0][0] == "pending" and "pavel" in tasks[0][2] and "предложите клиенту" in tasks[0][1].lower()


def test_the_analog_find_says_so_to_the_manager_with_its_own_tag_and_task(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, {**WANT, "any_brand": True})
    order_wait.check(conn, [tire(brand_key="KUMHO", code="K1")], NOW)
    card = orders.get_card(conn, card_id)
    assert "Есть аналоги" in card["tags"] and "Появилось" not in card["tags"]
    titles = [r[0] for r in conn.execute("SELECT title FROM order_task WHERE order_id = ?", (card_id,))]
    assert len(titles) == 1 and "аналог" in titles[0].lower()


def test_cards_that_are_not_waiting_are_not_looked_at(tmp_path):
    conn = db(tmp_path)
    card_id = orders.create_card(conn, {"client_name": "Иван"}, NOW, "worker2")
    order_wait.add_want(conn, card_id, WANT, NOW, "worker2")                                                    # still «Новый заказ»
    assert order_wait.check(conn, [tire()], NOW) == {"exact": [], "analogs": []}


def test_a_line_can_be_changed_and_keeps_its_ticks_unless_they_are_changed(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, {**WANT, "any_brand": True})
    line = order_wait.list_wants(conn, card_id)[0]
    order_wait.update_want(conn, line["id"], {"qty": 2}, NOW, "worker2")
    changed = order_wait.list_wants(conn, card_id)[0]
    assert changed["qty"] == 2 and changed["any_brand"] and not changed["any_size"] and changed["width"] == 205
    order_wait.update_want(conn, line["id"], {"any_brand": False, "any_size": True}, NOW, "worker2")
    again = order_wait.list_wants(conn, card_id)[0]
    assert not again["any_brand"] and again["any_size"]
    with pytest.raises(orders.OrderError):
        order_wait.update_want(conn, line["id"], {"qty": 0}, NOW, "worker2")


def test_the_card_shows_its_lines_in_words_and_in_the_feed_of_events(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, {**WANT, "any_brand": True, "any_size": True})
    text = order_wait.describe(order_wait.list_wants(conn, card_id)[0])
    assert "205/55 R16" in text and "зима" in text and "без шипов" in text and "4 шт." in text and "другие бренды" in text and "другие размеры" in text
    assert any("Что нужно клиенту" in e["text"] for e in orders.list_events(conn, card_id))


# ---- the review of 3f2f45e

def test_a_brand_of_a_second_supplier_carries_a_prefix_and_still_matches(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, WANT)
    assert order_wait.check(conn, [tire(brand_key="S2:IKON TYRES", code="S2-1")], NOW)["exact"] == [card_id]


def test_ten_tags_on_one_card_do_not_stop_the_check_of_the_others(tmp_path):
    conn = db(tmp_path)
    full = waiting(conn, WANT, tags=",".join("т%d" % i for i in range(10)))
    other = waiting(conn, WANT)
    found = order_wait.check(conn, [tire(stock=20)], NOW)
    assert other in found["exact"] and "Появилось" in orders.get_card(conn, other)["tags"]
    assert conn.execute("SELECT COUNT(*) FROM order_task WHERE order_id = ?", (full,)).fetchone()[0] == 1       # the task is left even where the tag does not fit


def test_the_tag_comes_with_the_task_only_the_exact_find_takes_the_analog_tag_away_and_a_removed_tag_stays_removed(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, {**WANT, "any_brand": True})
    order_wait.check(conn, [tire(brand_key="KUMHO", code="K1")], NOW)
    assert orders.get_card(conn, card_id)["tags"] == ["Есть аналоги"]
    order_wait.check(conn, [tire(), tire(brand_key="KUMHO", code="K1")], "2026-10-15T13:00:00")
    assert orders.get_card(conn, card_id)["tags"] == ["Появилось"]
    orders.update_card(conn, card_id, {"tags": ""}, NOW, "worker2")                                              # the manager took it off
    order_wait.check(conn, [tire()], "2026-10-15T14:00:00")
    assert orders.get_card(conn, card_id)["tags"] == []


def test_the_wait_stage_needs_no_placing_data_even_when_the_owner_moves_it_below_the_placed_stage(tmp_path):
    conn = db(tmp_path)
    for _ in range(3):
        orders.shift_stage(conn, wait_stage(conn)["id"], 1)
    codes = [s["code"] for s in orders.list_stages(conn)]
    assert codes.index("wait") > codes.index("placed")
    card_id = orders.create_card(conn, {"client_name": "Иван"}, NOW, "worker2")
    order_wait.add_want(conn, card_id, WANT, NOW, "worker2")
    assert orders.move_card(conn, card_id, wait_stage(conn)["id"], NOW, "worker2") is True


def test_a_huge_number_is_an_order_error_and_a_code_is_compared_without_case_and_spaces(tmp_path):
    conn = db(tmp_path)
    card_id = orders.create_card(conn, {"client_name": "Иван"}, NOW, "worker2")
    for field in ("width", "qty"):
        with pytest.raises(orders.OrderError):
            order_wait.add_want(conn, card_id, {**WANT, field: "9" * 5000}, NOW, "worker2")
    waiting_id = waiting(conn, {"code": " ts-9 ", "qty": 1})
    assert order_wait.check(conn, [tire(code="TS-9", stock=1)], NOW)["exact"] == [waiting_id]


# ---- W1b: the check in the publishing cycle, the default responsible manager

def test_the_default_responsible_manager_is_kept_cleaned_and_can_be_cleared(tmp_path):
    conn = db(tmp_path)
    assert orders.default_responsible(conn) == ""
    orders.set_default_responsible(conn, "  worker2  ")
    assert orders.default_responsible(conn) == "worker2"
    with pytest.raises(orders.OrderError):
        orders.set_default_responsible(conn, "я" * 61)
    orders.set_default_responsible(conn, "")
    assert orders.default_responsible(conn) == ""


def test_the_cycle_hook_checks_the_waiting_cards_against_every_offer_and_never_breaks_the_cycle(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tires_sync import cli
    conn = db(tmp_path)
    card_id = waiting(conn, WANT)
    cli._check_waiting(conn, SimpleNamespace(offers={"1": tire(stock=0, code="A"), "S2:2": tire(brand_key="S2:IKON TYRES", code="B")}))
    assert "Появилось" in orders.get_card(conn, card_id)["tags"]
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(order_wait, "check", boom)
    cli._check_waiting(conn, SimpleNamespace(offers={}))                                                       # a failure is only a warning


def test_the_publishing_cycle_runs_the_check_and_a_draft_cycle_does_not(tmp_path, monkeypatch):
    from datetime import datetime
    from test_review4_fixes import OK, ScriptedHttp, goods, published_ten
    from tires_sync import cli
    conn, key_file, box = published_ten(tmp_path)
    conn.close()
    (tmp_path / "unload.xml").write_bytes(goods(10))
    (tmp_path / "reference.txt").write_text('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>', encoding="utf-8")
    monkeypatch.setattr(cli, "_http", lambda: ScriptedHttp(OK, OK, OK, OK))
    monkeypatch.setattr(cli, "moscow_now", lambda: datetime(2026, 10, 7, 12, 0))
    calls = []
    monkeypatch.setattr(cli, "_check_waiting", lambda conn, parsed, box=None: calls.append(len(parsed.offers)))
    args = ["--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"), "--reference", str(tmp_path / "reference.txt"),
            "--feed-root", str(tmp_path / "feed"), "--key-file", str(key_file)]
    cli.main(["run-cycle"] + args)
    assert calls == []
    cli.main(["run-cycle", "--publish"] + args)
    assert calls == [10]
