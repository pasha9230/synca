"""«Закупка изменилась» (слово владельца 05–06.11): после цикла, где изменилась закупка хотя бы одной позиции (новая строка истории, любой
поставщик), одно сообщение на цикл владельцу и тем, кому можно видеть закупку; повышения и снижения отдельно, первые 20 по проценту и «ещё N
позиций» со ссылкой на весь список; красная строка колокольчика до «Разобрался»; первый запуск молчит; каждое изменение один раз; ночью ждёт
рабочих часов; сбой не останавливает цикл. Прежняя сводка «своя цена в процентах пошла за закупкой» заменена этим сообщением."""
from __future__ import annotations

from helpers import make_offer
from panel_helpers import post
from test_manual_prices import _gone_app, _owner
from test_roles import person
from test_stock_fresh import cycle, prepared, published, rows
from tires_sync import catalog_archive, cli, connections, order_notify, order_oth, purchase_alert, store, users
from tires_sync.webmim import ParseResult

DAY = "2026-11-06T10:00:00"
NIGHT = "2026-11-06T03:00:00"


def _cycle(conn, at, **prices):
    catalog_archive.record(conn, ParseResult(offers={g: make_offer(goods_id=g, name="Шина " + g, base_price=p) for g, p in prices.items()}), at)


def _sent(monkeypatch, ok=True):
    """Что ушло в канал важных оповещений: владельцу (_alert) и остальным (tell_people)."""
    sent = {"owner": [], "people": []}

    def alert(conn, box, text, **kw):
        sent["owner"].append(text)
        return ok
    monkeypatch.setattr(cli, "_alert", alert)
    monkeypatch.setattr(order_notify, "tell_people", lambda conn, box, logins, text, **kw: sent["people"].append((list(logins), text)) or "Telegram: отправлено")
    return sent


def _ready(tmp_path, **prices):
    """База, где история уже велась до выкладки: первый запуск только запоминает."""
    conn = store.connect(tmp_path / "t.db")
    _cycle(conn, "2026-11-05T10:00:00", **prices)
    _cycle(conn, "2026-11-05T11:00:00", **{g: p + 100 for g, p in prices.items()})       # изменения до выкладки: о них не говорим
    assert purchase_alert.collect(conn, DAY) is None
    return conn


# --- что и когда ---------------------------------------------------------------------------------------------------------------------

def test_the_first_run_after_the_deploy_is_silent_and_then_each_change_is_told_once(tmp_path, monkeypatch):
    conn = _ready(tmp_path, a=8000.0, b=5000.0)
    sent = _sent(monkeypatch)
    cli._alert_purchase(conn, None, now=DAY)
    assert sent["owner"] == []                                                   # нового нет: молчим
    _cycle(conn, "2026-11-06T09:00:00", a=8480.0, b=5100.0)                      # было 8 100 и 5 100: a выросла, b та же
    cli._alert_purchase(conn, None, now=DAY)
    assert len(sent["owner"]) == 1 and "Шина a: 8 100 → 8 480 (+5 %)" in sent["owner"][0] and "Шина b" not in sent["owner"][0]
    cli._alert_purchase(conn, None, now=DAY)                                    # повторный прогон: то же не шлём
    _cycle(conn, "2026-11-06T10:00:00", a=8480.0, b=5100.0)
    cli._alert_purchase(conn, None, now=DAY)
    assert len(sent["owner"]) == 1


def test_rises_and_falls_apart_with_our_price_when_there_is_one(tmp_path):
    conn = _ready(tmp_path, a=7900.0, b=5000.0, c=3000.0)                        # стало 8 000, 5 100, 3 100
    account = connections.create_account(conn, "A", DAY, "owner")
    conn.execute("INSERT INTO published_state (account_id, goods_id, price, base_price, point, snapshot) VALUES (?, 'a', 9500, 8000, '', '{}')", (account,))
    conn.commit()
    _cycle(conn, "2026-11-06T09:00:00", a=8480.0, b=4590.0, c=3100.0)
    alert = purchase_alert.collect(conn, DAY)
    text = purchase_alert.text(alert["rows"], "https://example.com/prices/purchase/1")
    lines = text.split("\n")
    assert lines[0] == "Tires Sync: Закупка у поставщика изменилась: 2 позиции (выросла: 1, снизилась: 1). Цены за одну шину."
    assert lines[1:] == ["Выросла:", "Шина a: 8 000 → 8 480 (+6 %), наша цена 9 500", "Снизилась:", "Шина b: 5 100 → 4 590 (−10 %)"]


