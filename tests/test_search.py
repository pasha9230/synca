"""The one search of the top bar (the owner 2026-10-21: «Единый поиск в верхней полосе — делаем»): the orders, the clients, the chats and the
positions in one answer, five a group with a link to the page of the section. The rights are the main thing: a section the role has not is
never searched; the manager of a point finds the orders of the own point and the chats of the point's account only — never another point's
order by its number, the phone, the name or the text of a chat; without «money» there is no purchase price in the answer."""
from __future__ import annotations

import os
import pathlib

import pytest

from helpers import catalog_xml, commodity_xml

from test_point_rights import bind, world
from test_roles import person
from tires_sync import clients, orders, search, users
from tires_sync.panel import routes_search


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def find(client, q):
    response = client.get("/search.json", query_string={"q": q})
    assert response.status_code == 200
    return {g["key"]: g for g in response.get_json()["groups"]}


def titles(groups, key):
    return [i["title"] for i in groups.get(key, {"items": []})["items"]]


@pytest.fixture
def w(tmp_path):
    w = world(tmp_path)
    conn = w["conn"]
    orders.update_card(conn, w["other"], {"client_phone": "+7 900 000-00-11", "webmim_number": "7700001"}, "2026-10-13T12:00:00", "o")
    orders.update_card(conn, w["mine"], {"client_phone": "+7 900 111-22-33", "webmim_number": "5000100"}, "2026-10-13T12:00:00", "o")
    for card in (w["mine"], w["other"]):
        clients.attach_order(conn, card, "2026-10-13T12:00:00", "o")
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (w["other"], w["petr"]["id"]))     # Пётр's chat (Альфа's account) is of Гамма's order
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text) VALUES (?, 'old', '2026-06-01T10:00:00', 0, 'in', "
                 "'text', 'Давнишний вопрос про диски')", (w["tanya"]["id"],))
    conn.commit()
    return w


# --- the query ----------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("q, kind, value", [
    ("Ч-2346", "chat", 2346), ("ч-12", "chat", 12), ("Ч 7", "chat", 7),
    ("№123", "order", 123), ("#123", "order", 123), ("заказ 123", "order", 123), ("Заказ №123", "order", 123),
    ("123", "number", 123), ("5000100", "number", 5000100),
    ("+7 (900) 000-00-11", "phone", "9000000011"), ("89000000011", "phone", "9000000011"), ("9000000011", "phone", "9000000011"),
    ("Иван", "text", None), ("205/55 R16", "text", None),
])
def test_the_query_is_read_as_a_number_a_chat_a_phone_or_words(q, kind, value):
    p = search.parse(q)
    assert p["kind"] == kind
    if kind in ("chat", "order", "number"):
        assert p["number"] == value
    if kind == "phone":
        assert p["phone"] == value


# --- the rights: one filter ---------------------------------------------------------------------------------------------------------

def test_the_scope_of_every_role():
    full = {"orders", "clients", "chats", "positions"}
    assert search.scope_for("owner") == search.Scope(frozenset(full), money=True)
    assert search.scope_for("partner") == search.Scope(frozenset(full), money=True)
    assert search.scope_for("chats") == search.Scope(frozenset(full), money=False)
    assert search.scope_for("viewer") == search.Scope(frozenset(full), money=False)
    assert search.scope_for("point", 3, 9) == search.Scope(frozenset({"orders", "chats"}), point=3, account=9, money=False)
    assert search.scope_for("point", 3, None) == search.Scope(frozenset({"orders"}), point=3, money=False)      # no account: no chat
    assert search.scope_for("point", None, 9).groups == frozenset()                                             # no point: nothing
    assert search.scope_for("nobody").groups == frozenset()


def test_the_point_never_searches_the_clients_nor_the_positions_even_with_those_sections(monkeypatch):
    """The clients and the positions are not split by point: were the role ever given those sections, the search still keeps out of them."""
    wider = dict(users.MATRIX["point"], sections=dict(users.MATRIX["point"]["sections"], clients=users.VIEW, catalog=users.VIEW))
    monkeypatch.setitem(users.MATRIX, "point", wider)
    assert search.scope_for("point", 3, 9).groups == frozenset({"orders", "chats"})


def test_every_role_opens_the_search_and_sees_the_field(w):
    bind(w)
    people = {"owner": w["owner"]}
    for role in ("partner", "chats", "viewer"):
        people[role], _ = person(w["app"], role)
    people["point"], _ = person(w["app"], "point", w["rust"])
    for role, client in people.items():
        assert client.get("/search?q=Свой").status_code == 200, role
        page = client.get("/orders").get_data(as_text=True)
        assert 'id="topsearch"' in page and "/static/topbar.js" in page, role
    keys = {role: set(find(client, "клиент")) for role, client in people.items()}
    assert keys["owner"] == keys["partner"] == keys["chats"] == keys["viewer"] == {"orders", "clients"}
    assert keys["point"] == {"orders"}


def test_the_point_finds_its_own_order_never_another_points(w):
    bind(w)
    manager, _ = person(w["app"], "point", w["rust"])
    mine, other = "Заказ №%d" % w["mine"], "Заказ №%d" % w["other"]
    for q in ("Свой", "свой клиент", "№%d" % w["mine"], str(w["mine"]), "+7 900 111-22-33", "89001112233", "9001112233", "5000100"):
        assert [t for t in titles(find(manager, q), "orders") if t.startswith(mine)], q
    for q in ("Чужой", "чужой клиент", "№%d" % w["other"], "заказ %d" % w["other"], str(w["other"]), "+7 900 000-00-11", "89000000011",
              "9000000011", "7700001", "клиент"):
        groups = find(manager, q)
        assert not [t for t in titles(groups, "orders") if t.startswith(other)], q
        assert "Чужой" not in str(groups), q


