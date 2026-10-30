"""The prices of the competitors (market.py, routes_market; the plan price-parser, stage 0): the matching of a competitor's listing with a
position of ours on synthetic listings, the summary, the intake from the Mac by its own token, the catalog of the owner. No network."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from panel_helpers import login, make_app, post
from test_chats_workplace import as_role
from tires_sync import connections, market, store
from tires_sync.models import PublishedItem
from tires_sync.panel import routes_market

RUN = "2026-10-31T07:40:00"
OURS = market.Ours("100", "Ikon Tyres", "Character Ice 8", 205, 55, 16, "Зимние нешипованные", "94", "T", False, False, 6000.0)


def listing(avito_id=1, price=5500, **fields):
    raw = dict(avito_id=avito_id, price=price, brand="Ikon Tyres", model="Character Ice 8", width=205, height=55, diameter=16,
               tire_type="Зимние нешипованные", load="94", speed="T", condition="Новое", seller_kind="shop", seller_name="Шинный двор",
               url="https://www.avito.ru/sankt-peterburg/zapchasti/x_%d" % avito_id, listed_at="2026-10-25T10:00:00")
    raw.update(fields)
    return market.parse_listing(raw)


# ---- the matching ---------------------------------------------------------------------------------------------------------------------

def test_the_same_tyre_is_a_competitor_of_full_weight():
    assert market.match(OURS, listing(), RUN) == (1.0, "")
    assert market.match(OURS, listing(brand="IKON", model="character ice 8"), RUN) == (1.0, "")      # the forms of the names


@pytest.mark.parametrize("fields, reason", [
    (dict(brand="Nokian"), "другой бренд"),
    (dict(model="Character Ice 7"), "другая модель"),
    (dict(width=215), "другой размер"), (dict(height=60), "другой размер"), (dict(diameter="R17"), "другой размер"),
    (dict(tire_type="Зимние шипованные"), "другой сезон или шипы"), (dict(tire_type="Летние"), "другой сезон или шипы"),
    (dict(condition="Б/у"), "не новая"),
    (dict(listed_at="2026-09-20T10:00:00"), "старше 30 дней"),
    (dict(load="96"), "индексы дальше одной ступени"), (dict(speed="H"), "индексы дальше одной ступени"),
])
def test_a_hard_feature_apart_is_no_competitor(fields, reason):
    assert market.match(OURS, listing(**fields), RUN) == (0.0, reason)


def test_the_season_and_the_studs_may_come_apart():
    assert market.match(OURS, listing(tire_type="", season="winter", studded=False), RUN) == (1.0, "")
    assert market.match(OURS, listing(tire_type="", season="winter", studded=True), RUN)[1] == "другой сезон или шипы"


def test_our_own_ads_are_left_out_by_number_and_by_seller():
    assert market.match(OURS, listing(avito_id=77), RUN, own_ids={77}) == (0.0, "наше объявление")
    assert market.match(OURS, listing(seller_id="555"), RUN, own_sellers={"555"}) == (0.0, "наше объявление")


def test_the_soft_features_lower_the_weight():
    assert market.match(OURS, listing(load="95"), RUN) == (0.5, "")
    assert market.match(OURS, listing(speed="S"), RUN) == (0.5, "") and market.match(OURS, listing(speed="U"), RUN) == (0.5, "")
    assert market.match(OURS, listing(runflat=True), RUN) == (0.5, "")
    assert market.match(OURS, listing(xl=True), RUN) == (0.75, "")
    assert market.match(OURS, listing(year=2023), RUN) == (0.75, "")
    assert market.match(OURS, listing(year=2025), RUN) == (1.0, "")


def test_a_synonym_of_the_model_is_the_same_model():
    synonyms = {("IKON", "CHARACTERICE8SUV"): "CHARACTERICE8"}
    assert market.match(OURS, listing(model="Character Ice 8 SUV"), RUN, synonyms=synonyms) == (1.0, "")


def test_a_set_is_priced_for_one_tyre():
    assert market.per_unit(listing(price=22000, lot=4)) == 5500
    assert market.per_unit(listing(price=5500)) == 5500


def test_no_personal_data_and_no_foreign_link_is_kept():
    private = listing(seller_kind="private", seller_name="Иван Петров", phone="+79001234567", url="https://evil.example/x")
    assert private["seller"] == "частник" and private["url"] == "" and "phone" not in private
    assert listing()["seller"] == "Шинный двор"
    assert market.parse_listing({"avito_id": "x", "price": 1}) is None and market.parse_listing({"avito_id": 1, "price": "—"}) is None


# ---- the summary ----------------------------------------------------------------------------------------------------------------------

def test_fewer_than_three_reliable_analogues_is_insufficient():
    snapshot, rows = market.summarize(OURS, [listing(1, 5000), listing(2, 5200), listing(3, 5100, load="95")], RUN)
    assert snapshot["status"] == "insufficient" and snapshot["count"] == 2
    assert [r["reason"] for r in rows] == ["", "", "низкий вес"]


def test_the_quartiles_without_the_outlier_and_other_tyres_not_kept():
    prices = [5000, 5100, 5200, 5300, 5400, 20000]
    found = [listing(i + 1, p) for i, p in enumerate(prices)] + [listing(9, 1000, model="Другая"), listing(10, 4000, condition="б/у")]
    snapshot, rows = market.summarize(OURS, found, RUN)
    assert snapshot["status"] == "ok" and snapshot["count"] == 5
    assert (snapshot["min"], snapshot["median"], snapshot["max"]) == (5000, 5200, 5400)
    assert (snapshot["q1"], snapshot["q3"]) == (5100, 5300)
    assert {r["avito_id"]: r["reason"] for r in rows if r["reason"]} == {6: "выброс", 10: "не новая"}       # another model: not kept at all


def test_the_place_of_our_price():
    snapshot = {"status": "ok", "min": 5000, "q1": 5100, "median": 5200, "q3": 5300}
    assert market.place(4900, snapshot) == "дешевле всех" and market.place(5100, snapshot) == "в нижней четверти"
    assert market.place(5200, snapshot) == "ниже медианы" and market.place(5564, snapshot) == "выше медианы на 7 %"
    assert market.dearer(5260, snapshot) and not market.dearer(5201, snapshot)              # less than 1 %: at the median
    assert market.place(5000, dict(snapshot, status="insufficient")) == "" and not market.dearer(9000, None)


# ---- the base and the intake ----------------------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def market_shown(monkeypatch):
    """«Рынок» is hidden and its intake off by default (the owner 2026-10-30): these tests look at the stage 0 switched on."""
    from tires_sync.panel import routes_catalog
    monkeypatch.setattr(routes_catalog, "MARKET_SHOWN", True)


def setup(tmp_path, on=True):
    from datetime import datetime
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 31, 8, 0, 0))
    conn = store.connect(app.config["DB_PATH"])
    if on:
        store.set_state(conn, routes_market.STOP_KEY, "0")
    account = connections.create_account(conn, "Главный", "2026-10-30T10:00:00", "o")
    store.upsert_published(conn, PublishedItem("100", 4810, 5000.0, "P1"), "<Ad/>", account)
    store.upsert_published(conn, PublishedItem("102-x4", 4810, 5000.0, "P1"), "<Ad/>", account)
    return app, conn, account


def body(listings, **fields):
    data = {"run_at": RUN, "slot": "morning", "searches": [{"width": 205, "height": 55, "diameter": 16, "listings": listings}]}
    data.update(fields)
    return data


def send(app, token, data):
    return app.test_client().post("/api/market-snapshots", data=json.dumps(data), content_type="application/json",
                                  headers={"Authorization": "Bearer " + token} if token else {})


def raw(avito_id, price, model="Test Model", **fields):
    item = dict(avito_id=avito_id, price=price, brand="Ikon Tyres", model=model, width=205, height=55, diameter=16, tire_type="Зимние нешипованные",
                load="94", speed="T", condition="new", seller_kind="private", seller_name="Иван", phone="+79001112233", listed_at="2026-10-30T08:00:00")
    item.update(fields)
    return item


def test_the_intake_needs_its_own_token_and_keeps_only_the_hash(tmp_path):
    app, conn, account = setup(tmp_path)
    assert send(app, "", body([])).status_code == 403
    assert send(app, "whatever", body([])).status_code == 403                           # none issued: always 403
    token = routes_market.issue_token(conn)
    stored = store.get_state(conn, routes_market.TOKEN_KEY)
    assert token not in stored and len(stored) == 64
    r = send(app, token, body([]))
    assert r.status_code == 200 and "Set-Cookie" not in r.headers                       # no session for the Mac, no CSRF
    assert app.test_client().get("/catalog", headers={"Authorization": "Bearer " + token}).status_code == 302     # it opens nothing else
    other = routes_market.issue_token(conn)
    assert send(app, token, body([])).status_code == 403 and send(app, other, body([])).status_code == 200
    from tires_sync.panel import routes_agent                                           # the photo agent's token is not this one
    assert send(app, routes_agent.issue_agent_token(conn), body([])).status_code == 403


def test_the_intake_matches_our_positions_of_the_size_and_stores_no_personal_data(tmp_path):
    app, conn, account = setup(tmp_path)
    token = routes_market.issue_token(conn)
    found = [raw(1, 5000), raw(2, 5200), raw(3, 20800, lot=4), raw(4, 5100, load="96"), raw(5, 3000, brand="Laufenn", model="G Fit", load="96")]
    r = send(app, token, body(found))
    assert r.status_code == 200 and r.get_json() == {"snapshots": 2}                     # 100 and 102 (its «-x4»), both 205/55 R16
    snaps = market.latest(conn)
    assert set(snaps) == {"100", "102"}
    assert (snaps["100"]["status"], snaps["100"]["count"], snaps["100"]["median"]) == ("ok", 3, 5200)
    assert snaps["102"]["status"] == "insufficient" and snaps["102"]["count"] == 1
    rows = market.listings_of(conn, snaps["100"]["id"])
    assert [(r["avito_id"], r["per_unit"], r["lot"], r["reason"]) for r in rows] == [(1, 5000, 1, ""), (2, 5200, 1, ""), (3, 5200, 4, ""),
                                                                                    (4, 5100, 1, "индексы дальше одной ступени")]
    dump = "\n".join(map(str, conn.execute("SELECT * FROM market_listing").fetchall()))
    assert "Иван" not in dump and "79001112233" not in dump and "частник" in dump


def test_the_intake_refuses_a_bad_body_and_writes_nothing(tmp_path):
    app, conn, account = setup(tmp_path)
    token = routes_market.issue_token(conn)
    for bad in (body([], run_at="вчера"), body([], slot="night"), body([], status="fine"), {"run_at": RUN, "slot": "morning", "searches": "x"},
                body([raw(i, 5000) for i in range(routes_market.MAX_LISTINGS + 1)])):
        assert send(app, token, bad).status_code == 400
    assert app.test_client().post("/api/market-snapshots", data="not json", headers={"Authorization": "Bearer " + token}).status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM market_snapshot").fetchone()[0] == 0


def test_a_blocked_run_says_why_on_the_sizes_it_searched(tmp_path):
    app, conn, account = setup(tmp_path)
    token = routes_market.issue_token(conn)
    assert send(app, token, body([], status="blocked", note="капча 07:42")).get_json() == {"snapshots": 2}
    snapshot = market.latest(conn)["100"]
    assert snapshot["status"] == "insufficient" and snapshot["note"] == "прогон остановлен: капча 07:42"


def test_our_own_ad_and_cabinet_are_left_out_at_the_intake(tmp_path):
    app, conn, account = setup(tmp_path)
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, price, status, seen_at) VALUES (?, 7, '100', 't', 5000, 'active', ?)",
                 (account, RUN))
    conn.commit()
    connections.set_avito_identity(conn, account, 4242, "Главный")
    token = routes_market.issue_token(conn)
    send(app, token, body([raw(7, 4000), raw(8, 4100, seller_id="4242"), raw(9, 5000)]))
    rows = market.listings_of(conn, market.latest(conn)["100"]["id"])
    assert {r["avito_id"]: r["reason"] for r in rows} == {7: "наше объявление", 8: "наше объявление", 9: ""}


def test_the_switch_stops_the_intake(tmp_path):
    app, conn, account = setup(tmp_path)
    token = routes_market.issue_token(conn)
    client = app.test_client()
    tok = login(client)
    post(client, "/catalog/market-switch", tok, stop="1")
    assert send(app, token, body([raw(1, 5000)])).status_code == 503
    assert conn.execute("SELECT COUNT(*) FROM market_snapshot").fetchone()[0] == 0
    post(client, "/catalog/market-switch", tok, stop="0")
    assert send(app, token, body([raw(1, 5000)])).status_code == 200


def test_the_old_rows_go(tmp_path):
    app, conn, account = setup(tmp_path)
    ours = {"100": market.Ours("100", "Ikon Tyres", "Test Model", 205, 55, 16, "Зимние нешипованные", "94", "T", False, False)}
    searches = body([raw(1, 5000)])["searches"]
    market.save(conn, ours, searches, "2025-10-01T07:00:00", "morning", "2025-10-01T07:00:00")
    market.save(conn, ours, searches, "2026-08-25T07:00:00", "morning", "2026-08-25T07:00:00")
    market.save(conn, ours, searches, RUN, "morning", RUN)
    runs = [r[0] for r in conn.execute("SELECT run_at FROM market_snapshot ORDER BY run_at")]
    assert runs == ["2026-08-25T07:00:00", RUN]                                           # a year for the snapshots
    assert [r[0] for r in conn.execute("SELECT DISTINCT run_at FROM market_listing")] == [RUN]   # 60 days for the listings


def test_the_token_page_shows_it_once_and_the_audit_has_a_text(tmp_path):
    app, conn, account = setup(tmp_path)
    client = app.test_client()
    tok = login(client)
    r = post(client, "/catalog/market-token", tok)
    page = r.get_data(as_text=True)
    assert r.headers["Cache-Control"] == "no-store" and "Новый токен парсера цен" in page
    token = page.split("<code>")[1].split("</code>")[0]
    assert send(app, token, body([])).status_code == 200
    from tires_sync.panel import texts
    entry = conn.execute("SELECT action, detail FROM audit WHERE action = 'market_token'").fetchone()
    assert entry and "токен парсера" in texts.audit_text({"action": entry[0], "detail": json.loads(entry[1] or "{}")})


# ---- the catalog ----------------------------------------------------------------------------------------------------------------------

def test_the_catalog_without_data_is_empty_and_works(tmp_path):
    app, conn, account = setup(tmp_path)
    client = app.test_client()
    login(client)
    page = client.get("/catalog").get_data(as_text=True)
    assert ">Рынок</th>" in page and 'name="mk"' in page and "Парсер цен конкурентов" in page
    assert client.get("/catalog?mk=over").status_code == 200
    item = client.get("/catalog/item/100").get_data(as_text=True)
    assert "Конкуренты" in item and "Данных пока нет" in item


def test_the_catalog_shows_the_market_and_filters_the_dearer(tmp_path):
    app, conn, account = setup(tmp_path)
    token = routes_market.issue_token(conn)
    send(app, token, body([raw(1, 500), raw(2, 520), raw(3, 540)]))                         # far below any price of ours
    client = app.test_client()
    login(client)
    page = client.get("/catalog").get_data(as_text=True)
    assert "выше медианы на" in page and "500 ₽ / 520 ₽" in page.replace(" ", " ")
    over = client.get("/catalog?mk=over").get_data(as_text=True)
    assert "Найдено 1 позиций" in over                                                       # 100 only: 102 has too few analogues
    item = client.get("/catalog/item/100").get_data(as_text=True)
    block = item.split("Конкуренты")[1]
    assert "медиана" in block and "частник" in block and "Иван" not in block


def test_a_role_without_money_sees_no_market(tmp_path):
    app, conn, account = setup(tmp_path)
    send(app, routes_market.issue_token(conn), body([raw(1, 500), raw(2, 520), raw(3, 540)]))
    viewer = as_role(app, conn, "chats", "masha")
    page = viewer.get("/catalog").get_data(as_text=True)
    assert viewer.get("/catalog").status_code == 200
    assert ">Рынок</th>" not in page and "выше медианы" not in page and "Парсер цен" not in page
    item = viewer.get("/catalog/item/100").get_data(as_text=True)
    assert "Конкуренты" not in item
    assert post(viewer, "/catalog/market-token", "tok").status_code == 403


# ---- review, round 1 ------------------------------------------------------------------------------------------------------------------

def test_a_listing_repeated_on_the_pages_counts_once():
    snapshot, rows = market.summarize(OURS, [listing(1, 5000), listing(1, 5000), listing(1, 5000), listing(2, 5200)], RUN)
    assert snapshot["status"] == "insufficient" and snapshot["count"] == 2 and len(rows) == 2


def test_only_the_sizes_searched_get_a_snapshot(tmp_path):
    """A «similar» listing of another size in the answer never writes a snapshot «мало данных» over the size's own one."""
    app, conn, account = setup(tmp_path)
    token = routes_market.issue_token(conn)
    data = {"run_at": RUN, "slot": "morning", "searches": [{"width": 195, "height": 65, "diameter": "R15", "listings": [raw(1, 5000)]}]}
    assert send(app, token, data).get_json() == {"snapshots": 0}
    data["searches"][0].update(width=205, height=55, diameter="R16", listings=[])
    assert send(app, token, data).get_json() == {"snapshots": 2}                          # «R16» of a search is read as of a listing


