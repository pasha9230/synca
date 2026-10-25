"""Coloured dots by the order numbers in the list and on the board of orders."""
from __future__ import annotations

import re
from datetime import datetime

from panel_helpers import login, make_app
from tires_sync import orders, store

NOW = "2026-10-20T12:00:00"


def setup(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 20, 9, 0, 0))
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    conn.commit()
    return app, client, conn


def card(conn, name):
    cid = orders.create_card(conn, {"client_name": name}, NOW, "owner")        # its own «Заказ создан»
    conn.commit()
    return cid


def event(conn, cid, kind, text, level="", author="система"):
    orders.add_event(conn, cid, NOW, author, kind, text, level=level)
    conn.commit()


def dots(html):
    """{order: (colour, title)} from the page; a dot stands right before its number."""
    return {int(m.group(3)): (m.group(1), m.group(2)) for m in re.finditer(
        r'<span class="odot odot--(\w+)" title="([^"]*)"></span>(?:<a href="/orders/\d+">)?\u2116(\d+)', html)}


def test_the_most_important_colour_in_list_and_board(tmp_path):
    app, client, conn = setup(tmp_path)
    red, yellow, plain = card(conn, "Красный"), card(conn, "Жёлтый"), card(conn, "Тихий")
    for cid in (red, yellow, plain):                      # own «Заказ создан» is no alert for the author
        conn.execute("DELETE FROM order_event WHERE order_id = ?", (cid,))
    conn.commit()
    event(conn, red, "reserve", "Оповещение о конце резерва")
    event(conn, red, "notify", "Оповещение не отправлено: у точки нет ВК")
    event(conn, yellow, "reserve", "Оповещение о конце резерва")
    event(conn, plain, "notify", "Оповещение отправлено: Telegram")             # grey
    for url in ("/orders?v=list", "/orders"):
        found = dots(client.get(url).get_data(as_text=True))
        assert found[red] == ("red", "Нужно действие")
        assert found[yellow] == ("yellow", "Внимание")
        assert plain not in found


def test_own_green_is_no_dot_but_a_failure_of_the_user_is(tmp_path):
    app, client, conn = setup(tmp_path)
    cid, bad = card(conn, "Свой"), card(conn, "Сбой")
    event(conn, cid, "reserve", "Оповещение о конце резерва", author="owner")
    event(conn, bad, "notify", "Оповещение не отправлено: у точки нет ВК", level="red", author="owner")
    found = dots(client.get("/orders?v=list").get_data(as_text=True))
    assert cid not in found and found[bad][0] == "red"


def test_a_failing_count_does_not_break_the_page(tmp_path, monkeypatch):
    app, client, conn = setup(tmp_path)
    cid = card(conn, "Иван")
    from tires_sync.panel import routes_alerts

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(routes_alerts, "grouped", boom)
    for url in ("/orders?v=list", "/orders"):
        response = client.get(url)
        assert response.status_code == 200 and "odot" not in response.get_data(as_text=True).split("<body")[1]
        assert "\u2116%d" % cid in response.get_data(as_text=True)


def test_an_older_red_beats_a_newer_yellow_and_green_has_its_colour(tmp_path):
    app, client, conn = setup(tmp_path)
    mixed, calm = card(conn, "Смесь"), card(conn, "Спокойный")
    for cid in (mixed, calm):
        conn.execute("DELETE FROM order_event WHERE order_id = ?", (cid,))
    conn.commit()
    event(conn, mixed, "notify", "Оповещение не отправлено: у точки нет ВК", level="red")    # older
    event(conn, mixed, "reserve", "Оповещение о конце резерва")                              # newer, yellow
    event(conn, calm, "stage", "Заказ создан", level="green")
    for url in ("/orders?v=list", "/orders"):
        found = dots(client.get(url).get_data(as_text=True))
        assert found[mixed] == ("red", "Нужно действие")
        assert found[calm] == ("green", "Движется")
