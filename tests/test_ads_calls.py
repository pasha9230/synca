"""«Аналитика рекламы» by the calls (the owner 2026-11-13): the ad of a call when Avito gives one, a sale linked by the call of the order's
client when the order has no chat with an ad (the last call before the order, within 30 days, before «Выполнено»), a call without an ad
gives the account, each sale once; the calls of an ad and of an account in the report. Made-up numbers and data only."""
from __future__ import annotations

from test_ads_spend import TODAY, db, mark, sale, spend
from test_ads_spend_report import by_id, won_order
from test_calls import FakeClient, call
from tires_sync import ads_spend, calls


def phone_call(conn, account, at, item="", answered=1, client=1, order=None):
    """A call of the client `client` (client_call) at `at` (Moscow, as kept), its ad `item`."""
    cur = conn.execute("INSERT INTO avito_call (account_id, call_id, call_at, buyer_phone, item_id, answered, talk_seconds, order_id) "
                       "VALUES (?, ?, ?, '+79990000001', ?, ?, ?, ?)",
                       (account, conn.execute("SELECT COUNT(*) + 1 FROM avito_call").fetchone()[0], at, item, answered, 30 if answered else 0, order))
    if client is not None:
        conn.execute("INSERT INTO client_call (call_row, client_id) VALUES (?, ?)", (cur.lastrowid, client))
    return cur.lastrowid


def of_client(conn, order_id, client=1):
    conn.execute("INSERT INTO client_order (order_id, client_id) VALUES (?, ?)", (order_id, client))


def sources(conn):
    """The ad and the source of each sale of the period."""
    by_ad, by_account, none = ads_spend.sales_by_ad(conn, "2026-11-04", "2026-11-10", [1, 2])
    return by_ad, by_account, none


# ---- the ad of a call from Avito's answer --------------------------------------------------------------------------------------------

def test_the_ad_of_a_call_is_kept_when_avito_gives_one_and_empty_when_not(tmp_path):
    from tires_sync import connections, store
    conn = store.connect(str(tmp_path / "x.db"))
    account = connections.create_account(conn, "Основной", "2026-10-23T15:00:00", "o")
    page = [dict(call(1), itemId=4001), dict(call(2), item={"id": "4002"}), call(3), dict(call(4), itemId=0), dict(call(5), itemId="x")]
    calls.sync_account(conn, FakeClient([page]), account, "2026-10-23T15:00:00", lambda s: None)
    got = dict(conn.execute("SELECT call_id, item_id FROM avito_call").fetchall())
    assert got == {1: "4001", 2: "4002", 3: "", 4: "", 5: ""}


# ---- the link of a sale: а) chat, б) call with an ad, в) call without an ad → the account, г) none -------------------------------------

def test_a_won_order_without_a_chat_ad_takes_the_ad_of_the_last_call_before_it_was_created(tmp_path):
    conn = db(tmp_path, accounts=2)
    card = won_order(conn, day="2026-11-08")                              # created 2026-11-01T10:00:00, «Выполнено» 2026-11-08T12:00:00
    of_client(conn, card)
    phone_call(conn, 1, "2026-10-20T10:00:00", item="11")                 # older one
    phone_call(conn, 1, "2026-10-31T18:00:00", item="12")                 # the last before the order: this one
    phone_call(conn, 2, "2026-11-03T10:00:00", item="13")                 # after the creation: not chosen while one before exists
    conn.commit()
    by_ad, by_account, none = sources(conn)
    assert list(by_ad) == [(1, 12)] and by_ad[(1, 12)]["call_pieces"] == 4 and by_ad[(1, 12)]["call_revenue"] == 20000.0
    assert by_account == {} and none["count"] == 0


def test_a_call_after_the_order_was_created_counts_when_there_is_none_before(tmp_path):
    conn = db(tmp_path, accounts=2)
    card = won_order(conn, day="2026-11-08")
    of_client(conn, card)
    phone_call(conn, 2, "2026-11-05T10:00:00", item="21")
    phone_call(conn, 2, "2026-11-04T10:00:00", item="22")                 # the first after the creation
    conn.commit()
    assert list(sources(conn)[0]) == [(2, 22)]


def test_a_call_after_done_or_older_than_30_days_does_not_count(tmp_path):
    conn = db(tmp_path)
    card = won_order(conn, day="2026-11-08")
    of_client(conn, card)
    phone_call(conn, 1, "2026-11-08T12:00:01", item="31")                 # a second after «Выполнено»
    phone_call(conn, 1, "2026-10-02T09:59:59", item="32")                 # 30 days and a second before the creation
    conn.commit()
    by_ad, by_account, none = sources(conn)
    assert by_ad == {} and by_account == {} and none["reasons"]["no_chat"] == 1
    phone_call(conn, 1, "2026-10-02T10:00:00", item="33")                 # exactly 30 days: counts
    conn.commit()
    assert list(sources(conn)[0]) == [(1, 33)]


def test_a_call_without_an_ad_gives_the_account_and_outside_the_feed_the_reason(tmp_path):
    conn = db(tmp_path)
    first = won_order(conn, day="2026-11-08")
    of_client(conn, first)
    phone_call(conn, 1, "2026-10-30T10:00:00")                            # no ad: «Звонки без объявления» of account 1
    second = won_order(conn, day="2026-11-09", items=((2, 3000.0, 2500.0),))
    of_client(conn, second, client=2)
    phone_call(conn, 7, "2026-10-30T11:00:00", client=2)                  # an account without a feed, no ad
    third = won_order(conn, day="2026-11-09", items=((1, 1000.0, 900.0),))
    of_client(conn, third, client=3)
    phone_call(conn, 7, "2026-10-30T12:00:00", item="5", client=3)        # an account without a feed, an ad
    conn.commit()
    by_ad, by_account, none = sources(conn)
    assert by_ad == {} and list(by_account) == [1]
    assert (by_account[1]["pieces"], by_account[1]["call_pieces"], by_account[1]["call_revenue"]) == (4, 4, 20000.0)
    assert none["count"] == 2 and none["reasons"]["call_no_item"] == 1 and none["reasons"]["no_feed"] == 1


