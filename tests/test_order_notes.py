"""«Есть примечание» on the board and in the list of orders (the owner 2026-10-30): a note of a person (the comment of the order, a note of
the journal) — not the robot's, the system's, the bot's or an automatic record; the hint is the latest, its first 100 characters.
«Товар в наличии на точке» is shown for an open order only, never on «Выполнено», «Отмена / невыкуп», «Возврат»."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from panel_helpers import post
from test_oth_fixes import IN_STOCK, _quiet
from test_oth_sale import LATER, T, new_order, panel, stage, take
from tests.test_order_reserve import card_at
from tires_sync import order_notes, orders, orders_export

SRC = Path(__file__).resolve().parents[1] / "src" / "tires_sync"


def _new(conn, **fields):
    return orders.create_card(conn, dict({"client_name": "Иван", "client_phone": "+79001112233"}, **fields), T, "o")


# ---- «Есть примечание» --------------------------------------------------------------------------------------------------------------

def test_a_note_of_a_person_counts_the_bots_and_the_systems_do_not(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    person, bot, system, auto, plain = (_new(conn, client_phone="+7900111000%d" % i) for i in range(5))
    orders.add_note(conn, person, "Клиент просил позвонить после 18", T, "manager1")
    orders.add_note(conn, bot, "Ответ бота", T, "бот ВК")
    orders.add_note(conn, system, "Запись системы", T, "система")
    orders.add_event(conn, auto, T, "manager1", "note", "Возвращён после массового перевода от manager1")
    conn.commit()
    found = order_notes.latest(conn, [person, bot, system, auto, plain])
    assert found == {person: "Клиент просил позвонить после 18"}
    page = owner.get("/orders").get_data(as_text=True)
    assert page.count("Есть примечание") == 1 and 'title="Клиент просил позвонить после 18"' in page
    assert "Есть примечание" in owner.get("/orders?v=list").get_data(as_text=True)


def test_the_comment_counts_unless_the_source_wrote_it_and_the_hint_is_the_latest_cut_to_100(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    typed = _new(conn, comment="Нужен чек", client_phone="+79001110001")
    ad = _new(conn, comment="Объявление: Шины Ikon 205/55 https://avito.ru/1", client_phone="+79001110002")
    call = _new(conn, comment="Звонок 2026-10-30 12:00 на аккаунт «Основной» (принят)", client_phone="+79001110003")
    found = order_notes.latest(conn, [typed, ad, call])
    assert found == {typed: "Нужен чек"}
    orders.update_card(conn, ad, {"comment": "Объявление: оставить до пятницы"}, LATER, "manager1")     # a person changed it: it counts
    orders.update_card(conn, call, {"comment": "Звонок 2026-10-30 13:00 (пропущен)"}, LATER, "система")  # the system changed it: it does not
    long = "а" * 150
    orders.add_note(conn, typed, long, LATER, "manager1")                                                # newer than the comment
    found = order_notes.latest(conn, [typed, ad, call])
    assert found == {typed: "а" * 100 + "…", ad: "Объявление: оставить до пятницы"}
    orders.update_card(conn, ad, {"comment": ""}, LATER, "manager1")                                     # removed: the badge goes
    assert ad not in order_notes.latest(conn, [ad])


def test_every_automatic_note_the_panel_writes_is_known():
    """A record of the kind «note» the code writes by itself (not orders.add_note of a person) starts with order_notes.AUTO_NOTES."""
    found = []
    for path in SRC.rglob("*.py"):
        for m in re.finditer(r'add_event\([^)]*?"note",\s*"([^"]+)"', path.read_text(encoding="utf-8")):
            found.append(m.group(1))
    assert found and all(t.startswith(order_notes.AUTO_NOTES) for t in found), found


def test_the_point_reads_no_account_name_in_the_hint(tmp_path, monkeypatch):
    from test_mypoint import manager
    from test_oth_sale import point_id
    app, owner, tok, conn = panel(tmp_path)
    cid = new_order(conn, qty=2)
    orders.add_note(conn, cid, "Писал с аккаунта Основной", T, "manager1")
    conn.execute("UPDATE order_card SET point_id = ?, deal_kind = 'stock' WHERE id = ?", (point_id(conn), cid))   # a point's own sale (2026-10-30)
    conn.commit()
    monkeypatch.setattr(orders, "without_account_names", lambda conn, text: str(text).replace("Основной", "…"))
    page = manager(app, conn, point_id(conn)).get("/orders").get_data(as_text=True)
    assert 'title="Писал с аккаунта …"' in page and "Основной" not in page
    assert 'title="Писал с аккаунта Основной"' in owner.get("/orders").get_data(as_text=True)          # the owner reads it as written


# ---- «Товар в наличии на точке»: open stages only --------------------------------------------------------------------------------------

@pytest.mark.parametrize("closed", ["done", "cancel", "return"])
def test_in_stock_is_shown_while_open_and_never_on_a_closed_stage(tmp_path, monkeypatch, closed):
    from datetime import datetime
    app, owner, tok, conn = panel(tmp_path)
    card_at(conn, "oth", T)
    cid = new_order(conn, qty=2)
    take(conn, cid)
    conn.execute("UPDATE order_card SET delivery_date = '' WHERE id = ?", (cid,))
    conn.commit()
    _quiet(monkeypatch)
    assert post(owner, "/orders/%d/oth-at-point" % cid, tok).status_code == 302

    def shown():
        board = owner.get("/orders").get_data(as_text=True)
        card = owner.get("/orders/%d" % cid).get_data(as_text=True)
        row = orders_export._row(orders.get_card(conn, cid))
        return IN_STOCK in board, IN_STOCK in card, IN_STOCK in row
    assert shown() == (True, True, True)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, closed), cid))       # straight to the closed stage
    conn.commit()
    assert shown() == (False, False, False)
    assert orders.get_card(conn, cid)["in_stock"] is True                       # the flag itself (the checks of the fields) is not changed
    assert not any(c["in_stock"] for col in orders.board(conn, datetime(2026, 10, 31))["columns"] for c in col["cards"])


def test_a_point_manager_is_a_person_and_server_names_are_not():
    assert order_notes.is_person("manager1") and order_notes.is_person("owner") and order_notes.is_person("менеджер Альфа")
    for author in ("система", "бот ВК", "чаты Авито", "cli", "server", "system", ""):
        assert not order_notes.is_person(author), author


def test_the_need_a_point_adds_to_an_order_of_a_chat_counts(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    ad = _new(conn, comment="Объявление: Шины Ikon 205/55 https://avito.ru/1")
    assert order_notes.latest(conn, [ad]) == {}
    orders.update_card(conn, ad, {"comment": "Объявление: Шины Ikon 205/55 https://avito.ru/1\nнужно к пятнице"}, LATER, "менеджер Альфа")
    assert order_notes.latest(conn, [ad]) == {ad: "Объявление: Шины Ikon 205/55 https://avito.ru/1 нужно к пятнице"}


def test_the_account_name_is_taken_out_before_the_hint_is_cut(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    cid = _new(conn)
    orders.add_note(conn, cid, "х" * 95 + " Основной аккаунт", T, "manager1")         # the cut at 100 falls inside the name
    clean = lambda text: text.replace("Основной", "…")
    hint = order_notes.latest(conn, [cid], clean=clean)[cid]
    assert "Осн" not in hint and hint == ("х" * 95 + " … аккаунт")[:100] + "…"
    assert "Осн" in order_notes.latest(conn, [cid])[cid]                                      # cut first, the start of the name would stay
