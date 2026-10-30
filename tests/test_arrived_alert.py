"""«Приехал, а клиенту не сообщили» (the owner 2026-10-30, the orders 464, 467, 469 moved by hand and 473 refused with a 402): an order on
«Товар на точке» whose client was not told «можно забирать» in this stay is found, told to the responsible and the owner once a day with
the reason and the link, marked in its card with a button that only opens the message; a 402 of «no messenger API» is told the owner once a
day per account. Nothing is ever sent to a client by itself. A fake Avito only."""
from __future__ import annotations

import pytest

from test_arrived_wait import LATER, deliver_by, env, waiting  # noqa: F401  (the fixture)
from test_stuck_sending import NOW, Sender
from tires_sync import chats, order_arrived, order_engine as engine, order_manager as mgr, orders, store

LATE = "2026-10-13T13:00:00"          # an hour after the arrival: past the grace of the sends still running
DAY = "2026-10-14T13:30:00"
NO_API = chats.AvitoError(402, None, "Перейдите на подписку с API мессенджера")


class NoApi(Sender):
    """Avito of the account 2 on 30.10: every send refused with a 402 «Перейдите на подписку с API мессенджера»."""
    def send_message(self, user_id, chat_id, text):
        raise NO_API


def found(conn, now=LATE):
    return {r["card"]["id"]: r["reason"] for r in order_arrived.untold(conn, now)}


def test_moved_by_hand_is_found_with_its_reason(env):
    conn, chat_row, card = env
    assert found(conn, NOW) == {}                                                  # just arrived: not yet
    assert found(conn) == {card: order_arrived.BY_HAND + ", нет чата"}


def test_told_by_itself_by_hand_or_skipped_is_never_found(env):
    conn, chat_row, card = env
    from tires_sync import chat_deals
    chat_deals.link_chat(conn, chat_row, card, NOW, "owner")
    assert mgr.tell_client_arrived(conn, card, deliver_by(conn, Sender()), NOW, "точка")[0]
    assert found(conn) == {}                                                       # sent by itself


def test_a_message_sent_by_hand_or_skipped_ends_it(env):
    conn, chat_row, card = env
    mgr.tell_client_arrived(conn, card, None, NOW, "точка")                       # no chat: it waits
    assert found(conn) == {card: "нет чата"}
    engine.run_task(conn, waiting(conn, card)[0]["id"], LATER, "owner")            # «Отправлено вручную»
    assert found(conn) == {}


def test_a_402_without_the_messenger_api_is_named(env):
    conn, chat_row, card = env
    from tires_sync import chat_deals
    chat_deals.link_chat(conn, chat_row, card, NOW, "owner")
    sent, _note = mgr.tell_client_arrived(conn, card, deliver_by(conn, NoApi()), NOW, "точка")
    assert not sent
    assert found(conn) == {card: order_arrived.NO_API}


def test_the_alert_goes_to_the_responsible_and_the_owner_once_a_day_with_the_link(env, monkeypatch):
    conn, chat_row, card = env
    told = []
    monkeypatch.setenv("TIRES_PUBLIC_BASE", "https://panel.test")
    tell = lambda logins, text: told.append((tuple(logins), text))
    assert order_arrived.check(conn, LATE, tell) == 1
    assert len(told) == 1 and "https://panel.test/orders/%d" % card in told[0][1] and order_arrived.BY_HAND in told[0][1]
    assert set(told[0][0]) >= {"owner"}
    assert any(e["level"] == "yellow" and "не сообщили" in e["text"] for e in orders.list_events(conn, card))
    assert order_arrived.check(conn, "2026-10-13T18:00:00", tell) == 0              # the same day: quiet
    assert order_arrived.check(conn, DAY, tell) == 1                                # a day later, still not told: again
    assert len(told) == 2