def test_the_call_the_order_was_made_from_counts_without_a_client(tmp_path):
    conn = db(tmp_path)
    card = won_order(conn, day="2026-11-08")
    phone_call(conn, 1, "2026-11-01T09:00:00", item="44", client=None, order=card)
    conn.commit()
    assert list(sources(conn)[0]) == [(1, 44)]


def test_a_sale_with_a_chat_ad_and_a_call_is_counted_once_by_the_chat(tmp_path):
    conn = db(tmp_path)
    card = won_order(conn, day="2026-11-08", chats=[(1, "5")])
    of_client(conn, card)
    phone_call(conn, 1, "2026-10-31T10:00:00", item="6")
    conn.commit()
    by_ad, by_account, none = sources(conn)
    assert list(by_ad) == [(1, 5)] and by_ad[(1, 5)]["count"] == 1 and by_ad[(1, 5)]["call_count"] == 0
    assert by_account == {} and none["count"] == 0
    acc = ads_spend.report(conn, TODAY, 7)["accounts"][0]
    assert acc["total"]["pieces"] == 4 and acc["total"]["call_pieces"] == 0


def test_a_sale_linked_by_a_sale_record_uses_the_day_of_the_sale_as_done(tmp_path):
    conn = db(tmp_path)
    card = won_order(conn, day="2026-11-08", sale_ids=[sale(conn, "2026-11-06", 9000, 900)], journal=False)
    of_client(conn, card)
    phone_call(conn, 1, "2026-11-06T20:00:00", item="7")                  # the day of the sale: before its end
    conn.commit()
    assert list(sources(conn)[0]) == [(1, 7)]


# ---- the calls in the report ---------------------------------------------------------------------------------------------------------

def test_the_report_has_the_calls_of_an_ad_and_of_the_account_and_no_division_by_zero(tmp_path):
    conn = db(tmp_path)
    mark(conn, 1, 7)
    spend(conn, 1, 5, "2026-11-09", 300, views=100)
    spend(conn, 1, 6, "2026-11-09", 100, views=10)                         # no calls: the price of a call is a dash
    phone_call(conn, 1, "2026-11-05T10:00:00", item="5", client=None)
    phone_call(conn, 1, "2026-11-06T10:00:00", item="5", answered=0, client=None)
    phone_call(conn, 1, "2026-11-07T10:00:00", item="5", client=None)
    phone_call(conn, 1, "2026-11-07T11:00:00", client=None)                # without an ad
    phone_call(conn, 1, "2026-11-11T10:00:00", item="5", client=None)      # today: out of the period
    card = won_order(conn, day="2026-11-08")
    of_client(conn, card)
    phone_call(conn, 1, "2026-10-28T10:00:00", item="5")                   # before the period: links the sale, not counted as a call
    other = won_order(conn, day="2026-11-09", items=((2, 3000.0, 2500.0),))
    of_client(conn, other, client=2)
    phone_call(conn, 1, "2026-10-29T10:00:00", client=2)                   # without an ad: the account
    conn.commit()
    acc = ads_spend.report(conn, TODAY, 7)["accounts"][0]
    five, six = by_id(acc)[5], by_id(acc)[6]
    assert (five["calls"], five["calls_answered"], five["call_pieces"], five["call_revenue"], five["call_cost"]) == (3, 2, 4, 20000.0, 100.0)
    assert (six["calls"], six["call_cost"], six["call_pieces"]) == (0, None, 0)
    line = acc["calls_no_item"]
    assert (line["calls"], line["calls_answered"], line["call_pieces"], line["call_revenue"], line["revenue"], line["call_cost"]) == (
        1, 1, 2, 6000.0, 6000.0, None)
    total = acc["total"]
    assert (total["calls"], total["calls_answered"], total["call_pieces"], total["call_revenue"], total["call_cost"]) == (4, 3, 6, 26000.0, 100.0)
    assert (total["pieces"], total["revenue"], total["ads"]) == (6, 26000.0, 2)
    summary = ads_spend.report(conn, TODAY, 7)["summary"]
    assert summary["total"]["calls"] == 4 and summary["total"]["call_cost"] == 100.0
    from tires_sync import ads_tree                                     # review 13.11: the tree counts the sales through calls without an ad too
    tree = ads_tree.build(conn, TODAY, 7, ("account", "ad"))
    assert tree["total"]["m"]["pieces"] == total["pieces"] and tree["total"]["m"]["revenue"] == total["revenue"]
    assert any(r["kind"] == "ad" and r["label"] == ads_spend.CALLS_NO_ITEM_TITLE and r["m"]["revenue"] == 6000.0 for r in tree["rows"])


def test_an_account_without_spending_or_calls_has_dashes(tmp_path):
    conn = db(tmp_path)
    acc = ads_spend.report(conn, TODAY, 7)["accounts"][0]
    assert (acc["total"]["calls"], acc["total"]["call_cost"], acc["calls_no_item"]["calls"]) == (0, None, 0)
    assert ads_spend.per_call(100.0, 0) is None and ads_spend.per_call(100.0, 3) == 33.33
