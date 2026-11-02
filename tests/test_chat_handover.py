"""«Передать клиента» in a point's chat (the owner 2026-11-02, instead of «Передать под заказ» there): no order; the chat is lit «передан с
точки <точка>» in the «Чаты» of the owner and the partner, each gets a task «Взять клиента» (the chat's number, the point, the task leads to
the chat) and a Telegram message; the light goes out with «Взять клиента» in the chat, with an answer in it, or with a closed task (the other
task is cancelled). The calls keep «Передать под заказ»."""
from __future__ import annotations

import pytest

from panel_helpers import post
from test_point_rights import bind, world
from test_roles import person
from tires_sync import chat_handover, clients, order_notify, tasks, users

NOW = "2026-11-02T12:00:00"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


@pytest.fixture
def told(monkeypatch):
    sent = []
    monkeypatch.setattr(order_notify, "tell_people", lambda conn, box, logins, text, **kw: sent.append((list(logins), text)) or "ok")
    return sent


@pytest.fixture
def w(tmp_path, told):
    w = world(tmp_path)
    bind(w)                                                        # the point Альфа works with the chats of its Avito account (Пётр)
    w["manager"], _ = person(w["app"], "point", w["rust"])
    w["partner"], _ = person(w["app"], "partner", login_="zaman")   # Заман: found by the role, never by the login
    w["told"] = told
    return w


def give(w, client=None):
    return (client or w["manager"]).post("/chats/%d/handoff" % w["petr"]["id"], data={"csrf": "tok"})


def open_tasks(conn, chat_row):
    return tasks.of_place(conn, chat_row=chat_row)


def test_the_point_hands_the_client_to_the_owner_and_the_partner_without_an_order(w):
    conn, chat = w["conn"], w["petr"]["id"]
    page = w["manager"].get("/chats/%d" % chat).get_data(as_text=True)
    assert ">Передать клиента</button>" in page and 'action="/chats/%d/handoff"' % chat in page
    assert "Передать под заказ" not in page and "Клиент хочет шины под заказ?" not in page
    orders_before = conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0]
    assert give(w).status_code == 302
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == orders_before                 # no order is made
    assert conn.execute("SELECT COUNT(*) FROM order_handoff").fetchone()[0] == 0
    [h] = chat_handover.open_of(conn).values()
    assert h["point"] == "Альфа"
    found = open_tasks(conn, chat)
    assert sorted(t["assignee"] for t in found) == ["owner", "zaman"]
    for t in found:
        assert t["kind"] == "other" and t["title"] == "Взять клиента: чат Ч-%d, точка «Альфа»" % chat and t["chat_row"] == chat
    [(logins, text)] = w["told"]
    assert logins == ["owner", "zaman"] and "Альфа" in text and "Ч-%d" % chat in text and "/chats/%d" % chat in text
    assert "Передан офису" in w["manager"].get("/chats/%d" % chat).get_data(as_text=True)              # the point sees it went
    give(w)                                                                                               # twice: one handover
    assert len(open_tasks(conn, chat)) == 2 and len(chat_handover.open_of(conn)) == 1 and len(w["told"]) == 1


def test_the_office_sees_the_light_and_takes_the_client(w):
    conn, chat = w["conn"], w["petr"]["id"]
    give(w)
    for who in ("owner", "partner"):
        listing = w[who].get("/chats?acc=all").get_data(as_text=True)
        assert "передан с точки Альфа" in listing and "is-handed" in listing
        assert "Взять клиента" in w[who].get("/chats/%d" % chat).get_data(as_text=True)
        assert "/chats/%d" % chat in w[who].get("/tasks?tab=mine").get_data(as_text=True)               # the task leads to the chat
    assert "Взять клиента</button>" not in w["manager"].get("/chats/%d" % chat).get_data(as_text=True)
    assert w["manager"].post("/chats/%d/take" % chat, data={"csrf": "tok"}).status_code == 403           # the point never takes its own
    assert post(w["partner"], "/chats/%d/take" % chat, "tok").status_code == 302
    assert chat_handover.open_of(conn) == {} and open_tasks(conn, chat) == []
    done = conn.execute("SELECT done_at IS NOT NULL, done_by FROM callback_task WHERE chat_row = ?", (chat,)).fetchall()
    assert done == [(1, "zaman"), (1, "zaman")]
    assert "передан с точки" not in w["owner"].get("/chats?acc=all").get_data(as_text=True)
    assert give(w).status_code == 302 and len(chat_handover.open_of(conn)) == 1                         # taken: may be handed over again


