"""The order changed after the panel told the point in VK (the owner 2026-11-25): not only the client — the positions, their pieces and prices,
the dates the message carries. A message kept with its template is built again by it (messages.edit) and one short note goes; an old message
(no template, no number) gets the note «По заказу №… изменено: …» only; nothing changed: VK is not asked; another point: the old message stays.
Fakes only: nothing leaves the tests."""
from __future__ import annotations

import json

from cryptography.fernet import Fernet

from http_fakes import ScriptedHttp
from panel_helpers import post
from test_order_messages import vk_calls, with_vk
from test_order_placed import Deliver, panel, ready  # noqa: F401  (panel: the fixture of the press on the page)
from test_vk_point_fix import LATER, Vk, autosave, notify_events, placed_on_page, rows, told
from test_webmim_create import NOW, db
from tires_sync import connections, order_notify, orders, oth_sale, vk_point_fix, vk_send
from tires_sync.secretbox import SecretBox


def changed(conn, card_id, vk, edit_of, *args, **kw):
    """`edit_of(conn, …)` changes the order between the snapshot and vk_point_fix.order_changed, as a press of the window does."""
    before = vk_point_fix.snapshot(conn, card_id, LATER)
    edit_of(conn, *args, **kw)
    return vk_point_fix.order_changed(conn, card_id, before, LATER, "owner", vk.send, vk.edit)


def item(conn, card_id, n=0):
    return orders.get_card(conn, card_id)["items"][n]


def qty(conn, item_id, pieces, sale_unit=None):
    row = conn.execute("SELECT sale_unit FROM order_item WHERE id = ?", (item_id,)).fetchone()
    orders.update_item(conn, item_id, str(pieces), str(sale_unit if sale_unit is not None else row[0]), LATER, "owner")


def card_fields(conn, card_id, **fields):
    orders.update_card(conn, card_id, fields, LATER, "owner")


# ---- kept with the template --------------------------------------------------------------------------------------------------------------

def test_the_placed_message_is_kept_with_its_template(tmp_path):
    conn, mine, vk = told(tmp_path)
    [row] = conn.execute("SELECT template, vals, wrap FROM vk_point_message WHERE order_id = ?", (mine,)).fetchall()
    assert "{items_full}" in row[0] and row[2] == "bot"
    assert json.loads(row[1])["webmim_number"] == "5001"


def test_new_pieces_rebuild_the_message_and_one_note_goes(tmp_path):
    conn, mine, vk = told(tmp_path)
    old_text = rows(conn, mine)[0]["text"]
    first = item(conn, mine)
    changed(conn, mine, vk, qty, first["id"], 3)
    [(peer, message_id, text)] = vk.edits
    assert peer == "555" and message_id == 101 and text.startswith("(бот) Заказ 5001")
    assert "Кол-во 3 шт\nЦена ед. 5 000 ₽\nСумма 15 000 ₽" in text and "Кол-во 4 шт" not in text
    assert text == old_text.replace("Кол-во 4 шт\nЦена ед. 5 000 ₽\nСумма 20 000 ₽", "Кол-во 3 шт\nЦена ед. 5 000 ₽\nСумма 15 000 ₽")
    [note] = vk.sent[1:]
    assert note[0] == "555" and note[1].startswith("(бот) По заказу №") and "изменено: позиции: Шина 111 × 3; Шина 222 × 2" in note[1]
    assert rows(conn, mine)[0]["text"] == text
    [event] = notify_events(conn, mine)
    assert event["text"].startswith("Сообщение точке «Гамма-адрес» в ВК исправлено: изменились позиции") and event["level"] == "grey"


def test_a_new_price_for_the_point_rebuilds_the_message(tmp_path):
    conn, mine, vk = told(tmp_path)
    changed(conn, mine, vk, qty, item(conn, mine, 1)["id"], 2, 5500)
    [(_p, _m, text)] = vk.edits
    assert "Товар Шина 222\nКод 222\nКол-во 2 шт\nЦена ед. 5 500 ₽\nСумма 11 000 ₽" in text


