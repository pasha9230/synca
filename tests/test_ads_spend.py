"""«Аналитика рекламы» (stage 1, read only): Avito's spending per ad and day (kopecks → rubles), the sums of 7 / 30 / 90 days, the ДРР,
the sales of an ad through the chat of the order, «без объявления», the pauses of Avito's limits and the page's rights."""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from http_fakes import ScriptedHttp
from tires_sync import ads_spend, connections, store
from tires_sync.avito import AvitoClient, RateLimited

TOKEN = (200, {"access_token": "t", "expires_in": 3600})
SELF = (200, {"id": 111, "name": "Шины"})
TODAY = date(2026, 11, 11)
NOW = datetime(2026, 11, 11, 4, 20, 0)


class Clock:
    """A fake clock: sleeping moves it."""

    def __init__(self):
        self.t, self.sleeps = 1000.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(round(s, 1))
        self.t += s


def client_for(*replies):
    http = ScriptedHttp(TOKEN, *replies)
    return AvitoClient("id", "secret", http=http, now=lambda: 0.0, sleep=lambda s: None), http


def db(tmp_path, accounts=1):
    conn = store.connect(str(tmp_path / "x.db"))
    for n in range(accounts):
        connections.create_account(conn, "Аккаунт %d" % (n + 1), "2026-11-01T10:00:00", "o")
    return conn


def answer(*entries):
    return (200, {"result": {"groupings": [
        {"id": avito_id, "type": "item", "metrics": [{"slug": k, "value": v} for k, v in metrics.items()]} for avito_id, metrics in entries]}})


# ---- reading the answers -------------------------------------------------------------------------------------------------------------

def test_the_spending_comes_in_kopecks_and_is_kept_in_rubles():
    rows = ads_spend.parse_spend(answer((5, {"allSpending": 12345, "presenceSpending": 2345, "promoSpending": 10000, "views": 40, "contacts": 3}))[1])
    assert rows == [{"avito_id": 5, "spend": 123.45, "presence": 23.45, "promo": 100.0, "views": 40, "contacts": 3}]


def test_other_shapes_of_the_answer_are_read_and_an_entry_without_an_id_is_left_out():
    data = {"result": {"items": [{"itemId": "7", "metrics": {"presenceSpending": 100, "promoSpending": 250, "views": 2}},
                                 {"metrics": {"allSpending": 999}}, {"id": "abc", "metrics": {}}]}}
    assert ads_spend.parse_spend(data) == [{"avito_id": 7, "spend": 3.5, "presence": 1.0, "promo": 2.5, "views": 2, "contacts": 0}]
    assert ads_spend.parse_spend({}) == [] and ads_spend.parse_spend(None) == []


def test_the_shape_keeps_the_keys_and_never_the_values():
    text = ads_spend.shape({"result": {"groupings": [{"id": 123456, "metrics": [{"slug": "views", "value": 7}]}, {"id": 2}]}})
    assert "groupings" in text and "slug" in text and "123456" not in text and "×2" in text


def test_promotions_and_bids_are_read_in_rubles():
    promos = ads_spend.parse_promotions({"items": [{"itemId": 1, "manualPromotion": {"bidPenny": 350, "limitPenny": 50000}},
                                                   {"itemId": 2, "autoPromotion": {"budgetPenny": 10000}}, {"itemId": 3}]})
    assert promos == {1: {"mode": "manual", "bid": 3.5, "day_limit": 500.0}, 2: {"mode": "auto", "bid": None, "day_limit": 100.0},
                      3: {"mode": "", "bid": None, "day_limit": None}}
    bids = ads_spend.parse_bids({"manual": {"bidPenny": 300, "recBidPenny": 420, "minBidPenny": 100, "maxBidPenny": 2000,
                                            "bids": [{"valuePenny": 900, "minForecast": 10, "maxForecast": 20}, {"nothing": 1}]}})
    assert bids == {"bid": 3.0, "rec_bid": 4.2, "min_bid": 1.0, "max_bid": 20.0, "options": [{"bid": 9.0, "info": {"minForecast": 10, "maxForecast": 20}}]}
    assert ads_spend.parse_bids({}) == {"bid": None, "rec_bid": None, "min_bid": None, "max_bid": None, "options": []}