def test_twenty_biggest_in_percent_and_the_rest_by_the_link(tmp_path):
    prices = {"p%02d" % n: 1000.0 for n in range(25)}
    conn = _ready(tmp_path, **prices)                                            # стало 1 100 у всех
    _cycle(conn, "2026-11-06T09:00:00", **{g: 1110.0 + 10 * n for n, g in enumerate(sorted(prices))})
    alert = purchase_alert.collect(conn, DAY)
    text = purchase_alert.text(alert["rows"], "https://example.com/prices/purchase/%d" % alert["id"])
    assert "Шина p24:" in text and "Шина p05:" in text and "Шина p04:" not in text and "Шина p00:" not in text
    assert text.endswith("ещё 5 позиций: https://example.com/prices/purchase/%d" % alert["id"])
    assert text.index("Шина p24:") < text.index("Шина p23:")                     # самое большое изменение первым


def test_at_night_nothing_is_made_and_the_morning_tells_all_in_one(tmp_path, monkeypatch):
    conn = _ready(tmp_path, a=1000.0, b=2000.0)
    sent = _sent(monkeypatch)
    _cycle(conn, "2026-11-06T02:00:00", a=1200.0, b=2100.0)
    cli._alert_purchase(conn, None, now=NIGHT)
    assert sent["owner"] == [] and purchase_alert.bell_rows(conn, "owner") == []
    _cycle(conn, "2026-11-06T03:00:00", a=1300.0, b=2100.0)
    cli._alert_purchase(conn, None, now=NIGHT)
    cli._alert_purchase(conn, None, now=DAY)
    assert len(sent["owner"]) == 1 and "Шина a: 1 100 → 1 300" in sent["owner"][0] and "Шина b: 2 100 → 2 100" not in sent["owner"][0]
    assert "(выросла: 1, снизилась: 0)" in sent["owner"][0]


def test_a_change_that_came_back_says_nothing(tmp_path, monkeypatch):
    conn = _ready(tmp_path, a=1000.0)
    sent = _sent(monkeypatch)
    _cycle(conn, "2026-11-06T02:00:00", a=1200.0)
    _cycle(conn, "2026-11-06T03:00:00", a=1100.0)
    cli._alert_purchase(conn, None, now=DAY)
    assert sent["owner"] == [] and purchase_alert.bell_rows(conn, "owner") == []


# --- кому и каким каналом ------------------------------------------------------------------------------------------------------------

def test_the_owner_and_who_may_see_the_purchase_are_told_nobody_else(tmp_path, monkeypatch):
    conn = _ready(tmp_path, a=1000.0)
    for login, role in (("zaman", "partner"), ("olya", "chats"), ("vera", "viewer"), ("old", "partner")):
        users.add_user(conn, login, "Passw0rd-long-enough", DAY, role=role)
    conn.execute("UPDATE panel_user_role SET active = 0 WHERE login = 'old'")
    conn.commit()
    assert purchase_alert.people(conn) == ["owner", "zaman"]
    sent = _sent(monkeypatch)
    _cycle(conn, "2026-11-06T09:00:00", a=1200.0)
    cli._alert_purchase(conn, None, now=DAY)
    assert len(sent["owner"]) == 1 and sent["people"] == [(["zaman"], sent["owner"][0])]


def test_a_refused_telegram_is_printed_the_bell_keeps_it_and_it_is_not_resent(tmp_path, monkeypatch, capsys):
    conn = _ready(tmp_path, a=1000.0)
    sent = _sent(monkeypatch, ok=False)
    _cycle(conn, "2026-11-06T09:00:00", a=1200.0)
    cli._alert_purchase(conn, None, now=DAY)
    rows = purchase_alert.bell_rows(conn, "owner")
    assert len(rows) == 1 and rows[0]["level"] == "red"
    cli._alert_purchase(conn, None, now=DAY)
    assert len(sent["owner"]) == 1


