"""Every field of the order window is kept by the first save and by the next ones (the owner 2026-10-26: «поле «Точка» слетела после
первого сохранения»): the window is sent as a browser sends it, the card is read back. «+» of the card sends the whole window: its fields
are saved with the position, never dropped."""
from __future__ import annotations

import html
import re

from test_order_window import created, setup
from panel_helpers import post
from tires_sync import orders, users
from tires_sync import order_points as points


def window(page: str) -> list:
    """The pairs the browser sends for the form #ow-main of the page: its own fields (not those of form="…"), a checkbox only when ticked,
    a select with its selected option (the first one without)."""
    start = page.index('id="ow-main"')
    body = page[start:page.index("</form>", start)]
    out = []
    for m in re.finditer(r"<(input|select|textarea)\b([^>]*)>", body):
        tag, attrs = m.group(1), m.group(2)
        name = re.search(r'\bname="([^"]*)"', attrs)
        if not name or 'form="' in attrs or " disabled" in attrs:
            continue
        name = name.group(1)
        if tag == "input":
            kind = re.search(r'\btype="([^"]*)"', attrs)
            if kind and kind.group(1) == "checkbox" and "checked" not in attrs:
                continue
            value = re.search(r'\bvalue="([^"]*)"', attrs)
            out.append((name, html.unescape(value.group(1)) if value else ""))
        elif tag == "select":
            options = body[m.end():body.index("</select>", m.end())]
            chosen = re.search(r'<option value="([^"]*)"[^>]*\bselected', options) or re.search(r'<option value="([^"]*)"', options)
            out.append((name, html.unescape(chosen.group(1)) if chosen else ""))
        else:
            out.append((name, html.unescape(body[m.end():body.index("</textarea>", m.end())])))
    return out


def send(client, url, pairs, **changed):
    """The window as the browser sends it, with the fields the manager changed."""
    data = {}
    for name, value in pairs:
        data.setdefault(name, []).append(value)
    for name, value in changed.items():
        data[name] = value if isinstance(value, list) else [value]
    return client.post(url, data=data)


def fields(conn, card_id) -> dict:
    c = orders.get_card(conn, card_id)
    return {k: c[k] for k in ("client_name", "client_phone", "point_id", "deal_kind", "responsible", "tags", "delivery_date", "delivery_time",
                              "webmim_number", "transfer_number", "comment", "special_terms", "special_amount", "receipt_needed", "avito_chat",
                              "source", "webmim_id", "webmim_status", "reserve_until")}


