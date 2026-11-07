"""«Ответственный в чатах всегда по умолчанию тот, чей аккаунт Авито» (the owner 2026-11-07, lane chat-resp): a chat gets the account's
«Ответственный за заказы этого аккаунта» (connections.account_responsible) — a new chat at once, a chat without one when it comes alive again
(new messages); never one a person chose (or took away) in the panel, never a chat of a point's account, nobody when the account has nobody or
that person may not answer chats. No mass rewrite of the base: a quiet old chat stays as it is."""
from __future__ import annotations

import json

import pytest

from test_chats_sync import NOW, TS, FakeClient, chat, db, msg
from tires_sync import chats, connections, store, users

LATER = "2026-10-13T13:00:00"


@pytest.fixture
def env(tmp_path):
    conn, account = db(tmp_path)
    for login, role in (("ivan", "chats"), ("petr", "partner"), ("vera", "viewer")):
        users.add_user(conn, login, "password-123", NOW, role=role)
    conn.commit()
    return conn, account


def responsible_of(conn, account, login):
    connections.set_responsible(conn, account, login, [login], NOW, "o")


def sync(conn, account, *items, now=NOW, messages=None):
    messages = messages or {i["id"]: [msg("m-%s-%d" % (i["id"], i["updated"]), "Здравствуйте", created=i["updated"])] for i in items}
    return chats.sync_account(conn, FakeClient([list(items)], messages), account, now)


def who(conn, chat_id="c1"):
    return conn.execute("SELECT responsible FROM avito_chat WHERE chat_id = ?", (chat_id,)).fetchone()[0]


def row(conn, chat_id="c1"):
    return conn.execute("SELECT id FROM avito_chat WHERE chat_id = ?", (chat_id,)).fetchone()[0]


def test_a_new_chat_gets_the_account_s_responsible(env):
    conn, account = env
    responsible_of(conn, account, "ivan")
    sync(conn, account, chat("c1"), chat("c2"))
    assert who(conn, "c1") == "ivan" and who(conn, "c2") == "ivan"
    details = [json.loads(r[0]) for r in conn.execute("SELECT detail FROM audit WHERE action = ? ORDER BY id", (chats.ASSIGN_DEFAULT,))]
    assert details == [{"chat": row(conn, c), "account": account, "to": "ivan"} for c in ("c1", "c2")]      # one per chat, with the write
    from tires_sync.panel import texts
    assert conn.execute("SELECT author FROM audit WHERE action = ?", (chats.ASSIGN_DEFAULT,)).fetchone()[0] in store.SYSTEM_AUTHORS   # «Системные»
    assert texts.audit_text({"action": chats.ASSIGN_DEFAULT, "detail": details[0]}) == \
        "Чат Ч-%d: ответственный по умолчанию — ivan (ответственный аккаунта №%d)" % (row(conn, "c1"), account)


def test_no_responsible_of_the_account_as_before(env):
    conn, account = env
    sync(conn, account, chat("c1"))
    assert who(conn) == ""
    assert not conn.execute("SELECT 1 FROM audit WHERE action = ?", (chats.ASSIGN_DEFAULT,)).fetchone()


def test_a_person_who_may_not_answer_chats_is_not_given_one(env):
    conn, account = env
    responsible_of(conn, account, "vera")                                         # a viewer: «Ответственный» of the orders, never of a chat
    sync(conn, account, chat("c1"))
    assert who(conn) == ""


def test_the_shared_owner_is_given_one(env):
    conn, account = env
    connections.set_responsible(conn, account, "owner", ["owner"], NOW, "o")    # the panel's shared owner: no row of panel_user
    sync(conn, account, chat("c1"))
    assert who(conn) == "owner"


def test_a_person_s_choice_is_never_overwritten(env):
    conn, account = env
    responsible_of(conn, account, "ivan")
    sync(conn, account, chat("c1"))
    chats.set_responsible(conn, row(conn), "petr", NOW, "o")                       # «Отвечает» in the chat
    sync(conn, account, chat("c1", updated=TS + 600, text="Ещё вопрос"), now=LATER)
    assert who(conn) == "petr"
    chats.set_responsible(conn, row(conn), "", NOW, "o")                           # «никто»: the person's too
    sync(conn, account, chat("c1", updated=TS + 1200, text="И ещё"), now=LATER)
    assert who(conn) == ""


