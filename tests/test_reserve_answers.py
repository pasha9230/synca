"""The owner 2026-11-12 (ТЗ, the answers to the reminder of the reservation). After the reminder of 12:30 of the last day the panel waits 8
hours (12 before) and reads the client's messages of these hours by three lists of words kept in the settings of the orders:
«Забрал» (the stage stays, a task to the responsible, a line to the point on its «Рабочее место», no move by itself), «Продлить» (+2 days,
once per order, nobody told, the move at 20:30 of the new last day), «Отказ» (to «На ОТХ» at once; also the staff's one word «Отказ»).
Words of two outcomes in one message, any other answer and an answer after the 8 hours do nothing: at 20:30 the order goes to «На ОТХ».
Nothing goes to VK. No real send: the transports are fakes, the data is made up."""
from __future__ import annotations

import re

import pytest

from panel_helpers import post
from test_order_reserve import PointVk, card_at, db
from test_point_new_view import flag, w  # noqa: F401  (the fixture)
from test_reserve_date import Notify, said, tick, told, until
from test_reserve_noon import DAY, NEXT, code, read_chats
from test_vk_commands import ready as panel_ready
from tires_sync import order_points as points, order_reserve as reserve, orders, store, vk_send
from tires_sync.cli import main
from tires_sync.panel import MOSCOW_AUDIT

NUMBER = "5200001"                                     # the WebMIM number of the first order (test_order_reserve.card_at)
END = DAY + "T20:30:00"                                # 12:30 + 8 hours
TASK = "Клиент утверждает, что забрал заказ №%s. Проверьте, выдала ли точка, и поменяйте статус заказа" % NUMBER
NOTE = ("Клиент пишет, что забрал заказ №%s, Nokian 205/55 R16, 4 шт.. Подтвердите: он точно купил и приехал, или шины остались на точке? "
        "Отметьте корректный статус в панели" % NUMBER)


@pytest.fixture(autouse=True)
def no_vk(monkeypatch):
    """Nothing of these rules goes to VK: any send through the community fails the test."""
    def refuse(*_a, **_k):
        raise AssertionError("в ВК ничего не уходит")
    monkeypatch.setattr(vk_send, "send_message", refuse, raising=False)
    monkeypatch.setattr(vk_send, "sender_for", refuse)


def tasks_of(conn, cid):
    return [r[0] for r in conn.execute("SELECT title FROM callback_task WHERE order_id = ? ORDER BY id", (cid,))]


def events(conn, cid, part):
    return [e for e in orders.list_events(conn, cid) if part in e["text"]]


def alerts(notify):
    """What the responsible was told, without the alert of the end of the reservation that was there before («Сегодня заканчивается резерв…»)."""
    return [text for _card, text in notify.sent if "заканчивается резерв" not in text and "закончился" not in text]


def run(conn, when, deliver, notify):
    """A tick with a VK sender that records: the tests assert it stayed empty."""
    deliver.at = when
    vk = PointVk()
    reserve.check(conn, when, notify, deliver, vk)
    assert vk.sent == []
    return vk


# ---- 1. the wait is 8 hours -----------------------------------------------------------------------------------------------------------

def test_the_wait_is_eight_hours_and_silence_moves_the_order_to_oth_at_20_30(tmp_path):
    conn, cid, _chat, deliver, notify = told(tmp_path)
    assert reserve.NO_ANSWER.total_seconds() == 8 * 3600
    run(conn, DAY + "T20:29:59", deliver, notify)
    assert code(conn, cid) == "at_point"
    run(conn, END, deliver, notify)
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "oth" and card["cancel_reason"] == reserve.NO_ANSWER_REASON
    [record] = events(conn, cid, "заказ переведён на «На ОТХ»")
    assert record["text"].startswith("За 8 часов после напоминания о сроке резерва клиент не написал") and record["author"] == "система"
    assert len(events(conn, cid, "Позвонить клиенту")) == 1                             # as before, when the client is silent
    run(conn, NEXT + "T10:07:00", deliver, notify)
    assert len(events(conn, cid, "заказ переведён на «На ОТХ»")) == 1