def test_a_removed_and_an_added_position_rebuild_the_message(tmp_path):
    conn, mine, vk = told(tmp_path)
    changed(conn, mine, vk, lambda c: orders.remove_item(c, item(c, mine, 1)["id"], LATER, "owner"))
    [(_p, _m, text)] = vk.edits
    assert "Шина 111" in text and "Шина 222" not in text
    one = dict(item(conn, mine))

    def add(c):
        orders.add_item(c, mine, goods_id="333", name="Шина 333", brand_key="b", season="summer", diameter=16, territory=one["territory"],
                        qty=1, sale_unit=7000, supplier_price=6000, terms={}, fee_pct=0, promo_pct=0, now=LATER, author="owner")
    changed(conn, mine, vk, add)
    assert len(vk.edits) == 2 and "Товар Шина 333\nКод 333\nКол-во 1 шт\nЦена ед. 7 000 ₽" in vk.edits[1][2]
    assert "Шина 222" not in vk.edits[1][2]                       # built from the message as it is now in VK


def test_a_new_client_and_new_pieces_go_in_one_edit_and_one_note(tmp_path):
    conn, mine, vk = told(tmp_path)

    def both(c):
        qty(c, item(c, mine)["id"], 2)
        card_fields(c, mine, client_name="Пётр")
    changed(conn, mine, vk, both)
    [(_p, _m, text)] = vk.edits
    assert "Покупатель Пётр" in text and "Кол-во 2 шт" in text
    [note] = vk.sent[1:]
    assert "имя Пётр" in note[1] and "позиции: Шина 111 × 2" in note[1]
    assert "Пётр" not in notify_events(conn, mine)[0]["text"]


def test_nothing_the_point_was_told_changed_vk_is_not_asked(tmp_path):
    conn, mine, vk = told(tmp_path)
    changed(conn, mine, vk, card_fields, mine, comment="позвонить вечером", tags="vip")      # not in the message
    changed(conn, mine, vk, qty, item(conn, mine)["id"], 4)                          # the same pieces
    assert vk.tries == 0 and len(vk.sent) == 1 and notify_events(conn, mine) == []


def test_a_value_taken_away_never_empties_the_line(tmp_path):
    conn, mine, vk = told(tmp_path)
    changed(conn, mine, vk, lambda c: c.execute("UPDATE order_card SET webmim_number = '' WHERE id = ?", (mine,)) and c.commit())
    assert vk.tries == 0 and len(vk.sent) == 1


def test_too_old_for_vk_sends_the_note_only(tmp_path):
    for code in vk_send.TOO_OLD:
        (tmp_path / str(code)).mkdir()
        conn, mine, vk = told(tmp_path / str(code), Vk(edit_fail=vk_send.VkError("ВК не принял сообщение (код %d): old" % code, code=code)))
        text = rows(conn, mine)[0]["text"]
        changed(conn, mine, vk, qty, item(conn, mine)["id"], 3)
        assert vk.tries == 1 and vk.edits == [] and len(vk.sent) == 2 and "позиции: Шина 111 × 3" in vk.sent[1][1]
        [event] = notify_events(conn, mine)
        assert "отправлено новое сообщение об изменении заказа" in event["text"] and "срок правки вышел" in event["text"]
        assert rows(conn, mine)[0]["text"] == text                # VK keeps the old one: so does the panel


def test_vk_down_stops_and_writes_a_red_record(tmp_path):
    conn, mine, vk = told(tmp_path)
    vk.edit_fail = vk.send_fail = vk_send.VkError("ВК недоступен: нет связи с сервером.")
    changed(conn, mine, vk, qty, item(conn, mine)["id"], 3)
    assert vk.tries == 1 and len(vk.sent) == 1 and item(conn, mine)["qty"] == 3
    [event] = notify_events(conn, mine)
    assert event["level"] == "red" and "об изменении заказа: ВК недоступен" in event["text"]


def test_another_point_keeps_the_old_message(tmp_path):
    conn, mine, vk = told(tmp_path)
    text = rows(conn, mine)[0]["text"]
    other = conn.execute("INSERT INTO point (name, vk_peer_id, channel) VALUES ('Дельта', '556', 'vk')").lastrowid
    conn.commit()

    def move(c):
        card_fields(c, mine, point_id=str(other))
        qty(c, item(c, mine)["id"], 3)
    changed(conn, mine, vk, move)
    assert vk.tries == 0 and len(vk.sent) == 1 and rows(conn, mine)[0]["text"] == text and notify_events(conn, mine) == []


