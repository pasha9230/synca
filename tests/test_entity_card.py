"""The one look of the cards (owner, 2026-10-21: «Единая карточка (заказ, клиент, позиция, аккаунт) — делаем»): the data of a card is a dict
built in Python (ready to be JSON for Ant Design later), the templates only lay it out (_entity.html). The page of a position is new."""
from __future__ import annotations

import json
import re

import pytest

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app, post
from tires_sync import clients, connections, order_points as points, orders, store, users

T = "2026-10-07T10:00:00"
ROWS = (
    commodity_xml(NNOMMODIF="1", SMNFCODE="C1", NREST="12"),
    commodity_xml(NNOMMODIF="3", SMNFCODE="C3", SDIAMETR="13", NREST="9"),
)


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def setup(tmp_path):
    app = make_app(tmp_path)
    owner = app.test_client()
    tok = login(owner)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    return app, owner, tok, store.connect(app.config["DB_PATH"])


def as_role(app, conn, role, point_id=None):
    users.add_user(conn, role + "1", "длинный-пароль-1", T, role=role, point_id=point_id)
    c = app.test_client()
    with c.session_transaction() as session:
        session.update(user=role + "1", epoch=store.get_session_epoch(conn, role + "1"), csrf="tok")
    return c


def new_order(owner, tok, **extra):
    r = post(owner, "/orders/new", tok, client_name="Иван", client_phone="89001112233", item_goods_id=["1"], item_qty=["4"], **extra)
    assert r.status_code == 302, r.get_data(as_text=True)[:300]
    return int(re.search(r"/orders/(\d+)$", r.headers["Location"]).group(1))


def publish(conn, account_id, goods_id, ad_id, price):
    conn.execute("INSERT INTO published_state (account_id, goods_id, price, base_price, point, snapshot) VALUES (?, ?, ?, 0, 'Бета', ?)",
                 (account_id, goods_id, price, "<Ad><Id>%s</Id><Title>t</Title></Ad>" % ad_id))
    conn.commit()


def test_the_card_dicts_are_json_and_hold_no_secret(tmp_path):
    app, owner, tok, conn = setup(tmp_path)
    cid = new_order(owner, tok)
    card = orders.get_card(conn, cid)
    view = orders.card_view(card)
    assert json.loads(json.dumps(view)) == view
    assert view["type"] == "order" and view["ref"] == "№%d" % cid and view["title"] == "Иван" and view["status"]["text"] == card["stage"]["name"]
    client = clients.get_client(conn, card["client"]["id"])
    cv = clients.card_view(conn, client, {}, None, True)
    assert json.loads(json.dumps(cv)) == cv and "Куплено на" not in json.dumps(cv, ensure_ascii=False)
    assert cv["orders"][0]["ref"] == {"text": "№%d" % cid, "href": "/orders/%d" % cid}
    account = connections.create_account(conn, "Основной", T, "o")
    av = connections.card_view(conn, connections.get_account(conn, account))
    text = json.dumps(av, ensure_ascii=False)
    assert av["type"] == "account" and connections.get_account(conn, account)["feed_token"] not in text


def test_the_order_card_keeps_its_window_and_actions_in_the_one_head(tmp_path):
    app, owner, tok, conn = setup(tmp_path)
    cid = new_order(owner, tok)
    page = owner.get("/orders/%d" % cid).get_data(as_text=True)
    head = page[page.index('class="entity-head owin-top"'):page.index('class="owin-stages"')]
    assert "Заказ №%d" % cid in head and 'id="ow-title"' in head and ">Иван</h1>" in head
    assert "entity-actions" in head and "Ещё ⋯" in head and "Перевести в «" in head      # the next stage and the menu stay in the head
    assert 'form id="ow-main"' in page or 'id="ow-main"' in page
    assert '<div class="crumbs"><a href="/orders">Заказы</a>' in page
    for part in ("Продажа и расчёты", "Лента событий", "Позиции", "Доставка и WebMIM"):
        assert part in page
    r = post(owner, "/orders/%d/note" % cid, tok, text="позвонить завтра")       # an action of the card still works
    assert r.status_code == 302 and "позвонить завтра" in owner.get("/orders/%d" % cid).get_data(as_text=True)
    new = owner.get("/orders/new").get_data(as_text=True)
    assert ">Новый заказ</h1>" in new and "entity-head owin-top" in new and "entity-eyebrow" not in new
    assert 'aria-labelledby="ow-title-kind ow-title"' in page and 'id="ow-title-kind">Заказ №%d<' % cid in page


def test_the_client_card_has_fields_lists_and_its_events(tmp_path):
    app, owner, tok, conn = setup(tmp_path)
    cid = new_order(owner, tok)
    second = new_order(owner, tok)
    client_id = orders.get_card(conn, cid)["client"]["id"]
    assert orders.get_card(conn, second)["client"]["id"] == client_id             # the same phone: one client
    post(owner, "/clients/%d/save" % client_id, tok, name="Иван Петров", note="")
    page = owner.get("/clients/%d" % client_id).get_data(as_text=True)
    assert "Клиент №%d" % client_id in page and ">Иван Петров</h1>" in page
    assert "Телефоны" in page and "+7 900 111-22-33" in page and "Куплено на" in page   # the owner has «money»
    assert 'href="/orders/%d"' % cid in page and "Отделить" in page
    assert "События" in page and "Клиент заведён" in page and "Заказ №%d создан" % second in page and "Имя, комментарий или автомобиль изменены" in page
    r = post(owner, "/clients/%d/split-order/%d" % (client_id, second), tok)
    new_id = int(r.headers["Location"].rsplit("/", 1)[1])
    assert "Отделён заказ №%d: теперь клиент №%d" % (second, new_id) in owner.get("/clients/%d" % client_id).get_data(as_text=True)
    assert "Отделён от клиента №%d" % client_id in owner.get("/clients/%d" % new_id).get_data(as_text=True)
    viewer = as_role(app, conn, "viewer")
    seen = viewer.get("/clients/%d" % client_id).get_data(as_text=True)
    assert "Куплено на" not in seen and "Отделить" not in seen and "Телефоны" in seen


