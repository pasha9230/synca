"""The client's name or phone changed after the panel told the point in VK (the owner 2026-11-24): the number of the message is kept on both
roads (send_point_task, tell_point), the message is corrected by messages.edit and one short note goes; too old for VK, no number, VK down:
the note only, the card stays saved. Fakes only: nothing leaves the tests."""
from __future__ import annotations

import re
import urllib.parse

from cryptography.fernet import Fernet

from http_fakes import ScriptedHttp
from panel_helpers import post
from test_order_messages import VK_OK, keys, vk_calls, with_vk
from test_order_placed import Deliver, panel, ready  # noqa: F401  (panel: the fixture of the press on the page)
from test_oth_sale import LATER as OTH_LATER, T
from test_webmim_create import NOW, FakeWebmim, db, place
from test_order_reserve import card_at, db as reserve_db
from tires_sync import connections, order_notify, order_placed, orders, oth_sale, vk_point_fix, vk_send
from tires_sync.secretbox import SecretBox

LATER = "2026-10-23T13:00:00"
NEW_PHONE = "+7 900 555-66-77"


class Vk:
    """The community's send and edit: records the calls; `edit_fail` / `send_fail` raise instead."""

    def __init__(self, edit_fail=None, send_fail=None):
        self.sent, self.edits, self.edit_fail, self.send_fail = [], [], edit_fail, send_fail
        self.tries = 0                                              # every edit asked, failed or not

    def send(self, peer_id, text, random_id=None):
        if self.send_fail is not None:
            raise self.send_fail
        self.sent.append((peer_id, text, random_id))
        return 100 + len(self.sent)

    def edit(self, peer_id, message_id, text):
        self.tries += 1
        if self.edit_fail is not None:
            raise self.edit_fail
        self.edits.append((peer_id, message_id, text))
        return 1


def told(tmp_path, vk=None):
    """An order placed in WebMIM whose point got «Менеджеру: заказ оформлен» in VK (message 101)."""
    conn = db(tmp_path)
    mine = with_vk(conn, ready(conn))
    place(conn, mine, FakeWebmim())
    vk = vk or Vk()
    order_placed.on_placed(conn, mine, "1001", NOW, "owner", Deliver(), vk.send)
    assert len(vk.sent) == 1
    return conn, mine, vk


def rows(conn, card_id):
    return [dict(zip(("peer_id", "message_id", "kind", "text", "client_name", "client_phone"), r)) for r in conn.execute(
        "SELECT peer_id, message_id, kind, text, client_name, client_phone FROM vk_point_message WHERE order_id = ? ORDER BY id", (card_id,))]


def change(conn, card_id, vk, **fields):
    card = orders.get_card(conn, card_id)
    before = {"client_name": card["client_name"], "client_phone": card["client_phone"]}
    orders.update_card(conn, card_id, fields, LATER, "owner")
    return vk_point_fix.client_changed(conn, card_id, before, LATER, "owner", vk.send, vk.edit)


def notify_events(conn, card_id):
    return [e for e in orders.list_events(conn, card_id) if e["kind"] == "notify"]


# ---- the number is kept ------------------------------------------------------------------------------------------------------------------

def test_the_number_of_the_placed_message_is_kept(tmp_path):
    conn, mine, vk = told(tmp_path)
    card = orders.get_card(conn, mine)
    [row] = rows(conn, mine)
    assert row["peer_id"] == "555" and row["message_id"] == 101 and row["kind"] == vk_point_fix.TASK
    assert row["text"] == vk.sent[0][1] and row["text"].startswith("(бот) Заказ 5001")
    assert row["client_name"] == card["client_name"] and row["client_phone"] == card["client_phone"]