def test_a_closed_task_puts_the_light_out_and_cancels_the_other(w):
    conn, chat = w["conn"], w["petr"]["id"]
    give(w)
    mine = next(t for t in open_tasks(conn, chat) if t["assignee"] == "owner")
    tasks.close(conn, mine["id"], "done", NOW, "owner", "позвонил")
    assert chat_handover.open_of(conn) == {} and open_tasks(conn, chat) == []
    rows = dict(conn.execute("SELECT assignee, CASE WHEN done_at IS NOT NULL THEN 'done' WHEN cancelled_at IS NOT NULL THEN 'cancel' END "
                             "FROM callback_task WHERE chat_row = ?", (chat,)).fetchall())
    assert rows == {"owner": "done", "zaman": "cancel"}
    assert conn.execute("SELECT taken_how, taken_by FROM chat_handover").fetchone() == ("task", "owner")


def test_a_task_closed_already_does_not_stop_the_take(w):
    conn, chat = w["conn"], w["petr"]["id"]
    give(w)
    [first, second] = sorted(t["id"] for t in open_tasks(conn, chat))
    conn.execute("UPDATE callback_task SET cancelled_at = ? WHERE id = ?", (NOW, first))                  # closed some other way
    conn.commit()
    assert chat_handover.take(conn, chat, "owner", NOW) is True
    assert chat_handover.open_of(conn) == {} and open_tasks(conn, chat) == []
    assert chat_handover.take(conn, chat, "owner", NOW) is False


def test_an_answer_of_the_office_in_the_chat_takes_the_client(w, monkeypatch):
    from tires_sync.panel import routes_chats
    conn, chat = w["conn"], w["petr"]["id"]
    give(w)
    with w["app"].test_request_context():
        monkeypatch.setattr(routes_chats, "own_point", lambda: None)
        monkeypatch.setattr(routes_chats, "can", lambda section, level: True)
        monkeypatch.setattr(routes_chats, "author", lambda: "owner")
        routes_chats._took(conn, chat)
    assert chat_handover.open_of(conn) == {}


def test_only_the_point_of_the_chats_account_hands_it_over(w):
    conn, chat = w["conn"], w["petr"]["id"]
    assert "Передать клиента" not in w["owner"].get("/chats/%d" % chat).get_data(as_text=True)
    assert give(w, w["owner"]).status_code == 403                                                        # the office takes, it does not hand over
    other, _ = person(w["app"], "point", w["gamma"], login_="gamma1")
    assert give(w, other).status_code == 403                                                             # a chat of another point's account
    with pytest.raises(chat_handover.HandoverError):
        chat_handover.hand(conn, w["rust"], w["tanya"]["id"], "point1", NOW)                              # Татьяна: the main account's chat
    assert chat_handover.open_of(conn) == {} and conn.execute("SELECT COUNT(*) FROM callback_task").fetchone()[0] == 0


def test_the_partner_is_found_by_the_role_and_an_inactive_one_gets_nothing(w):
    conn = w["conn"]
    assert chat_handover.recipients(conn) == ["owner", "zaman"]
    person(w["app"], "partner", login_="second")
    assert chat_handover.recipients(conn) == ["owner", "second", "zaman"]
    conn.execute("UPDATE panel_user_role SET active = 0 WHERE login = 'second'")
    conn.commit()
    assert chat_handover.recipients(conn) == ["owner", "zaman"]


def test_the_client_of_the_chat_is_tagged_handed_by_the_point(w):
    conn, chat = w["conn"], w["petr"]["id"]
    cid = conn.execute("INSERT INTO client (name, created_at, created_by) VALUES ('Пётр', ?, 'o')", (NOW,)).lastrowid
    conn.execute("INSERT INTO client_chat (chat_row, client_id, how, at) VALUES (?, ?, 'chat', ?)", (chat, cid, NOW))
    conn.commit()
    assert not clients.handed(conn, cid)
    give(w)
    assert clients.handed(conn, cid)
    assert [r["id"] for r in clients.list_clients(conn, tag=clients.HANDED)] == [cid]


def test_a_failed_task_leaves_nothing_half_made(w, monkeypatch):
    conn, chat = w["conn"], w["petr"]["id"]
    real, calls = tasks.add, []

    def second_fails(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise tasks.TaskError("сбой")
        return real(*a, **k)
    monkeypatch.setattr(tasks, "add", second_fails)
    with pytest.raises(tasks.TaskError):
        chat_handover.hand(conn, w["rust"], chat, "point1", NOW)
    assert conn.execute("SELECT COUNT(*) FROM chat_handover").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM callback_task").fetchone()[0] == 0


def test_a_chat_bound_to_another_points_order_is_not_handed_over(w):
    conn, chat = w["conn"], w["petr"]["id"]
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (w["other"], chat))                  # Пётр's chat, an order of Гамма
    conn.commit()
    assert "Передать клиента" not in w["manager"].get("/chats/%d" % chat).get_data(as_text=True)
    assert give(w).status_code == 403 and chat_handover.open_of(conn) == {}


def test_a_new_handover_of_the_same_chat_is_told_again(w):
    give(w)
    post(w["owner"], "/chats/%d/take" % w["petr"]["id"], "tok")
    give(w)
    assert len(w["told"]) == 2 and w["told"][0][1] != w["told"][1][1]                                     # never a repeat of the hour