def test_a_stopped_run_keeps_what_it_found_and_the_last_data_stays(tmp_path):
    app, conn, account = setup(tmp_path)
    token = routes_market.issue_token(conn)
    send(app, token, body([raw(1, 5000), raw(2, 5200), raw(3, 5300)]))
    first = market.latest(conn)["100"]
    send(app, token, {"run_at": "2026-10-31T07:55:00", "slot": "evening", "status": "blocked", "note": "капча 19:05", "searches": []})
    assert market.latest(conn)["100"] == first                                            # nothing searched: nothing written over
    send(app, token, body([raw(1, 5100), raw(2, 5200), raw(3, 5300)], run_at="2026-10-31T07:58:00", slot="evening", status="blocked",
                          note="капча 19:05"))
    last = market.latest(conn)["100"]
    assert last["status"] == "ok" and last["median"] == 5200 and last["note"] == "прогон остановлен: капча 19:05"   # the searches done before it count


@pytest.mark.parametrize("when", ["2099-01-01T07:00:00", "2026-10-28T07:00:00", "2026-13-45T99:99:00"])
def test_a_run_at_far_from_now_is_refused_and_deletes_nothing(tmp_path, when):
    app, conn, account = setup(tmp_path)
    token = routes_market.issue_token(conn)
    send(app, token, body([raw(1, 5000)], run_at="2026-10-30T20:00:00"))
    assert send(app, token, body([raw(1, 5000)], run_at=when)).status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM market_listing").fetchone()[0] == 1