def test_the_number_of_the_oth_text_is_kept_by_tell_point(tmp_path):
    conn = db(tmp_path)
    mine = with_vk(conn, ready(conn))
    box = SecretBox(Fernet.generate_key())
    connections.set_secret(conn, box, "vk", 0, "token", "vk-TOKEN-1", NOW, "owner")
    conn.commit()
    card = orders.get_card(conn, mine)
    text = "Шины отложены под нового клиента.\nПокупатель: %s\nТелефон: %s" % (card["client_name"], card["client_phone"])
    told_ = order_notify.tell_point(conn, box, card, text, NOW, vk_http=ScriptedHttp((200, {"response": 77})), remember=vk_point_fix.OTH_HOLD)
    assert told_.startswith("отправлено в ВК")
    [row] = rows(conn, mine)
    assert row["message_id"] == 77 and row["kind"] == vk_point_fix.OTH_HOLD and row["text"] == "(бот) " + text
    order_notify.tell_point(conn, box, card, text + " ", LATER, vk_http=ScriptedHttp((200, {"response": 78})))       # an alert: not kept
    assert len(rows(conn, mine)) == 1


# ---- the change --------------------------------------------------------------------------------------------------------------------------

def test_a_new_name_and_phone_edit_the_message_and_one_note_goes(tmp_path):
    conn, mine, vk = told(tmp_path)
    card = orders.get_card(conn, mine)
    old_text = rows(conn, mine)[0]["text"]
    change(conn, mine, vk, client_name="Пётр", client_phone=NEW_PHONE)
    expected = old_text.replace(card["client_name"], "Пётр").replace(card["client_phone"], NEW_PHONE)
    assert vk.edits == [("555", 101, expected)] and "Пётр" in expected and card["client_name"] not in expected
    [note] = vk.sent[1:]
    assert note[0] == "555" and note[1].startswith("(бот) По заказу №") and "изменены данные покупателя: имя Пётр, телефон %s" % NEW_PHONE in note[1]
    assert rows(conn, mine)[0]["text"] == expected and rows(conn, mine)[0]["client_name"] == "Пётр"
    [event] = notify_events(conn, mine)
    assert event["text"].startswith("Сообщение точке «Гамма-адрес» в ВК исправлено") and event["level"] != "red"
    assert "Пётр" not in event["text"] and NEW_PHONE not in event["text"]


def test_too_old_for_vk_sends_a_new_message_and_says_so(tmp_path):
    conn, mine, vk = told(tmp_path, Vk(edit_fail=vk_send.VkError("ВК не принял сообщение (код 909): too old", code=909)))
    change(conn, mine, vk, client_name="Пётр")
    assert vk.edits == [] and len(vk.sent) == 2 and "имя Пётр" in vk.sent[1][1]
    [event] = notify_events(conn, mine)
    assert "отправлено новое сообщение" in event["text"] and "срок правки вышел" in event["text"] and "909" not in event["text"]


def test_vk_down_keeps_the_card_and_writes_a_red_record(tmp_path):
    conn, mine, vk = told(tmp_path)
    down = vk_send.VkError("ВК недоступен: нет связи с сервером.")
    vk.edit_fail = vk.send_fail = down
    change(conn, mine, vk, client_name="Пётр")
    assert orders.get_card(conn, mine)["client_name"] == "Пётр"
    [event] = notify_events(conn, mine)
    assert event["level"] == "red" and "ВК недоступен" in event["text"]


def test_after_vk_down_nothing_more_is_tried_in_this_save(tmp_path):
    conn, mine, vk = told(tmp_path)
    [row] = rows(conn, mine)
    vk_point_fix.remember(conn, mine, "555", 202, row["text"], vk_point_fix.OTH_HOLD, NOW)      # a second message of the order
    conn.commit()
    vk.edit_fail = vk_send.VkError("ВК недоступен: нет связи с сервером.")
    change(conn, mine, vk, client_name="Пётр")
    assert vk.tries == 1 and len(vk.sent) == 1                    # no second edit, no note
    [event] = notify_events(conn, mine)
    assert event["level"] == "red" and "ВК недоступен" in event["text"]


def test_only_the_buyer_line_is_corrected_not_a_word_elsewhere(tmp_path):
    conn, mine, vk = told(tmp_path)
    name = orders.get_card(conn, mine)["client_name"]
    tail = "\nКомментарий: %s просил %sа не звать, %sов в курсе" % (name, name, name)
    conn.execute("UPDATE vk_point_message SET text = text || ? WHERE order_id = ?", (tail, mine))
    conn.commit()
    change(conn, mine, vk, client_name="Пётр")
    [(_peer, _mid, text)] = vk.edits
    assert text.endswith(tail) and "Покупатель Пётр" in text and text.count("Пётр") == 1