def test_the_real_alert_refused_by_telegram_is_printed_as_every_alert(tmp_path, monkeypatch, capsys):
    from tires_sync.notify import NotifyError
    conn = _ready(tmp_path, a=1000.0)

    def refuse(text):
        raise NotifyError("403")
    monkeypatch.setattr(cli, "notifier_for", lambda *a, **k: refuse)
    _cycle(conn, "2026-11-06T09:00:00", a=1200.0)
    cli._alert_purchase(conn, None, now=DAY)
    assert "не удалось отправить уведомление: 403" in capsys.readouterr().err
    assert len(purchase_alert.bell_rows(conn, "owner")) == 1
    assert any("Закупка у поставщика изменилась" in e["detail"]["text"] for e in store.list_audit(conn, 20) if e["action"] == "notice")


def test_a_broken_alert_never_stops_the_cycle(tmp_path, monkeypatch, capsys):
    conn = prepared(tmp_path)
    monkeypatch.setattr(order_oth, "in_alert_hours", lambda conn, now: True)

    def broken(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(purchase_alert, "collect", broken)
    assert cycle(tmp_path, rows(dict(NNOMMODIF="101", NPRICE_DELAYED="5000"))) == 0
    assert "закупка изменилась» не удалась: RuntimeError: boom" in capsys.readouterr().err
    assert "101" in published(tmp_path)


def test_the_cycle_tells_the_change_after_the_publication_and_no_longer_the_old_own_price_summary(tmp_path, monkeypatch):
    from tires_sync import staging
    from tires_sync.settings_store import confirm_draft
    conn = prepared(tmp_path)
    staging.set_own_price(conn, "101", [1], {"pct": 10}, DAY)
    confirm_draft(conn, staging.summary(conn)["draft_id"], DAY, "owner", strong=True)
    monkeypatch.setattr(order_oth, "in_alert_hours", lambda conn, now: True)
    for price in ("5000", "5000", "4950"):
        assert cycle(tmp_path, rows(dict(NNOMMODIF="101", NPRICE_DELAYED=price))) == 0
    notices = [e["detail"]["text"] for e in store.list_audit(conn, 200) if e["action"] == "notice"]
    assert not [t for t in notices if "своя цена в процентах" in t]
    told = [t for t in notices if "Закупка у поставщика изменилась" in t]
    assert len(told) == 1 and "5 000 → 4 950 (−1 %), наша цена " + "{:,}".format(published(tmp_path)["101"].price).replace(",", " ") in told[0]
    assert published(tmp_path)["101"].price == 5450                           # цены считает цикл, как и без сообщения
    assert cycle(tmp_path, rows(dict(NNOMMODIF="101", NPRICE_DELAYED="4950"))) == 0
    assert len([e for e in store.list_audit(conn, 200) if e["action"] == "notice" and "Закупка у поставщика" in e["detail"]["text"]]) == 1


# --- колокольчик и страница ----------------------------------------------------------------------------------------------------------

def test_the_bell_has_one_red_row_until_handled_and_only_for_who_may_see_the_purchase(tmp_path):
    app, conn, _ids = _gone_app(tmp_path)
    assert purchase_alert.collect(conn, DAY) is None                             # первый запуск
    _cycle(conn, "2026-11-06T09:00:00", **{"100": 5200.0})
    first = purchase_alert.collect(conn, DAY)
    _cycle(conn, "2026-11-06T10:00:00", **{"100": 5400.0})
    second = purchase_alert.collect(conn, "2026-11-06T11:00:00")
    zaman, _c = person(app, "partner")
    olya, _c = person(app, "chats")
    owner, tok = _owner(app)
    feed = owner.get("/alerts/feed").get_json()
    mine = [i for i in feed["items"] if i["title"] == "Закупка изменилась"]
    assert feed["red"] and len(mine) == 1 and mine[0]["href"] == "/prices/purchase/%d" % second["id"]
    page = owner.get("/alerts").get_data(as_text=True)
    assert 'action="/alerts/purchase/%d/ack"' % second["id"] in page and "ещё 1" in page
    assert not [i for i in olya.get("/alerts/feed").get_json()["items"] if i["title"] == "Закупка изменилась"]
    assert [i for i in zaman.get("/alerts/feed").get_json()["items"] if i["title"] == "Закупка изменилась"]
    assert post(owner, "/alerts/purchase/%d/ack" % second["id"], tok).status_code == 302
    assert not [i for i in owner.get("/alerts/feed").get_json()["items"] if i["title"] == "Закупка изменилась"]
    assert [i for i in zaman.get("/alerts/feed").get_json()["items"] if i["title"] == "Закупка изменилась"]      # у каждого своё
    assert first["id"] < second["id"]


def test_the_whole_list_is_for_who_may_see_the_purchase_only(tmp_path):
    app, conn, _ids = _gone_app(tmp_path)
    assert purchase_alert.collect(conn, DAY) is None
    _cycle(conn, "2026-11-06T09:00:00", **{"100": 5200.0, "200": 3000.0})
    _cycle(conn, "2026-11-06T09:30:00", **{"100": 5200.0, "200": 3300.0})
    alert = purchase_alert.collect(conn, DAY)
    for client in (_owner(app)[0], person(app, "partner")[0]):
        page = client.get("/prices/purchase/%d" % alert["id"]).get_data(as_text=True).replace("\xa0", " ")
        assert "data-purchase-changes" in page and "Шина 200" in page and "3 000 ₽" in page and "3 300 ₽" in page and "+10 %" in page
        assert client.get("/prices/purchase").status_code == 200
    for role in ("chats", "viewer"):
        client = person(app, role)[0]
        assert client.get("/prices/purchase/%d" % alert["id"]).status_code == 403, role
        assert client.get("/prices/purchase").status_code == 403, role
    assert _owner(app)[0].get("/prices/purchase/999").status_code == 404


# --- по ревью ------------------------------------------------------------------------------------------------------------------------

def test_two_overlapping_cycles_make_one_alert(tmp_path, monkeypatch):
    """Цикл по таймеру и цикл с кнопки панели могут идти одновременно (feed_now.hold_cycle): то же изменение не уходит дважды."""
    first = _ready(tmp_path, a=1000.0)
    second = store.connect(tmp_path / "t.db")
    _cycle(first, "2026-11-06T09:00:00", a=1200.0)
    real, made = store.get_state, []

    def stale(conn, key):
        value = real(conn, key)                                                  # второй прочитал отметку…
        if conn is second and key == purchase_alert.UPTO and not made:
            made.append(purchase_alert.collect(first, DAY))                      # …а первый успел сделать сообщение
        return value
    monkeypatch.setattr(purchase_alert.store, "get_state", stale)
    assert purchase_alert.collect(second, DAY) is None
    assert made[0] is not None and first.execute("SELECT COUNT(*) FROM purchase_alert").fetchone()[0] == 1


def test_a_position_of_4tochki_shown_for_viewing_only_is_told_by_its_name(tmp_path, monkeypatch):
    from tires_sync import tochki_feed
    conn = store.connect(tmp_path / "t.db")
    price = {"s3-1": 2000.0}

    def view(conn_, parsed, base):
        only = {g: make_offer(goods_id=g, name="Шина 4tochki", base_price=p) for g, p in price.items()}
        return ParseResult(offers={**parsed.offers, **only}), frozenset(only)
    monkeypatch.setattr(tochki_feed, "catalog_view", view)
    sent = _sent(monkeypatch)
    parsed = ParseResult(offers={"100": make_offer()})
    for value in (2000.0, 2100.0):
        price["s3-1"] = value
        cli._record_archive(conn, None, parsed, tmp_path / "unload.xml")
        cli._alert_purchase(conn, None, parsed, tmp_path / "unload.xml", now=DAY)
    assert len(sent["owner"]) == 1 and "Шина 4tochki: 2 000 → 2 100 (+5 %)" in sent["owner"][0]
    assert purchase_alert.get(conn)["rows"][0]["name"] == "Шина 4tochki"           # и на странице списка


def test_a_refused_owner_telegram_still_tells_the_partner(tmp_path, monkeypatch):
    conn = _ready(tmp_path, a=1000.0)
    users.add_user(conn, "zaman", "Passw0rd-long-enough", DAY, role="partner")
    sent = _sent(monkeypatch, ok=False)
    _cycle(conn, "2026-11-06T09:00:00", a=1200.0)
    cli._alert_purchase(conn, None, now=DAY)
    assert [p[0] for p in sent["people"]] == [["zaman"]]
