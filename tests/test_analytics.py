"""The analytics of the orders (read only): the funnel by its slices, the time on the stages, the money equal to the report of the orders.
A test base with orders over several months, sources, points, responsibles, cancellations, a return and manual sales."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tires_sync import analytics, connections, order_points as points, order_report, orders, sales, store

MADE = "2026-07-01T09:00:00"
MSK = timezone(timedelta(hours=3))                                          # the test's own Moscow: a wrong zone in the module must show


def ts(moment: str) -> int:
    return int(datetime.fromisoformat(moment).replace(tzinfo=MSK).timestamp())


def make_card(conn, created: str, source="", responsible="", point=None, name="Иван"):
    cid = orders.create_card(conn, {"client_name": name, "client_phone": "+79001234567", "source": source, "responsible": responsible,
                                    "point_id": str(point) if point else "", "delivery_date": created[:10]}, created, "manager")
    orders.add_item(conn, cid, goods_id="1", name="Ikon 185/65 R15", brand_key="ikon", season="winter", diameter=15, territory="1", qty=4,
                    sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=created, author="manager")
    return cid


def move(conn, cid, code, at, **changes):
    stage = {s["code"]: s for s in orders.list_stages(conn)}[code]
    assert orders.move_card(conn, cid, stage["id"], at, "manager", changes=changes or None, fire_rules=False)


def chat(conn, account, chat_id, started: str, order_id=None):
    conn.execute("INSERT INTO avito_chat (account_id, chat_id, created_ts, order_id) VALUES (?,?,?,?)",
                 (account, chat_id, ts(started) if started else 0, order_id))
    conn.commit()


@pytest.fixture
def base(tmp_path):
    conn = store.connect(str(tmp_path / "a.db"))
    orders.ensure_pipeline(conn, MADE)
    main = connections.create_account(conn, "Основной", MADE, "o")
    worker2 = connections.create_account(conn, "Партнёр-2", MADE, "o")
    beta = points.save_point(conn, {"name": "Бета"})
    rust = points.save_point(conn, {"name": "Альфа"})
    c = {}
    # August: three from chats of «Основной», one by hand; one issued, one cancelled, one returned after the issue
    c["a1"] = make_card(conn, "2026-08-03T10:00:00", "avito", "owner", beta)
    c["a2"] = make_card(conn, "2026-08-10T10:00:00", "avito", "worker2", rust)
    c["a3"] = make_card(conn, "2026-08-20T10:00:00", "avito", "owner", beta)
    c["a4"] = make_card(conn, "2026-08-31T23:59:59", "", "owner")                         # the last second of August, by hand, no point
    for key, account, started in (("a1", main, "2026-08-02T20:00:00"), ("a2", main, "2026-08-10T09:00:00"), ("a3", worker2, "2026-08-19T08:00:00")):
        chat(conn, account, "ch-" + key, started, c[key])
    chat(conn, main, "ch-empty", "2026-08-15T12:00:00")                                  # a chat with no order
    chat(conn, main, "ch-midnight", "2026-09-01T00:30:00")                                # Moscow 00:30 is still 31 August in UTC: September here
    chat(conn, worker2, "ch-undated", "")                                                    # Avito gave no time
    move(conn, c["a1"], "placed", "2026-08-03T12:00:00")
    move(conn, c["a1"], "done", "2026-08-05T12:00:00")                                      # 2 h on «Новый заказ», 48 h on «Заказ оформлен»
    move(conn, c["a2"], "cancel", "2026-08-11T10:00:00", cancel_reason="передумал")        # 24 h on «Новый заказ»
    move(conn, c["a3"], "placed", "2026-08-20T14:00:00")                                    # 4 h
    move(conn, c["a3"], "done", "2026-08-21T14:00:00")                                      # 24 h
    move(conn, c["a3"], "return", "2026-08-25T14:00:00", return_kind=next(iter(orders.RETURN_KINDS)), return_reason="брак", return_amount="20000")
    # September: one from a repeat client, one passed by a point; both still open
    c["s1"] = make_card(conn, "2026-09-01T00:00:00", "repeat", "worker2", rust)               # the first second of September
    c["s2"] = make_card(conn, "2026-09-15T10:00:00", "pvz", "", beta)
    move(conn, c["s2"], "placed", "2026-09-15T16:00:00")                                    # 6 h, ended in September
    return conn, c, {"main": main, "worker2": worker2, "beta": beta, "rust": rust}


def rows(result):
    return {r["label"]: r for r in result["rows"]}


def test_the_whole_funnel_counts_the_chats_and_the_orders_of_the_period_both_days_included(base):
    conn, c, ids = base
    aug = analytics.funnel(conn, "2026-08-01", "2026-08-31")
    whole = aug["rows"][0]
    assert (whole["chats"], whole["orders"], whole["issued"], whole["cancelled"], whole["returned"], whole["open"]) == (4, 4, 1, 1, 1, 1)
    assert whole["chats_with_order"] == 3 and whole["chat_to_order"] == 0.75 and whole["order_to_issued"] == 0.25   # the chat with no order
    assert aug["chats_undated"] == 1
    sep = analytics.funnel(conn, "2026-09-01", "2026-09-30")["rows"][0]
    assert (sep["chats"], sep["orders"], sep["open"]) == (1, 2, 2)                      # the midnight chat is September's in Moscow
    one_day = analytics.funnel(conn, "2026-08-31", "2026-08-31")["rows"][0]
    assert (one_day["chats"], one_day["orders"]) == (0, 1)                                 # 23:59:59 is in, the 00:30 chat of the next day is not


def test_by_account_the_chats_of_the_period_and_the_orders_made_from_them(base):
    conn, c, ids = base
    r = rows(analytics.funnel(conn, "2026-08-01", "2026-08-31", by="account"))
    assert set(r) == {"Основной", "Партнёр-2"}
    assert (r["Основной"]["chats"], r["Основной"]["orders"], r["Основной"]["issued"], r["Основной"]["cancelled"]) == (3, 2, 1, 1)
    assert r["Основной"]["chat_to_order"] == round(2 / 3, 4) and r["Основной"]["order_to_issued"] == 0.5
    assert (r["Партнёр-2"]["chats"], r["Партнёр-2"]["orders"], r["Партнёр-2"]["returned"]) == (1, 1, 1)   # the undated chat is in no period


def test_by_source_responsible_and_point_the_orders_of_the_period(base):
    conn, c, ids = base
    src = rows(analytics.funnel(conn, "2026-08-01", "2026-09-30", by="source"))
    assert {k: (v["orders"], v["issued"]) for k, v in src.items()} == {"Авито": (3, 1), "не указан": (1, 0), "Повторное обращение": (1, 0),
                                                                         "Передал ПВЗ": (1, 0)}
    assert all(v["chats"] is None and v["chat_to_order"] is None for v in src.values())  # a chat has no source until it has an order
    who = rows(analytics.funnel(conn, "2026-08-01", "2026-09-30", by="responsible"))
    assert {k: v["orders"] for k, v in who.items()} == {"owner": 3, "worker2": 2, "не указан": 1}
    where = analytics.funnel(conn, "2026-08-01", "2026-08-31", by="point")
    assert {r["label"]: (r["key"], r["orders"], r["cancelled"]) for r in where["rows"]} == {
        "Бета": (ids["beta"], 2, 0), "Альфа": (ids["rust"], 1, 1), "точка не указана": (None, 1, 0)}
    assert (where["total"]["orders"], where["total"]["issued"], where["total"]["chats"]) == (4, 1, None)


def test_a_deleted_order_is_not_counted_and_its_chat_has_no_order(base):
    from tires_sync import trash
    conn, c, ids = base
    trash.delete_order(conn, c["a2"], "2026-10-01T10:00:00", "owner")
    whole = analytics.funnel(conn, "2026-08-01", "2026-08-31")["rows"][0]
    assert (whole["chats"], whole["orders"], whole["cancelled"]) == (4, 3, 0)
    main = rows(analytics.funnel(conn, "2026-08-01", "2026-08-31", by="account"))["Основной"]
    assert (main["chats"], main["orders"]) == (3, 1)


def test_wrong_periods_and_slices_are_refused(base):
    conn, c, ids = base
    for start, end in (("2026-08-31", "2026-08-01"), ("31.08.2026", "2026-09-01"), ("", "2026-09-01")):
        with pytest.raises(ValueError):
            analytics.funnel(conn, start, end)
    with pytest.raises(ValueError):
        analytics.funnel(conn, "2026-08-01", "2026-08-31", by="brand")


def test_the_time_on_the_stages_counts_the_stays_that_ended_in_the_period(base):
    conn, c, ids = base
    st = {s["name"]: s for s in analytics.stage_times(conn, "2026-08-01", "2026-08-31", now="2026-10-01T10:00:00")["stays"]}
    assert (st["Новый заказ"]["count"], st["Новый заказ"]["mean_hours"], st["Новый заказ"]["median_hours"]) == (3, 10.0, 4.0)   # 2, 24, 4
    assert (st["Заказ оформлен"]["count"], st["Заказ оформлен"]["mean_hours"]) == (2, 36.0)                                      # 48, 24
    assert (st["Выполнено"]["count"], st["Выполнено"]["median_hours"]) == (1, 96.0)                                              # done → return
    assert st["Переговоры"]["count"] == 0 and st["Переговоры"]["mean_hours"] is None
    sep = {s["name"]: s for s in analytics.stage_times(conn, "2026-09-01", "2026-09-30", now="2026-10-01T10:00:00")["stays"]}
    assert (sep["Новый заказ"]["count"], sep["Новый заказ"]["mean_hours"]) == (1, 6.0)                       # an order still on a stage is no stay yet


def test_a_renamed_stage_keeps_its_old_stays_under_the_old_name(base):
    conn, c, ids = base
    placed = {s["code"]: s for s in orders.list_stages(conn)}["placed"]
    orders.update_stage(conn, placed["id"], "Оформлен у поставщика", placed["color"], placed["kind"], placed["hint"], placed["limit_hours"])
    stays = analytics.stage_times(conn, "2026-08-01", "2026-08-31", now="2026-10-01T10:00:00")["stays"]
    old = next(s for s in stays if s["name"] == "Заказ оформлен")
    assert old["stage_id"] is None and old["count"] == 2 and stays[-1] is old
    assert next(s for s in stays if s["name"] == "Оформлен у поставщика")["count"] == 0


def test_stuck_orders_by_the_given_limit_or_the_stage_limit(base):
    conn, c, ids = base
    now = "2026-09-16T10:00:00"
    stuck = {s["name"]: s for s in analytics.stage_times(conn, "2026-09-01", "2026-09-30", now=now, stuck_hours=24)["stuck"]}
    assert stuck["Новый заказ"]["order_ids"] == [c["a4"], c["s1"]] and stuck["Заказ оформлен"]["count"] == 0       # s2 is 18 h on «Заказ оформлен»
    assert "Выполнено" not in stuck and "Отмена" not in stuck                                                # closed stages never stick
    assert all(s["limit_hours"] == 24 for s in stuck.values())
    by_stage = analytics.stage_times(conn, "2026-09-01", "2026-09-30", now=now)["stuck"]
    assert [s["name"] for s in by_stage] == [s["name"] for s in orders.list_stages(conn) if s["kind"] == "open" and s["limit_hours"]]
    placed = {s["code"]: s for s in orders.list_stages(conn)}["placed"]
    orders.update_stage(conn, placed["id"], placed["name"], placed["color"], placed["kind"], placed["hint"], 12)
    own = {s["name"]: s for s in analytics.stage_times(conn, "2026-09-01", "2026-09-30", now=now)["stuck"]}
    assert own["Заказ оформлен"]["order_ids"] == [c["s2"]] and own["Заказ оформлен"]["limit_hours"] == 12


def sell(conn, order, point, sale_total, supplier, day, source="order", pieces=4):
    return sales.add_sale(conn, day=day, goods_id="g%s" % order, brand_key="nokian", season="winter", diameter=16, name="Nokian 205/55 R16",
                          ad_kind="single", pieces=pieces, sale_total=sale_total, supplier_price=supplier, terms=None, fee_pct=0, promo_pct=0,
                          account_id=None, point=point, note="Заказ №%s" % order if source == "order" else "вручную", now="2026-08-01T10:00:00",
                          author="o", today="2026-10-20", source=source)


@pytest.mark.parametrize("start,end", [("2026-08-01", "2026-08-31"), ("2026-09-01", "2026-09-30"), ("2026-08-01", "2026-09-30"),
                                       ("2026-08-31", "2026-09-01"), ("2026-10-01", "2026-10-31")])
def test_the_money_is_the_report_of_the_orders_to_the_ruble_with_dated_rates_and_manual_sales_apart(tmp_path, start, end):
    conn = store.connect(str(tmp_path / "m.db"))
    order_report.set_rates(conn, "2026-09-01", "2026-08-01", "2026-08-01T10:00:00", "owner", point_fee=100, owner_share=0.6)
    sell(conn, 1, "Бета", 20000, 4000, "2026-08-10")                      # margin 4000, fee 75, share 0.5
    sell(conn, 2, "Альфа", 10333, 2500, "2026-08-31")                  # the last day with the old rates
    sell(conn, 3, "Бета", 15000, 3000, "2026-09-01")                      # the new rates from this day
    sell(conn, 3, "Бета", 7000, 1500, "2026-09-01", pieces=2)             # the same order: one fee
    sell(conn, 4, "", 9999.99, 2400, "2026-09-20")
    gone = sell(conn, 5, "Бета", 50000, 4000, "2026-09-21")
    sales.reverse_sale(conn, gone, "2026-09-22T10:00:00", "o", from_order=True)
    sell(conn, 6, "Бета", 30000, 4000, "2026-08-15", source="manual")
    sell(conn, 7, "Альфа", 8000, 1500, "2026-09-05", source="manual", pieces=2)
    got = analytics.money(conn, start, end)
    report = order_report.build(conn, start, end)
    t = report["total"]
    assert (got["orders"], got["pieces"], got["revenue"], got["cost"], got["profit"], got["point_fees"], got["net"], got["owner"], got["partner"]) == (
        t["orders"], t["pieces"], t["sale"], t["cost"], t["margin"], t["fee"], t["net"], t["owner"], t["partner"])
    assert [(p["point"], p["revenue"], p["net"]) for p in got["by_point"]] == [(r["point"], r["sale"], r["net"]) for r in report["lines"]]
    manual = report["manual"] or {"sales": 0, "sale": 0.0, "margin": 0.0}
    assert (got["manual"]["sales"], got["manual"]["revenue"], got["manual"]["profit"]) == (manual["sales"], manual["sale"], manual["margin"])
    assert got["rates_used"] == report["rates_used"]


def test_the_money_numbers_themselves(tmp_path):
    conn = store.connect(str(tmp_path / "m.db"))
    order_report.set_rates(conn, "2026-09-01", "2026-08-01", "2026-08-01T10:00:00", "owner", point_fee=100, owner_share=0.6)
    sell(conn, 1, "Бета", 20000, 4000, "2026-08-31")                      # margin 4000 − 75 = 3925, owner 0.5
    sell(conn, 2, "Бета", 20000, 4000, "2026-09-01")                      # margin 4000 − 100 = 3900, owner 0.6
    sell(conn, 3, "Бета", 30000, 4000, "2026-09-01", source="manual")
    got = analytics.money(conn, "2026-08-01", "2026-09-30")
    assert (got["orders"], got["revenue"], got["profit"], got["point_fees"], got["net"]) == (2, 40000, 8000, 175, 7825)
    assert (got["owner"], got["partner"]) == (round(3925 * 0.5 + 3900 * 0.6, 2), round(7825 - (3925 * 0.5 + 3900 * 0.6), 2))
    assert (got["manual"]["sales"], got["manual"]["revenue"], got["manual"]["profit"]) == (1, 30000, 14000)


def test_the_analytics_writes_nothing(base):
    conn, c, ids = base
    before = conn.total_changes
    analytics.funnel(conn, "2026-08-01", "2026-09-30")
    for by in analytics.SLICES:
        analytics.funnel(conn, "2026-08-01", "2026-09-30", by=by)
    analytics.stage_times(conn, "2026-08-01", "2026-09-30", now="2026-10-01T10:00:00", stuck_hours=1)
    analytics.money(conn, "2026-08-01", "2026-09-30")
    assert conn.total_changes == before and not conn.in_transaction


def test_an_order_with_two_chats_is_one_order_and_the_conversion_is_the_share_of_chats_with_an_order(base):
    conn, c, ids = base
    chat(conn, ids["main"], "ch-a1-again", "2026-08-04T10:00:00", c["a1"])                # the same client wrote again: one deal, two chats
    chat(conn, ids["worker2"], "ch-a1-other", "2026-08-04T11:00:00", c["a1"])               # and on the other account
    r = rows(analytics.funnel(conn, "2026-08-01", "2026-08-31", by="account"))
    assert (r["Основной"]["chats"], r["Основной"]["chats_with_order"], r["Основной"]["orders"], r["Основной"]["issued"]) == (4, 3, 2, 1)
    assert r["Основной"]["chat_to_order"] == 0.75
    assert (r["Партнёр-2"]["chats"], r["Партнёр-2"]["orders"]) == (2, 2)
    total = analytics.funnel(conn, "2026-08-01", "2026-08-31", by="account")["total"]
    assert (total["chats"], total["chats_with_order"], total["orders"], total["issued"]) == (6, 5, 3, 1)        # a1, a2, a3 once each; a3 went back (return)
    whole = analytics.funnel(conn, "2026-08-01", "2026-08-31")["rows"][0]
    assert (whole["chats"], whole["chats_with_order"], whole["chat_to_order"]) == (6, 5, round(5 / 6, 4))
    chat(conn, ids["main"], "ch-claimed", "2026-08-05T10:00:00", -1)                          # a deal being made this moment: no order yet
    assert analytics.funnel(conn, "2026-08-01", "2026-08-31")["rows"][0]["chats_with_order"] == 5


def test_a_restored_order_does_not_count_its_time_in_the_deleted(base):
    from tires_sync import trash
    conn, c, ids = base
    record = trash.delete_order(conn, c["s1"], "2026-09-02T00:00:00", "owner")
    trash.restore_order(conn, record, "2026-09-20T00:00:00", "owner")
    move(conn, c["s1"], "talk", "2026-09-20T05:00:00")
    sep = {s["name"]: s for s in analytics.stage_times(conn, "2026-09-01", "2026-09-30", now="2026-10-01T10:00:00")["stays"]}
    assert (sep["Новый заказ"]["count"], sep["Новый заказ"]["mean_hours"]) == (1, 6.0)          # s2 only: s1's stay crossed the deletion


def test_a_period_without_money_says_so(base):
    conn, c, ids = base
    got = analytics.money(conn, "2026-08-01", "2026-08-31")
    assert got["has_data"] is False and got["revenue"] == 0 and got["manual"]["sales"] == 0
    sell(conn, 9, "Бета", 30000, 4000, "2026-08-15", source="manual")
    assert analytics.money(conn, "2026-08-01", "2026-08-31")["has_data"] is True


def test_a_stuck_limit_must_be_positive(base):
    conn, c, ids = base
    for hours in (0, -5):
        with pytest.raises(ValueError):
            analytics.stage_times(conn, "2026-09-01", "2026-09-30", now="2026-10-01T10:00:00", stuck_hours=hours)
