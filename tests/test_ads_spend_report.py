"""«Аналитика рекламы», the report (the owner 2026-11-12): the summary per Avito account with the total, the sales of an ad taken from the
orders on a won stage (and from «Продажи» where the order has a sale, once), «без объявления» with why, the ads out of the list, the empty
columns and the filters, the short cell of the price of a view. Made-up data only."""
from __future__ import annotations

import json

from test_ads_spend import TODAY, db, mark, sale, spend
from tires_sync import ads_spend, connections, economics, orders


def stage(conn, code):
    orders.ensure_pipeline(conn, "2026-11-01T10:00:00")
    return conn.execute("SELECT id, name FROM order_stage WHERE code = ?", (code,)).fetchone()


def won_order(conn, code="done", day="2026-11-08", items=((4, 5000.0, 4000.0),), chats=(), sale_ids=(), terms=None, fee=0.0, journal=True):
    """An order put on the stage `code` on `day` (the journal record of the move), its positions (qty, sale_unit, supplier_price), its chats."""
    stage_id, name = stage(conn, code)
    cur = conn.execute("INSERT INTO order_card (stage_id, sale_ids, created_at, created_by, updated_at, entered_at) VALUES (?, ?, ?, 'o', ?, ?)",
                       (stage_id, json.dumps(list(sale_ids)), "2026-11-01T10:00:00", day + "T12:00:00", day + "T12:00:00"))
    card = cur.lastrowid
    if journal:
        conn.execute("INSERT INTO order_event (order_id, at, author, kind, text) VALUES (?, '2026-11-01T10:00:00', 'o', 'created', "
                     "'Заказ создан на этапе «Новый»')", (card,))
        conn.execute("INSERT INTO order_event (order_id, at, author, kind, text) VALUES (?, ?, 'o', 'stage', ?)",
                     (card, day + "T12:00:00", "Этап: «Новый» → «%s»" % name))
    for qty, unit, purchase in items:
        conn.execute("INSERT INTO order_item (order_id, goods_id, name, qty, supplier_price, sale_unit, terms, fee_pct) VALUES (?, 'g1', 'Шина', ?, ?, ?, ?, ?)",
                     (card, qty, purchase, unit, json.dumps(terms or {}), fee))
    for n, (account, item) in enumerate(chats):
        conn.execute("INSERT INTO avito_chat (account_id, chat_id, item_id, item_title, order_id) VALUES (?, ?, ?, ?, ?)",
                     (account, "w%d-%d" % (card, n), item, "Чат о шине", card))
    return card


def ad(conn, account, avito_id, title, ad_id="G"):
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, seen_at) VALUES (?, ?, ?, ?, '2026-11-11')", (account, avito_id, ad_id, title))


def by_id(acc):
    return {r["avito_id"]: r for r in acc["rows"]}


# ---- the summary per account ---------------------------------------------------------------------------------------------------------

def test_the_summary_has_a_line_per_account_and_the_total(tmp_path):
    conn = db(tmp_path, accounts=2)
    mark(conn, 1, 7)
    mark(conn, 2, 7)
    spend(conn, 1, 5, "2026-11-09", 300, views=100, contacts=6)
    spend(conn, 1, 6, "2026-11-09", 100, views=100, contacts=2)
    spend(conn, 2, 8, "2026-11-09", 100, views=50, contacts=0)
    won_order(conn, items=((4, 5000.0, 4000.0),), chats=[(1, "5")])
    conn.commit()
    data = ads_spend.report(conn, TODAY, 7)
    one, two = data["summary"]["rows"]
    total = data["summary"]["total"]
    assert (one["name"], one["spend"], one["views"], one["contacts"], one["view_cost"], one["contact_cost"]) == ("Аккаунт 1", 400.0, 200, 8, 2.0, 50.0)
    assert (one["pieces"], one["revenue"], one["margin"], one["drr"], one["drr_margin"], one["share"]) == (4, 20000.0, 4000.0, 2.0, 10.0, 80.0)
    assert (two["spend"], two["contact_cost"], two["drr"], two["share"]) == (100.0, None, None, 20.0)
    assert (total["name"], total["spend"], total["views"], total["contacts"], total["pieces"], total["revenue"], total["share"]) == (
        "Итого", 500.0, 250, 8, 4, 20000.0, 100.0)
    assert total["drr"] == 2.5 and total["view_cost"] == 2.0 and total["contact_cost"] == 62.5


def test_one_account_is_chosen_the_first_by_default(tmp_path):
    conn = db(tmp_path, accounts=2)
    spend(conn, 1, 5, "2026-11-09", 300)
    spend(conn, 2, 8, "2026-11-09", 100)
    conn.commit()
    assert ads_spend.report(conn, TODAY, 7)["account"]["id"] == 1
    chosen = ads_spend.report(conn, TODAY, 7, account=2)["account"]
    assert chosen["id"] == 2 and [r["avito_id"] for r in chosen["rows"]] == [8]
    assert ads_spend.report(conn, TODAY, 7, account=99)["account"]["id"] == 1            # not a feed account: the first