# ---- collection ----------------------------------------------------------------------------------------------------------------------

def test_the_first_runs_fill_the_history_newest_first_and_the_daily_run_reads_the_last_two_days_again(tmp_path):
    conn = db(tmp_path)
    assert ads_spend.days_to_read(conn, 1, TODAY, daily=False, limit=3) == ["2026-11-10", "2026-11-09", "2026-11-08"]
    for day in ("2026-11-10", "2026-11-09", "2026-11-08"):
        conn.execute("INSERT INTO ad_spend_day (account_id, avito_id, day) VALUES (1, 0, ?)", (day,))
    assert ads_spend.days_to_read(conn, 1, TODAY, daily=False, limit=2) == ["2026-11-07", "2026-11-06"]
    assert ads_spend.days_to_read(conn, 1, TODAY, daily=True, limit=3) == ["2026-11-10", "2026-11-09", "2026-11-07"]
    assert len(ads_spend.days_to_read(conn, 1, TODAY, daily=False, limit=500)) == ads_spend.HISTORY_DAYS - 3


def test_collect_reads_one_day_a_request_a_minute_apart_and_keeps_each_day(tmp_path):
    conn = db(tmp_path)
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, seen_at) VALUES (1, 5, 'A5', 'Шина', '2026-11-11')")
    client, http = client_for(SELF, answer((5, {"allSpending": 1000, "views": 10})), answer((5, {"allSpending": 500, "views": 4}), (9, {"allSpending": 1})))
    clock = Clock()
    result = ads_spend.collect(conn, client, 1, TODAY, NOW, ads_spend.Pacer(clock.now, clock.sleep), daily=False, days_limit=2)
    assert (result["days"], result["rows"], result["unknown"], result["bids"]) == (2, 3, 1, 0)
    assert clock.sleeps == [61.0]                                                       # one request a minute
    body = json.loads(http.calls[2][3])
    assert http.calls[2][1].endswith("/stats/v2/accounts/111/items") and body["grouping"] == "item" and body["dateFrom"] == body["dateTo"] == "2026-11-10"
    assert set(body["metrics"]) >= {"allSpending", "views", "contacts"} and body["limit"] == 1000 and body["offset"] == 0
    assert conn.execute("SELECT avito_id, day, spend, views FROM ad_spend_day ORDER BY day, avito_id").fetchall() == [
        (0, "2026-11-09", 0, 0), (5, "2026-11-09", 5.0, 4), (9, "2026-11-09", 0.01, 0), (0, "2026-11-10", 0, 0), (5, "2026-11-10", 10.0, 10)]
    assert "groupings" in result["notes"][0] and store.get_state(conn, "ads_spend_shape:itemAnalytics")
    assert store.get_state(conn, ads_spend.error_key(1)) == ""


def test_a_full_page_asks_for_the_next_one(tmp_path, monkeypatch):
    monkeypatch.setattr(ads_spend, "PAGE", 2)
    conn = db(tmp_path)
    client, http = client_for(SELF, answer((1, {"allSpending": 100}), (2, {"allSpending": 100})), answer((3, {"allSpending": 100})))
    clock = Clock()
    ads_spend.collect(conn, client, 1, TODAY, NOW, ads_spend.Pacer(clock.now, clock.sleep), daily=False, days_limit=1)
    assert [(json.loads(c[3])["offset"], json.loads(c[3])["limit"]) for c in http.calls[2:]] == [(0, 2), (2, 2)]
    assert conn.execute("SELECT COUNT(*) FROM ad_spend_day WHERE avito_id <> 0").fetchone()[0] == 3