def test_the_account_card_head_and_who_opens_it(tmp_path):
    app, owner, tok, conn = setup(tmp_path)
    account = connections.create_account(conn, "Основной", T, "o")
    page = owner.get("/connections/avito/%d" % account).get_data(as_text=True)
    assert "Аккаунт Авито №%d" % account in page and "Публикация выключена" in page and "Внутреннее название" in page
    assert "своя сборка" in page and "появится после «Проверить подключение»" in page
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"})
    assert as_role(app, conn, "point", pid).get("/connections/avito/%d" % account).status_code == 403
    assert as_role(app, conn, "viewer").get("/connections/avito/%d" % account).status_code == 403


def test_the_page_of_a_position(tmp_path):
    app, owner, tok, conn = setup(tmp_path)
    a = connections.create_account(conn, "A", T, "owner")
    publish(conn, a, "1", "C1", 4810)
    cid = new_order(owner, tok)
    page = owner.get("/catalog/item/1").get_data(as_text=True)
    assert '<div class="crumbs"><a href="/catalog">Каталог</a>' in page and "Позиция C1" in page and "в фиде" in page
    assert "Остаток" in page and "12 шт." in page and "Цена продажи" in page and "4 810 ₽" in page
    assert "Закупка (за 1 шт.)" in page                                            # the owner has «money»
    assert "Объявления по аккаунтам" in page and ">C1<" in page and "Бета" in page
    assert "Заказы с этой позицией" in page and 'href="/orders/%d"' % cid in page and "Иван" in page
    unbuilt = owner.get("/catalog/item/3").get_data(as_text=True)
    assert "Объявление не собирается" in unbuilt and "Ни в одном опубликованном фиде" in unbuilt
    assert owner.get("/catalog/item/999").status_code == 404
    assert app.test_client().get("/catalog/item/1").status_code == 302                 # signed out: to the login
    preview = owner.get("/catalog/preview/1").get_data(as_text=True)
    assert 'href="/catalog/item/1"' in preview and "Открыть карточку" in preview


def test_the_page_of_a_position_by_rights(tmp_path):
    app, owner, tok, conn = setup(tmp_path)
    new_order(owner, tok)
    for role in ("viewer", "chats"):                                              # the catalog without «money»: no purchase price
        page = as_role(app, conn, role).get("/catalog/item/1").get_data(as_text=True)
        assert "Цена продажи" in page and "Закупка" not in page and "Заказы с этой позицией" in page
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"})
    assert as_role(app, conn, "point", pid).get("/catalog/item/1").status_code == 403          # a point has no catalog
    assert as_role(app, conn, "partner").get("/catalog/item/1").status_code == 200


def test_the_client_events_are_in_moscow_time_and_newest_first(tmp_path):
    """The journal (audit) is written in UTC, the orders in Moscow time: the events show both in Moscow time, in their real order."""
    app, owner, tok, conn = setup(tmp_path)
    cid = new_order(owner, tok)                                                  # the panel clock: 12:00 UTC, the order 15:00 Moscow
    client_id = orders.get_card(conn, cid)["client"]["id"]
    post(owner, "/clients/%d/save" % client_id, tok, name="Иван Петров", note="")   # 12:00:00 UTC = 15:00:00 Moscow, the same second as the order
    events = clients.client_events(conn, clients.get_client(conn, client_id))
    assert [e["text"] for e in events][:2] == ["Имя, комментарий или автомобиль изменены", "Заказ №%d создан (сейчас: %s)" % (cid, orders.get_card(conn, cid)["stage"]["name"])]
    assert events[0]["at"] == events[1]["at"] == "2026-10-07T15:00:00"
    page = owner.get("/clients/%d" % client_id).get_data(as_text=True)
    assert 'action="/clients/%d/split-order/%d"' % (client_id, cid) not in page     # one order only: nothing to split off


def test_the_position_page_hides_the_orders_from_who_cannot_open_them(tmp_path):
    app, owner, tok, conn = setup(tmp_path)
    new_order(owner, tok)
    from datetime import date
    from tires_sync.panel import service
    from tires_sync.panel.routes_main import context
    with app.test_request_context():
        _, settings, parsed, ref = context(app)
        hidden = service.catalog_item_view(conn, settings, parsed, ref, "1", date(2026, 10, 7), money=False, orders=False)
        shown = service.catalog_item_view(conn, settings, parsed, ref, "1", date(2026, 10, 7), money=True, orders=True)
    assert hidden["orders"] is None and "Закупка" not in json.dumps(hidden, ensure_ascii=False)
    assert len(shown["orders"]) == 1 and any(f["label"].startswith("Закупка") for f in shown["fields"])
    conn.execute("INSERT INTO published_state (account_id, goods_id, price, base_price, point, snapshot) VALUES (1, '1', 1, 0, 'x', 'not xml')")
    conn.commit()
    with app.test_request_context():
        _, settings, parsed, ref = context(app)
        broken = service.catalog_item_view(conn, settings, parsed, ref, "1", date(2026, 10, 7), money=False, orders=False)
    assert broken["ads"][0]["ad_id"]["text"] == ""                                # a broken snapshot: the row without the Id
