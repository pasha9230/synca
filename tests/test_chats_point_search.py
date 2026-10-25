"""The search of the page «Чаты» (/chats?q=) keeps the points apart as the one search does: the manager of a point never finds a chat of the
point's account through another point's order — its panel number, its WebMIM number or its phone; the own order finds its chat. The owner and
the manager of the chats find the chat of any order, as before."""
from __future__ import annotations

import pytest

from test_point_rights import bind, world
from test_roles import person
from tires_sync import orders, users


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


@pytest.fixture
def w(tmp_path):
    w = world(tmp_path)
    conn = w["conn"]
    orders.update_card(conn, w["other"], {"client_phone": "+7 900 000-00-11", "webmim_number": "7700001"}, "2026-10-13T12:00:00", "o")
    orders.update_card(conn, w["mine"], {"client_phone": "+7 900 111-22-33", "webmim_number": "5000100"}, "2026-10-13T12:00:00", "o")
    conn.commit()
    bind(w)
    return w


def listed(client, q, chat) -> bool:
    response = client.get("/chats", query_string={"q": q})
    assert response.status_code == 200
    return 'href="/chats/%d' % chat["id"] in response.get_data(as_text=True)


def bound_to(w, card):
    w["conn"].execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (card, w["petr"]["id"]))     # Пётр's chat (Альфа's account)
    w["conn"].commit()


ANOTHER_POINTS = {"number": ("№{other}",), "webmim": ("7700001",), "phone": ("+7 900 000-00-11", "89000000011", "9000000011")}


def another_points_queries(w):
    return [q.format(other=w["other"]) for qs in ANOTHER_POINTS.values() for q in qs]


@pytest.mark.parametrize("kind", sorted(ANOTHER_POINTS))
def test_the_point_finds_no_chat_through_another_points_order(w, kind):
    bound_to(w, w["other"])
    manager, _ = person(w["app"], "point", w["rust"])
    assert listed(manager, "Пётр", w["petr"])                                                # the chat itself is the point's
    for q in ANOTHER_POINTS[kind]:
        assert not listed(manager, q.format(other=w["other"]), w["petr"]), q


def test_the_point_finds_the_chat_through_its_own_order(w):
    bound_to(w, w["mine"])
    manager, _ = person(w["app"], "point", w["rust"])
    for q in ("№%d" % w["mine"], "5000100", "+7 900 111-22-33", "89001112233", "9001112233"):
        assert listed(manager, q, w["petr"]), q


@pytest.mark.parametrize("role", ["owner", "chats"])
def test_the_owner_and_the_manager_of_the_chats_find_the_chat_of_any_order(w, role):
    bound_to(w, w["other"])
    client = w["owner"] if role == "owner" else person(w["app"], role)[0]
    for q in another_points_queries(w):
        assert listed(client, q, w["petr"]), q


def test_the_point_has_no_account_button_and_its_list_keeps_one_place(w):
    """Run catalog-chats (5)-(6): the account of a point is fixed, so no «Аккаунт» button (its «Все аккаунты» did nothing); the key of the
    list's place is the one of its list (the server puts the point's account in), from the menu and after a click on a chat alike."""
    import re
    manager, _ = person(w["app"], "point", w["rust"])
    place = lambda page: re.search(r'id="chats-live"[^>]* data-place="([^"]*)"', page).group(1)
    page = manager.get("/chats").get_data(as_text=True)
    assert "Состояние: Открытые" in page and "Аккаунт:" not in page
    assert place(page) == "?acc=%d" % w["rust_acc"]
    assert place(manager.get("/chats/%d?acc=%d" % (w["petr"]["id"], w["rust_acc"])).get_data(as_text=True)) == place(page)
    assert "Аккаунт: Все аккаунты" in w["owner"].get("/chats").get_data(as_text=True)              # the owner keeps the button