def test_a_body_over_the_limit_is_refused(tmp_path):
    app, conn, account = setup(tmp_path)
    token = routes_market.issue_token(conn)
    big = json.dumps(body([raw(1, 5000, title="x" * (routes_market.MAX_BODY + 10))]))
    r = app.test_client().post("/api/market-snapshots", data=big, content_type="application/json", headers={"Authorization": "Bearer " + token})
    assert r.status_code == 413
    assert app.test_client().post("/api/market-snapshots", data=big, content_type="application/json").status_code == 403


@pytest.mark.parametrize("fields, reason", [
    (dict(condition=""), "состояние не указано"),
    (dict(listed_at="вчера"), "дата не разобрана"),
    (dict(tire_type="", season="winter"), "другой сезон или шипы"),         # winter without the studs said: not known
])
def test_an_unknown_hard_feature_is_no_competitor(fields, reason):
    raw_listing = dict(avito_id=1, price=5000, brand="Ikon Tyres", model="Character Ice 8", width=205, height=55, diameter=16,
                       tire_type="Зимние нешипованные", load="94", speed="T", condition="new", listed_at="2026-10-25T10:00:00")
    raw_listing.update(fields)
    assert market.match(OURS, market.parse_listing(raw_listing), RUN) == (0.0, reason)