# ---- 2. the words ---------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, outcome", [
    ("забрал", "took"), ("Забрала вчера", "took"), ("мы ЗАБРАЛИ, спасибо", "took"), ("уже получил", "took"), ("Купил.", "took"), ("взял", "took"),
    ("Продлить", "extend"), ("продлите пожалуйста", "extend"), ("Завтра заберу", "extend"),
    ("отказ", "refuse"), ("Отказываюсь", "refuse"), ("уже не нужно", "refuse"), ("не буду брать", "refuse"), ("передумал", "refuse"),
    ("уже купил", ""), ("купил, продлите", ""), ("ок", ""), ("", ""), ("завтра", ""), ("забралась", ""), ("нужно подумать", ""),
])
def test_the_first_lists_are_the_owners_and_read_whole_words_in_any_case(text, outcome):
    assert reserve.WORDS_DEFAULT == {"took": "забрал, забрала, забрали, получил, купил, взял", "extend": "продлить, продлите, завтра заберу",
                                     "refuse": "отказ, отказываюсь, не нужно, не буду, передумал, уже купил"}
    assert reserve.client_outcome(text, reserve.WORDS_DEFAULT) == outcome


def test_words_of_two_outcomes_are_no_answer_and_the_rule_is_one_function():
    assert reserve.found_outcomes("уже купил", reserve.WORDS_DEFAULT) == {"took", "refuse"}
    assert reserve.decide({"took", "refuse"}) == "" and reserve.decide({"refuse"}) == "refuse" and reserve.decide(set()) == ""


