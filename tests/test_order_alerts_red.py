"""Red records «an error, somebody has to act» in the order journal: a refused move of the stage, a reply or a photo that did not reach the client;
and green ones for what did reach them."""
from __future__ import annotations

import io
import sqlite3

import pytest

from test_chats_sync import FakeClient, chat, db as chat_db, msg, NOW as CHAT_NOW, TS
from tires_sync import chat_deals as deals, chats, order_bulk as bulk, order_engine as engine, order_rules as rules, orders, store
from tires_sync.avito import AvitoError

NOW = "2026-10-20T12:00:00"


def db(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    return conn


def stage(conn, code):
    return next(s["id"] for s in orders.list_stages(conn) if s["code"] == code)


def reds(conn, card_id):
    return [e for e in orders.list_events(conn, card_id) if e["level"] == "red"]


def test_a_refused_move_is_a_red_record_with_what_to_fill_and_the_same_error_goes_up(tmp_path):
    conn = db(tmp_path)
    card = orders.create_card(conn, {"client_name": "Иван"}, NOW, "owner")
    with pytest.raises(orders.OrderError) as err:
        orders.move_card(conn, card, stage(conn, "placed"), NOW, "pavel", changes={"comment": "позвонить"})
    assert "заполните" in str(err.value)
    [red] = reds(conn, card)
    assert red["kind"] == "refused" and red["author"] == "pavel" and str(err.value) in red["text"] and red["text"].startswith("Перевод не выполнен")
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "new" and got["comment"] == ""                    # the fields that came with the move are rolled back all the same


def test_a_move_refused_for_a_recorded_sale_is_red_too(tmp_path):
    conn = db(tmp_path)
    card = orders.create_card(conn, {"client_name": "Иван"}, NOW, "owner")
    orders.move_card(conn, card, stage(conn, "cancel"), NOW, "pavel", changes={"cancel_reason": "дубль"})
    conn.execute("UPDATE order_card SET sale_ids = '[7]' WHERE id = ?", (card,))
    conn.commit()
    with pytest.raises(orders.OrderError, match="записана продажа"):
        orders.move_card(conn, card, stage(conn, "new"), NOW, "pavel")
    [red] = reds(conn, card)
    assert "записана продажа" in red["text"]


def test_a_move_without_a_journal_writes_nothing_and_a_move_that_is_allowed_writes_no_red(tmp_path):
    conn = db(tmp_path)
    card = orders.create_card(conn, {"client_name": "Иван"}, NOW, "owner")
    with pytest.raises(orders.OrderError):
        orders.move_card(conn, card, stage(conn, "placed"), NOW, "pavel", journal=False)
    assert reds(conn, card) == []
    assert orders.move_card(conn, card, stage(conn, "cancel"), NOW, "pavel", changes={"cancel_reason": "дубль"})
    assert reds(conn, card) == []


def test_a_rule_that_cannot_move_the_card_leaves_one_red_record_not_two(tmp_path):
    conn = db(tmp_path)
    bare = orders.create_card(conn, {"client_name": "Пётр"}, NOW, "owner")
    rules.add_rule(conn, stage(conn, "new"), "enter", 0, "move", None, str(stage(conn, "placed")), "auto", None)
    engine.fire_enter(conn, bare, NOW)
    [red] = reds(conn, bare)
    assert red["kind"] == "rule" and "заполните" in red["text"]


@pytest.mark.parametrize("how", ["run", "undo"])
def test_the_bulk_move_asks_for_no_journal(tmp_path, monkeypatch, how):
    path = tmp_path / "t.db"
    conn = store.connect(str(path))
    orders.ensure_pipeline(conn, NOW)
    cards = [orders.create_card(conn, {"client_name": "Клиент %d" % n}, NOW, "owner") for n in range(2)]
    cancel = stage(conn, "cancel")
    bulk.run(conn, path, cards, cancel, "дубль", bulk.check(conn, cards, cancel, "дубль")["sign"], NOW, "owner")
    calls = []
    real = orders.move_card

    def refusing(*args, **kwargs):
        calls.append(kwargs.get("journal", True))
        kwargs.setdefault("journal", True)
        if kwargs["journal"]:
            raise AssertionError("the bulk move must not write refusals to the journal")
        raise orders.OrderError("Чтобы перейти, заполните: точка.")
    monkeypatch.setattr(orders, "move_card", refusing)
    if how == "run":
        fresh = [orders.create_card(conn, {"client_name": "Ещё"}, NOW, "owner")]
        result = bulk.run(conn, path, fresh, cancel, "дубль", bulk.check(conn, fresh, cancel, "дубль")["sign"], "2026-10-20T12:05:00", "owner")
    else:
        [run] = bulk.runs(conn)
        result = bulk.undo(conn, path, run["id"], cards, "2026-10-20T12:05:00", "owner")
    monkeypatch.setattr(orders, "move_card", real)
    assert calls and calls == [False] * len(calls)
    assert result["skipped"] == len(calls)


# --- the chat: a reply and a photo to the client ---

def _chat_with_deal(tmp_path):
    conn, account = chat_db(tmp_path)
    orders.ensure_pipeline(conn, CHAT_NOW)
    chats.sync_account(conn, FakeClient([[chat("c1", text="Хочу заказать")]], {"c1": [msg("m1", "Хочу заказать")]}), account, CHAT_NOW)
    conn.execute("UPDATE account SET chats_send = 1, chats_enabled = 1 WHERE id = ?", (account,))
    conn.commit()
    row = chats.list_chats(conn)[0]["id"]
    deals.create_deal(conn, row, CHAT_NOW, "pavel")
    return conn, row, chats.get_chat(conn, row)["order_id"]


class Sender:
    def __init__(self, send_error=None, upload_error=None):
        self.send_error, self.upload_error = send_error, upload_error

    def get_self(self, retry=True):
        return {"id": 1}

    def send_message(self, user_id, chat_id, text):
        if self.send_error:
            raise self.send_error
        return {"id": "m-out", "created": TS + 100}

    def upload_image(self, user_id, filename, data):
        if self.upload_error:
            raise self.upload_error
        return "img-1"

    def send_image(self, user_id, chat_id, image_id):
        if self.send_error:
            raise self.send_error
        return {"id": "m-img", "created": TS + 100}


def jpeg():
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (20, 10), (200, 10, 10)).save(out, "JPEG")
    return out.getvalue()


