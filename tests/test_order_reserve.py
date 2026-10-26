"""The reservation at the point: three days from the arrival of the goods, a date set by hand, the alerts about its end."""
from __future__ import annotations

import pytest

from order_fixtures import free_phone
from tires_sync import order_points as points, order_reserve as reserve, order_templates as tpl, orders, store
from tires_sync.cli import main

PLACED = "2026-10-12T12:00:00"
POINT_VK = "100500"


def stage(conn, code):
    return next(s["id"] for s in orders.list_stages(conn) if s["code"] == code)


def db(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, PLACED)
    return conn


def card_at(conn, code, when, delivery="2026-10-13"):
    pid = next((p["id"] for p in points.list_points(conn) if p["name"] == "Бета"), None) or points.save_point(
        conn, dict(name="Бета", manager_name="Алексей", channel="vk", vk_peer_id=POINT_VK))
    cid = orders.create_card(conn, dict(client_name="Иван", client_phone=free_phone(conn, "+7 900 111 22 33"), point_id=pid, delivery_date=delivery), PLACED, "o")
    conn.execute("UPDATE order_card SET webmim_number = ? WHERE id = ?", (str(5200000 + cid), cid))      # the number the points and the client know
    orders.add_item(conn, cid, goods_id="1", name="Nokian 205/55 R16", brand_key="nokian", season="winter", diameter=16, territory="1",
                    qty=4, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=PLACED, author="o")
    for step in ("placed", "moving", code):
        if step == "moving" and code == "placed":
            break
        orders.move_card(conn, cid, stage(conn, step), when, "o")
        if step == code:
            break
    return cid


