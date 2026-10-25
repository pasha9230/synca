"""Deleting an order or a chat in the panel: it leaves our base whole, can be put back, and a deleted chat is not read back from Avito."""
from __future__ import annotations

import json

import pytest

from panel_helpers import login, make_app, post
from test_chat_deals import Fake
from tires_sync import chat_deals as deals, chats, connections, order_wait, orders, store, trash

NOW = "2026-10-19T12:00:00"
LATER = "2026-10-19T13:00:00"


def base(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    orders.ensure_pipeline(conn, NOW)
    account = connections.create_account(conn, "Основной", NOW, "o")
    connections.set_modes(conn, account, True, True, NOW, "o")
    fake = Fake()
    fake.add("c1", "Татьяна", [("in", "Здравствуйте, есть 185/65 R15?"), ("out", "Есть")])
    chats.sync_account(conn, fake, account, NOW)
    chat = chats.list_chats(conn)[0]
    card = deals.create_deal(conn, chat["id"], NOW, "o")
    orders.add_item(conn, card, goods_id="1", name="Nokian 205/55 R16", brand_key="nokian", season="winter", diameter=16, territory="1",
                    qty=4, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    order_wait.add_want(conn, card, {"brand": "Nokian", "width": "205", "profile": "55", "radius": "16", "qty": "4"}, NOW, "o")
    conn.execute("INSERT INTO order_field_value (order_id, field, value) VALUES (?, 'car', 'Лада')", (card,))
    conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, status, title, created_at) VALUES (?, 1, ?, 'notify', 'pending', 'Сообщить', ?)",
                 (card, NOW, NOW))
    item = conn.execute("SELECT id FROM order_item WHERE order_id = ?", (card,)).fetchone()[0]       # the rows of «На ОТХ» (хвост З16)
    conn.execute("INSERT INTO order_oth_client (order_id, client_id, how, linked_at, detached_at, detached_by) VALUES (?, 1, 'chat', ?, ?, 'o')", (card, NOW, NOW))
    conn.execute("INSERT INTO oth_return (item_id, order_id, qty, at, author) VALUES (?, ?, 4, ?, 'o')", (item, card, NOW))
    conn.commit()
    return conn, fake, account, chats.get_chat(conn, chat["id"]), card


def counts(conn, card):
    return {t: conn.execute("SELECT COUNT(*) FROM %s WHERE %s = ?" % (t, c), (card,)).fetchone()[0]
            for t, c in trash.ORDER_TABLES + (("order_card", "id"),)}


def test_a_deleted_order_leaves_whole_and_comes_back_with_the_same_number_and_its_chat(tmp_path):
    conn, fake, account, chat, card = base(tmp_path)
    before = counts(conn, card)
    assert all(before.values())
    record = trash.delete_order(conn, card, LATER, "owner")
    assert not any(counts(conn, card).values())
    assert chats.get_chat(conn, chat["id"])["order_id"] is None and chats.get_chat(conn, chat["id"])["unlinked"] == 1     # the chat stays, not linked again
    deals.process_all(conn, LATER, auto_create=True)
    assert chats.get_chat(conn, chat["id"])["order_id"] is None                                                             # no new deal made for it
    [row] = trash.list_deleted(conn)
    assert row["id"] == record and row["kind"] == "order" and "Заказ №%d" % card in row["title"] and row["deleted_by"] == "owner"
    assert conn.execute("SELECT action FROM audit WHERE action = 'order_delete'").fetchone()
    with pytest.raises(orders.OrderError):
        orders.get_card(conn, card)

    assert trash.restore_order(conn, record, LATER, "owner") == card
    after = counts(conn, card)
    assert after == dict(before, order_event=before["order_event"] + 1)                                                   # plus «восстановлен»
    restored = orders.get_card(conn, card)
    assert restored["items"][0]["name"] == "Nokian 205/55 R16" and restored["wants"] and restored["avito_chat"] == deals.ref(chat["id"])
    assert chats.get_chat(conn, chat["id"])["order_id"] == card and chats.get_chat(conn, chat["id"])["unlinked"] == 0
    assert trash.list_deleted(conn) == []
    with pytest.raises(orders.OrderError):
        trash.restore_order(conn, record, LATER, "owner")                                                                    # once only