def world(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", "2026-10-15T12:00:00")
    a, b = points.save_point(conn, {"name": "Альфа"}), points.save_point(conn, {"name": "Бета"})
    return app, client, tok, conn, a, b


EVERY = dict(client_name="Антон", client_phone="+7 900 123-45-67", deal_kind="stock", responsible="worker2", tags="Основной, повторный",
             delivery_date="2026-10-21", delivery_time="после 18", webmim_number="5000300", transfer_number="26R-1", comment="Объявление",
             special_terms="предоплата", special_amount="1500", receipt_needed=["1", ""], avito_chat="Ч-2346", source="repeat")
STORED = dict(client_name="Антон", client_phone="+7 900 123-45-67", deal_kind="stock", responsible="worker2", tags=["Основной", "повторный"],
              delivery_date="2026-10-21", delivery_time="после 18", webmim_number="5000300", transfer_number="26R-1", comment="Объявление",
              special_terms="предоплата", special_amount=1500.0, receipt_needed=1, avito_chat="Ч-2346", source="repeat")


def test_every_field_of_a_new_order_is_kept_by_the_first_save_and_by_a_save_of_the_card_as_it_is(tmp_path, monkeypatch):
    app, client, tok, conn, a, b = world(tmp_path, monkeypatch)
    page = client.get("/orders/new").get_data(as_text=True)
    cid = created(send(client, "/orders/new", window(page), point_id=str(a), **EVERY))
    first = fields(conn, cid)
    assert first == dict(first, point_id=a, **STORED)
    for _ in range(2):                                                    # «Сохранить» of the card with nothing changed, twice
        page = client.get("/orders/%d" % cid).get_data(as_text=True)
        assert send(client, "/orders/%d/save" % cid, window(page)).status_code == 302
        assert fields(conn, cid) == first


def test_every_field_of_the_card_is_saved_and_kept_by_the_next_save(tmp_path, monkeypatch):
    app, client, tok, conn, a, b = world(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert send(client, "/orders/%d/save" % cid, window(page), point_id=str(b), webmim_id="77", webmim_status="Собран",
                reserve_until="2026-10-30", **EVERY).status_code == 302
    saved = fields(conn, cid)
    assert saved == dict(saved, point_id=b, webmim_id="77", webmim_status="Собран", reserve_until="2026-10-30", **STORED)
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    send(client, "/orders/%d/save" % cid, window(page), comment="перезвонить")
    assert fields(conn, cid) == dict(saved, comment="перезвонить")


def test_the_point_of_an_existing_order_is_changed_and_taken_off(tmp_path, monkeypatch):
    app, client, tok, conn, a, b = world(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван", point_id=str(a)))
    for point in (str(b), "", str(a)):
        page = client.get("/orders/%d" % cid).get_data(as_text=True)
        send(client, "/orders/%d/save" % cid, window(page), point_id=point)
        assert orders.get_card(conn, cid)["point_id"] == (int(point) if point else None)


def test_the_plus_of_the_card_saves_the_point_and_the_fields_chosen_with_the_position(tmp_path, monkeypatch):
    app, client, tok, conn, a, b = world(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    r = send(client, "/orders/%d/items/add" % cid, window(page), point_id=str(a), comment="к вечеру", goods_id="100",
             query="Autograph Snow 3 · 205/55 R16", qty="4")
    assert r.status_code == 302 and r.headers["Location"].endswith("#items")
    c = orders.get_card(conn, cid)
    assert (c["point_id"], c["comment"], [(i["goods_id"], i["qty"]) for i in c["items"]]) == (a, "к вечеру", [("100", 4)])
    page = client.get("/orders/%d" % cid).get_data(as_text=True)                                   # the next «Сохранить» keeps them
    send(client, "/orders/%d/save" % cid, window(page))
    c = orders.get_card(conn, cid)
    assert (c["point_id"], c["comment"], len(c["items"])) == (a, "к вечеру", 1)


def test_the_plus_with_a_wrong_field_adds_nothing_and_the_window_keeps_what_was_typed(tmp_path, monkeypatch):
    app, client, tok, conn, a, b = world(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    r = send(client, "/orders/%d/items/add" % cid, window(page), point_id=str(a), client_phone="12", goods_id="100",
             query="Autograph Snow 3 · 205/55 R16", qty="4")
    body = r.get_data(as_text=True)
    c = orders.get_card(conn, cid)
    assert (c["point_id"], c["client_phone"], c["items"]) == (None, "", [])
    assert "Телефон клиента" in body and 'value="12"' in body and 'value="Autograph Snow 3 · 205/55 R16"' in body


def test_the_plus_without_the_window_adds_the_position_as_before(tmp_path, monkeypatch):
    app, client, tok, conn, a, b = world(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван", point_id=str(a), comment="старый"))
    post(client, "/orders/%d/items/add" % cid, tok, goods_id="100", query="Autograph Snow 3 · 205/55 R16", qty="4")
    c = orders.get_card(conn, cid)
    assert (c["point_id"], c["comment"], len(c["items"])) == (a, "старый", 1)


def test_the_plus_with_no_row_saves_the_fields_and_says_the_position_is_not_chosen(tmp_path, monkeypatch):
    app, client, tok, conn, a, b = world(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    r = send(client, "/orders/%d/items/add" % cid, window(page), point_id=str(b))
    c = orders.get_card(conn, cid)
    assert r.status_code == 302 and (c["point_id"], c["items"]) == (b, [])
    assert "Позиция не выбрана" in client.get(r.headers["Location"]).get_data(as_text=True)