def _last(conn, order_id):
    return orders.list_events(conn, order_id)[0]


def test_a_sent_reply_and_a_sent_photo_are_green(tmp_path):
    conn, row, order_id = _chat_with_deal(tmp_path)
    chats.send_reply(conn, Sender(), row, "Есть в наличии", "tok-1", CHAT_NOW, "pavel")
    assert (_last(conn, order_id)["text"], _last(conn, order_id)["level"]) == ("Отправлен ответ клиенту: Есть в наличии", "green")
    chats.send_image(conn, Sender(), row, jpeg(), "tok-2", CHAT_NOW, "pavel")
    assert (_last(conn, order_id)["text"], _last(conn, order_id)["level"]) == ("Отправлено фото клиенту", "green")


@pytest.mark.parametrize("error, raised, start", [
    (AvitoError(403, None, "forbidden"), chats.ChatError, "Сообщение клиенту не отправлено: Авито не принял сообщение (код 403: forbidden)"),
    (AvitoError(502, None, "bad gateway"), chats.ChatSendUnknown, "Сообщение клиенту не подтверждено: Неизвестно, ушло ли сообщение (ответ Авито: код 502: bad gateway)"),
    (TimeoutError("timeout"), chats.ChatSendUnknown, "Сообщение клиенту не подтверждено: Неизвестно, ушло ли сообщение (нет ответа от Авито)"),
])
def test_a_reply_that_did_not_reach_the_client_is_red_and_the_error_still_goes_up(tmp_path, error, raised, start):
    conn, row, order_id = _chat_with_deal(tmp_path)
    with pytest.raises(raised) as err:
        chats.send_reply(conn, Sender(send_error=error), row, "Есть в наличии", "tok-1", CHAT_NOW, "pavel")
    last = _last(conn, order_id)
    assert (last["kind"], last["level"], last["author"]) == ("chat", "red", "pavel")
    assert last["text"].startswith(start) and last["text"].endswith(str(err.value))
    assert "Есть в наличии" not in last["text"]                                      # the text of the reply stays out of the red record


@pytest.mark.parametrize("sender, raised, start", [
    (Sender(upload_error=AvitoError(400, None, "bad")), chats.ChatError, "Фото клиенту не отправлено: Авито не принял фото (код 400: bad)"),
    (Sender(upload_error=OSError("cut")), chats.ChatError, "Фото клиенту не отправлено: Фото не загружено в Авито"),
    (Sender(send_error=AvitoError(403, None, "forbidden")), chats.ChatError, "Фото клиенту не отправлено: Авито не принял сообщение с фото (код 403: forbidden)"),
    (Sender(send_error=AvitoError(500, None, "oops")), chats.ChatSendUnknown, "Фото клиенту не подтверждено: Неизвестно, ушло ли фото (ответ Авито: код 500: oops)"),
    (Sender(send_error=TimeoutError("t")), chats.ChatSendUnknown, "Фото клиенту не подтверждено: Неизвестно, ушло ли фото (нет ответа от Авито)"),
])
def test_a_photo_that_did_not_reach_the_client_is_red_and_the_error_still_goes_up(tmp_path, sender, raised, start):
    conn, row, order_id = _chat_with_deal(tmp_path)
    with pytest.raises(raised) as err:
        chats.send_image(conn, sender, row, jpeg(), "tok-1", CHAT_NOW, "pavel")
    last = _last(conn, order_id)
    assert (last["kind"], last["level"]) == ("chat", "red")
    assert last["text"].startswith(start) and last["text"].endswith(str(err.value))


