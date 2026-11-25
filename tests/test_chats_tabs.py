"""The tabs of «Чаты» (ТЗ №93, the owner 2026-11-25): «Все» (by default), «Открытые», «Непрочитанные» (its number), «С заказом»,
«Без заказа», «Закрытые»; no «Не требуют ответа». Every chat is in «Все»; «Открытые» + «Закрытые» and «С заказом» + «Без заказа» are
«Все» without a loss; «Всего» and the number of «Непрочитанные» are the rows listed; the old addresses and saved choices open «Все»."""
from __future__ import annotations

import re

import pytest

from test_audit_rest import _menu_chats, _new_view
from test_chats_pages import _names, _say, setup
from test_chat_deals import Fake
from test_point_rights import bind, world
from test_roles import person
from tires_sync import chats, orders, store, user_prefs, users
from tires_sync.panel import page_prefs
from tires_sync.panel.routes_chats import FILTERS, OLD_FILTERS, filter_key, in_filter

NOW = "2026-10-13T12:00:00"
ALL = ["Иван", "Борис", "Ольга", "Пётр", "Анна", "Татьяна"]        # the list's order: the last message first


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def six(tmp_path):
    """Татьяна: open, waits, an order; Иван: open, answered, no order; Ольга: «не требует ответа», her message; Пётр: closed, an order;
    Анна: closed, no order, no time from Avito (created_ts 0: NOT NULL in the base); Борис: open, his order being made (order_id = -1), created_ts 0."""
    app, client, tok, conn, account = setup(tmp_path)
    fake = Fake()
    for chat_id, name in (("c3", "Ольга"), ("c4", "Пётр"), ("c5", "Анна"), ("c6", "Борис")):
        fake.add(chat_id, name, [("out", "Добрый день!")], item=300)
    chats.sync_account(conn, fake, account, NOW)
    by = {c["peer_name"]: c for c in chats.list_chats(conn)}
    for row in by.values():
        conn.execute("DELETE FROM avito_message WHERE chat_row = ?", (row["id"],))
    times = {"Татьяна": "11:00", "Анна": "11:05", "Пётр": "11:10", "Ольга": "11:15", "Борис": "11:20", "Иван": "11:25"}
    for name, at in times.items():
        _say(conn, by[name]["id"], "2026-10-13T%s:00" % at, "out" if name in ("Иван", "Борис", "Пётр", "Анна") else "in")
    card = orders.create_card(conn, {"client_name": "Пётр"}, NOW, "o")
    conn.execute("UPDATE avito_chat SET status = 'no_reply' WHERE id = ?", (by["Ольга"]["id"],))
    conn.execute("UPDATE avito_chat SET status = 'closed', order_id = ? WHERE id = ?", (card, by["Пётр"]["id"]))
    conn.execute("UPDATE avito_chat SET status = 'closed', order_id = NULL, created_ts = 0 WHERE id = ?", (by["Анна"]["id"],))
    conn.execute("UPDATE avito_chat SET order_id = -1, created_ts = 0 WHERE id = ?", (by["Борис"]["id"],))
    conn.execute("UPDATE avito_chat SET order_id = NULL WHERE id = ?", (by["Иван"]["id"],))
    store.set_state(conn, "chats_baseline_%d" % account, "2026-10-13T09:00:00")
    conn.commit()
    assert by["Татьяна"]["order_id"] and chats.waiting(conn, chats.list_chats(conn)) == {by["Татьяна"]["id"]}
    return app, client, tok, conn, account, by


EXPECTED = {"": ALL, "open": ["Иван", "Борис", "Ольга", "Татьяна"], "wait": ["Татьяна"], "deal": ["Пётр", "Татьяна"],
            "nodeal": ["Иван", "Борис", "Ольга", "Анна"], "closed": ["Пётр", "Анна"]}


def test_the_tabs_are_the_owners_six_with_all_first_and_by_default():
    assert list(FILTERS.items()) == [("", "Все"), ("open", "Открытые"), ("wait", "Непрочитанные"), ("deal", "С заказом"),
                                     ("nodeal", "Без заказа"), ("closed", "Закрытые")]
    assert "Не требуют ответа" not in FILTERS.values() and filter_key("noreply") == "" and filter_key("junk") == "" and filter_key(None) == ""
    assert page_prefs.RENAMED["chats_page"]["f"] == OLD_FILTERS                     # one list of the old tabs for the page and the saved choice


def test_each_tab_lists_its_chats_and_all_is_every_chat_without_a_loss(tmp_path):
    app, client, tok, conn, account, by = six(tmp_path)
    for flt, names in EXPECTED.items():
        page = client.get("/chats" + ("?f=" + flt if flt else "")).get_data(as_text=True)
        assert _names(page) == names, flt
        assert "Всего: %d" % len(names) in page, flt                              # the count is the list
    assert sorted(EXPECTED["open"] + EXPECTED["closed"]) == sorted(ALL)           # each chat in exactly one of the two
    assert sorted(EXPECTED["deal"] + EXPECTED["nodeal"]) == sorted(ALL)
    assert len(chats.list_chats(conn)) == len(ALL)                                # «Все» loses none (created_ts 0 too)


