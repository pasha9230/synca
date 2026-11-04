"""The description of a position on OTH (the owner 2026-11-02): {stock} is the supplier's stock plus OTH in any wording of the account's template,
also on a saved ad; the first line says where the tyres are in proper Russian, from the setting «Где (для описания)» of each point, and only the
points with this position's tyres now; the check of a feed stops a line that names any other (rule `oth_line_point`)."""
from __future__ import annotations

import pytest

from panel_helpers import login, make_app, post
from tests.test_feed_check import ad as check_ad, ctx as check_ctx, rules_of
from tests.test_oth_feed import connect, description, move, on_oth, passed, snapshot
from tests.test_stock_pass import AFTER, FIVE, ads_of, args_of, prepared, public, rows, run
from tires_sync import connections, cycle, feed_check, order_points, oth_manual, oth_stock, store, sync
from tires_sync.cli import main
from tires_sync.description import set_stock

MAIN = "{title}\n\nЦена за 1 шину: {price} руб.\nОстаток на складе: {stock} шт.\n\nАртикул: {code}"
ZAMAN = "{title}\n▸ Цена: {price}\n▸ Остаток шин: {stock}\n▸ Артикул: {code}"
NOW = "2026-11-04T10:00:00"


def where(tmp_path, **by_name):
    """Set «Где» of the points as the form of the page does."""
    conn = connect(tmp_path)
    try:
        for name, text in by_name.items():
            point = next(p for p in order_points.list_points(conn) if p["name"] == name)
            order_points.save_point(conn, dict(point, where_text=text), point["id"])
    finally:
        conn.close()


def templates(tmp_path, *texts):
    conn = connect(tmp_path)
    try:
        for account, text in zip((1, 2), texts):
            connections.set_description_template(conn, account, text, NOW, "owner")
        conn.commit()
    finally:
        conn.close()


# ---- {stock} ------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("template, before, after", [
    (MAIN, "Остаток на складе: более 4 шт.", "Остаток на складе: 3 шт."),
    (ZAMAN, "▸ Остаток шин: 8", "▸ Остаток шин: 3"),
    ("Осталось {stock}, код {code}", "Осталось 1, код K7", "Осталось 3, код K7"),
    ("  Только сегодня: {stock} {new_only}", "Только сегодня: 9", "Только сегодня: 3"),
])
def test_the_count_of_a_saved_ad_is_written_in_any_wording_of_the_template(template, before, after):
    text = "Ikon 205/55\nЦена: 4 шт за 4000\n" + before + "\nв наличии 12 шт на складе"
    assert set_stock(text, template, 3) == text.replace(before, after)        # only that line: no other number of the text
    assert oth_stock.with_count(text, 3, template) == text.replace(before, after)


def test_above_four_a_saved_ad_says_more_than_four_as_the_placeholder_does():
    assert set_stock("▸ Остаток шин: 2", ZAMAN, 5) == "▸ Остаток шин: более 4"
    assert set_stock("▸ Остаток шин: более 4", ZAMAN, 4) == "▸ Остаток шин: 4"


def test_a_text_the_template_does_not_explain_is_not_guessed_at():
    assert set_stock("Другой шаблон: 8", ZAMAN, 3) is None                     # saved under another template
    assert oth_stock.with_count("▸ В наличии: 8 шт.\nх", 3, ZAMAN) == "▸ В наличии: 3 шт.\nх"    # the wording of the older templates still reads
    assert oth_stock.with_count("Другой шаблон: 8", 3, ZAMAN) == "Другой шаблон: 8"
    twice = "▸ Остаток шин: 8\n▸ Остаток шин: 2"
    assert set_stock(twice, ZAMAN, 3) == twice                                 # which of the two is it?
    assert set_stock("7\n8", "{stock}\n{code}", 3) is None                     # a line of nothing but values could be any line
    wild = "Шина Ikon: 8\nШина Цена: 5000"
    assert set_stock(wild, "Шина {brand}: {stock}", 3) == wild                    # another value reads as the line too: left whole


