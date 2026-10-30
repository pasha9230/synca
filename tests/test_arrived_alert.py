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


# ---- the owner 2026-10-30 evening: a move by hand sends «можно забирать» as «приехал» -------------------------------------------------------

def _panel_order(tmp_path, code="moving"):
    from panel_helpers import login, make_app
    app = make_app(tmp_path)
    owner = app.test_client()
    tok = login(owner)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    from tires_sync import order_points as points
    pid = points.save_point(conn, dict(name="Бета", manager_name="А", channel="vk", vk_peer_id="555"))
    card = orders.create_card(conn, dict(client_name="Татьяна", client_phone="+7 900 111-22-33", webmim_number="5000777", point_id=pid,
                                         delivery_date="2026-10-14"), NOW, "o")
    orders.add_item(conn, card, goods_id="1", name="Nokian 205/55 R16", brand_key="nokian", season="winter", diameter=16, territory="1", qty=4,
                    sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    stage = conn.execute("SELECT id FROM order_stage WHERE code = ?", (code,)).fetchone()[0]
    conn.execute("UPDATE order_card SET stage_id = ?, entered_at = ? WHERE id = ?", (stage, NOW, card))
    conn.commit()
    return app, owner, tok, conn, card


def _stage(conn, code):
    return conn.execute("SELECT id FROM order_stage WHERE code = ?", (code,)).fetchone()[0]


def test_a_move_by_hand_to_the_point_tells_the_client_as_arrived(tmp_path, monkeypatch):
    """Was (the lead's default of 30.10 morning): a move by hand sent nothing — the orders 464, 467, 469. Now (the owner, «впредь надо делать
    это автоматически»): as the point's «приехал», once per arrival."""
    from panel_helpers import post
    told = []
    monkeypatch.setattr(mgr, "tell_client_arrived", lambda conn, card_id, deliver, now, who, body=None: (told.append(card_id), (True, "Клиенту отправлено."))[1])
    app, owner, tok, conn, card = _panel_order(tmp_path)
    post(owner, "/orders/%d/move" % card, tok, stage_id=str(_stage(conn, "at_point")))
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point" and told == [card]
    post(owner, "/orders/%d/move" % card, tok, stage_id=str(_stage(conn, "at_point")))      # already there: nothing more
    assert told == [card]


def test_a_move_back_from_oth_without_a_new_client_tells_nobody_but_a_new_client_is_told(tmp_path, monkeypatch):
    told = []
    monkeypatch.setattr(mgr, "tell_client_arrived", lambda conn, card_id, deliver, now, who, body=None: (told.append(card_id), (True, ""))[1])
    app, owner, tok, conn, card = _panel_order(tmp_path, "oth")
    before = orders.get_card(conn, card)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (_stage(conn, "at_point"), card))
    conn.commit()
    assert mgr.arrived_by_move(conn, before, None, LATE, "o") == "" and told == []
    conn.execute("UPDATE order_card SET client_phone = '+7 900 999-88-77', client_name = 'Пётр' WHERE id = ?", (card,))
    conn.commit()
    mgr.arrived_by_move(conn, before, None, LATE, "o")
    assert told == [card]


def test_the_chat_page_move_tells_the_client_too(tmp_path, monkeypatch):
    from panel_helpers import post
    told = []
    monkeypatch.setattr(mgr, "tell_client_arrived", lambda conn, card_id, deliver, now, who, body=None: (told.append(card_id), (True, ""))[1])
    app, owner, tok, conn, card = _panel_order(tmp_path)
    conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id) VALUES (1, 'c9', ?)", (card,))
    conn.commit()
    row = conn.execute("SELECT id FROM avito_chat WHERE chat_id = 'c9'").fetchone()[0]
    post(owner, "/chats/%d/stage" % row, tok, stage_id=str(_stage(conn, "at_point")))
    assert told == [card]


# ---- the review of 2026-10-30 -----------------------------------------------------------------------------------------------------------------

def test_an_answer_of_ours_in_the_chat_or_to_the_syncs_line_ends_it(env):
    conn, chat_row, card = env
    from tires_sync import chat_deals
    chat_deals.link_chat(conn, chat_row, card, NOW, "owner")
    assert found(conn)
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status) VALUES ('reply-1', ?, ?, 'zaman', 'sent')", (chat_row, LATER))
    conn.commit()
    assert found(conn) == {}                                                       # a free answer in the order's chat: told
    conn.execute("DELETE FROM chat_send_log")
    token = mgr.arrived_token(orders.get_card(conn, card), LATE)
    conn.execute("INSERT INTO manager_message (message_id, at, sender, text, status, answer) VALUES (?, ?, 'система', 'x', 'answered', 'написал сам')",
                 ("send-%s-1" % token, LATER))
    conn.commit()
    assert found(conn) == {}                                                       # the sync's line after the 402s, answered by a person


def test_told_another_way_ends_it_and_sends_nothing(env):
    conn, chat_row, card = env
    assert order_arrived.mark_told(conn, card, LATE, "owner")
    assert found(conn) == {} and conn.execute("SELECT COUNT(*) FROM chat_send_log").fetchone()[0] == 0


def test_an_order_of_a_points_account_is_told_the_point_only(env, monkeypatch):
    conn, chat_row, card = env
    from tires_sync import order_points
    monkeypatch.setattr(order_points, "of_account", lambda c, order_id: {"id": 1})
    staff, point = [], []
    order_arrived.check(conn, LATE, lambda logins, text: staff.append(text), lambda c, text: point.append(text))
    assert staff == [] and len(point) == 1 and "Напишите клиенту" in point[0]


def test_only_the_staff_and_the_owner_are_told(env):
    conn, chat_row, card = env
    from tires_sync import users
    from tires_sync import order_points as points
    pid = points.save_point(conn, dict(name="Бета", manager_name="А", channel="vk", vk_peer_id="555"))
    users.add_user(conn, "beta", "длинный-пароль-1", NOW, role="point", point_id=pid)
    conn.execute("UPDATE order_card SET responsible = 'beta' WHERE id = ?", (card,))
    conn.commit()
    told = []
    order_arrived.check(conn, LATE, lambda logins, text: told.append(tuple(logins)))
    assert told == [("owner",)]                                                    # a point's login, «система»: never


def test_no_messenger_api_asks_to_write_in_the_avito_app_and_offers_no_send(env):
    assert order_arrived.advice(order_arrived.NO_API).startswith("Напишите клиенту в приложении Авито")
    assert not order_arrived.can_send(order_arrived.NO_API) and not order_arrived.can_send(order_arrived.UNKNOWN)
    assert order_arrived.can_send(order_arrived.BY_HAND)


def test_the_button_never_makes_a_second_message_beside_a_rules(env):
    conn, chat_row, card = env
    conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, arg, stage_id, status, title, body, result, created_at) "
                 "VALUES (?, 99, ?, 'message', '', 0, 'pending', ?, 'x', '', ?)", (card, NOW, mgr.ARRIVED_TEMPLATE, LATER))
    conn.commit()
    order_arrived.prepare(conn, card, LATE, "owner")
    assert waiting(conn, card) == []