def test_an_order_with_a_recorded_sale_is_not_deleted(tmp_path):
    conn, fake, account, chat, card = base(tmp_path)
    conn.execute("UPDATE order_card SET sale_ids = '[5]' WHERE id = ?", (card,))
    conn.commit()
    with pytest.raises(orders.OrderError, match="сторнируйте"):
        trash.delete_order(conn, card, LATER, "owner")
    assert counts(conn, card)["order_card"] == 1 and trash.list_deleted(conn) == []


def test_an_order_whose_stage_and_point_were_deleted_meanwhile_comes_back_on_the_first_stage(tmp_path):
    conn, fake, account, chat, card = base(tmp_path)
    stage = orders.add_stage(conn, "Временный", "#123456", "open", "", 0)
    conn.execute("UPDATE order_card SET stage_id = ?, point_id = 999 WHERE id = ?", (stage, card))
    conn.commit()
    record = trash.delete_order(conn, card, LATER, "owner")
    orders.delete_stage(conn, stage)
    trash.restore_order(conn, record, LATER, "owner")
    restored = orders.get_card(conn, card)
    assert restored["stage"]["code"] == "new" and restored["point_id"] is None
    assert "этап удалён" in orders.list_events(conn, card)[0]["text"]


def test_a_deleted_chat_is_not_read_back_from_avito_and_comes_back_with_what_was_written_meanwhile(tmp_path):
    conn, fake, account, chat, card = base(tmp_path)
    messages = len(chats.list_messages(conn, chat["id"]))
    record = trash.delete_chat(conn, chat["id"], LATER, "owner")
    assert chats.get_chat(conn, chat["id"]) is None and chats.list_messages(conn, chat["id"]) == []
    assert orders.get_card(conn, card)["avito_chat"] == ""                                                                  # the deal stays, without the chat
    fake.add("c1", "Татьяна", [("in", "Здравствуйте, есть 185/65 R15?"), ("out", "Есть"), ("in", "Беру")], updated=1_791_999_999)
    stats = chats.sync_account(conn, fake, account, LATER)
    assert chats.list_chats(conn) == [] and stats["new_chats"] == 0                                                        # not back with the next reading
    assert conn.execute("SELECT COUNT(*) FROM avito_message").fetchone()[0] == 0

    assert trash.restore_chat(conn, record, LATER, "owner") == chat["id"]
    back = chats.get_chat(conn, chat["id"])
    assert back["order_id"] == card and len(chats.list_messages(conn, chat["id"])) == messages
    assert orders.get_card(conn, card)["avito_chat"] == deals.ref(chat["id"])
    chats.sync_account(conn, fake, account, LATER)
    assert "Беру" in [m["text"] for m in chats.list_messages(conn, chat["id"])]                                            # read again after it came back


def test_a_chat_deleted_while_its_messages_are_read_leaves_no_messages_behind(tmp_path):
    conn, fake, account, chat, card = base(tmp_path)
    other = store.connect(tmp_path / "t.db")
    real = fake.chat_messages

    def messages(uid, chat_id, **kw):
        trash.delete_chat(other, chat["id"], LATER, "owner")                    # the panel deletes it while the reading waits for Avito
        return real(uid, chat_id, **kw)

    fake.chat_messages = messages
    fake.add("c1", "Татьяна", [("in", "Здравствуйте, есть 185/65 R15?"), ("out", "Есть"), ("in", "Беру")], updated=1_791_999_999)
    chats.sync_account(conn, fake, account, LATER)
    assert conn.execute("SELECT COUNT(*) FROM avito_message").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM avito_chat").fetchone()[0] == 0