def test_a_text_changed_by_hand_is_not_kept_with_the_template(tmp_path):
    conn, mine, vk = told(tmp_path)
    vk_point_fix.remember(conn, mine, "555", 300, "(бот) Иван, свой текст", vk_point_fix.TASK, NOW, template="Покупатель {client_name}")
    conn.commit()
    assert conn.execute("SELECT template, vals FROM vk_point_message WHERE message_id = 300").fetchone() == (None, None)


# ---- old orders ------------------------------------------------------------------------------------------------------------------------

def test_a_message_kept_without_its_template_gets_the_note_only(tmp_path):
    conn, mine, vk = told(tmp_path)
    conn.execute("UPDATE vk_point_message SET template = NULL, vals = NULL, wrap = NULL")      # kept on 2026-11-24
    conn.commit()
    changed(conn, mine, vk, qty, item(conn, mine)["id"], 3)
    assert vk.tries == 0 and len(vk.sent) == 2 and "изменено: позиции: Шина 111 × 3" in vk.sent[1][1]
    [event] = notify_events(conn, mine)
    assert "отправлено новое сообщение об изменении заказа" in event["text"] and "старое сообщение не пересобрать" in event["text"]


def test_without_a_number_the_note_says_only_what_the_message_had(tmp_path):
    conn, mine, vk = told(tmp_path)
    conn.execute("DELETE FROM vk_point_message")
    conn.commit()

    def both(c):
        qty(c, item(c, mine)["id"], 3)
        card_fields(c, mine, delivery_date="2026-10-26")
    changed(conn, mine, vk, both)
    assert vk.tries == 0 and len(vk.sent) == 2
    note = vk.sent[1][1]
    assert "позиции: Шина 111 × 3" in note and "WebMIM" not in note
    assert "дата доставки" not in note                             # the point's message never had the date: nothing to announce of it
    assert "номера сообщения нет" in notify_events(conn, mine)[0]["text"]


def test_an_order_the_point_was_never_told_gets_nothing(tmp_path):
    conn = db(tmp_path)
    mine = with_vk(conn, ready(conn))
    vk = Vk()
    assert vk_point_fix.snapshot(conn, mine, LATER) == {}
    changed(conn, mine, vk, qty, item(conn, mine)["id"], 3)
    assert vk.tries == 0 and vk.sent == [] and notify_events(conn, mine) == []


# ---- «Точке: шины с ОТХ отложены под клиента» ---------------------------------------------------------------------------------------------

def test_the_oth_text_is_rebuilt_with_the_new_reserve_date(tmp_path):
    conn = db(tmp_path)
    mine = with_vk(conn, ready(conn))
    box = SecretBox(Fernet.generate_key())
    connections.set_secret(conn, box, "vk", 0, "token", "vk-TOKEN-1", NOW, "owner")
    conn.commit()
    card = orders.get_card(conn, mine)
    text = oth_sale.point_text(conn, card, NOW)
    assert "резерв до 27.10.2026" in text
    order_notify.tell_point(conn, box, card, text, NOW, vk_http=ScriptedHttp((200, {"response": 77})), remember=vk_point_fix.OTH_HOLD)
    [row] = conn.execute("SELECT wrap FROM vk_point_message WHERE order_id = ?", (mine,)).fetchall()
    assert row[0] == "bot_point"
    vk = Vk()
    changed(conn, mine, vk, card_fields, mine, delivery_date="2026-10-26")
    [(_p, message_id, new)] = vk.edits
    assert message_id == 77 and "резерв до 29.10.2026" in new and new.startswith("(бот) ")
    assert "резерв до 29.10.2026" in vk.sent[0][1]


# ---- nothing to the client ---------------------------------------------------------------------------------------------------------------