def test_nothing_is_sent_to_the_client_by_the_check(env):
    conn, chat_row, card = env
    sender = Sender()
    order_arrived.check(conn, LATE, lambda logins, text: None)
    assert sender.sent == [] and conn.execute("SELECT COUNT(*) FROM chat_send_log").fetchone()[0] == 0


def test_an_order_off_the_point_is_never_found(env):
    conn, chat_row, card = env
    done = conn.execute("SELECT id FROM order_stage WHERE code = 'done'").fetchone()[0]
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (done, card))
    conn.commit()
    assert found(conn) == {}


def test_the_card_button_only_prepares_the_message(env):
    conn, chat_row, card = env
    assert order_arrived.prepare(conn, card, LATE, "owner")
    [task] = waiting(conn, card)
    assert task["status"] == "pending" and "5000777" in task["body"]
    assert conn.execute("SELECT COUNT(*) FROM chat_send_log").fetchone()[0] == 0   # nothing went
    assert order_arrived.prepare(conn, card, LATE, "owner")                        # twice: the same one message
    assert len(waiting(conn, card)) == 1


def test_the_402_of_an_account_is_told_the_owner_once_a_day(env):
    conn, chat_row, card = env
    from tires_sync import chat_deals
    chat_deals.link_chat(conn, chat_row, card, NOW, "owner")
    mgr.tell_client_arrived(conn, card, deliver_by(conn, NoApi()), NOW, "точка")
    said = order_arrived.api_refusals(conn, LATE)
    name = conn.execute("SELECT a.name FROM account a JOIN avito_chat c ON c.account_id = a.id WHERE c.id = ?", (chat_row,)).fetchone()[0]
    assert said == ["%s: Авито не даёт отправлять сообщения (нет подписки API мессенджера)" % name]
    assert order_arrived.api_refusals(conn, "2026-10-13T20:00:00") == []           # the same day: quiet
    assert order_arrived.api_refusals(conn, DAY) == []                             # no new refusal since: nothing


def test_the_card_shows_the_line_and_the_button(tmp_path, monkeypatch):
    from panel_helpers import login, make_app, post
    from tires_sync.panel import routes_orders
    app = make_app(tmp_path)
    owner = app.test_client()
    tok = login(owner)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    card = orders.create_card(conn, dict(client_name="Татьяна", webmim_number="5000777"), NOW, "o")
    at_point = conn.execute("SELECT id FROM order_stage WHERE code = 'at_point'").fetchone()[0]
    conn.execute("UPDATE order_card SET stage_id = ?, entered_at = ? WHERE id = ?", (at_point, NOW, card))
    conn.commit()
    monkeypatch.setattr(routes_orders, "local_iso", lambda: LATE)
    page = owner.get("/orders/%d" % card).get_data(as_text=True)
    assert "Клиенту не сообщили, что шины на точке" in page and "/orders/%d/arrived-message" % card in page
    r = post(owner, "/orders/%d/arrived-message" % card, tok)
    assert r.status_code == 302 and "messages=1" in r.headers["Location"]
    assert conn.execute("SELECT COUNT(*) FROM chat_send_log").fetchone()[0] == 0


def test_a_message_skipped_by_a_person_ends_it(env):
    conn, chat_row, card = env
    mgr.tell_client_arrived(conn, card, None, NOW, "точка")
    engine.skip_task(conn, waiting(conn, card)[0]["id"], LATER, "owner")            # «Пропустить»: the person decided
    assert found(conn) == {}


def test_another_402_is_not_the_messenger_api(env):
    conn, chat_row, card = env
    from tires_sync import chat_deals

    class Other(Sender):
        def send_message(self, user_id, chat_id, text):
            raise chats.AvitoError(402, None, "Слишком много запросов")
    chat_deals.link_chat(conn, chat_row, card, NOW, "owner")
    mgr.tell_client_arrived(conn, card, deliver_by(conn, Other()), NOW, "точка")
    assert found(conn) == {card: order_arrived.REFUSED_402}
    assert order_arrived.api_refusals(conn, LATE) == []