# ---- the sales of an ad: the orders on a won stage -----------------------------------------------------------------------------------

def test_a_won_order_is_a_sale_of_the_ad_of_its_chat_and_a_cancelled_one_or_a_storage_record_is_not(tmp_path):
    conn = db(tmp_path)
    mark(conn, 1, 7)
    spend(conn, 1, 5, "2026-11-09", 300, views=100)
    won_order(conn, "done", items=((4, 5000.0, 4000.0),), chats=[(1, ""), (1, "5")])                   # the first chat with an ad
    won_order(conn, "shipped", items=((2, 6000.0, 5000.0), (0, 6000.0, 5000.0)), chats=[(1, "5")])     # a position emptied: no pieces
    won_order(conn, "cancel", items=((4, 9000.0, 1000.0),), chats=[(1, "5")])                          # cancelled
    record = won_order(conn, "done", items=((4, 7000.0, 1000.0),), chats=[(1, "5")])                   # a record of storage Х
    conn.execute("INSERT INTO app_state (key, value) VALUES (?, '{\"order\": 1}')", ("oth_storage:%d" % record,))
    won_order(conn, "done", day="2026-11-02", chats=[(1, "5")])                                         # won before the period
    won_order(conn, "done", day="2026-11-11", chats=[(1, "5")])                                         # won today: not in the period
    conn.commit()
    row = by_id(ads_spend.report(conn, TODAY, 7)["accounts"][0])[5]
    assert (row["pieces"], row["revenue"], row["margin"], row["orders"]) == (6, 32000.0, 6000.0, 2)
    assert row["drr"] == round(300 / 32000 * 100, 1) and row["drr_margin"] == 5.0


def test_the_day_of_a_sale_is_the_day_the_order_came_to_the_won_stage(tmp_path):
    conn = db(tmp_path)
    mark(conn, 1, 7)
    card = won_order(conn, "done", day="2026-11-01", chats=[(1, "5")])          # won on the 1st, moved between won stages on the 9th
    conn.execute("INSERT INTO order_event (order_id, at, author, kind, text) VALUES (?, '2026-11-09T10:00:00', 'o', 'stage', "
                 "'Этап: «Выполнено» → «Отправлен в ТК»')", (card,))
    won_order(conn, "done", day="2026-11-08", chats=[(1, "6")], journal=False)   # no journal: the time on the stage
    conn.commit()
    rows = by_id(ads_spend.report(conn, TODAY, 7)["accounts"][0])
    assert 5 not in rows and rows[6]["pieces"] == 4


def test_the_margin_is_the_one_of_the_economics_of_a_sale(tmp_path):
    conn = db(tmp_path)
    terms = {"discount_pct": 5, "bonus_pct": 2, "monthly_pct": 1}
    won_order(conn, items=((4, 5000.0, 4000.0),), chats=[(1, "5")], terms=terms, fee=3.0)
    won_order(conn, items=((2, 3000.0, 0.0),), chats=[(1, "5")])                  # no purchase typed: the margin is not made up
    conn.commit()
    row = by_id(ads_spend.report(conn, TODAY, 7)["accounts"][0])[5]
    assert row["margin"] == economics.unit(4000.0, 5000.0, terms, 3.0, 0.0, 4)["margin"]
    assert row["revenue"] == 26000.0 and row["no_purchase"] == 1


def test_an_order_with_a_sale_record_is_counted_once_from_the_sale(tmp_path):
    conn = db(tmp_path)
    linked = sale(conn, "2026-11-08", 20000, 3000)
    won_order(conn, items=((4, 9999.0, 1.0),), chats=[(1, "5")], sale_ids=[linked])
    conn.commit()
    row = by_id(ads_spend.report(conn, TODAY, 7)["accounts"][0])[5]
    assert (row["pieces"], row["revenue"], row["margin"], row["orders"]) == (4, 20000.0, 3000.0, 1)


def test_the_orders_without_an_ad_say_why(tmp_path):
    conn = db(tmp_path)
    won_order(conn)                                                                   # no chat
    won_order(conn, chats=[(1, "")])                                                  # a chat without an ad
    won_order(conn, chats=[(3, "77")])                                                # an account without a feed
    sale(conn, "2026-11-08", 4000, 400, pieces=2)                                     # a manual sale
    conn.commit()
    none = ads_spend.report(conn, TODAY, 7)["none"]
    assert (none["count"], none["pieces"], none["revenue"], none["margin"]) == (4, 14, 64000.0, 12400.0)
    assert none["reasons"] == {"no_chat": 1, "no_item": 1, "no_feed": 1, "call_no_item": 0, "manual": 1}


# ---- the ads out of the list ---------------------------------------------------------------------------------------------------------