def test_refusals_before_sending_and_chats_without_a_deal_write_nothing(tmp_path):
    conn, row, order_id = _chat_with_deal(tmp_path)
    before = len(orders.list_events(conn, order_id))
    for bad in ("", "x" * 2001):
        with pytest.raises(chats.ChatError):
            chats.send_reply(conn, Sender(), row, bad, "tok-%d" % len(bad), CHAT_NOW, "pavel")      # the form answers at once: nothing to act on later
    with pytest.raises(chats.ChatError):
        chats.send_image(conn, Sender(), row, b"not a picture", "tok-p", CHAT_NOW, "pavel")
    assert len(orders.list_events(conn, order_id)) == before
    conn.execute("UPDATE avito_chat SET order_id = NULL WHERE id = ?", (row,))
    conn.commit()
    with pytest.raises(chats.ChatError):
        chats.send_reply(conn, Sender(send_error=AvitoError(403, None, "x")), row, "Привет", "tok-x", CHAT_NOW, "pavel")
    assert conn.execute("SELECT COUNT(*) FROM order_event WHERE level = 'red'").fetchone()[0] == 0


def test_a_locked_journal_never_hides_the_send_error(tmp_path, monkeypatch):
    conn, row, order_id = _chat_with_deal(tmp_path)
    card = orders.create_card(conn, {"client_name": "Иван"}, NOW, "owner")

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(orders, "add_event", broken)
    with pytest.raises(chats.ChatError, match="Авито не принял сообщение"):
        chats.send_reply(conn, Sender(send_error=AvitoError(403, None, "x")), row, "Привет", "tok-1", CHAT_NOW, "pavel")
    with pytest.raises(orders.OrderError, match="заполните"):
        orders.move_card(conn, card, stage(conn, "placed"), NOW, "pavel")


def test_a_refused_move_is_not_taken_for_a_move(tmp_path):
    from tires_sync import order_cleanup
    conn = db(tmp_path)
    card = orders.create_card(conn, {"client_name": "Иван"}, NOW, "owner")
    with pytest.raises(orders.OrderError):
        orders.move_card(conn, card, stage(conn, "placed"), NOW, "owner")
    assert not conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND kind = 'stage'", (card,)).fetchone()
    assert order_cleanup.keep_reason(conn, orders.get_card(conn, card), NOW) != "moved"


def test_a_move_that_goes_through_closes_the_refusals_of_the_order_for_everybody(tmp_path):
    from tires_sync import order_alerts as alerts
    conn = db(tmp_path)
    card = orders.create_card(conn, {"client_name": "Иван"}, NOW, "owner")
    other = orders.create_card(conn, {"client_name": "Пётр"}, NOW, "owner")
    for cid in (card, other):
        with pytest.raises(orders.OrderError):
            orders.move_card(conn, cid, stage(conn, "placed"), NOW, "pavel")
    refused = lambda login, cid: [e for e in alerts.open_alerts(conn, login, cid) if e["kind"] == "refused"]
    assert len(refused("owner", card)) == 1 and len(refused("worker2", card)) == 1
    assert orders.move_card(conn, card, stage(conn, "cancel"), "2026-10-20T12:10:00", "pavel", changes={"cancel_reason": "дубль"})
    assert refused("owner", card) == [] and refused("worker2", card) == []              # the cause is gone: closed for everybody
    assert conn.execute("SELECT author FROM order_event_ack WHERE login = ?", (alerts.EVERYBODY,)).fetchall() == [("pavel",)]
    assert len(refused("owner", other)) == 1                                          # another order keeps its own
    assert orders.move_card(conn, card, stage(conn, "new"), "2026-10-20T12:20:00", "pavel")
    with pytest.raises(orders.OrderError):
        orders.move_card(conn, card, stage(conn, "placed"), "2026-10-20T12:30:00", "pavel")
    [again] = refused("owner", card)                                                  # a new refusal is a new red record
    assert again["at"] == "2026-10-20T12:30:00"


def test_a_move_that_does_not_happen_closes_nothing(tmp_path):
    from tires_sync import order_alerts as alerts
    conn = db(tmp_path)
    card = orders.create_card(conn, {"client_name": "Иван"}, NOW, "owner")
    with pytest.raises(orders.OrderError):
        orders.move_card(conn, card, stage(conn, "placed"), NOW, "pavel")
    assert orders.move_card(conn, card, stage(conn, "new"), NOW, "pavel", changes={"comment": "x"}) is False   # already there: no move
    assert len([e for e in alerts.open_alerts(conn, "owner", card) if e["kind"] == "refused"]) == 1