def test_both_accounts_show_supplier_plus_oth_in_their_own_wording_fresh_and_held(tmp_path, capsys):
    prepared(tmp_path)
    templates(tmp_path, MAIN, ZAMAN)
    assert run(tmp_path, FIVE) == 0
    assert "Остаток на складе: более 4 шт." in description(public(tmp_path, 1), "101")
    on_oth(tmp_path, "101", "102")
    capsys.readouterr()
    assert run(tmp_path, AFTER) == 0                                           # 101 fell to 2 (built afresh), 102 left the unload (its saved ad)
    passed(capsys)
    first, second = public(tmp_path, 1), public(tmp_path, 2)
    assert "Остаток на складе: более 4 шт." in description(first, "101")        # 4 + 2
    assert "▸ Остаток шин: более 4" in description(second, "101_a2")
    assert "Остаток на складе: 4 шт." in description(first, "102")              # ours only: the saved ad said «более 4»
    assert "▸ Остаток шин: 4" in description(second, "102_a2")
    assert run(tmp_path, AFTER) == 0                                           # an hour later the saved ad is written anew: no drift, one line
    assert "▸ Остаток шин: 4" in description(public(tmp_path, 2), "102_a2")


def test_manual_tyres_count_too_on_a_saved_ad(tmp_path, capsys):
    prepared(tmp_path, accounts=("A",))
    templates(tmp_path, MAIN)
    assert run(tmp_path, FIVE) == 0
    conn = connect(tmp_path)
    try:
        oth_manual.set_qty(conn, "102", order_points.save_point(conn, dict(name="Юнона", channel="panel")), 3, NOW, "owner")
    finally:
        conn.close()
    capsys.readouterr()
    broken = rows(dict(NNOMMODIF="102", NPRICE_DELAYED="0", NREST="0"), *(dict(NNOMMODIF=str(n)) for n in (101, 103, 104, 105)))
    assert run(tmp_path, broken) == 0                                          # held on its saved ad, which said «более 4»
    passed(capsys)
    assert "Остаток на складе: 3 шт." in description(public(tmp_path, 1), "102")


# ---- the first line -----------------------------------------------------------------------------------------------------------------------


def test_the_line_says_each_point_as_its_setting_says_in_the_order_of_the_points(tmp_path, capsys):
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    on_oth(tmp_path, "103", point="Юнона")
    on_oth(tmp_path, "103", point="Руставели")
    on_oth(tmp_path, "103", point="Янино")
    where(tmp_path, Юнона="на Юноне", Руставели="на Руставели", Янино="в Янино")
    capsys.readouterr()
    assert run(tmp_path, FIVE) == 0
    passed(capsys)
    assert description(public(tmp_path), "103").startswith("Шины в наличии на Юноне, на Руставели, в Янино\n")
    assert oth_stock.FIRST_LINE not in snapshot(tmp_path, "103")                # the saved ad stays clean, «в Янино» too


def test_a_point_without_the_setting_or_of_the_tests_is_not_named(tmp_path, capsys):
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    on_oth(tmp_path, "103", point="Юнона")
    on_oth(tmp_path, "103", point="Руставели")
    on_oth(tmp_path, "103", point="Тест Заман")
    on_oth(tmp_path, "104", point="Руставели")
    where(tmp_path, Юнона="на Юноне", **{"Тест Заман": "на Тесте"})             # a test point never, even with the setting
    capsys.readouterr()
    assert run(tmp_path, FIVE) == 0
    passed(capsys)
    assert description(public(tmp_path), "103").startswith("Шины в наличии на Юноне\n")
    assert description(public(tmp_path), "104").startswith("Шины в наличии\n")   # tyres at a point without the setting: the line, no name


def test_only_the_points_with_the_tyres_of_this_position_now(tmp_path, capsys):
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    gone = on_oth(tmp_path, "103", point="Юнона")
    on_oth(tmp_path, "103", point="Янино")
    on_oth(tmp_path, "104", point="Юнона")
    where(tmp_path, Юнона="на Юноне", Янино="в Янино")
    assert run(tmp_path, FIVE) == 0
    assert description(public(tmp_path), "104").startswith("Шины в наличии на Юноне\n")
    move(tmp_path, gone, "done")
    capsys.readouterr()
    assert run(tmp_path, FIVE) == 0
    passed(capsys)
    assert description(public(tmp_path), "103").startswith("Шины в наличии в Янино\n")


def test_the_line_of_one_tire_names_the_points_of_the_lot_that_handed_over_in_the_order_of_the_points(tmp_path, capsys):
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    on_oth(tmp_path, "107", point="Юнона")                                     # the same tire as 101, not on Avito: 101 carries the line
    on_oth(tmp_path, "101", point="Янино")
    where(tmp_path, Юнона="на Юноне", Янино="в Янино")
    lots = rows(*(dict(NNOMMODIF=str(n)) for n in range(101, 106)), dict(NNOMMODIF="107", SLOAD="95", NREST="1"))
    capsys.readouterr()
    assert run(tmp_path, lots) == 0
    passed(capsys)                                                             # the new rule knows the hand-over: the check passes
    assert description(public(tmp_path), "101").startswith("Шины в наличии на Юноне, в Янино\n")