def test_the_owner_finds_any_order_by_the_number_the_phone_in_three_forms_webmim_and_the_name(w):
    other = "Заказ №%d" % w["other"]
    for q in ("Чужой", "№%d" % w["other"], "#%d" % w["other"], "заказ %d" % w["other"], str(w["other"]), "+7 (900) 000-00-11", "89000000011",
              "9000000011", "7700001"):
        assert [t for t in titles(find(w["owner"], q), "orders") if t.startswith(other)], q


def test_the_point_finds_the_chats_of_its_account_only_and_never_through_another_points_order(w):
    bind(w)
    manager, _ = person(w["app"], "point", w["rust"])
    petr, tanya = "Ч-%d" % w["petr"]["id"], "Ч-%d" % w["tanya"]["id"]
    assert titles(find(manager, "Пётр"), "chats") == [petr + " · Пётр"]
    assert titles(find(manager, "зимние"), "chats") == [petr + " · Пётр"]                           # the text of its own chat
    assert titles(find(manager, petr), "chats") == [petr + " · Пётр"]
    for q in ("Татьяна", "актуально", tanya, "№%d" % w["other"], "+7 900 000-00-11"):
        assert titles(find(manager, q), "chats") == [], q                                         # another account; another point's order
    assert petr + " · Пётр" in titles(find(w["owner"], "№%d" % w["other"]), "chats")                  # the owner: the chat of the order
    assert tanya + " · Татьяна" in titles(find(w["owner"], tanya), "chats")


def test_the_text_of_the_messages_is_searched_for_three_months_the_name_for_ever(w):
    tanya = "Ч-%d · Татьяна" % w["tanya"]["id"]
    assert titles(find(w["owner"], "давнишний"), "chats") == []
    assert titles(find(w["owner"], "актуально"), "chats") == [tanya]
    assert titles(find(w["owner"], "татьяна"), "chats") == [tanya]


def test_a_section_the_role_has_not_is_never_searched(w, monkeypatch):
    bind(w)
    manager, _ = person(w["app"], "point", w["rust"])

    def never(*args, **kwargs):
        raise AssertionError("searched a section the role has not")
    monkeypatch.setattr(search.clients, "list_clients", never)
    monkeypatch.setattr(routes_search, "context", never)
    assert set(find(manager, "клиент")) == {"orders"}
    assert set(find(manager, "laufenn")) <= {"orders", "chats"}
    assert manager.get("/search?q=laufenn").status_code == 200


def test_the_clients_are_found_in_any_case_and_by_the_phone(w):
    other = "Чужой клиент"
    for q in ("чужой", "ЧУЖОЙ", "+7 900 000-00-11", "89000000011"):
        assert other in titles(find(w["owner"], q), "clients"), q


def test_the_purchase_price_only_with_the_right_money(w):
    owner = find(w["owner"], "laufenn")["positions"]["items"]
    assert owner and "закупка 5 000 ₽" in owner[0]["subtitle"] and owner[0]["url"].startswith("/catalog?q=")
    for role in ("chats", "viewer"):
        client, _ = person(w["app"], role)
        response = client.get("/search.json", query_string={"q": "laufenn"})
        items = {g["key"]: g for g in response.get_json()["groups"]}["positions"]["items"]
        text = response.get_data(as_text=True)
        assert items and "закуп" not in text and "5000" not in text and "5 000" not in text, role
        assert "закуп" not in client.get("/search?q=laufenn").get_data(as_text=True), role


def test_five_a_group_and_a_link_to_the_section_with_the_same_query(w):
    conn = w["conn"]
    for n in range(7):
        orders.create_card(conn, {"client_name": "Многоразовый %d" % n}, "2026-10-13T12:00:00", "o")
    conn.commit()
    group = find(w["owner"], "многоразовый")["orders"]
    assert len(group["items"]) == 5 and group["more"] is True
    assert group["all_url"] == "/orders?q=%D0%BC%D0%BD%D0%BE%D0%B3%D0%BE%D1%80%D0%B0%D0%B7%D0%BE%D0%B2%D1%8B%D0%B9"
    assert all(i["url"].startswith("/orders/") for i in group["items"])
    page = w["owner"].get("/search?q=многоразовый").get_data(as_text=True)
    assert all("Многоразовый %d" % n in page for n in range(7))                      # the page of Enter: more than five


def test_a_short_or_empty_query_finds_nothing(w):
    for q in ("", " ", "а"):
        assert w["owner"].get("/search.json", query_string={"q": q}).get_json()["groups"] == []


def test_a_new_unload_is_searched_anew(w):
    """The search keeps the fields of the positions while the unload is the same: a new unload is never answered from the old one."""
    assert find(w["owner"], "laufenn").get("positions")
    unload = pathlib.Path(w["app"].config["UNLOAD_PATH"])
    unload.write_bytes(catalog_xml(commodity_xml(NNOMMODIF="100"), commodity_xml(NNOMMODIF="102", SMARKA="Nokian", SMODIFNAME="NOKIAN 205/55/16 Hakka",
                                                                                 SMODIFNAME_ORIG="Nokian 205/55/16 Hakka", SMODEL="Hakka")))   # the same position
    stat = unload.stat()
    os.utime(unload, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))
    assert "positions" not in find(w["owner"], "laufenn")
    assert titles(find(w["owner"], "hakka"), "positions") == ["Nokian 205/55/16 Hakka"]