def test_nothing_goes_to_the_client(tmp_path):
    conn = db(tmp_path)
    mine = with_vk(conn, ready(conn))
    from test_webmim_create import FakeWebmim, place
    from tires_sync import order_placed
    place(conn, mine, FakeWebmim())
    deliver, vk = Deliver(), Vk()
    order_placed.on_placed(conn, mine, "1001", NOW, "owner", deliver, vk.send)
    to_client = list(deliver.sent)
    changed(conn, mine, vk, qty, item(conn, mine)["id"], 3)
    assert deliver.sent == to_client and all(peer == "555" for peer, *_ in vk.sent) and vk.edits[0][0] == "555"


# ---- the window on the page --------------------------------------------------------------------------------------------------------------

def test_the_pieces_changed_on_the_page_edit_the_message(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_on_page(panel)
    app.config["VK_HTTP"] = ScriptedHttp((200, {"response": 1}), (200, {"response": 8}))
    first = item(conn, mine)
    post(client, "/orders/%d/items/%d/update" % (mine, first["id"]), "tok", qty="3", sale_unit=str(first["sale_unit"]))
    assert item(conn, mine)["qty"] == 3
    edit, note = vk_calls(app)
    assert edit["message_id"] == ["7"] and "Кол-во 3 шт" in edit["message"][0] and "позиции:" in note["message"][0]


def test_the_comment_saved_on_the_page_asks_nothing_of_vk(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_on_page(panel)
    app.config["VK_HTTP"] = ScriptedHttp()
    assert post(client, "/orders/%d/save" % mine, "tok", comment="перезвонить").status_code == 302
    assert orders.get_card(conn, mine)["comment"] == "перезвонить" and app.config["VK_HTTP"].calls == []


def test_a_new_point_on_the_page_leaves_the_old_message(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_on_page(panel)
    text = rows(conn, mine)[0]["text"]
    other = conn.execute("INSERT INTO point (name, vk_peer_id, channel) VALUES ('Дельта', '556', 'vk')").lastrowid
    conn.commit()
    app.config["VK_HTTP"] = ScriptedHttp((200, {"response": 9}))
    assert autosave(client, mine, "point_id", str(other)).get_json()["ok"]
    assert vk_send.EDIT_URL not in [c[1] for c in app.config["VK_HTTP"].calls] and rows(conn, mine)[0]["text"] == text


# ---- the review of 2026-11-25 ------------------------------------------------------------------------------------------------------------

def rows_vals(conn, card_id):
    return conn.execute("SELECT vals FROM vk_point_message WHERE order_id = ?", (card_id,)).fetchone()[0]


def test_after_a_refusal_the_change_is_announced_once(tmp_path):
    for fail in (vk_send.VkError("ВК не принял сообщение (код 909): old", code=909), vk_send.VkError("ВК не принял сообщение (код 15): no")):
        (tmp_path / str(fail.code)).mkdir()
        conn, mine, vk = told(tmp_path / str(fail.code), Vk(edit_fail=fail))
        text = rows(conn, mine)[0]["text"]
        changed(conn, mine, vk, qty, item(conn, mine)["id"], 3)
        assert vk.tries == 1 and len(vk.sent) == 2
        row = conn.execute("SELECT text, vals FROM vk_point_message WHERE order_id = ?", (mine,)).fetchone()
        assert row[0] == text and "Кол-во 3 шт" in json.loads(row[1])["items_full"]      # the text stays old, the values are taken
        changed(conn, mine, vk, card_fields, mine, comment="ещё раз")                     # the next save changes nothing of it
        assert vk.tries == 1 and len(vk.sent) == 2
        changed(conn, mine, vk, card_fields, mine, client_name="Пётр")                    # a new change: only it
        assert vk.tries == 2 and len(vk.sent) == 3 and "имя Пётр" in vk.sent[2][1] and "позиции" not in vk.sent[2][1]


def test_a_message_too_long_for_vk_is_announced_once(tmp_path):
    conn, mine, vk = told(tmp_path)
    many = "\n".join("Строка %d" % n for n in range(500))
    vals = json.dumps(dict(json.loads(rows_vals(conn, mine)), point_hours=many), ensure_ascii=False)
    conn.execute("UPDATE vk_point_message SET template = template || ?, vals = ?, text = text || ? WHERE order_id = ?",
                 ("\n{point_hours}", vals, "\n" + many, mine))
    conn.commit()
    changed(conn, mine, vk, qty, item(conn, mine)["id"], 3)
    assert vk.tries == 0 and len(vk.sent) == 2 and "текст длиннее лимита ВК" in notify_events(conn, mine)[0]["text"]
    changed(conn, mine, vk, card_fields, mine, comment="ещё раз")
    assert vk.tries == 0 and len(vk.sent) == 2


def test_a_change_made_elsewhere_is_not_gathered_by_another_save(tmp_path):
    conn, mine, vk = told(tmp_path)
    conn.execute("UPDATE order_item SET qty = 3 WHERE id = ?", (item(conn, mine)["id"],))       # not by a press of the window
    conn.commit()
    changed(conn, mine, vk, card_fields, mine, comment="перезвонить")
    assert vk.tries == 0 and len(vk.sent) == 1
    changed(conn, mine, vk, card_fields, mine, client_name="Пётр")                   # a change of this save: only it
    [(_p, _m, text)] = vk.edits
    assert "Покупатель Пётр" in text and "Кол-во 4 шт" in text and "Кол-во 3 шт" not in text


def test_a_value_filled_in_edits_the_message_without_a_note(tmp_path):
    conn, mine, vk = told(tmp_path)
    template = "Заказ {webmim_number}\nПеремещение {transfer_number}"
    vk_point_fix.remember(conn, mine, "555", 300, "(бот) Заказ 5001\nПеремещение ", vk_point_fix.TASK, NOW, template=template)
    conn.commit()
    changed(conn, mine, vk, card_fields, mine, transfer_number="П-77")
    assert vk.edits == [("555", 300, "(бот) Заказ 5001\nПеремещение П-77")] and len(vk.sent) == 1
    [event] = notify_events(conn, mine)
    assert "дополнено: номер перемещения" in event["text"] and event["level"] == "grey"
    changed(conn, mine, vk, card_fields, mine, transfer_number="П-78")               # a value put in place of another: the note
    assert len(vk.edits) == 2 and len(vk.sent) == 2 and "номер перемещения П-78" in vk.sent[1][1]


def test_a_closed_order_is_never_corrected(tmp_path):
    conn, mine, vk = told(tmp_path)
    conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE kind != 'open' ORDER BY id LIMIT 1) WHERE id = ?", (mine,))
    conn.commit()
    changed(conn, mine, vk, card_fields, mine, client_name="Пётр", delivery_date="2026-10-26")
    assert vk.tries == 0 and len(vk.sent) == 1 and notify_events(conn, mine) == []


def test_an_old_message_gets_the_note_only_for_a_value_it_has(tmp_path):
    conn, mine, vk = told(tmp_path)
    conn.execute("UPDATE vk_point_message SET template = NULL, vals = NULL, wrap = NULL")
    conn.commit()
    changed(conn, mine, vk, card_fields, mine, delivery_date="2026-10-26")            # the message has no date
    assert vk.tries == 0 and len(vk.sent) == 1 and notify_events(conn, mine) == []
    changed(conn, mine, vk, card_fields, mine, webmim_number="5002")                  # it has the number
    assert vk.tries == 0 and len(vk.sent) == 2 and "номер заказа у поставщика 5002" in vk.sent[1][1]


def test_a_message_another_worker_took_is_not_edited_twice(tmp_path, monkeypatch):
    conn, mine, vk = told(tmp_path)
    real = vk_point_fix.templates.values

    def values(c, card, now=None):                                # the other worker writes the row between the read and the claim
        if now == NOW:
            c.execute("UPDATE vk_point_message SET vals = '{}' WHERE order_id = ?", (mine,))
        return real(c, card, now)
    before = vk_point_fix.snapshot(conn, mine, LATER)
    qty(conn, item(conn, mine)["id"], 3)
    monkeypatch.setattr(vk_point_fix.templates, "values", values)
    vk_point_fix.order_changed(conn, mine, before, LATER, "owner", vk.send, vk.edit)
    assert vk.tries == 0 and len(vk.sent) == 1