def test_a_template_line_that_only_looks_like_it_is_left_alone():
    for text in ("Шины в наличии, звоните\nх", "Шины в наличии у нас\nх", "Шины в наличиих\nх"):
        assert oth_stock.drop_line(text) == text and oth_stock.named_in(text) == ()
    assert oth_stock.drop_line("Шины в наличии во Всеволожске\nх") == "х"
    assert oth_stock.named_in("Шины в наличии на Юноне, в Янино\nх") == ("на Юноне", "в Янино") and oth_stock.named_in("Шины в наличии\nх") == ()


# ---- the setting ----------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["Юнона", "На Юноне", "у Юноны", "на Юноне, в Янино", "на Юноне,", "на ,", "на", "в ", "на Юн\x01оне", "на " + "ю" * 40])
def test_the_setting_is_refused_when_the_line_could_not_say_it(tmp_path, bad):
    conn = store.connect(tmp_path / "t.db")
    pid = order_points.save_point(conn, dict(name="Юнона", channel="panel"))
    with pytest.raises(order_points.PointError, match="Где"):
        order_points.save_point(conn, dict(name="Юнона", channel="panel", where_text=bad), pid)
    assert order_points.get_point(conn, pid)["where_text"] == ""


def test_the_setting_is_kept_when_a_caller_does_not_send_it_and_can_be_emptied(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    pid = order_points.save_point(conn, dict(name="Янино", channel="panel", where_text="  в   Янино "))
    assert order_points.get_point(conn, pid)["where_text"] == "в Янино"
    order_points.save_point(conn, dict(name="Янино", channel="panel"), pid)
    assert order_points.get_point(conn, pid)["where_text"] == "в Янино"
    order_points.save_point(conn, dict(name="Янино", channel="panel", where_text=""), pid)
    assert order_points.get_point(conn, pid)["where_text"] == ""


def test_a_value_put_in_the_base_past_the_form_never_reaches_a_line(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO point (name, where_text) VALUES ('А', 'у Юноны'), ('Б', 'на Б, в В'), ('В', 'в Янино')")
    assert list(oth_stock._where(conn).values()) == ["в Янино"]                # drop_line could not take those back


def test_the_point_page_shows_and_saves_the_setting_and_says_what_is_wrong(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    post(client, "/connections/points/save", tok, name="Янино", channel="panel", where_text="в Янино")
    page = client.get("/connections/points").get_data(as_text=True)
    assert "Где (для описания)" in page and 'name="where_text" value="в Янино"' in page
    conn = store.connect(tmp_path / "t.db")
    pid = order_points.list_points(conn)[0]["id"]
    conn.close()
    post(client, "/connections/points/save", tok, id=str(pid), name="Янино", channel="panel", where_text="Янино")
    assert "с предлога" in client.get("/connections/points").get_data(as_text=True)
    conn = store.connect(tmp_path / "t.db")
    assert order_points.get_point(conn, pid)["where_text"] == "в Янино"
    conn.close()


# ---- the rule -------------------------------------------------------------------------------------------------------------------------------


def marked(line):
    node = check_ad()
    node.find("Description").text = line + "\n" + node.find("Description").text
    return node


def judged(node, **kw):
    return feed_check.check_feed([node], check_ctx(**kw))


def test_the_rule_stops_a_line_that_names_a_point_without_the_tyres():
    found = judged(marked("Шины в наличии на Юноне, в Янино"), oth=frozenset({"100"}), oth_places={"100": {"на Юноне"}})
    assert "oth_line_point" in rules_of(found)
    finding = next(f for f in found if f.rule == "oth_line_point")
    assert finding.level == "critical" and "в Янино" in finding.text and "на Юноне" not in finding.text
    assert "oth_line_point" in rules_of(judged(marked("Шины в наличии на Юноне"), oth_places={}))     # no OTH at all


def test_the_rule_passes_what_is_true_and_says_nothing_of_a_text_without_a_name():
    good = {"100": {"на Юноне", "в Янино"}}
    assert "oth_line_point" not in rules_of(judged(marked("Шины в наличии на Юноне, в Янино"), oth=frozenset({"100"}), oth_places=good))
    assert "oth_line_point" not in rules_of(judged(marked("Шины в наличии"), oth=frozenset({"100"}), oth_places={}))
    assert "oth_line_point" not in rules_of(judged(check_ad(), oth_places={}))
    assert "oth_line_point" not in rules_of(judged(marked("Шины в наличии на Юноне"), oth_places=None))   # not judged here (the light pass)


def test_a_mirror_is_only_told():
    found = judged(marked("Шины в наличии на Юноне"), mode="mirror", oth_places={})
    assert next(f for f in found if f.rule == "oth_line_point").level == "warning"


def test_the_rules_file_has_the_rule():
    assert feed_check.load_rules()["rules"]["oth_line_point"]["generated"] == "critical"


def test_what_a_line_may_name_is_read_from_the_base_with_the_lots_that_handed_over(tmp_path, monkeypatch):
    monkeypatch.setattr(oth_stock, "places", lambda _conn: {"1": ("на Юноне",), "2": ("в Янино",), "3": ("на Руставели",)})
    assert cycle._oth_places(None, {"1": "2", "2": "4"}) == {"1": {"на Юноне"}, "2": {"в Янино", "на Юноне"}, "3": {"на Руставели"},
                                                             "4": {"на Юноне", "в Янино"}}
    assert cycle._oth_places(None, {"1": "2", "2": "1"})["1"] == {"на Юноне", "в Янино"}      # a loop ends


def test_the_cycle_stops_the_feed_when_the_build_named_a_wrong_point(tmp_path, capsys, monkeypatch):
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    on_oth(tmp_path, "103", point="Юнона")
    where(tmp_path, Юнона="на Юноне")
    good = public(tmp_path)
    real = oth_stock.add_line
    monkeypatch.setattr(oth_stock, "add_line", lambda text, named=(): real(text, tuple(named) + ("в Янино",)))
    capsys.readouterr()
    run(tmp_path, FIVE)
    out = capsys.readouterr()
    assert "oth_line_point" in out.out + out.err and public(tmp_path) == good  # the published feed stays


def test_the_light_pass_is_not_stopped_by_a_line_the_last_cycle_wrote(tmp_path, capsys):
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    cid = on_oth(tmp_path, "103", point="Юнона")
    where(tmp_path, Юнона="на Юноне")
    assert run(tmp_path, FIVE) == 0
    move(tmp_path, cid, "done")                                                # left OTH after the hourly cycle: the line is out of date till the next
    capsys.readouterr()
    fallen = rows(dict(NNOMMODIF="101", NREST="1"), *(dict(NNOMMODIF=str(n)) for n in range(102, 106)))
    assert run(tmp_path, fallen, "--stock-only") == 0
    passed(capsys)
    assert "101" not in ads_of(public(tmp_path))                               # its removal went through
    assert description(public(tmp_path), "103").startswith("Шины в наличии на Юноне\n")
    capsys.readouterr()
    assert run(tmp_path, FIVE) == 0                                            # the next hourly cycle puts it right
    passed(capsys)
    assert not description(public(tmp_path), "103").startswith(oth_stock.FIRST_LINE)


def test_outside_the_cycle_a_line_out_of_date_is_told_and_does_not_fail_the_deploy(tmp_path, capsys):
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    cid = on_oth(tmp_path, "103", point="Юнона")
    where(tmp_path, Юнона="на Юноне")
    assert run(tmp_path, FIVE) == 0
    move(tmp_path, cid, "done")                                                # or a deploy before the first hourly cycle: old lines in the file
    capsys.readouterr()
    code = main(["feed-check", "--published"] + args_of(tmp_path))
    out = capsys.readouterr().out
    assert code == 0 and "[warning] oth_line_point" in out and "на Юноне" in out


def test_the_bare_line_is_not_read_as_a_count_with_the_next_line():
    node = check_ad(Id="s9-100")
    node.find("Description").text = "Шины в наличии\n205/55 R16\nВ наличии: 12 шт."
    found = judged(node, tochki_prefix="s9-", tochki_trial=True, oth=frozenset({"s9-100"}), oth_places={})
    assert "tochki_stock" not in rules_of(found)
    node.find("Description").text = "В наличии: 41 шт."
    assert "tochki_stock" in rules_of(judged(node, tochki_prefix="s9-", tochki_trial=True))


def test_a_fresh_ad_that_fails_its_validation_is_held_with_the_count_of_now(tmp_path, capsys, monkeypatch):
    prepared(tmp_path, accounts=("A",))
    templates(tmp_path, MAIN)
    assert run(tmp_path, FIVE) == 0
    on_oth(tmp_path, "101")
    monkeypatch.setattr(sync, "validate_ad", lambda _ad: ["broken"])
    capsys.readouterr()
    assert run(tmp_path, rows(dict(NNOMMODIF="101", NREST="0"), *(dict(NNOMMODIF=str(n)) for n in range(102, 106)))) == 0
    passed(capsys)
    assert "Остаток на складе: 4 шт." in description(public(tmp_path), "101")