def test_a_text_longer_than_vk_takes_sends_a_note_instead(tmp_path):
    conn, mine, vk = told(tmp_path)
    conn.execute("UPDATE vk_point_message SET text = text || ? WHERE order_id = ?", ("\n" + "я" * vk_send.VK_LIMIT, mine))
    conn.commit()
    change(conn, mine, vk, client_name="Пётр")
    assert vk.tries == 0 and len(vk.sent) == 2 and "имя Пётр" in vk.sent[1][1]
    assert "текст длиннее лимита ВК" in notify_events(conn, mine)[0]["text"]


def test_another_manager_of_the_point_gets_nothing_and_the_journal_says_so(tmp_path):
    conn, mine, vk = told(tmp_path)
    conn.execute("UPDATE point SET vk_peer_id = '777' WHERE id = (SELECT point_id FROM order_card WHERE id = ?)", (mine,))
    conn.commit()
    change(conn, mine, vk, client_name="Пётр")
    assert vk.tries == 0 and len(vk.sent) == 1
    [event] = notify_events(conn, mine)
    assert event["level"] == "grey" and "Менеджер точки" in event["text"] and "не исправлено" in event["text"]


def test_a_closed_order_without_a_number_gets_no_note(tmp_path):
    conn, mine, vk = told(tmp_path)
    conn.execute("DELETE FROM vk_point_message")
    card = orders.get_card(conn, mine)
    conn.execute("UPDATE order_card SET client_name = 'Пётр', stage_id = (SELECT id FROM order_stage WHERE kind != 'open' ORDER BY id LIMIT 1) "
                 "WHERE id = ?", (mine,))
    conn.commit()
    assert orders.get_card(conn, mine)["stage"]["kind"] != "open"
    before = {"client_name": card["client_name"], "client_phone": card["client_phone"]}
    assert vk_point_fix.client_changed(conn, mine, before, LATER, "owner", vk.send, vk.edit) == ""
    assert vk.tries == 0 and len(vk.sent) == 1 and notify_events(conn, mine) == []


def test_a_new_client_on_oth_forgets_the_messages_of_the_old_one(tmp_path):
    conn = reserve_db(tmp_path)
    a = card_at(conn, "oth", T)
    conn.execute("INSERT INTO vk_point_message (order_id, peer_id, message_id, kind, text, client_name, client_phone, sent_at) "
                 "VALUES (?, '555', 9, 'task', '(бот) Покупатель Иван', 'Иван', '', ?)", (a, T))
    conn.commit()
    assert oth_sale.to_new_client(conn, a, "Пётр", "+7 900 555 66 77", OTH_LATER, "o") == a
    assert rows(conn, a) == []


def test_without_a_number_a_new_message_goes(tmp_path):
    conn, mine, vk = told(tmp_path)
    conn.execute("DELETE FROM vk_point_message")                  # an order told before the numbers were kept
    conn.commit()
    change(conn, mine, vk, client_name="Пётр")
    assert vk.edits == [] and len(vk.sent) == 2 and "имя Пётр" in vk.sent[1][1]
    assert "номера сообщения нет" in notify_events(conn, mine)[0]["text"]


def test_the_same_phone_written_otherwise_sends_nothing(tmp_path):
    conn, mine, vk = told(tmp_path)
    digits = re.sub(r"\D", "", orders.get_card(conn, mine)["client_phone"])
    change(conn, mine, vk, client_phone="8" + digits[1:])
    assert vk.edits == [] and len(vk.sent) == 1 and notify_events(conn, mine) == []


def test_the_neighbour_order_of_the_same_client_is_not_touched(tmp_path):
    conn, mine, vk = told(tmp_path)
    name = orders.get_card(conn, mine)["client_name"]
    other = orders.create_card(conn, {"client_name": name, "client_phone": "+7 900 000 00 01"}, NOW, "owner")
    conn.execute("UPDATE order_card SET point_id = (SELECT point_id FROM order_card WHERE id = ?) WHERE id = ?", (mine, other))
    vk_point_fix.remember(conn, other, "555", 555, "(бот) Покупатель %s +7 900 000 00 01" % name, vk_point_fix.TASK, NOW)
    conn.commit()
    change(conn, mine, vk, client_name="Пётр")
    assert [e[1] for e in vk.edits] == [101] and len(vk.sent) == 2
    assert rows(conn, other)[0]["text"] == "(бот) Покупатель %s +7 900 000 00 01" % name and notify_events(conn, other) == []