def test_a_chat_whose_deal_was_deleted_comes_back_without_a_deal(tmp_path):
    conn, fake, account, chat, card = base(tmp_path)
    record = trash.delete_chat(conn, chat["id"], LATER, "owner")
    data = json.loads(conn.execute("SELECT data FROM deleted_record WHERE id = ?", (record,)).fetchone()[0])
    assert data["chat"]["order_id"] == card
    trash.delete_order(conn, card, LATER, "owner")
    trash.restore_chat(conn, record, LATER, "owner")
    assert chats.get_chat(conn, chat["id"])["order_id"] is None


def setup_pages(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    account = connections.create_account(conn, "Основной", NOW, "o")
    connections.set_modes(conn, account, True, True, NOW, "o")
    fake = Fake()
    fake.add("c1", "Татьяна", [("in", "Здравствуйте")])
    chats.sync_account(conn, fake, account, NOW)
    chat = chats.list_chats(conn)[0]
    card = deals.create_deal(conn, chat["id"], NOW, "o")
    return client, tok, conn, chat, card


def test_the_panel_deletes_with_a_confirmation_and_restores_from_deleted(tmp_path):
    client, tok, conn, chat, card = setup_pages(tmp_path)
    page = client.get("/orders/%d" % card).get_data(as_text=True)
    assert 'action="/orders/%d/delete"' % card in page and "data-confirm-title=\"Удалить заказ №%d?" % card in page
    assert client.post("/orders/%d/delete" % card).status_code == 400                                                     # no CSRF token
    assert post(client, "/orders/%d/delete" % card, tok).headers["Location"].endswith("/orders")
    assert client.get("/orders/%d" % card).status_code == 404
    page = client.get("/deleted").get_data(as_text=True)
    assert "Заказ №%d" % card in page and "Восстановить" in page
    [record] = trash.list_deleted(conn)
    assert post(client, "/deleted/%d/restore" % record["id"], tok).headers["Location"].endswith("/orders/%d" % card)
    assert client.get("/orders/%d" % card).status_code == 200

    page = client.get("/chats/%d" % chat["id"]).get_data(as_text=True)
    assert 'action="/chats/%d/delete"' % chat["id"] in page and "data-confirm-title=\"Удалить чат Ч-%d?" % chat["id"] in page
    assert post(client, "/chats/%d/delete" % chat["id"], tok).status_code == 302
    assert client.get("/chats/%d" % chat["id"]).status_code == 404 and "Татьяна" not in client.get("/chats").get_data(as_text=True)
    assert "Татьяна" in client.get("/deleted?k=chat").get_data(as_text=True)
    assert "Татьяна" not in client.get("/deleted?k=order").get_data(as_text=True)
    [record] = trash.list_deleted(conn, "chat")
    assert post(client, "/deleted/%d/restore" % record["id"], tok).headers["Location"].endswith("/chats/%d" % chat["id"])
    assert "Татьяна" in client.get("/chats").get_data(as_text=True)
    assert post(client, "/deleted/%d/restore" % record["id"], tok).headers["Location"].endswith("/deleted")              # already back: a message


def test_a_chat_whose_deal_is_being_made_is_not_deleted(tmp_path):
    conn, fake, account, chat, card = base(tmp_path)
    conn.execute("UPDATE avito_chat SET order_id = -1 WHERE id = ?", (chat["id"],))
    conn.commit()
    with pytest.raises(orders.OrderError, match="создаётся сделка"):
        trash.delete_chat(conn, chat["id"], LATER, "owner")
    assert chats.get_chat(conn, chat["id"]) is not None


def test_an_order_whose_chat_was_deleted_meanwhile_comes_back_without_the_chat_field(tmp_path):
    conn, fake, account, chat, card = base(tmp_path)
    record = trash.delete_order(conn, card, LATER, "owner")
    trash.delete_chat(conn, chat["id"], LATER, "owner")
    trash.restore_order(conn, record, LATER, "owner")
    assert orders.get_card(conn, card)["avito_chat"] == "" and "не вернулся" in orders.list_events(conn, card)[0]["text"]