def test_a_date_of_the_card_in_the_russian_form_is_read():
    assert market.match(OURS, listing(listed_at="25.10.2026"), RUN) == (1.0, "")
    assert market.match(OURS, listing(listed_at="20.09.2026"), RUN)[1] == "старше 30 дней"


def test_a_lot_over_eight_and_odd_numbers_are_dropped():
    assert listing(lot=10) is None and listing(lot=0)["lot"] == 1
    assert market.parse_listing(dict(avito_id="²", price=5000)) is None
    assert listing(year=10 ** 30)["year"] is None


def test_a_phone_in_the_title_is_not_kept():
    assert "900" not in listing(title="Ikon 205/55 звоните +7 900 123-45-67")["title"]
    assert "8 (921) 1234567" not in listing(title="Ikon 8 (921) 1234567 срочно")["title"]


def test_the_place_at_the_median_and_a_model_named_with_its_brand():
    snapshot = {"status": "ok", "min": 5000, "q1": 5100, "median": 5200, "q3": 5300}
    assert market.place(5210, snapshot) == "на уровне медианы" and not market.dearer(5210, snapshot)
    assert market.match(OURS, listing(model="Ikon Character Ice 8"), RUN) == (1.0, "")


def test_the_time_of_a_run_comes_with_its_offset_and_moscow_without_it_is_refused(tmp_path):
    """Review, round 2: the server's clock is UTC; the Mac sends its time with the offset, the panel shows Moscow time."""
    app, conn, account = setup(tmp_path)
    token = routes_market.issue_token(conn)
    assert send(app, token, body([raw(1, 5000)], run_at="2026-10-31T10:40:00")).status_code == 400     # Moscow without its offset
    assert send(app, token, body([raw(1, 5000)], run_at="2026-10-31T10:40:00+03:00")).status_code == 200
    assert send(app, token, body([raw(1, 5000)], run_at="2026-10-31T07:45:00Z")).status_code == 200
    assert [r[0] for r in conn.execute("SELECT DISTINCT run_at FROM market_snapshot ORDER BY run_at")] == ["2026-10-31T07:40:00", "2026-10-31T07:45:00"]
    client = app.test_client()
    login(client)
    send(app, token, body([raw(1, 5000)], status="blocked", note="капча"))
    item = client.get("/catalog/item/100").get_data(as_text=True).split("Конкуренты")[1]
    assert "Прогон 31.10.2026 10:40" in item and "прогон остановлен: капча" in item


def test_the_market_is_hidden_and_its_intake_off_by_default(tmp_path, monkeypatch):
    from tires_sync.panel import routes_catalog
    monkeypatch.setattr(routes_catalog, "MARKET_SHOWN", False)
    app, conn, account = setup(tmp_path, on=False)
    token = routes_market.issue_token(conn)
    assert send(app, token, body([raw(1, 5000)])).status_code == 503                          # nothing taken until switched on
    client = app.test_client()
    login(client)
    page = client.get("/catalog").get_data(as_text=True)
    assert "Рынок" not in page and "Дороже рынка" not in page and "data-market-parser" not in page and "Парсер цен" not in page
    assert "Конкуренты" not in client.get("/catalog/item/100").get_data(as_text=True)