def test_the_reserve_runs_three_days_from_the_arrival_not_from_the_order(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    assert orders.get_card(conn, cid)["reserve_until"] == "2026-10-18"
    assert any("срок до 2026-10-18" in e["text"] for e in orders.list_events(conn, cid))


def test_a_date_set_in_advance_is_kept_and_an_old_one_is_replaced(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "moving", "2026-10-13T10:00:00")
    orders.update_card(conn, cid, {"reserve_until": "2026-10-25"}, "2026-10-13T11:00:00", "o")
    orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-14T10:00:00", "o")
    assert orders.get_card(conn, cid)["reserve_until"] == "2026-10-25"
    other = card_at(conn, "moving", "2026-10-13T10:00:00")
    orders.update_card(conn, other, {"reserve_until": "2026-10-01"}, "2026-10-13T11:00:00", "o")      # left from an earlier stay
    orders.move_card(conn, other, stage(conn, "at_point"), "2026-10-14T10:00:00", "o")
    assert orders.get_card(conn, other)["reserve_until"] == "2026-10-17"


def test_other_stages_and_cards_already_at_the_point_are_left_alone(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "moving", "2026-10-13T10:00:00")
    assert orders.get_card(conn, cid)["reserve_until"] == ""
    old = card_at(conn, "at_point", "2026-10-13T10:00:00")                              # a card that was at the point before the change
    conn.execute("UPDATE order_card SET reserve_until = '' WHERE id = ?", (old,))
    conn.commit()
    assert reserve.check(conn, "2026-10-30T12:00:00", lambda card, text: "ok") == 0       # no date, no alert, nothing is made up afterwards
    assert orders.get_card(conn, old)["reserve_until"] == ""


def test_the_date_is_checked_and_changes_are_in_the_feed(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    for bad in ("18.10.2026", "2026-13-01", "1999-01-01"):
        with pytest.raises(orders.OrderError):
            orders.update_card(conn, cid, {"reserve_until": bad}, "2026-10-16T10:00:00", "o")
    orders.update_card(conn, cid, {"reserve_until": "2026-10-20"}, "2026-10-16T10:00:00", "o")
    assert orders.get_card(conn, cid)["reserve_until"] == "2026-10-20"
    assert orders.list_events(conn, cid)[0]["text"] == "Крайняя дата резерва: 2026-10-20"
    orders.update_card(conn, cid, {"reserve_until": ""}, "2026-10-16T11:00:00", "o")
    assert orders.get_card(conn, cid)["reserve_until"] == ""


def test_the_alerts_come_the_day_before_and_on_the_day_once_each(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")                              # until 2026-10-18
    sent = []
    notify = lambda card, text: (sent.append((card["id"], text)), "отправлено")[1]
    assert reserve.check(conn, "2026-10-16T12:00:00", notify) == 0                      # two days ahead
    assert reserve.check(conn, "2026-10-17T08:00:00", notify) == 0                      # before 09:00
    assert reserve.check(conn, "2026-10-17T12:00:00", notify) == 1 and "Завтра (18.10.2026)" in sent[0][1]
    assert reserve.check(conn, "2026-10-17T13:00:00", notify) == 0                      # the hourly tick does not repeat it
    assert reserve.check(conn, "2026-10-18T12:00:00", notify) == 1 and "Сегодня (18.10.2026)" in sent[1][1]
    assert reserve.check(conn, "2026-10-18T13:00:00", notify) == 0
    assert any("[резерв:due:2026-10-18]" in e["text"] for e in orders.list_events(conn, cid))


def test_an_alert_that_was_missed_still_comes_and_an_extension_starts_over(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    sent = []
    notify = lambda card, text: (sent.append(text), "ok")[1]
    assert reserve.check(conn, "2026-10-20T12:00:00", notify) == 1 and "закончился 18.10.2026" in sent[0]       # the tick was down on the 18th
    orders.update_card(conn, cid, {"reserve_until": "2026-10-23"}, "2026-10-20T13:00:00", "o")
    assert reserve.check(conn, "2026-10-22T12:00:00", notify) == 1 and "Завтра (23.10.2026)" in sent[1]


def test_only_cards_at_the_point_and_nothing_without_a_way_to_send(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    assert reserve.check(conn, "2026-10-18T12:00:00", None) == 0                        # not marked as done: it can still be sent later
    assert reserve.check(conn, "2026-10-18T12:00:00", lambda card, text: "ok") == 1
    done = card_at(conn, "at_point", "2026-10-15T10:00:00")
    orders.move_card(conn, done, stage(conn, "done"), "2026-10-16T10:00:00", "o")
    assert reserve.check(conn, "2026-10-18T13:00:00", lambda card, text: "ok") == 0     # taken by the client: no alert
    assert cid != done


def test_a_failing_notify_does_not_stop_the_other_cards(tmp_path):
    conn = db(tmp_path)
    first = card_at(conn, "at_point", "2026-10-15T10:00:00")
    second = card_at(conn, "at_point", "2026-10-15T10:00:00")
    seen = []

    def notify(card, text):
        seen.append(card["id"])
        if card["id"] == first:
            raise RuntimeError("boom")
        return "ok"
    assert reserve.check(conn, "2026-10-18T12:00:00", notify) == 1 and seen == [first, second]
    assert reserve.check(conn, "2026-10-18T13:00:00", lambda card, text: "ok") == 1      # the failed one is tried again, the sent one is not


def test_the_text_for_the_client_counts_from_the_reserve_date_or_the_planned_delivery(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "placed", "2026-10-12T12:00:00", delivery="2026-10-13")
    text, _ = tpl.render(conn, orders.get_card(conn, cid), "{hold_until}")
    assert text == "16.10.2026"                                                         # planned delivery 13th + 3 days, not the day of the order
    orders.update_card(conn, cid, {"reserve_until": "2026-10-25"}, "2026-10-12T13:00:00", "o")
    text, _ = tpl.render(conn, orders.get_card(conn, cid), "{hold_until}")
    assert text == "25.10.2026"                                                         # a date set by hand wins


def test_the_tick_without_a_key_warns_and_still_works(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    conn = store.connect(path)
    orders.ensure_pipeline(conn, PLACED)
    conn.close()
    monkeypatch.delenv("TIRES_SECRET_KEY_FILE", raising=False)
    assert main(["orders-tick", "--db", str(path)]) == 0
    assert "нет ключа" in capsys.readouterr().err


# The last day of the reservation: the client is asked once in the order's Avito chat (the owner 2026-10-23, п. 17), after the point's
# «на точке» (the owner 2026-10-26; the question to the point: test_reserve_ask_point).

OLD_TEXT = ("Добрый день. Срок резерва истекает. Вы отказываетесь от выкупа шин или нужно продлить ожидание? "
            "Готовы перевести предоплату в 10%, чтобы они вас ещё ждали?")


def owner_text(cid=1, until="18.10.2026"):
    return ("Добрый день. Срок хранения вашего заказа №%d истекает %s. Если вы уже забрали заказ — спасибо, не обращайте внимания. "
            "Если нужно продлить ожидание, напишите нам." % (5200000 + cid, until))


OWNER_TEXT = owner_text()


class PointVk:
    """The VK of the community as order_reserve.check gets it (vk_send.sender_for): records the questions to the points."""

    def __init__(self, error=None):
        self.sent, self.error = [], error

    def __call__(self, peer_id, text, random_id=None, keyboard=None):
        if self.error is not None:
            raise self.error
        self.sent.append((peer_id, text))
        return len(self.sent)


def told(conn, at, deliver, notify=None, point_vk=None):
    """The tick of `at` asks the points, and every point asked answers «на точке» at once (order_reserve.here): the way the client hears
    «срок резерва истекает» since 2026-10-26."""
    point_vk = point_vk or PointVk()
    asked = len(point_vk.sent)
    made = reserve.check(conn, at, notify or ok, deliver, point_vk)
    for _peer, text in point_vk.sent[asked:]:
        number = int(text.split("№", 1)[1].split(" ", 1)[0])
        reserve.here(conn, orders.get_card(conn, number - 5200000), "менеджер Бета", at, deliver)
    return made


def chat_of(conn, cid, send=1):
    from tires_sync import connections
    account = connections.create_account(conn, "Основной", PLACED, "o")
    conn.execute("UPDATE account SET chats_enabled = 1, chats_send = ? WHERE id = ?", (send, account))
    row = conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id, last_at) VALUES (?, ?, ?, ?)", (account, "c-%d" % cid, cid, PLACED)).lastrowid
    conn.commit()
    return row


class Deliver:
    def __init__(self, error=None):
        self.sent, self.error = [], error

    def __call__(self, chat_row, text, token):
        if self.error is not None:
            raise self.error
        self.sent.append((chat_row, text, token))
        conn = self.conn
        conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status) VALUES (?, ?, ?, 'система', 'sent')", (token, chat_row, PLACED))


def _deliver(conn, error=None):
    deliver = Deliver(error)
    deliver.conn = conn
    return deliver


def ok(card, text):
    return "ok"


def test_the_client_is_asked_once_on_the_last_day_in_the_working_hours(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")                              # until 2026-10-18
    chat = chat_of(conn, cid)
    deliver, vk = _deliver(conn), PointVk()
    told(conn, "2026-10-17T12:00:00", deliver, point_vk=vk)                             # the day before: only the manager
    assert deliver.sent == [] and vk.sent == []
    assert told(conn, "2026-10-18T09:30:00", deliver, point_vk=vk) == 1 and deliver.sent == [] and vk.sent == []     # the manager at 09:00, the point from 10:00
    told(conn, "2026-10-18T10:05:00", deliver, point_vk=vk)
    assert [(c, t) for c, t, _ in deliver.sent] == [(chat, OWNER_TEXT)]                 # the owner's text word for word
    told(conn, "2026-10-18T11:05:00", deliver, point_vk=vk)
    told(conn, "2026-10-18T19:05:00", deliver, point_vk=vk)
    assert len(deliver.sent) == 1 and len(vk.sent) == 1                                 # the hourly tick asks once, the client hears it once
    events = [e for e in orders.list_events(conn, cid) if "срок резерва истекает" in e["text"]]
    assert len(events) == 1 and events[0]["text"].startswith("Клиенту отправлено «Клиенту: срок резерва истекает»") and events[0]["level"] == "green"


def test_after_an_extension_the_client_is_not_asked_again_in_the_same_stay(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    chat_of(conn, cid)
    deliver = _deliver(conn)
    told(conn, "2026-10-18T12:00:00", deliver)
    orders.update_card(conn, cid, {"reserve_until": "2026-10-21"}, "2026-10-18T13:00:00", "o")
    alerts = []
    told(conn, "2026-10-21T12:00:00", deliver, lambda card, text: (alerts.append(text), "ok")[1])
    assert len(deliver.sent) == 1 and alerts and "Сегодня (21.10.2026)" in alerts[0]   # the manager hears it again, the client does not (the lead 2026-10-24)


def test_a_date_already_past_the_evening_and_other_stages_ask_no_client(tmp_path):
    conn = db(tmp_path)
    late = card_at(conn, "at_point", "2026-10-15T10:00:00")                             # until 2026-10-18
    chat_of(conn, late)
    deliver, vk = _deliver(conn), PointVk()
    told(conn, "2026-10-18T20:30:00", deliver, point_vk=vk)                             # after the working hours (10–20)
    told(conn, "2026-10-19T12:00:00", deliver, point_vk=vk)                             # the day has passed: «истекает» is no longer true
    assert deliver.sent == [] and vk.sent == []
    oth = card_at(conn, "at_point", "2026-10-20T10:00:00")
    chat_of(conn, oth)
    orders.move_card(conn, oth, stage(conn, "oth"), "2026-10-20T11:00:00", "o")
    orders.update_card(conn, oth, {"reserve_until": "2026-10-23"}, "2026-10-20T12:00:00", "o")
    told(conn, "2026-10-23T12:00:00", deliver, point_vk=vk)
    assert deliver.sent == [] and vk.sent == []                                         # «На ОТХ» is no reservation of a client
    reserve.check(conn, "2026-10-23T12:00:00", ok, None)                                # no keys: nothing, and nothing marked


def test_no_chat_is_one_red_record_to_write_by_hand(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    deliver = _deliver(conn)
    told(conn, "2026-10-18T12:00:00", deliver)
    told(conn, "2026-10-18T13:00:00", deliver)
    red = [e for e in orders.list_events(conn, cid) if e["level"] == "red"]
    assert [e["text"].split(" [")[0] for e in red] == [
        "Сообщение клиенту не отправлено («Клиенту: срок резерва истекает»): у заказа нет чата Авито. Напишите клиенту вручную"]


def test_a_send_of_unknown_outcome_is_never_repeated_nor_asked_by_hand(tmp_path):
    from tires_sync import chats
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    chat_of(conn, cid)
    broken = _deliver(conn, chats.ChatSendUnknown("timeout"))
    told(conn, "2026-10-18T12:00:00", broken)
    good = _deliver(conn)
    told(conn, "2026-10-18T13:00:00", good)
    assert good.sent == [] and reserve.here(conn, orders.get_card(conn, cid), "менеджер Бета", "2026-10-18T13:30:00", good)[0] == "уже отмечено"
    assert good.sent == []
    [red] = [e for e in orders.list_events(conn, cid) if e["level"] == "red"]
    assert "неизвестно, ушло ли" in red["text"] and "повторно не отправляйте" in red["text"]


def test_the_owners_template_of_that_name_is_sent_in_place_of_the_sample(tmp_path):
    conn = db(tmp_path)
    tpl.add_template(conn, "Клиенту: срок резерва истекает", "client", "{client_name}, резерв до {hold_until} истекает сегодня.")
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    chat_of(conn, cid)
    deliver = _deliver(conn)
    told(conn, "2026-10-18T12:00:00", deliver)
    assert [t for _, t, _ in deliver.sent] == ["Иван, резерв до 18.10.2026 истекает сегодня."]


def test_the_sample_is_offered_among_the_templates(tmp_path):
    conn = db(tmp_path)
    tpl.add_samples(conn)
    assert any(t["name"] == "Клиенту: срок резерва истекает" and t["body"] == tpl.RESERVE_SAMPLE[2] for t in tpl.list_templates(conn))
    assert "предоплат" not in tpl.RESERVE_SAMPLE[2] and "{webmim_number}" in tpl.RESERVE_SAMPLE[2] and "{hold_until}" in tpl.RESERVE_SAMPLE[2]


def _inbox(conn):
    return [r[0] for r in conn.execute("SELECT text FROM manager_message WHERE status = 'new' ORDER BY id")]


def test_a_reserve_message_that_did_not_go_is_a_line_of_the_inbox_with_its_text(tmp_path):
    """Хвост З17: as «можно забирать»: the line of «Входящие» says what failed and carries the text to send by hand; once per stay."""
    from tires_sync import order_manager
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")                              # no chat of the order
    deliver = _deliver(conn)
    told(conn, "2026-10-18T12:00:00", deliver)
    told(conn, "2026-10-18T13:00:00", deliver)
    [line] = _inbox(conn)
    assert line.startswith("Заказ №%d: клиенту не отправлено «Клиенту: срок резерва истекает» — у заказа нет чата Авито. Отправьте вручную." % (5200000 + cid))
    assert line.endswith(order_manager.COPY_MARK + OWNER_TEXT)


def test_a_reserve_message_of_unknown_outcome_is_a_line_without_the_text_and_a_sent_one_is_none(tmp_path):
    from tires_sync import chats, order_manager
    conn = db(tmp_path)
    unknown = card_at(conn, "at_point", "2026-10-15T10:00:00")
    chat_of(conn, unknown)
    told(conn, "2026-10-18T12:00:00", _deliver(conn, chats.ChatSendUnknown("timeout")))
    [line] = _inbox(conn)
    assert "повторно не отправляйте" in line and order_manager.COPY_MARK not in line
    sent = card_at(conn, "at_point", "2026-10-16T10:00:00")
    chat_of(conn, sent)
    told(conn, "2026-10-19T12:00:00", _deliver(conn))
    assert len(_inbox(conn)) == 1


class AvitoSender:
    """The writing client of the chats (chats.send_reply): records what went."""

    def __init__(self):
        self.sent = []

    def get_self(self, retry=True):
        return {"id": 1}

    def send_message(self, user_id, chat_id, text):
        self.sent.append(text)
        return {"id": "m-out-%d" % len(self.sent), "created": 1792300000 + len(self.sent), "direction": "out", "type": "text", "content": {"text": text}}


def test_the_reserve_message_is_one_record_of_the_journal_not_two(tmp_path):
    """Хвост З18: the send of «Срок резерва истекает» through the chat is the green record of the reserve only (no «Отправлен ответ клиенту»
    as well); an answer by hand in the chat keeps its own record."""
    from tires_sync import chats
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    chat = chat_of(conn, cid)
    avito = AvitoSender()
    told(conn, "2026-10-18T12:00:00", lambda row, text, token: chats.send_reply(conn, avito, row, text, token, "2026-10-18T12:00:00", "система"))
    assert avito.sent == [OWNER_TEXT]
    sent = [e for e in orders.list_events(conn, cid) if e["kind"] in ("chat", "reserve") and ("отправлен" in e["text"].lower())
            and not e["text"].startswith("Точке «Бета» отправлен вопрос")]                 # the question to the point is a record of its own
    assert [e["text"].split(" [")[0] for e in sent] == ["Клиенту отправлено «Клиенту: срок резерва истекает»"] and sent[0]["level"] == "green"
    chats.send_reply(conn, avito, chat, "Продлим до пятницы", "reply-1", "2026-10-18T12:30:00", "owner")
    assert orders.list_events(conn, cid)[0]["text"] == "Отправлен ответ клиенту: Продлим до пятницы"


def _arrived_sent(conn, cid, at):
    """«Можно забирать» told to the client the way the point's «приехал» does it (order_manager.tell_client_arrived), through the chat."""
    from tires_sync import chats, order_manager
    avito = AvitoSender()
    tpl.add_template(conn, order_manager.ARRIVED_TEMPLATE, "client", "{client_name}, шины приехали, можно забирать.")   # the owner's text
    sent, _note = order_manager.tell_client_arrived(conn, cid, lambda row, text, token: chats.send_reply(conn, avito, row, text, token, at, "система"),
                                                    at, "точка")
    assert sent and len(avito.sent) == 1


def test_no_reserve_message_on_the_day_the_client_was_told_the_goods_came(tmp_path):
    """Хвост З17 (the owner 2026-10-25): the reservation ends the day the goods came: «можно забирать» went today, «Срок резерва истекает»
    does not go as well (a grey record, once); the manager is alerted as before."""
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-18T09:00:00")
    orders.update_card(conn, cid, {"reserve_until": "2026-10-18"}, "2026-10-18T09:05:00", "o")
    chat_of(conn, cid)
    _arrived_sent(conn, cid, "2026-10-18T09:01:00")
    deliver, alerts, vk = _deliver(conn), [], PointVk()
    told(conn, "2026-10-18T12:00:00", deliver, lambda card, text: (alerts.append(text), "ok")[1], vk)
    told(conn, "2026-10-18T13:00:00", deliver, point_vk=vk)
    assert deliver.sent == [] and vk.sent == [] and alerts and "Сегодня (18.10.2026)" in alerts[0]     # nobody is asked either
    [grey] = [e for e in orders.list_events(conn, cid) if "уже ушло «можно забирать»" in e["text"]]
    assert grey["level"] == "grey" and _inbox(conn) == []


def test_the_reserve_message_goes_when_the_goods_came_on_an_earlier_day(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")                              # until 2026-10-18
    chat_of(conn, cid)
    _arrived_sent(conn, cid, "2026-10-15T10:01:00")
    deliver = _deliver(conn)
    told(conn, "2026-10-18T12:00:00", deliver)
    assert [t for _c, t, _tok in deliver.sent] == [OWNER_TEXT]
