"""«Заказ поставщику» (order_supplier, the owner 2026-10-29): the lines of «Сверка», the button shut exactly when webmim_create refuses,
the warning of 16:00 and «Перенести на завтра», «Заказать в 4 Точки» (the text, «Оформил вручную», no request anywhere), the page and the rights.
A fake WebMIM only (test_webmim_create.FakeWebmim): no request leaves the tests."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from panel_helpers import post, shared_mark_of
from test_webmim_create import NOW, TODAY, FakeWebmim, card, client_of, db, panel, person, point   # noqa: F401 (panel: the fixture)
from tires_sync import connections, order_supplier, orders, webmim_create


def unload(**offers):
    """The suppliers' last unload as order_supplier reads it: goods_id → (stock, price)."""
    return SimpleNamespace(offers={g: SimpleNamespace(stock=s, base_price=p) for g, (s, p) in offers.items()}, stocks={})


def lines(view):
    return {l["key"]: (l["state"], " ".join(l["texts"])) for l in view["lines"]}


def tochki(conn, sid=2):
    connections.set_config(conn, "tochki", 0, {"supplier_id": sid, "login": "x"}, NOW, "owner")
    conn.commit()


def test_a_whole_order_of_webmim_is_all_green_and_ready(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    view = order_supplier.view(conn, orders.get_card(conn, mine), unload(**{"111": (10, 4000), "222": (8, 4000)}), NOW)
    assert view["supplier"] == "webmim" and not view["blocked"] and not view["late"]
    assert all(state == "ok" for state, _ in lines(view).values()), lines(view)
    assert view["fields"] == webmim_create.fields(conn, orders.get_card(conn, mine))
    assert view["fingerprint"] == webmim_create.fingerprint(conn, orders.get_card(conn, mine))
    assert "Админ" in lines(view)["contact"][1] and "Грузим на ОТХ" in lines(view)["contact"][1]
    assert "+7(911)222-33-44" in lines(view)["phone"][1] and "10:00-18:00" in lines(view)["time"][1]


def test_the_stock_and_the_price_of_the_unload_only_warn(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    view = order_supplier.view(conn, orders.get_card(conn, mine), unload(**{"111": (2, 4100)}), NOW)
    found = lines(view)
    assert found["items"][0] == "warn" and "осталось 2 шт, нужно 4" in found["items"][1] and "«Шина 222»: нет в последней выгрузке" in found["items"][1]
    assert found["price"][0] == "warn" and "в выгрузке 4100 ₽, в заказе 4000 ₽" in found["price"][1]
    assert "«Шина 222»: не сверить, нет цены в выгрузке" in found["price"][1]
    assert not view["blocked"]                                                          # WebMIM answers for its stock; prices are never changed
    assert orders.get_card(conn, mine)["items"][0]["supplier_price"] == 4000


@pytest.mark.parametrize("make, key", [
    (lambda conn: card(conn, point_id=point(conn, name="Без адреса", webmim_address_id="")), "point"),
    (lambda conn: card(conn, point_id=point(conn, name="Телефон", manager_phone="+44 20 7946 0958")), "phone"),
    (lambda conn: card(conn, point_id=point(conn, name="Без времени", default_time="")), "time"),
    (lambda conn: card(conn, delivery_date="2026-10-22"), "date"),
    (lambda conn: card(conn, delivery_date=""), "date"),
    (lambda conn: card(conn, goods=(("x-0123456789ab", 4),)), "items"),
    (lambda conn: card(conn, territory=""), "territory"),
    (lambda conn: card(conn, point_id=None), "point"),
])
def test_a_red_line_shuts_the_button_exactly_as_the_press_refuses(tmp_path, make, key):
    conn = db(tmp_path)
    mine = make(conn)
    data = orders.get_card(conn, mine)
    view = order_supplier.view(conn, data, unload(), NOW)
    assert lines(view)[key][0] == "bad" and view["blocked"] and view["fields"] is None
    refusals = webmim_create.problems(conn, data, TODAY)
    assert refusals and all(any(r in " ".join(l["texts"]) for l in view["lines"]) for r in refusals)     # every refusal is on a line
    fake = FakeWebmim()
    with pytest.raises(orders.OrderError):
        webmim_create.place(conn, mine, client_of(fake), "test", NOW, TODAY, "owner", sleep=lambda _s: None)
    assert not fake.posts()                                                             # (a point without an address reads the addresses for the hint)


def test_today_after_sixteen_warns_and_tomorrow_moves_the_date(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, delivery_date="2026-10-23")
    late = "2026-10-23T16:30:00"
    view = order_supplier.view(conn, orders.get_card(conn, mine), unload(), late)
    assert view["late"] and lines(view)["date"][0] == "warn" and "до 16:00" in lines(view)["date"][1] and not view["blocked"]
    assert not order_supplier.view(conn, orders.get_card(conn, mine), unload(), "2026-10-23T15:59:00")["late"]
    assert order_supplier.tomorrow(conn, mine, late, "owner") == "2026-10-24"
    assert orders.get_card(conn, mine)["delivery_date"] == "2026-10-24"
    assert any("Дата доставки" in e["text"] for e in orders.list_events(conn, mine))


def test_what_the_placed_stage_lacks_is_said_before(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, client_name="")
    view = order_supplier.view(conn, orders.get_card(conn, mine), unload(), NOW)
    assert lines(view)["stage"][0] == "warn" and "имя клиента" in lines(view)["stage"][1] and not view["blocked"]


def test_nothing_to_order_shows_no_block(tmp_path):
    conn = db(tmp_path)
    for fields in ({"deal_kind": "stock"}, {"webmim_number": "123"}):
        mine = card(conn, **fields)
        assert order_supplier.view(conn, orders.get_card(conn, mine), unload(), NOW) is None


def test_positions_of_webmim_and_4tochki_together_are_ordered_apart(tmp_path):
    conn = db(tmp_path)
    tochki(conn)
    mine = card(conn, goods=(("111", 4),))
    orders.add_item(conn, mine, goods_id="s2-AB12", name="Шина 4т", brand_key="S2:cordiant", season="summer", diameter=16, territory="",
                    qty=4, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="owner")
    view = order_supplier.view(conn, orders.get_card(conn, mine), unload(), NOW)
    assert view["supplier"] == "mixed" and view["blocked"] and "отдельными заказами" in lines(view)["territory"][1]


def tochki_card(conn, **fields):
    tochki(conn)
    mine = card(conn, goods=(), point_id=point(conn, name="Юнона", address="СПб, Юнона", default_time="10:00-17:00"), **fields)
    orders.add_item(conn, mine, goods_id="s2-AB12", name="Шина 4т", brand_key="S2:cordiant", season="summer", diameter=16, territory="",
                    qty=4, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="owner")
    conn.commit()
    return mine


def test_4tochki_gets_the_text_for_its_cabinet(tmp_path):
    conn = db(tmp_path)
    mine = tochki_card(conn)
    view = order_supplier.view(conn, orders.get_card(conn, mine), unload(), NOW)
    assert view["supplier"] == "tochki" and not view["blocked"] and "territory" not in lines(view)
    text = view["tochki_text"]
    for words in ("Заказ панели №%d" % mine, "1. Шина 4т — код AB12 — 4 шт", "Адрес: СПб, Юнона (точка «Юнона»)", "Дата доставки: 24.10.2026",
                  "Время работы клиента: 10:00-17:00", "Контактное лицо: Админ", "Контактный телефон: +7(911)222-33-44",
                  "Примечание для поставщика: Грузим на ОТХ"):
        assert words in text, words


def test_placed_by_hand_in_4tochki_keeps_the_number_and_moves_the_order(tmp_path):
    conn = db(tmp_path)
    mine = tochki_card(conn)
    line, refused = order_supplier.mark_tochki(conn, mine, "  T-777 ", NOW, "owner")
    assert order_supplier.number_of(conn, mine) == "T-777" and not refused
    assert orders.get_card(conn, mine)["stage"]["code"] == "placed" and "«Заказ оформлен»" in line
    assert any("Номер заказа 4 Точек: T-777" in e["text"] for e in orders.list_events(conn, mine))
    assert orders._row(conn, mine)["webmim_id"] == "" and orders._row(conn, mine)["webmim_number"] == ""
    assert order_supplier.view(conn, orders.get_card(conn, mine), unload(), NOW) is None      # ordered: no block any more
    assert order_supplier.mark_tochki(conn, mine, "T-777", NOW, "owner") == ("", "")            # the same again: nothing


def test_the_number_of_4tochki_is_checked(tmp_path):
    conn = db(tmp_path)
    mine = tochki_card(conn)
    for bad in ("", "   ", "x" * 41):
        with pytest.raises(orders.OrderError):
            order_supplier.mark_tochki(conn, mine, bad, NOW, "owner")
    other = card(conn)
    with pytest.raises(orders.OrderError):
        order_supplier.mark_tochki(conn, other, "T-1", NOW, "owner")
    assert order_supplier.number_of(conn, other) == "" and order_supplier.number_of(conn, mine) == ""


# ---- the page and the rights -----------------------------------------------------------------------------------------------------------

def owner_client(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "owner", 0, "tok"
        session["pw"] = shared_mark_of(client.application)
    return client


def test_the_page_shows_the_lines_and_the_window(panel):
    app, conn, fake = panel
    mine = card(conn, delivery_date="2026-10-08")
    html = owner_client(app).get("/orders/%d" % mine).get_data(as_text=True)
    assert "Заказ поставщику" in html and "data-supplier-check" in html and "Заказать в МИМ</button>" in html
    for words in ("Признак оплаты</dt><dd>Отсрочка", "Вид оплаты</dt><dd>безнал", "Контактное лицо</dt><dd>Админ", "Контактный телефон</dt><dd>+7(911)222-33-44",
                  "Примечание для поставщика</dt><dd>Грузим на ОТХ", "Время работы клиента</dt><dd>10:00-18:00", "Подтвердить</button>"):
        assert words in html, words
    assert re.search(r'name="seen" value="[0-9a-f]{16}"', html)
    assert fake.calls == []                                                              # the page asks WebMIM nothing


def test_a_changed_order_after_the_window_is_refused_on_the_page(panel):
    app, conn, fake = panel
    mine = card(conn, delivery_date="2026-10-08")
    client = owner_client(app)
    seen = re.search(r'name="seen" value="([0-9a-f]+)"', client.get("/orders/%d" % mine).get_data(as_text=True)).group(1)
    orders.update_card(conn, mine, {"delivery_time": "после 12"}, NOW, "owner")
    post(client, "/orders/%d/webmim/create" % mine, "tok", seen=seen)
    assert "изменились после открытия окна" in client.get("/orders/%d" % mine).get_data(as_text=True)
    assert not fake.posts() and orders._row(conn, mine)["webmim_id"] == ""


def test_the_page_of_4tochki_and_placed_by_hand(panel):
    app, conn, fake = panel
    mine = tochki_card(conn, delivery_date="2026-10-08")
    client = owner_client(app)
    html = client.get("/orders/%d" % mine).get_data(as_text=True)
    assert "Заказать в 4 Точки</button>" in html and "Оформил вручную</button>" in html and 'data-copy="#tochki-text"' in html
    assert "Заказать в МИМ" not in html
    post(client, "/orders/%d/tochki/placed" % mine, "tok", number="T-9")
    html = client.get("/orders/%d" % mine).get_data(as_text=True)
    assert "Заказ 4 Точек №T-9" in html and order_supplier.number_of(conn, mine) == "T-9"
    assert orders.get_card(conn, mine)["stage"]["code"] == "placed" and fake.calls == []


def test_the_others_see_no_block_and_are_refused(panel):
    app, conn, fake = panel
    mine = tochki_card(conn, delivery_date="2026-10-08")
    mine_point = orders.get_card(conn, mine)["point_id"]
    for role, point_id in (("chats", None), ("viewer", None), ("point", mine_point)):
        client = person(app, conn, role, point_id)
        html = client.get("/orders/%d" % mine).get_data(as_text=True)
        assert "Заказ поставщику" not in html and "tochki/placed" not in html
        assert client.post("/orders/%d/tochki/placed" % mine, data={"csrf": "tok", "number": "X"}).status_code == 403
    assert order_supplier.number_of(conn, mine) == ""


def test_a_lost_answer_is_checked_as_the_press_checks_it(tmp_path):
    """Д5: after a lost answer a past date does not stop the press (it looks for the order first): «Сверка» agrees."""
    conn = db(tmp_path)
    mine = card(conn, delivery_date="2026-10-22")
    conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, started_at, author, webmim_id, result, updated_at) "
                 "VALUES (?, 'test', 'unknown', ?, ?, 'owner', '', '', ?)", (mine, NOW, NOW, NOW))
    conn.commit()
    view = order_supplier.view(conn, orders.get_card(conn, mine), unload(), NOW)
    assert lines(view)["date"][0] != "bad" and not view["blocked"]


def test_one_other_supplier_is_not_called_several(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, goods=(("s5-X1", 4),), brand="S5:x")
    view = order_supplier.view(conn, orders.get_card(conn, mine), unload(), NOW)
    assert view["supplier"] == "other" and view["blocked"] and "разных поставщиков" not in " ".join(t for l in view["lines"] for t in l["texts"])
