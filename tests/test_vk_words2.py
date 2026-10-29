"""The words of the real point messages (the owner 2026-10-25, the audit of «Входящие»): «Привезли», «Пришли», «Шины приехали», «5245392 шина
приехала!» are «приехал»; «Забрала», «выдала», «отдал клиенту» are «выдал»; «Возврат отдал», «отправлен на возврат» are a new command: the
tyres went back to the supplier — the order goes to «Возврат» (only from «Товар на точке», «На ОТХ», «Отказ/невыкуп»), the client is told
nothing. Every rule of doubt (a negation, «?», a part, a defect, «клиент», two commands, two numbers) holds for the new words too."""
from __future__ import annotations

import pytest

from test_order_manager import NOW, card_at, setup
from test_vk_commands import GROUP, SENT, hook, ready, to_client
from tires_sync import order_manager as mgr, orders

PLACED = "Заказ 5000100 оформлен на перемещение.\nТерритория СПб, ул. Примерова, 35А\n\nNokian 205/55 R16 × 4\n\nПокупатель Татьяна +7 900 111 22 33"


def _webmim(conn, pid, number, code="moving"):
    card = card_at(conn, pid, code)
    orders.update_card(conn, card, {"webmim_number": str(number)}, NOW, "o")
    return card


def _stage_id(conn, code):
    return conn.execute("SELECT id FROM order_stage WHERE code = ?", (code,)).fetchone()[0]


def _code(conn, card):
    return orders.get_card(conn, card)["stage"]["code"]


def _events(conn, card):
    return [(e[0], e[1]) for e in conn.execute("SELECT text, level FROM order_event WHERE order_id = ? ORDER BY id", (card,))]


@pytest.mark.parametrize("text, word, number", [
    ("Привезли", "привезли", None),
    ("привезли 5245392", "привезли", 5245392),
    ("5245392 привезли", "привезли", 5245392),
    ("Пришли", "пришли", None),
    ("5245392 пришли", "пришли", 5245392),
    ("Шины приехали", "приехали", None),
    ("шина приехала", "приехала", None),
    ("Приехала", "приехала", None),
    ("5245392 шина приехала!", "приехала", 5245392),
    ("шины 5245392 приехали", "приехали", 5245392),
    ("Колёса приехали 5245392", "приехали", 5245392),
    ("комплект приехал", "приехал", None),
    ("приехали 5259052 шины", "приехали", 5259052),
    ("5259052 приехали шины", "приехали", 5259052),
    ("Забрала", "забрала", None),
    ("5244244 забрали", "забрали", 5244244),
    ("выдала 5244244", "выдала", 5244244),
    ("отдал клиенту", "отдал клиенту", None),
    ("Отдала клиенту 5244244", "отдал клиенту", 5244244),
    ("5244244 отдали клиенту", "отдал клиенту", 5244244),
    ("Возврат отдал", mgr.RETURN, None),
    ("№5259052 возврат отдал", mgr.RETURN, 5259052),
    ("отдал на возврат 5259052", mgr.RETURN, 5259052),
    ("отдала на возврат", mgr.RETURN, None),
    ("отправлен на возврат", mgr.RETURN, None),
    ("5259052 отправлено на возврат", mgr.RETURN, 5259052),
    ("отправили на возврат", mgr.RETURN, None),
])
def test_the_words_of_the_points_are_read(text, word, number):
    assert mgr._word(text) == word and mgr.order_number(text) == number, text


@pytest.mark.parametrize("text", ["Привезли", "привезли!", "Пришли", "пришли все", "Шины приехали", "шина приехала", "Приехала",
                                  "привезли шины", "пришли колёса, 4 шт", "привезли на точку", "Добрый день, привезли", "приехала шина"])
def test_an_arrival_in_new_words_in_reply_moves_the_order(tmp_path, text):
    conn, pid, _ = setup(tmp_path)
    card = _webmim(conn, pid, 5259052)
    assert mgr.is_command(conn, text), text
    mgr.dispatch(conn, "a", "111", text, 5259052, NOW)
    assert _code(conn, card) == "at_point", text


def test_an_arrival_with_the_number_before_or_after_moves_the_order(tmp_path):
    conn, pid, _ = setup(tmp_path)
    for n, text in enumerate(("5245392 шина приехала!", "привезли {}", "{} пришли", "шины {} приехали", "приехали {} шины", "Приехала {}")):
        number = 5245392 + n
        card = _webmim(conn, pid, number)
        mgr.dispatch(conn, "n%d" % n, "111", text.format(number) if "{}" in text else text, None, NOW)
        assert _code(conn, card) == "at_point", text


def test_the_tyre_arrived_in_reply_tells_the_client_once(tmp_path):
    """The case of the audit: «5245392 шина приехала!» — here in reply to the order line, with its own number: «можно забирать» once."""
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    hook(app, conn, "Шины приехали", reply={"from_id": -int(GROUP), "text": PLACED})
    assert _code(conn, card) == "at_point" and len(to_client(app)) == 1