def test_two_outcomes_in_one_message_do_nothing_and_the_wait_goes_on(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T13:00:00", "Уже купил в другом месте")
    run(conn, DAY + "T13:07:00", deliver, notify)
    assert code(conn, cid) == "at_point" and until(conn, cid) == DAY and tasks_of(conn, cid) == [] and alerts(notify) == []
    assert not events(conn, cid, "ответил на напоминание")
    run(conn, END, deliver, notify)
    assert code(conn, cid) == "oth"


def test_the_lists_are_kept_in_the_settings_and_the_change_is_in_the_journal(tmp_path):
    conn = db(tmp_path)
    assert reserve.words(conn) == reserve.WORDS_DEFAULT
    kept = reserve.set_words(conn, " Вывез ,, вывез, Забрал ", "подождите", "", "2026-11-12T10:00:00", "owner")
    assert kept == {"took": "Вывез, Забрал", "extend": "подождите", "refuse": ""} and reserve.words(conn) == kept
    assert reserve.client_outcome("я ВЫВЕЗ шины", kept) == "took" and reserve.client_outcome("отказ", kept) == ""
    assert conn.execute("SELECT author, action FROM audit ORDER BY id DESC LIMIT 1").fetchone() == ("owner", "reserve_words")
    assert "reserve_words" in MOSCOW_AUDIT


def test_the_settings_page_has_the_three_fields_and_saves_them(tmp_path):
    _app, client, tok, conn, _pid, _card = panel_ready(tmp_path)
    html = client.get("/orders/settings").get_data(as_text=True)
    assert "Ответ клиента на напоминание о резерве" in html
    for key, first in (("took", "забрал, забрала"), ("extend", "продлить, продлите"), ("refuse", "отказ, отказываюсь")):
        assert re.search(r'name="%s"[^>]*value="%s' % (key, first), html)
    post(client, "/orders/settings/reserve-words", tok, took="выкупил", extend="продлить", refuse="отказ")
    assert reserve.words(conn) == {"took": "выкупил", "extend": "продлить", "refuse": "отказ"}
    assert 'value="выкупил"' in client.get("/orders/settings").get_data(as_text=True)


def test_a_message_of_the_client_before_the_reminder_is_not_read(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    from test_order_reserve import chat_of
    from test_reserve_noon import Deliver
    chat, deliver, notify = chat_of(conn, cid), Deliver(conn), Notify()
    said(conn, chat, "2026-10-15T11:00:00", "отказ")                                    # more than 3 days before: the reminder still goes
    said(conn, chat, "2026-10-15T11:01:00", "забрал")
    run(conn, DAY + "T12:30:00", deliver, notify)
    assert len(deliver.sent) == 1
    read_chats(conn, "2099-01-01T00:00:00")
    run(conn, DAY + "T13:07:00", deliver, notify)
    assert code(conn, cid) == "at_point" and tasks_of(conn, cid) == [] and not events(conn, cid, "ответил на напоминание")


# ---- 3. «Забрал» ----------------------------------------------------------------------------------------------------------------------

def test_took_keeps_the_stage_sets_the_task_tells_the_point_and_stops_the_move(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T14:00:00", "Здравствуйте, уже забрал")
    run(conn, DAY + "T14:07:00", deliver, notify)
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "at_point" and card["reserve_until"] == DAY
    assert tasks_of(conn, cid) == [TASK]
    [note] = points.notes(conn, card["point_id"])
    assert note["text"] == NOTE and note["order_id"] == cid and note["kind"] == "alert"
    assert reserve.took_open(conn, card)
    for when in (DAY + "T19:07:00", END, NEXT + "T01:07:00", NEXT + "T10:07:00", NEXT + "T19:07:00"):
        run(conn, when, deliver, notify)                                                # 5 hours later and after: no second task, no move
    assert code(conn, cid) == "at_point" and tasks_of(conn, cid) == [TASK] and len(points.notes(conn, card["point_id"])) == 1
    assert len(deliver.sent) == 1                                                       # the client got the reminder only


def test_after_took_nothing_else_is_read(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T14:00:00", "забрал")
    said(conn, chat, DAY + "T14:05:00", "отказ")
    said(conn, chat, DAY + "T14:06:00", "Отказ", direction="out")
    run(conn, DAY + "T14:07:00", deliver, notify)
    said(conn, chat, DAY + "T15:00:00", "продлите")
    run(conn, DAY + "T15:07:00", deliver, notify)
    assert code(conn, cid) == "at_point" and until(conn, cid) == DAY and tasks_of(conn, cid) == [TASK]


def test_the_label_stands_in_the_points_card_until_the_point_marks_the_order(w):  # noqa: F811
    conn, cid = w["conn"], w["ids"]["here"]
    for state in ("", "all"):                                                           # the old view and the new one
        flag(conn, state)
        assert reserve.TOOK_LABEL not in w["point"].get("/my-point").get_data(as_text=True)
    card = orders.get_card(conn, cid)
    store.set_state(conn, reserve._answer_key(card), '{"took": "2026-10-13T14:00:00"}')
    conn.commit()
    assert reserve.TOOK_LABEL == "Клиент пишет, что забрал заказ, отметьте корректный статус!"
    for state in ("", "all"):
        flag(conn, state)
        html = w["point"].get("/my-point").get_data(as_text=True)
        [label] = re.findall(r'<p class="mp-took"[^>]*>([^<]*)</p>', html)
        assert label == reserve.TOOK_LABEL and 'style="font-size:16px' in html and 'data-client-took="%d"' % cid in html
    done = next(s["id"] for s in orders.list_stages(conn) if s["code"] == "done")
    orders.move_card(conn, cid, done, "2026-10-13T15:00:00", "o")                       # the hand-out; the refusal moves it away the same way
    assert not reserve.took_open(conn, orders.get_card(conn, cid))
    assert reserve.TOOK_LABEL not in w["point"].get("/my-point").get_data(as_text=True)


# ---- 4. «Продлить» --------------------------------------------------------------------------------------------------------------------

def test_extend_moves_the_date_by_two_days_once_and_tells_nobody(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T13:00:00", "Продлите до пятницы 24.10, пожалуйста")       # the date named is read no more
    run(conn, DAY + "T13:07:00", deliver, notify)
    assert until(conn, cid) == "2026-10-20" and code(conn, cid) == "at_point"
    [record] = events(conn, cid, "Резерв продлён на 2 дня, до 20.10.2026")
    assert record["level"] == "green" and record["author"] == "система"
    said(conn, chat, DAY + "T15:00:00", "продлить ещё")                                # the second one does nothing
    run(conn, DAY + "T15:07:00", deliver, notify)
    run(conn, END, deliver, notify)
    assert until(conn, cid) == "2026-10-20" and code(conn, cid) == "at_point"
    assert alerts(notify) == [] and tasks_of(conn, cid) == [] and len(deliver.sent) == 1   # 8: nothing to the responsible; nothing to the client
    assert points.notes(conn, orders.get_card(conn, cid)["point_id"]) == []


def test_an_extended_order_goes_to_oth_at_20_30_of_the_new_last_day_without_a_new_reminder(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T13:00:00", "завтра заберу")
    run(conn, DAY + "T13:07:00", deliver, notify)
    for when in (NEXT + "T12:30:00", NEXT + "T21:07:00", "2026-10-20T12:30:00", "2026-10-20T13:07:00", "2026-10-20T20:29:00"):
        run(conn, when, deliver, notify)
        assert code(conn, cid) == "at_point", when
    said(conn, chat, "2026-10-20T13:00:00", "продлите")                                # the new last day: nothing is read
    run(conn, "2026-10-20T20:30:00", deliver, notify)
    assert code(conn, cid) == "oth" and len(deliver.sent) == 1                          # no new reminder to the client
    [record] = events(conn, cid, "заказ переведён на «На ОТХ»")
    assert record["text"].startswith("Продлённый резерв (до 20.10.2026) закончился, заказ не выдан")


def test_an_extended_order_issued_or_moved_by_hand_is_left_alone(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T13:00:00", "продлить")
    run(conn, DAY + "T13:07:00", deliver, notify)
    orders.update_card(conn, cid, {"reserve_until": "2026-10-25"}, NEXT + "T10:00:00", "o")      # a manager moved it further
    run(conn, "2026-10-20T20:30:00", deliver, notify)
    run(conn, "2026-10-25T20:30:00", deliver, notify)
    assert code(conn, cid) == "at_point"


def test_refuse_after_extend_in_the_same_eight_hours_still_moves_the_order(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T13:00:00", "продлите")
    said(conn, chat, DAY + "T13:30:00", "передумал")
    run(conn, DAY + "T14:07:00", deliver, notify)
    assert code(conn, cid) == "oth" and len(events(conn, cid, "Резерв продлён")) == 1


# ---- 5. «Отказ» -----------------------------------------------------------------------------------------------------------------------

def test_the_clients_refusal_moves_the_order_to_oth_at_once(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T13:00:00", "Спасибо, уже не нужно")
    run(conn, DAY + "T13:07:00", deliver, notify)
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "oth" and card["cancel_reason"] == reserve.REFUSED_REASON
    [record] = events(conn, cid, "заказ переведён на «На ОТХ»")
    assert record["text"].startswith("Клиент Иван написал в чате после напоминания о сроке резерва: «Спасибо, уже не нужно»: отказ")
    assert not events(conn, cid, "Позвонить клиенту") and tasks_of(conn, cid) == [] and len(deliver.sent) == 1


@pytest.mark.parametrize("text, moved", [("Отказ", True), ("отказ.", True), (" ОТКАЗ ", True), ("Отказ клиента, оформляем", False),
                                         ("клиент написал отказ", False), ("отказываюсь", False), ("забрал", False), ("продлить", False)])
def test_of_the_staffs_messages_only_the_one_word_refusal_is_read(tmp_path, text, moved):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T13:00:00", text, direction="out")
    run(conn, DAY + "T13:07:00", deliver, notify)
    assert (code(conn, cid) == "oth") is moved
    assert until(conn, cid) == ("" if moved else DAY) and tasks_of(conn, cid) == []
    assert reserve.staff_refused(text) is moved


# ---- 6, 7. another answer; an answer after the 8 hours --------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["ок", "а сколько можно хранить?", "заберу 24.10", "в субботу приеду"])
def test_any_other_answer_does_nothing_and_the_order_goes_to_oth_when_the_wait_ends(tmp_path, text):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T13:00:00", text)
    run(conn, DAY + "T13:07:00", deliver, notify)
    assert code(conn, cid) == "at_point" and until(conn, cid) == DAY
    assert alerts(notify) == [] and tasks_of(conn, cid) == [] and not events(conn, cid, "ответил на напоминание")
    run(conn, END, deliver, notify)
    assert code(conn, cid) == "oth"


def test_a_call_in_the_chat_is_no_answer(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T13:00:00", "забрал", kind="call")
    run(conn, END, deliver, notify)
    assert code(conn, cid) == "oth" and tasks_of(conn, cid) == []


@pytest.mark.parametrize("text", ["забрал", "продлите", "отказ"])
def test_an_answer_in_the_ninth_hour_does_nothing(tmp_path, text):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T20:30:01", text)
    said(conn, chat, DAY + "T20:40:00", "Отказ", direction="out")
    read_chats(conn, "2000-01-01T00:00:00")                                             # the chats are behind: the move waits, the order is at the point
    run(conn, DAY + "T21:07:00", deliver, notify)
    assert code(conn, cid) == "at_point" and until(conn, cid) == DAY and tasks_of(conn, cid) == []
    assert not events(conn, cid, "ответил на напоминание") and not events(conn, cid, "написал в чате")
    read_chats(conn, "2099-01-01T00:00:00")
    run(conn, DAY + "T22:07:00", deliver, notify)
    assert code(conn, cid) == "oth" and orders.get_card(conn, cid)["cancel_reason"] == reserve.NO_ANSWER_REASON


def test_an_answer_of_the_last_minute_read_late_still_counts(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, END, "забрал")
    run(conn, DAY + "T21:07:00", deliver, notify)
    assert code(conn, cid) == "at_point" and tasks_of(conn, cid) == [TASK]


# ---- an order without a chat; VK ------------------------------------------------------------------------------------------------------

def test_an_order_without_a_chat_is_not_touched(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    from test_reserve_noon import Deliver
    deliver, notify = Deliver(conn), Notify()
    for when in (DAY + "T12:30:00", DAY + "T13:07:00", END, NEXT + "T10:07:00"):
        run(conn, when, deliver, notify)
    assert code(conn, cid) == "at_point" and until(conn, cid) == DAY and tasks_of(conn, cid) == []
    assert reserve.wait_list(conn, NEXT + "T10:07:00") == []


def test_nothing_goes_to_vk(tmp_path, monkeypatch):
    conn, cid, chat, deliver, notify = told(tmp_path)
    from tires_sync import order_notify
    monkeypatch.setattr(order_notify, "deliver", lambda *a, **k: pytest.fail("оповещение через order_notify не отправляется"))
    monkeypatch.setattr(order_notify, "tell_point", lambda *a, **k: pytest.fail("точке через order_notify не пишем"))
    said(conn, chat, DAY + "T13:00:00", "забрал")
    vk = run(conn, DAY + "T13:07:00", deliver, notify)
    assert vk.sent == [] and tasks_of(conn, cid) == [TASK]
    assert orders.get_card(conn, cid)["point"]["channel"] == "vk"                       # a point of VK reads it on «Рабочее место» all the same
    assert len(points.notes(conn, orders.get_card(conn, cid)["point_id"])) == 1


# ---- the list for the owner before the rules go live ----------------------------------------------------------------------------------

def test_the_wait_list_names_the_orders_the_next_tick_moves_and_writes_nothing(tmp_path, capsys):
    conn, cid, chat, deliver, notify = told(tmp_path)
    took = card_at(conn, "at_point", "2026-10-15T10:00:00")
    chat2 = conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id, last_at, synced_at) VALUES (1, 'c-2', ?, '', '2099-01-01T00:00:00')",
                         (took,)).lastrowid
    conn.commit()
    run(conn, DAY + "T12:40:00", deliver, notify)                                       # the second order's reminder
    said(conn, chat, DAY + "T13:00:00", "ок, понял")                                   # kept the order at the point before; not now
    said(conn, chat2, DAY + "T13:00:00", "забрал")
    assert reserve.wait_list(conn, DAY + "T20:29:00") == []
    before = conn.total_changes
    [row] = reserve.wait_list(conn, DAY + "T21:30:00")
    assert row == {"id": cid, "label": NUMBER, "point": "Бета", "sent_at": DAY + "T12:30:00", "hours": 9.0, "why": "прошло 8 часов без ответа"}
    assert conn.total_changes == before and code(conn, cid) == "at_point" and tasks_of(conn, took) == []
    conn.close()
    assert main(["reserve-wait-list", "--db", str(tmp_path / "x.db"), "--now", DAY + "T21:30:00"]) == 0
    out = capsys.readouterr().out
    assert "уйдут на «На ОТХ» при ближайшей проверке: 1" in out
    assert "заказ №%s | точка: Бета | напоминание ушло %s 12:30:00 | прошло 9,0 ч | прошло 8 часов без ответа" % (NUMBER, DAY) in out


def test_the_wait_list_has_a_refusal_not_read_yet_and_no_order_decided_by_the_old_rules(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T13:00:00", "отказываюсь")
    [row] = reserve.wait_list(conn, DAY + "T13:07:00")
    assert row["id"] == cid and row["why"] == "отказ в чате"
    orders.add_event(conn, cid, DAY + "T13:10:00", "система", "reserve", "Клиент ответил на напоминание о сроке резерва: на «На ОТХ» не "
                     "переводим " + reserve.OTH_MARK % orders.get_card(conn, cid)["entered_at"], level="grey")      # the record of before 12.11
    conn.commit()
    assert reserve.wait_list(conn, DAY + "T21:30:00") == []
    run(conn, DAY + "T21:30:00", deliver, notify)
    assert code(conn, cid) == "at_point"