def test_a_429_after_the_repeats_rests_the_account_and_keeps_the_days_already_read(tmp_path):
    conn = db(tmp_path)
    limited = (429, {"message": "too many"})
    client, http = client_for(SELF, answer((5, {"allSpending": 100})), limited, limited, limited, limited)
    clock = Clock()
    with pytest.raises(RateLimited):
        ads_spend.collect(conn, client, 1, TODAY, NOW, ads_spend.Pacer(clock.now, clock.sleep), daily=False, days_limit=3)
    assert conn.execute("SELECT day FROM ad_spend_day WHERE avito_id = 0").fetchall() == [("2026-11-10",)]
    assert store.get_state(conn, "ads_spend_pause:1") == "2026-11-11T04:30:00"
    assert "429" in store.get_state(conn, ads_spend.error_key(1))
    calls = len(http.calls)
    with pytest.raises(RateLimited):                                                   # within the pause: no request at all
        ads_spend.collect(conn, client, 1, TODAY, datetime(2026, 11, 11, 4, 29), ads_spend.Pacer(clock.now, clock.sleep), daily=False)
    assert len(http.calls) == calls


def test_the_pacer_sleeps_only_the_rest_of_the_gap_per_method():
    clock = Clock()
    pacer = ads_spend.Pacer(clock.now, clock.sleep)
    pacer.wait("stats", 61)
    pacer.wait("bids", 3.1)
    clock.t += 20
    pacer.wait("stats", 61)
    pacer.wait("stats", 61)
    assert clock.sleeps == [41.0, 61.0]


def test_the_daily_run_reads_the_prices_of_a_view_of_the_promoted_ads(tmp_path):
    conn = db(tmp_path)
    for day, promo in (("2026-11-01", 50.0), ("2026-11-02", 0.0)):
        conn.execute("INSERT INTO ad_spend_day (account_id, avito_id, day, spend, promo) VALUES (1, 5, ?, ?, ?)", (day, promo + 10, promo))
    conn.execute("INSERT INTO ad_spend_day (account_id, avito_id, day, spend, promo) VALUES (1, 6, '2026-11-01', 10, 0)")   # not promoted
    for n in range(1, 91):                                                             # the history is read: no stats request
        conn.execute("INSERT OR IGNORE INTO ad_spend_day (account_id, avito_id, day) VALUES (1, 0, date('2026-11-11', ?))", ("-%d days" % n,))
    conn.commit()
    client, http = client_for(SELF, answer(), answer(),
                              (200, {"items": [{"itemId": 5, "manualPromotion": {"bidPenny": 350, "limitPenny": 30000}}]}),
                              (200, {"manual": {"bidPenny": 350, "recBidPenny": 500, "minBidPenny": 100, "maxBidPenny": 3000, "bids": []}}))
    clock = Clock()
    result = ads_spend.collect(conn, client, 1, TODAY, NOW, ads_spend.Pacer(clock.now, clock.sleep), daily=True)
    assert result["bids"] == 1 and result["days"] == 2
    assert json.loads(http.calls[4][3]) == {"itemIds": [5]} and http.calls[5][1].endswith("/cpxpromo/1/getBids/5")
    assert all(call[0] in ("GET", "POST") and "setManual" not in call[1] for call in http.calls)     # reads only
    assert conn.execute("SELECT mode, bid, day_limit, rec_bid, min_bid, max_bid FROM ad_bid").fetchall() == [("manual", 3.5, 300.0, 5.0, 1.0, 30.0)]


# ---- the report ----------------------------------------------------------------------------------------------------------------------

def spend(conn, account, avito_id, day, rub, views=0, contacts=0):
    conn.execute("INSERT OR REPLACE INTO ad_spend_day (account_id, avito_id, day, spend, views, contacts) VALUES (?,?,?,?,?,?)",
                 (account, avito_id, day, rub, views, contacts))