@pytest.mark.parametrize("text", [
    # «пришли» is also «send», «приехала» is also a car or a client: after the new words only the order, the tyres, «все», «шт»
    "пришли фото", "Пришли номер", "пришли за заказом", "пришли клиенты", "приехала машина", "приехала клиентка", "привезли не те",
    "привезли другие", "привезли летние вместо зимних", "пришли, но не все", "привезли 3 из 4", "привезли?", "пришли?", "привезли и выдали",
    "шины приехали, брак", "шины не приехали", "шина приехала?", "шины приехали только 2", "привезли 5259052 и 5253058", "Пришли 5 259 052",
    "Клиенты пришли", "шины", "Шины 5259052", "приехали 5259052 шины 4", "приехала, одна битая", "привезли, но одна повреждена",
    "забрала не всё", "выдала 2 из 4", "отдал клиенту?", "отдал", "отдал 5259052", "приехали, отдал клиенту",
    "возврат отдал?", "возврат не отдал", "отдал на возврат 2 из 4", "возврат отдал клиенту", "клиент вернул, отдал на возврат",
    "приехали, возврат 5259052", "отдал на возврат и выдал", "лтправлен на возврат", "возврат", "возврат 5259052", "на возврат",
    "отправил фото на возврат?",
    # review 2026-10-26: «пришли нам» is «send us», «отдал клиенту деньги» is money
    "пришли нам шины", "пришли нам", "пришли нам комплект", "пришли к нам", "пришли нам шины по заказу 5259052", "отдал клиенту деньги",
    "отдала клиенту предоплату", "отдал клиенту сдачу", "отдал клиенту чек",
])
def test_doubtful_new_words_are_never_a_command(tmp_path, text):
    conn, pid, _ = setup(tmp_path)
    card = _webmim(conn, pid, 5259052, "at_point" if any(w in text for w in ("забрала", "выдала", "отдал", "возврат")) else "moving")
    before = _code(conn, card)
    assert not mgr.is_command(conn, text), text
    mgr.dispatch(conn, "x", "111", text, 5259052, NOW)                                             # even in reply to the order
    assert _code(conn, card) == before, text


@pytest.mark.parametrize("text", ["Забрала", "выдала", "отдал клиенту", "отдала клиенту", "отдали клиенту", "забрали"])
def test_an_issue_in_new_words_moves_the_order_to_done(tmp_path, text):
    conn, pid, _ = setup(tmp_path)
    card = _webmim(conn, pid, 5244244, "at_point")
    assert mgr.is_command(conn, text), text
    mgr.dispatch(conn, "i", "111", text, 5244244, NOW)
    assert _code(conn, card) == "done", text
    card = _webmim(conn, pid, 5244245, "at_point")
    mgr.dispatch(conn, "j", "111", "5244245 " + text, None, NOW)
    assert _code(conn, card) == "done", text


def _with_amount(conn, card, amount=16000):
    orders.update_card(conn, card, {"return_amount": amount}, NOW, "o")


@pytest.mark.parametrize("code", ["at_point", "oth", "cancel"])
def test_a_return_with_the_amount_moves_the_order_to_return_and_fills_the_kind_and_the_reason(tmp_path, code):
    """«cancel» with the reason «не выкупил» was «Отказ/невыкуп» before the merge (the owner 2026-10-29)."""
    conn, pid, _ = setup(tmp_path)
    card = _webmim(conn, pid, 5259052, "at_point" if code == "cancel" else code)
    if code == "cancel":
        orders.move_card(conn, card, _stage_id(conn, "cancel"), NOW, "o", {"cancel_reason": orders.NOT_BOUGHT})
    _with_amount(conn, card)
    answer = mgr.dispatch(conn, "r", "111", "№5259052 возврат отдал", None, NOW)
    got = orders.get_card(conn, card)
    assert answer == "Заказ №5259052: отмечено — шины отданы на возврат"
    assert got["stage"]["code"] == "return" and got["return_kind"] == "supplier" and got["return_reason"] == mgr.RETURN_REASON
    assert float(got["return_amount"]) == 16000                                                    # the amount is never touched
    assert mgr.RETURN_LINE in [t for t, _ in _events(conn, card)]