# ---- the window on the page --------------------------------------------------------------------------------------------------------------

def placed_on_page(panel):
    app, conn, fake, avito, box, client = panel
    app.config["VK_HTTP"] = ScriptedHttp(VK_OK)                     # the placed message: number 7
    mine = with_vk(conn, ready(conn))
    keys(conn, box, mine)
    post(client, "/orders/%d/webmim/create" % mine, "tok")
    assert [r["message_id"] for r in rows(conn, mine)] == [7]
    return mine


def autosave(client, mine, field, value):
    return client.post("/orders/%d/autosave" % mine, data={"csrf": "tok", "field": field, field: value}, headers={"X-Requested-With": "fetch"})


def test_the_autosaved_name_edits_the_message_and_sends_a_note(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_on_page(panel)
    app.config["VK_HTTP"] = ScriptedHttp((200, {"response": 1}), (200, {"response": 8}))
    assert autosave(client, mine, "client_name", "Пётр").get_json()["ok"]
    calls = app.config["VK_HTTP"].calls
    assert [c[1] for c in calls] == [vk_send.EDIT_URL, vk_send.URL]
    edit, note = vk_calls(app)
    assert edit["peer_id"] == ["555"] and edit["message_id"] == ["7"] and "Пётр" in edit["message"][0]
    assert note["peer_id"] == ["555"] and "имя Пётр" in note["message"][0]


def test_the_save_button_edits_the_message_with_a_short_timeout(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_on_page(panel)
    calls = []

    def http(method, url, headers, body, timeout=30):              # takes a timeout as transport.urllib_transport
        calls.append((url, timeout, urllib.parse.parse_qs(body.decode("utf-8"))))
        return 200, {}, b'{"response": 1}'
    app.config["VK_HTTP"] = http
    assert post(client, "/orders/%d/save" % mine, "tok", client_name="Пётр").status_code == 302
    assert [(c[0], c[1]) for c in calls] == [(vk_send.EDIT_URL, vk_point_fix.SHORT_TIMEOUT), (vk_send.URL, vk_point_fix.SHORT_TIMEOUT)]
    assert calls[0][2]["message_id"] == ["7"] and "Пётр" in calls[0][2]["message"][0] and "имя Пётр" in calls[1][2]["message"][0]
    assert orders.get_card(conn, mine)["client_name"] == "Пётр"


def test_vk_down_on_the_page_saves_the_card(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_on_page(panel)
    app.config["VK_HTTP"] = lambda *a: (_ for _ in ()).throw(OSError("timeout"))
    r = autosave(client, mine, "client_name", "Пётр")
    assert r.status_code == 200 and r.get_json()["ok"] and orders.get_card(conn, mine)["client_name"] == "Пётр"
    assert any(e["level"] == "red" and "ВК недоступен" in e["text"] for e in notify_events(conn, mine))


# ---- vk_send.edit_message ----------------------------------------------------------------------------------------------------------------

def test_edit_message_sends_the_number_and_keeps_the_code_of_a_refusal():
    http = ScriptedHttp((200, {"response": 1}), (200, {"error": {"error_code": 909, "error_msg": "too old"}}))
    assert vk_send.edit_message("tok-1", "555", 7, "текст", http=http) == 1
    fields = urllib.parse.parse_qs(http.calls[0][3].decode("utf-8"))
    assert http.calls[0][1] == vk_send.EDIT_URL and fields["message_id"] == ["7"] and fields["peer_id"] == ["555"] and fields["message"] == ["текст"]
    try:
        vk_send.edit_message("tok-1", "555", 7, "текст", http=http)
        raise AssertionError("909 must raise")
    except vk_send.VkError as err:
        assert err.code == 909 and "tok-1" not in str(err)
    long = ScriptedHttp()
    try:
        vk_send.edit_message("tok-1", "555", 7, "я" * (vk_send.VK_LIMIT + 1), http=long)
        raise AssertionError("a long text must raise")
    except vk_send.VkError:
        assert long.calls == []