def test_in_filter_splits_every_chat_in_two_by_status_and_by_order():
    for status in ("open", "closed", "no_reply"):
        for order_id in (None, -1, 0, 7):
            chat = {"id": 1, "status": status, "order_id": order_id}
            assert in_filter("", chat, set())
            assert in_filter("open", chat, set()) != in_filter("closed", chat, set())
            assert in_filter("deal", chat, set()) != in_filter("nodeal", chat, set())
            assert in_filter("deal", chat, set()) == (order_id == 7)
    assert in_filter("wait", {"id": 1, "status": "open", "order_id": None}, {1}) and not in_filter("wait", {"id": 2, "status": "open", "order_id": None}, {1})


@pytest.mark.parametrize("new", [False, True])
def test_unread_shows_its_number_in_both_views_and_it_is_the_menus(tmp_path, new):
    app, client, tok, conn, account, by = six(tmp_path)
    if new:
        _new_view(conn)
    page = client.get("/chats").get_data(as_text=True)
    assert "Непрочитанные <b>1</b>" in page and _menu_chats(page) == 1
    assert "Всего: 1" in client.get("/chats?f=wait").get_data(as_text=True)
    assert "Не требуют ответа" not in page and ">Ждут ответа<" not in page
    _say(conn, by["Татьяна"]["id"], "2026-10-13T11:50:00", "out")                 # answered: nobody waits, no number
    conn.commit()
    page = client.get("/chats").get_data(as_text=True)
    assert "Непрочитанные <b>" not in page and _menu_chats(page) == 0


def test_the_search_keeps_the_tab(tmp_path):
    app, client, tok, conn, account, by = six(tmp_path)
    assert _names(client.get("/chats?q=Пётр").get_data(as_text=True)) == ["Пётр"]               # «Все»: a closed chat too
    assert _names(client.get("/chats?f=open&q=Пётр").get_data(as_text=True)) == []
    assert _names(client.get("/chats?f=closed&q=Пётр").get_data(as_text=True)) == ["Пётр"]


def test_an_old_address_of_no_reply_opens_all(tmp_path):
    app, client, tok, conn, account, by = six(tmp_path)
    page = client.get("/chats?f=noreply").get_data(as_text=True)
    assert _names(page) == ALL and "Состояние: Все" in page
    assert re.search(r'id="chats-live"[^>]* data-place=""', page)                    # one place of the list with «Все»
    assert _names(client.get("/chats?f=wait").get_data(as_text=True)) == ["Татьяна"]   # «Ждут ответа»'s address is «Непрочитанные»


def test_an_old_saved_choice_moves_to_the_new_tabs(tmp_path):
    app, client, tok, conn, account, by = six(tmp_path)
    user_prefs.put(conn, "owner", "f.chats_page", {"f": ["noreply"]}, NOW)
    conn.commit()
    r = client.get("/chats", headers={"Referer": "http://localhost/orders"})        # from the menu: «Не требуют ответа» is «Все», no redirect
    assert r.status_code == 200 and _names(r.get_data(as_text=True)) == ALL and "Фильтр включён" not in r.get_data(as_text=True)
    assert user_prefs.get(conn, "owner", "f.chats_page") == {"f": ["noreply"]}      # nothing rewritten
    user_prefs.put(conn, "owner", "f.chats_page", {"f": ["wait"]}, NOW)               # «Ждут ответа» saved: «Непрочитанные»
    conn.commit()
    r = client.get("/chats", headers={"Referer": "http://localhost/orders"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/chats?f=wait")
    user_prefs.put(conn, "owner", "f.chats_page", {"f": ["noreply"], "acc": [str(account)]}, NOW)     # the rest of the choice stays
    conn.commit()
    r = client.get("/chats", headers={"Referer": "http://localhost/orders"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/chats?acc=%d" % account)
    client.get("/chats?f=closed", headers={"Referer": "http://localhost/chats"})       # a tab picked on the page: kept as before
    assert user_prefs.get(conn, "owner", "f.chats_page") == {"f": ["closed"]}
    client.get("/chats?f=noreply", headers={"Referer": "http://localhost/chats"})      # an old tab from the page: nothing to keep
    assert user_prefs.get(conn, "owner", "f.chats_page") is None


@pytest.mark.parametrize("role", ["owner", "partner", "chats"])
def test_the_office_roles_see_every_tab(tmp_path, role):
    w = world(tmp_path)
    client = w["owner"] if role == "owner" else person(w["app"], role)[0]
    for flt in FILTERS:
        r = client.get("/chats" + ("?f=" + flt if flt else ""))
        assert r.status_code == 200, (role, flt)
        assert ("Состояние: " + FILTERS[flt]) in r.get_data(as_text=True) or "o2-chip is-on" in r.get_data(as_text=True)


def test_a_point_sees_its_own_chats_in_every_tab_and_never_anothers(tmp_path):
    w = world(tmp_path)
    bind(w)
    manager, _ = person(w["app"], "point", w["rust"])
    page = manager.get("/chats").get_data(as_text=True)
    assert "Состояние: Все" in page and "Пётр" in page and "Татьяна" not in page
    for flt in FILTERS:
        listed = manager.get("/chats" + ("?f=" + flt if flt else "")).get_data(as_text=True)
        assert "Татьяна" not in listed.split('class="card chats-list"')[1].split("</section>")[0], flt
    assert "Пётр" in manager.get("/chats?f=nodeal").get_data(as_text=True)