def test_an_ad_out_of_the_list_takes_its_name_from_a_chat_and_the_rest_is_one_line(tmp_path):
    conn = db(tmp_path)
    ad(conn, 1, 5, "Nokian 205/55 R16")
    spend(conn, 1, 5, "2026-11-09", 300, views=10)
    spend(conn, 1, 6, "2026-11-09", 50, views=5, listed=False)                                      # removed before the list: its chat has the name
    conn.execute("INSERT INTO avito_chat (account_id, chat_id, item_id, item_title) VALUES (1, 'c6', '6', 'Снятая 185/65 R15')")
    spend(conn, 1, 7, "2026-11-09", 40, views=4, listed=False)                                      # our Id known from the stock sent to Avito
    conn.execute("INSERT INTO avito_stock (account_id, avito_id, ad_id) VALUES (1, 7, 'G7')")
    spend(conn, 1, 8, "2026-11-09", 20, views=2, listed=False)
    spend(conn, 1, 9, "2026-11-09", 10, views=1, listed=False)
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, seen_at) VALUES (2, 9, 'X', 'Чужого аккаунта', '2026-11-11')")
    conn.commit()
    acc = ads_spend.report(conn, TODAY, 7)["accounts"][0]
    rows = by_id(acc)
    assert rows[6]["title"] == "Снятая 185/65 R15" and rows[7]["ad_id"] == "G7" and 8 not in rows and 9 not in rows
    assert (acc["strangers"]["count"], acc["strangers"]["spend"], [r["avito_id"] for r in acc["strangers"]["rows"]]) == (2, 30.0, [8, 9])
    assert acc["total"]["spend"] == 420.0 and acc["total"]["ads"] == 5


# ---- the columns, the filters, the price of a view -----------------------------------------------------------------------------------

def test_the_columns_empty_for_every_row_are_hidden_and_the_filters(tmp_path):
    conn = db(tmp_path)
    ad(conn, 1, 5, "А")
    ad(conn, 1, 6, "Б")
    spend(conn, 1, 5, "2026-11-09", 300, views=100)
    spend(conn, 1, 6, "2026-11-09", 0, views=10)
    conn.commit()
    acc = ads_spend.report(conn, TODAY, 7)["accounts"][0]
    assert {"spend", "views", "view_cost"} <= acc["columns"]
    assert not {"contacts", "pieces", "revenue", "margin", "drr", "drr_margin", "bid"} & acc["columns"]
    won_order(conn, chats=[(1, "6")])
    conn.commit()
    assert {"pieces", "revenue", "margin", "drr"} <= ads_spend.report(conn, TODAY, 7)["accounts"][0]["columns"]
    assert [r["avito_id"] for r in ads_spend.report(conn, TODAY, 7, only="sales")["accounts"][0]["rows"]] == [6]
    assert [r["avito_id"] for r in ads_spend.report(conn, TODAY, 7, only="spend")["accounts"][0]["rows"]] == [5]
    data = ads_spend.report(conn, TODAY, 7, only="bad")
    assert data["only"] == "" and len(data["accounts"][0]["rows"]) == 2
    assert data["accounts"][0]["total"]["spend"] == 300.0                             # the filter never changes the totals


def test_the_price_of_a_view_is_short_with_five_options_nearest_the_current_one(tmp_path):
    conn = db(tmp_path)
    ad(conn, 1, 5, "А")
    ad(conn, 1, 6, "Б")
    spend(conn, 1, 5, "2026-11-09", 300, views=100)
    spend(conn, 1, 6, "2026-11-09", 30, views=10)
    options = [{"bid": float(n), "info": {"minForecast": n * 10, "maxForecast": n * 20}} for n in range(1, 30)]
    conn.execute("INSERT INTO ad_bid (account_id, avito_id, taken_at, mode, bid, day_limit, rec_bid, min_bid, max_bid, options) "
                 "VALUES (1, 5, '2026-11-11T01:20:00', 'manual', 10, 300, 12, 1, 29, ?)", (json.dumps(options),))
    conn.commit()
    acc = ads_spend.report(conn, TODAY, 7)["accounts"][0]
    bid = by_id(acc)[5]["bid"]
    assert bid["short"] == "своя 10,00\u00a0₽" and bid["limit"] == 300
    assert [o["bid"] for o in bid["near"]] == [8.0, 9.0, 10.0, 11.0, 12.0] and bid["near"][0]["forecast"] == "80–160"
    assert (acc["bids_read"], acc["bids_of"]) == (1, 2)


def test_auto_and_an_unread_price(tmp_path):
    assert ads_spend.bid_short({"mode": "auto", "bid": None}) == "авто"
    assert ads_spend.bid_short({"mode": "", "bid": 4.5}) == "4,50\u00a0₽"
    assert ads_spend.bid_short({"mode": "", "bid": None}) == "не задана"
    assert ads_spend.near_options([{"bid": 3.0, "info": {"x": 1}}], None) == [{"bid": 3.0, "forecast": "", "info": {"x": 1}}]


def test_the_report_does_not_fall_over_an_empty_base(tmp_path):
    conn = db(tmp_path, accounts=0)
    data = ads_spend.report(conn, TODAY, 30)
    assert data["accounts"] == [] and data["account"] is None and data["summary"]["total"]["spend"] == 0
    connections.create_account(conn, "Аккаунт 1", "2026-11-01T10:00:00", "o")
    data = ads_spend.report(conn, TODAY, 30)
    assert data["account"]["rows"] == [] and data["summary"]["total"]["share"] is None and data["none"]["count"] == 0