def test_an_old_chat_without_one_gets_it_when_it_comes_alive_and_only_then(env):
    conn, account = env
    sync(conn, account, chat("c1"), chat("c2"))                                   # before the account had a responsible
    responsible_of(conn, account, "ivan")
    sync(conn, account, chat("c1"), chat("c2"), now=LATER)                         # nothing new: no mass rewrite
    assert who(conn, "c1") == "" and who(conn, "c2") == ""
    sync(conn, account, chat("c1", updated=TS + 600, text="Ещё вопрос"), chat("c2"), now=LATER)
    assert who(conn, "c1") == "ivan" and who(conn, "c2") == ""


def test_a_chat_with_one_keeps_it_when_the_account_changes_hands(env):
    conn, account = env
    responsible_of(conn, account, "ivan")
    sync(conn, account, chat("c1"))
    responsible_of(conn, account, "petr")
    sync(conn, account, chat("c1", updated=TS + 600, text="Ещё вопрос"), chat("c2"), now=LATER)
    assert who(conn, "c1") == "ivan" and who(conn, "c2") == "petr"


def test_a_chat_of_a_point_s_account_is_left_to_the_point(env):
    conn, account = env
    responsible_of(conn, account, "ivan")
    conn.execute("INSERT INTO point (name, avito_account_id) VALUES ('Альфа', ?)", (account,))
    conn.commit()
    sync(conn, account, chat("c1"))
    assert who(conn) == ""


def test_a_responsible_given_by_the_system_is_no_taking_from_a_point(env):
    """order_points.office_taken: a chat the owner or the partner answers for is a client taken from a point — only when a person chose it;
    the default is not (else «Клиент написал» of the orders the points work would come back to them, the owner 2026-11-03)."""
    from tires_sync import order_points
    conn, account = env
    responsible_of(conn, account, "petr")                                         # a partner
    sync(conn, account, chat("c1"))
    assert who(conn) == "petr" and row(conn) not in order_points.office_taken(conn)[1]
    chats.set_responsible(conn, row(conn), "ivan", NOW, "o")                       # a person's choices since: as before
    chats.set_responsible(conn, row(conn), "petr", NOW, "o")
    assert row(conn) in order_points.office_taken(conn)[1]


def test_a_chat_come_alive_by_the_webhook_gets_it_too(env):
    conn, account = env
    sync(conn, account, chat("c1"))
    responsible_of(conn, account, "ivan")
    assert chats.store_pushed(conn, account, "c1", msg("w-1", "Ещё вопрос", created=TS + 600), LATER) == "stored"
    conn.commit()
    assert who(conn) == "ivan"
    sync(conn, account, chat("c1", updated=TS + 600, text="Ещё вопрос"), now=LATER, messages={"c1": [msg("w-1", "Ещё вопрос", created=TS + 600)]})
    assert who(conn) == "ivan" and conn.execute("SELECT COUNT(*) FROM audit WHERE action = ?", (chats.ASSIGN_DEFAULT,)).fetchone()[0] == 1


def test_the_webhook_never_overwrites_a_person_nor_gives_a_point_s_chat(env):
    conn, account = env
    sync(conn, account, chat("c1"))
    responsible_of(conn, account, "ivan")
    chats.set_responsible(conn, row(conn), "petr", NOW, "o")
    chats.set_responsible(conn, row(conn), "", NOW, "o")                           # «никто», chosen
    chats.store_pushed(conn, account, "c1", msg("w-1", "Ещё вопрос", created=TS + 600), LATER)
    conn.commit()
    assert who(conn) == ""
    conn.execute("INSERT INTO point (name, avito_account_id) VALUES ('Альфа', ?)", (account,))
    conn.commit()
    sync(conn, account, chat("c2"), now=LATER)
    chats.store_pushed(conn, account, "c2", msg("w-2", "Вопрос", created=TS + 700), LATER)
    conn.commit()
    assert who(conn, "c2") == ""


def test_a_broken_record_never_hides_a_person_s_taking(env):
    from tires_sync import order_points
    conn, account = env
    sync(conn, account, chat("c1"))
    conn.execute("UPDATE avito_chat SET responsible = 'petr' WHERE id = ?", (row(conn),))      # set before the journal had such records
    store.write_audit(conn, NOW, "system", chats.ASSIGN_DEFAULT, {"account": account})          # no «chat»: NULL in NOT IN would hide every taking
    assert row(conn) in order_points.office_taken(conn)[1]


def test_the_journal_shows_it_in_moscow_time(tmp_path):
    from panel_helpers import make_app
    app = make_app(tmp_path)
    shown = app.jinja_env.globals.get("audit_dt") or app.jinja_env.filters.get("audit_dt")
    assert shown({"action": chats.ASSIGN_DEFAULT, "at": "2026-10-13T12:00:00", "author": "system"}) == \
        shown({"action": "chat_assign", "at": "2026-10-13T12:00:00", "author": "o"})