def mark(conn, account, days):
    for n in range(1, days + 1):
        conn.execute("INSERT OR IGNORE INTO ad_spend_day (account_id, avito_id, day) VALUES (?, 0, date('2026-11-11', ?))", (account, "-%d days" % n))


def sale(conn, day, total, margin, pieces=4, reversed_at=None, account=None):
    cur = conn.execute(
        "INSERT INTO sale (day, goods_id, brand_key, season, diameter, name, ad_kind, pieces, price_total, sale_total, cost_total, bonus_total, "
        "monthly_total, fee_total, promo_total, margin_total, account_id, created, author, reversed_at) "
        "VALUES (?, 'g', 'b', 'winter', 16, 'Шина', 'single', ?, ?, ?, 0, 0, 0, 0, 0, ?, ?, '2026-11-01', 'o', ?)",
        (day, pieces, total, total, margin, account, reversed_at))
    return cur.lastrowid


def order(conn, sale_ids, chats=()):
    stage = conn.execute("SELECT id FROM order_stage LIMIT 1").fetchone()
    cur = conn.execute("INSERT INTO order_card (stage_id, sale_ids, created_at, created_by, updated_at, entered_at) VALUES (?, ?, '', 'o', '', '')",
                       (stage[0] if stage else 1, json.dumps(sale_ids)))
    for n, (account, item) in enumerate(chats):
        conn.execute("INSERT INTO avito_chat (account_id, chat_id, item_id, order_id) VALUES (?, ?, ?, ?)", (account, "c%d-%d" % (cur.lastrowid, n), item, cur.lastrowid))
    return cur.lastrowid


def test_the_periods_sum_the_days_and_end_yesterday(tmp_path):
    conn = db(tmp_path)
    spend(conn, 1, 5, "2026-11-10", 100, views=50)
    spend(conn, 1, 5, "2026-11-04", 10, views=5)          # the 7th day back: inside 7 days
    spend(conn, 1, 5, "2026-11-03", 1000, views=100)      # the 8th: only in 30 and 90
    spend(conn, 1, 5, "2026-08-14", 5000)                 # the 89th day back: in 90
    spend(conn, 1, 5, "2026-11-11", 77777)                # today: never
    mark(conn, 1, 90)
    got = {days: ads_spend.report(conn, TODAY, days)["accounts"][0] for days in (7, 30, 90)}
    assert [got[d]["total"]["spend"] for d in (7, 30, 90)] == [110.0, 1110.0, 6110.0]
    assert got[7]["covered"] == 7 and got[7]["rows"][0]["view_cost"] == 2.0 and got[7]["rows"][0]["per_day"] == round(110 / 7, 2)
    assert ads_spend.report(conn, TODAY, 7)["since"] == "2026-11-04" and ads_spend.report(conn, TODAY, 7)["until"] == "2026-11-10"
    with pytest.raises(ValueError):
        ads_spend.report(conn, TODAY, 14)


def test_drr_is_a_dash_without_revenue_or_margin():
    assert ads_spend.drr(100, 1000) == 10.0 and ads_spend.drr(100, 0) is None and ads_spend.drr(100, -50) is None and ads_spend.drr(0, 500) == 0.0


