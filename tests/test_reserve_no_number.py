"""The owner 2026-10-26: «Требовать указать номер заказа у менеджера, если номера нет, уведомлять менеджера, а клиенту все равно отправлять
сообщение». An order due for the reserve reminder without a WebMIM number: the responsible is told once per order (the order journal keeps
it); the client still gets the reminder, without «№…». An order with a number is as before."""
from __future__ import annotations

from cryptography.fernet import Fernet

from http_fakes import ScriptedHttp
from test_order_reserve import POINT_VK, PointVk, _deliver, card_at, chat_of, db, owner_text
from test_reserve_ask_point import at, say
from tires_sync import connections, order_notify, order_reserve as reserve, orders
from tires_sync.secretbox import SecretBox

ASK_NUMBER = "Укажите номер заказа WebMIM по заказу %d (Иван)"
NO_NUMBER_TEXT = ("Добрый день. Срок хранения вашего заказа истекает 18.10.2026. Если вы уже забрали заказ — спасибо, не обращайте внимания. "
                  "Если нужно продлить ожидание, напишите нам.")


class Notify:
    def __init__(self):
        self.sent = []

    def __call__(self, card, text):
        self.sent.append((card["id"], text))
        return "ok"


def ready(tmp_path, number=False):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    if not number:
        conn.execute("UPDATE order_card SET webmim_number = '' WHERE id = ?", (cid,))
        conn.commit()
    return conn, cid, chat_of(conn, cid), _deliver(conn), PointVk(), Notify()


def asks(notify):
    return [(cid, text) for cid, text in notify.sent if text.startswith("Укажите номер")]


def test_no_number_tells_the_manager_once_and_the_client_still_gets_the_reminder_without_a_number(tmp_path):
    conn, cid, chat, deliver, vk, notify = ready(tmp_path)
    for hour in (10, 11, 15):
        reserve.check(conn, at(hour), notify, deliver, vk)
    assert asks(notify) == [(cid, ASK_NUMBER % cid)]
    assert vk.sent == [(POINT_VK, "(бот) Заказ №%d (Иван, 205/55 R16) ещё на точке? Ответьте «выдал %d» или «на точке %d»." % (cid, cid, cid))]
    assert say(conn, "на точке", deliver, at(11, 30), reply=cid) == \
        "Заказ №%d — отмечено: на точке. Клиенту отправлено напоминание о сроке хранения." % cid
    assert [(c, t) for c, t, _ in deliver.sent] == [(chat, NO_NUMBER_TEXT)]
    for when in (at(20), at(10, day="2026-10-19"), at(12, day="2026-10-20")):
        reserve.check(conn, when, notify, deliver, vk)
    assert asks(notify) == [(cid, ASK_NUMBER % cid)] and len(deliver.sent) == 1
    [record] = [e for e in orders.list_events(conn, cid) if "[резерв:нет-номера]" in e["text"]]
    assert record["text"] == "Менеджеру: «%s»: ok [резерв:нет-номера]" % (ASK_NUMBER % cid) and record["level"] == "yellow"


def test_the_manager_is_asked_for_the_number_once_per_order_even_after_a_new_stay_at_the_point(tmp_path):
    conn, cid, _chat, deliver, vk, notify = ready(tmp_path)
    reserve.check(conn, at(10), notify, deliver, vk)
    orders.move_card(conn, cid, next(s["id"] for s in orders.list_stages(conn) if s["code"] == "moving"), at(11), "o")
    orders.move_card(conn, cid, next(s["id"] for s in orders.list_stages(conn) if s["code"] == "at_point"), "2026-10-19T10:00:00", "o")
    reserve.check(conn, at(10, day="2026-10-22"), notify, deliver, vk)                 # the last day of the second stay
    assert len(vk.sent) == 2 and asks(notify) == [(cid, ASK_NUMBER % cid)]


def test_no_alert_of_the_reminder_before_its_day_or_outside_the_working_hours(tmp_path):
    conn, _cid, _chat, deliver, vk, notify = ready(tmp_path)
    reserve.check(conn, "2026-10-17T12:00:00", notify, deliver, vk)                    # the day before: only the alert of the end of the reserve
    reserve.check(conn, at(9, 30), notify, deliver, vk)                                 # before the working hours
    assert asks(notify) == [] and vk.sent == []


def test_an_order_with_a_number_is_as_before(tmp_path):
    conn, cid, chat, deliver, vk, notify = ready(tmp_path, number=True)
    reserve.check(conn, at(10), notify, deliver, vk)
    say(conn, "на точке 5200001", deliver, at(11))
    assert asks(notify) == [] and [(c, t) for c, t, _ in deliver.sent] == [(chat, owner_text())]
    assert not [e for e in orders.list_events(conn, cid) if "нет-номера" in e["text"]]