def test_a_return_without_the_amount_leaves_the_stage_and_asks_the_responsible(tmp_path):
    """The lead 2026-10-26 (A): the kind and the reason may be filled by the bot, the amount never; without it the stage stays, the journal
    has the point's line and a yellow record for the responsible; the point hears «отмечено»."""
    conn, pid, _ = setup(tmp_path)
    card = _webmim(conn, pid, 5259052, "at_point")
    answer = mgr.dispatch(conn, "r", "111", "отдал на возврат", 5259052, NOW)
    got = orders.get_card(conn, card)
    assert answer == "Заказ №5259052: отмечено — шины отданы на возврат"
    assert got["stage"]["code"] == "at_point" and float(got["return_amount"]) == 0
    assert got["return_kind"] == "supplier" and got["return_reason"] == mgr.RETURN_REASON          # kept: only the amount is left to fill
    events = _events(conn, card)
    assert mgr.RETURN_LINE in [t for t, _ in events]
    assert [lvl for t, lvl in events if "заполните сумму возврата и переведите в «Возврат»" in t] == ["yellow"]
    assert not [t for t, lvl in events if lvl == "red"]
    again = mgr.dispatch(conn, "r2", "111", "возврат отдал 5259052", None, NOW)                     # a repeat adds no second record
    assert "уже" in again and len(_events(conn, card)) == len(events)
    orders.update_card(conn, card, {"return_amount": 16000}, NOW, "manager")                       # the manager fills the amount and moves it
    orders.move_card(conn, card, next(s["id"] for s in orders.list_stages(conn) if s["code"] == "return"), NOW, "manager")
    assert _code(conn, card) == "return"


def test_a_return_kind_set_by_a_manager_is_kept(tmp_path):
    conn, pid, _ = setup(tmp_path)
    card = _webmim(conn, pid, 5259052, "at_point")
    orders.update_card(conn, card, {"return_kind": "client", "return_reason": "клиент вернул", "return_amount": 100}, NOW, "o")
    mgr.dispatch(conn, "r", "111", "отправлен на возврат 5259052", None, NOW)
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "return" and got["return_kind"] == "client" and got["return_reason"] == "клиент вернул"


@pytest.mark.parametrize("code", ["placed", "moving", "done"])
def test_a_return_from_a_stage_without_the_tyres_at_the_point_is_a_question(tmp_path, code):
    conn, pid, _ = setup(tmp_path)
    card = _webmim(conn, pid, 5259052, code)
    _with_amount(conn, card)
    mgr.dispatch(conn, "r", "111", "возврат отдал 5259052", None, NOW)
    assert _code(conn, card) == code and mgr.new_count(conn) == 1


def test_a_return_already_marked_is_not_repeated(tmp_path):
    conn, pid, _ = setup(tmp_path)
    card = _webmim(conn, pid, 5259052, "at_point")
    _with_amount(conn, card)
    mgr.dispatch(conn, "r1", "111", "возврат отдал 5259052", None, NOW)
    answer = mgr.dispatch(conn, "r2", "111", "возврат отдал 5259052", None, NOW)
    assert _code(conn, card) == "return" and "уже" in answer and mgr.new_count(conn) == 0


def test_a_return_never_tells_the_client(tmp_path):
    """«Возврат отдал» in reply to the order line on «Товар на точке»: no «можно забирать», no message to the client at all."""
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    orders.move_card(conn, card, next(s["id"] for s in orders.list_stages(conn) if s["code"] == "at_point"), NOW, "o")
    before = len(to_client(app))
    hook(app, conn, "Возврат отдал", reply={"from_id": -int(GROUP), "text": PLACED})
    hook(app, conn, "отправлен на возврат 5000100", event="e2")
    assert len(to_client(app)) == before
    assert conn.execute("SELECT word FROM order_command_log").fetchone()[0] == mgr.RETURN


def test_the_owner_confirms_a_disputed_return(tmp_path):
    conn, pid, _ = setup(tmp_path)
    card = _webmim(conn, pid, 5259052, "at_point")
    _with_amount(conn, card)
    mgr.dispatch(conn, "r", "111", "возврат отдал", None, NOW)                                       # no number: a question
    [item] = mgr.list_inbox(conn)
    reply = mgr.confirm(conn, item["id"], "5259052", NOW, "owner")
    assert _code(conn, card) == "return" and "шины отданы на возврат" in reply
    other = _webmim(conn, pid, 5259053, "moving")
    mgr.dispatch(conn, "r2", "111", "возврат отдал", None, NOW)
    item = mgr.list_inbox(conn)[0]
    with pytest.raises(orders.OrderError):
        mgr.confirm(conn, item["id"], "5259053", NOW, "owner")
    assert _code(conn, other) == "moving"


def test_the_hint_asks_for_a_reply_or_a_number():
    assert "Лучше всего — ответом на сообщение о заказе или с номером заказа" in mgr.HINT


def test_a_return_of_an_order_cancelled_not_for_not_buying_is_a_question(tmp_path):
    """«Отмена / невыкуп» with another reason: the tyres never came to the point, «возврат отдал» does not fit."""
    conn, pid, _ = setup(tmp_path)
    card = _webmim(conn, pid, 5259053, "at_point")
    orders.move_card(conn, card, _stage_id(conn, "cancel"), NOW, "o", {"cancel_reason": "отменил клиент"})
    _with_amount(conn, card)
    mgr.dispatch(conn, "r", "111", "№5259053 возврат отдал", None, NOW)
    assert orders.get_card(conn, card)["stage"]["code"] == "cancel"