def test_a_sale_belongs_to_the_ad_of_the_chat_of_its_order_and_the_rest_is_without_an_ad(tmp_path):
    conn = db(tmp_path, accounts=2)
    mark(conn, 1, 7)
    spend(conn, 1, 5, "2026-11-09", 300, views=100)
    spend(conn, 2, 8, "2026-11-09", 50)
    linked = sale(conn, "2026-11-08", 20000, 3000)
    order(conn, [linked], chats=[(1, ""), (1, "5"), (1, "6")])          # the first chat with an ad
    order(conn, [sale(conn, "2026-11-08", 9000, 1000, reversed_at="2026-11-09")], chats=[(1, "5")])   # reversed: not counted
    order(conn, [sale(conn, "2026-11-08", 7000, 700)])                  # an order without a chat
    sale(conn, "2026-11-08", 4000, 400, pieces=2)                       # a manual sale
    order(conn, [sale(conn, "2026-11-08", 1000, 100)], chats=[(3, "77")])  # an account without a feed
    order(conn, [sale(conn, "2026-11-02", 5000, 500)], chats=[(1, "5")])    # before the period
    order(conn, [sale(conn, "2026-11-08", 6000, 0)], chats=[(2, "8")])      # no margin: the ДРР of the margin is a dash
    conn.commit()
    data = ads_spend.report(conn, TODAY, 7)
    one, two = data["accounts"]
    row = one["rows"][0]
    assert (row["avito_id"], row["spend"], row["pieces"], row["revenue"], row["margin"], row["drr"], row["drr_margin"]) == (5, 300.0, 4, 20000.0, 3000.0, 1.5, 10.0)
    assert len(one["rows"]) == 1 and one["total"]["drr"] == 1.5
    assert (two["rows"][0]["drr"], two["rows"][0]["drr_margin"]) == (0.8, None)
    assert data["none"] == {"pieces": 10, "revenue": 12000.0, "margin": 1200.0, "count": 3}


def test_the_sort_by_drr_puts_the_dashes_last(tmp_path):
    conn = db(tmp_path)
    spend(conn, 1, 1, "2026-11-09", 500)
    spend(conn, 1, 2, "2026-11-09", 100)
    spend(conn, 1, 3, "2026-11-09", 200)
    order(conn, [sale(conn, "2026-11-08", 1000, 100)], chats=[(1, "2")])
    order(conn, [sale(conn, "2026-11-08", 10000, 100)], chats=[(1, "3")])
    conn.commit()
    assert [r["avito_id"] for r in ads_spend.report(conn, TODAY, 7)["accounts"][0]["rows"]] == [1, 3, 2]
    assert [r["avito_id"] for r in ads_spend.report(conn, TODAY, 7, "drr")["accounts"][0]["rows"]] == [2, 3, 1]


def test_the_report_writes_nothing(tmp_path):
    conn = db(tmp_path)
    spend(conn, 1, 5, "2026-11-09", 300)
    conn.commit()
    before = conn.total_changes
    ads_spend.report(conn, TODAY, 30, "drr_margin")
    assert conn.total_changes == before


# ---- the command ---------------------------------------------------------------------------------------------------------------------

def test_the_statistics_run_collects_the_spending_and_its_failure_never_fails_the_run(tmp_path, monkeypatch):
    from tires_sync import cli
    store.connect(tmp_path / "t.db").close()
    monkeypatch.setattr(cli, "load_box", lambda key_file: object())
    monkeypatch.setattr(cli.connections, "list_feed_accounts", lambda conn: [{"id": 1, "name": "A"}])
    monkeypatch.setattr(cli.connections, "get_secret", lambda *a, **k: "x")
    monkeypatch.setattr(cli, "AvitoClient", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_collect_avito_reports", lambda conn, box: None)
    monkeypatch.setattr(cli.ad_stats, "collect", lambda *a, **k: {"ads": 0, "mapped": 0, "days": 0})
    asked = []

    def collect(conn, client, account_id, today, now, pacer, daily=True, days_limit=10):
        asked.append(daily)
        if len(asked) == 1:
            raise RateLimited(429, None, "slow")
        return {"days": 1, "rows": 2, "unknown": 0, "bids": 0, "notes": ["itemAnalytics: {result: …}"]}
    monkeypatch.setattr(cli.ads_spend, "collect", collect)
    run = ["avito-stats", "--db", str(tmp_path / "t.db")]
    assert cli.main(run) == 0 and cli.main(run) == 0                    # the failure of the first: printed, no alert
    assert asked == [True, False]                                       # the second run of the day: only the days still missing
    assert cli.main(["ads-spend-collect", "--db", str(tmp_path / "t.db"), "--dry-run"]) == 0 and asked[-1] is True