def test_the_owners_own_template_loses_the_number_cleanly(tmp_path):
    conn, cid, chat, deliver, vk, notify = ready(tmp_path)
    conn.execute("INSERT INTO order_template (name, audience, body) VALUES (?, 'client', ?)",
                 ("Клиенту: срок резерва истекает", "Заказ {webmim_number} ждёт вас до {hold_until}. Заказ № {webmim_number}, ждём."))
    conn.commit()
    reserve.check(conn, at(10), notify, deliver, vk)
    say(conn, "на точке", deliver, at(11), reply=cid)                                  # a reply to the question (a bare «1» is a quantity)
    assert [(c, t) for c, t, _ in deliver.sent] == [(chat, "Заказ ждёт вас до 18.10.2026. Заказ, ждём.")]


def test_the_number_request_of_a_point_account_order_goes_only_to_the_point(tmp_path):
    conn, cid, _chat, deliver, vk, _notify = ready(tmp_path)
    box = SecretBox(Fernet.generate_key())
    connections.set_config(conn, "telegram", 0, {"chat_id": "1000"}, at(9), "owner")
    connections.set_secret(conn, box, "telegram", 0, "bot_token", "123456:ABCdef-TEST", at(9), "owner")
    connections.set_secret(conn, box, "vk", 0, "token", "vk1.a.TEST", at(9), "owner")
    account = conn.execute("SELECT account_id FROM avito_chat WHERE order_id = ?", (cid,)).fetchone()[0]
    conn.execute("UPDATE point SET avito_account_id = ? WHERE vk_peer_id = ?", (account, POINT_VK))     # the order's chat is the point's account
    conn.commit()
    tg, vk_http = ScriptedHttp((200, {"ok": True, "result": {}})), ScriptedHttp((200, {"response": 1}))
    reserve.check(conn, at(10), lambda card, text: order_notify.deliver(conn, box, card, text, at(10), tg_http=tg, vk_http=vk_http), deliver, vk)
    from test_notify_by_point import sent_text
    assert tg.calls == [] and [sent_text(c) for c in vk_http.calls if "Укажите номер" in sent_text(c)] == ["(бот) " + ASK_NUMBER % cid]



def test_a_failing_number_notice_never_stops_the_question_to_the_point(tmp_path):
    conn, cid, _chat, deliver, vk, _notify = ready(tmp_path)

    def notify(card, text):
        if text.startswith("Укажите номер"):
            raise RuntimeError("down")
        return "ok"
    reserve.check(conn, at(10), notify, deliver, vk)
    assert len(vk.sent) == 1 and orders.list_events(conn, cid)


def test_a_number_after_a_non_breaking_space_a_hash_or_n_is_left_out_cleanly(tmp_path):
    conn, cid, chat, deliver, vk, notify = ready(tmp_path)
    conn.execute("INSERT INTO order_template (name, audience, body) VALUES (?, 'client', ?)",
                 ("Клиенту: срок резерва истекает", "Заказ № {webmim_number} и #{webmim_number} и N{webmim_number} до {hold_until}."))
    conn.commit()
    reserve.check(conn, at(10), notify, deliver, vk)
    say(conn, "на точке", deliver, at(11), reply=cid)
    assert [(c, t) for c, t, _ in deliver.sent] == [(chat, "Заказ и и до 18.10.2026.")]


def test_without_an_automatic_client_message_the_manager_is_still_asked_for_the_number_once(tmp_path):
    conn, cid, _chat, _deliver_, vk, notify = ready(tmp_path)                    # the owner 2026-10-26: no sender of the client's messages
    for hour in (9, 10, 12, 18):
        reserve.check(conn, at(hour), notify, None, vk)
    assert asks(notify) == [(cid, ASK_NUMBER % cid)]
    assert vk.sent == []                                                          # the client side as before: the point is not asked
    assert not any("[резерв:клиенту:" in e["text"] for e in orders.list_events(conn, cid))


def test_without_avito_keys_the_number_is_asked_once_and_the_client_side_is_as_before(tmp_path):
    import tires_sync.chats as chats
    conn, cid, _chat, _ok, vk, notify = ready(tmp_path)
    deliver = _deliver(conn, chats.ChatError("У аккаунта не заданы ключи Авито."))
    reserve.check(conn, at(10), notify, deliver, vk)
    say(conn, "на точке", deliver, at(11), reply=cid)
    reserve.check(conn, at(12), notify, deliver, vk)
    assert asks(notify) == [(cid, ASK_NUMBER % cid)] and deliver.sent == []
    assert any("не заданы ключи" in e["text"] for e in orders.list_events(conn, cid))
