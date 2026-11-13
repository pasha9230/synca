"""The owner 2026-11-03: «На возврат» is a stage (code return_given). The point's «На точке» on the seventh day of «На ОТХ» moves the order
there with «Согласование возврата» (the tyres still on sale: the feed and «Взять с ОТХ»); «Согласовано с поставщиком» keeps the stage, «Согласовано,
отдать до ДД.ММ», the tyres leave the sale; «Отменить согласование» gives «Согласование возврата» back. «Подписал акт и отдал» (and the VK «возврат
отдал») closes it in «Вернули поставщику» with the refund = the purchase of the tyres given back. The board shows the label and filters by
it; «Рабочее место» shows the stage on «На возврат»; the press is undone as the others. The orders of the old flow are moved once."""
from __future__ import annotations

import pytest

from panel_helpers import post
from test_mypoint import manager, press, workplace
from test_oth_sale import new_order
from test_oth_seventh_day import ENTERED, on_oth, started
from test_order_reserve import card_at, db, stage
from test_vk_commands import _card, _stage, ready
from tires_sync import order_manager, order_oth, order_sheet, orders, oth_sale, oth_stock, store
from tires_sync.panel import routes_mypoint

DAY7 = "2026-10-26T13:00:00"


def returning(conn, cid=None):
    """An order on «Согласование возврата» (a stage since 2026-11-13): the seventh day moved it (the owner 2026-11-04)."""
    cid = cid or card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00")
    return cid


def agreed(conn, cid):
    order_oth.agree(conn, orders.get_card(conn, cid), orders.date.fromisoformat("2026-10-27"), "2026-10-27T10:00:00", "owner")
    return orders.get_card(conn, cid)


def label(conn, cid):
    return order_oth.label_of(order_oth.return_plan(conn, cid))


# ---- 1, 2: the stage and its label; the sale ------------------------------------------------------------------------------------

def test_the_seventh_day_moves_the_order_to_na_vozvrat_agreeing_and_the_tyres_leave_the_sale(tmp_path):
    conn = db(tmp_path)
    cid = returning(conn)
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == orders.RETURN_AGREE and card["stage"]["name"] == "Согласование возврата"     # 2026-11-13: the stage
    assert label(conn, cid) == "Согласование возврата" and order_oth.to_agree(conn, card)
    assert oth_stock.pieces(conn, manual=False) == {}                                    # the owner 2026-11-04: off sale from «Согласование возврата»
    assert conn.execute("SELECT COUNT(*) FROM callback_task WHERE order_id = ? AND title = ?", (cid, order_oth.RETURN_TASK)).fetchone()[0] == 1
    assert conn.execute("SELECT 1 FROM client_order WHERE order_id = ?", (cid,)).fetchone() is None     # still not the client's


def test_agreed_keeps_the_stage_takes_the_tyres_off_sale_and_unagree_gives_agreeing_back(tmp_path):
    conn = db(tmp_path)
    cid = returning(conn)
    card = agreed(conn, cid)
    assert card["stage"]["code"] == orders.RETURN_GIVEN and label(conn, cid) == "Согласовано, отдать до 28.10"
    assert oth_stock.pieces(conn, manual=False) == {} and not order_oth.to_agree(conn, card)
    assert [r["card"]["id"] for r in order_oth.to_return(conn, card["point_id"])] == [cid]
    assert order_oth.unagree(conn, card, "2026-10-27T11:00:00", "owner")
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == orders.RETURN_AGREE and label(conn, cid) == "Согласование возврата"     # back on the stage (2026-11-13)
    assert oth_stock.pieces(conn, manual=False) == {}                                    # the owner 2026-11-04: stays off sale


def test_agreed_from_oth_moves_the_order_to_na_vozvrat(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    card = agreed(conn, cid)
    assert card["stage"]["code"] == orders.RETURN_GIVEN and label(conn, cid) == "Согласовано, отдать до 28.10"
    assert oth_stock.pieces(conn, manual=False) == {}


def test_take_from_oth_works_on_oth_and_not_on_na_vozvrat(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    new = new_order(conn)
    line = orders.list_items(conn, new)[0]
    [offer] = oth_sale.offers(conn, orders.get_card(conn, new))[line["id"]]
    assert offer["from_order"] == cid and offer["qty"] == 4
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00")
    assert not oth_sale.offers(conn, orders.get_card(conn, new)).get(line["id"])         # the owner 2026-11-04: «Согласование возврата» is off sale
    with pytest.raises(oth_sale.OthSaleError):
        oth_sale.take(conn, new, line["id"], orders.list_items(conn, cid)[0]["id"], "2026-10-26T15:00:00", "owner")


# ---- 3: the act МХ-3 closes it with the purchase ------------------------------------------------------------------------------------

def test_sign_act_closes_the_order_with_the_purchase_as_the_refund(tmp_path):
    conn = db(tmp_path)
    cid = returning(conn)
    card = agreed(conn, cid)
    assert order_oth.sign_act(conn, card, "Бета", "2026-10-27T12:00:00", "менеджер Бета") == "return"
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "return" and card["stage"]["kind"] == "closed"
    assert (card["return_kind"], card["return_reason"], card["return_amount"]) == ("supplier", order_oth.RETURN_REASON, 16000.0)
    assert order_oth.return_plan(conn, cid)["signed_at"] == "2026-10-27T12:00:00"
    assert order_oth.sign_act(conn, card, "Бета", "2026-10-27T12:01:00", "менеджер Бета") == ""     # a second press


def test_a_partial_return_counts_only_the_pieces_given_back(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    new = new_order(conn, qty=1)
    line = orders.list_items(conn, new)[0]
    assert oth_sale.take(conn, new, line["id"], orders.list_items(conn, cid)[0]["id"], "2026-10-25T15:00:00", "owner") == 1     # still on «На ОТХ»
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00")
    card = agreed(conn, cid)
    assert order_oth.sign_act(conn, card, "Бета", "2026-10-27T12:00:00", "менеджер Бета") == "return"
    assert orders.get_card(conn, cid)["return_amount"] == 12000.0                        # 3 × 4000


def test_a_manager_s_own_refund_is_kept(tmp_path):
    conn = db(tmp_path)
    cid = returning(conn)
    orders.update_card(conn, cid, {"return_amount": "15000"}, "2026-10-27T09:00:00", "owner")
    card = agreed(conn, cid)
    assert order_oth.sign_act(conn, card, "Бета", "2026-10-27T12:00:00", "менеджер Бета") == "return"
    assert orders.get_card(conn, cid)["return_amount"] == 15000.0


def test_without_a_purchase_the_order_waits_for_the_amount_off_sale(tmp_path):
    conn = db(tmp_path)
    cid = returning(conn)
    card = agreed(conn, cid)
    conn.execute("UPDATE order_item SET supplier_price = 0 WHERE order_id = ?", (cid,))
    conn.commit()
    assert order_oth.sign_act(conn, orders.get_card(conn, cid), "Бета", "2026-10-27T12:00:00", "менеджер Бета") == "signed"
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == orders.RETURN_GIVEN and label(conn, cid) == "Отдано, нужна сумма возврата"
    assert any(e["level"] == "yellow" and e["text"].startswith(order_oth.SIGNED_HEAD) for e in orders.list_events(conn, cid))
    assert oth_stock.pieces(conn, manual=False) == {}


def test_the_vk_returned_closes_na_vozvrat_with_the_purchase_and_signs_the_plan(tmp_path):
    conn = db(tmp_path)
    cid = returning(conn)
    agreed(conn, cid)
    outcome, _answer = order_manager.returned(conn, orders.get_card(conn, cid), "менеджер Бета", "2026-10-27T12:00:00")
    card = orders.get_card(conn, cid)
    assert outcome.startswith("заказ перенесён") and card["stage"]["code"] == "return" and card["return_amount"] == 16000.0
    assert order_oth.return_plan(conn, cid)["signed_at"] == "2026-10-27T12:00:00"


def test_the_vk_returned_from_the_point_never_fills_the_amount(tmp_path):
    """The lead 2026-10-26 stays for an order of a client: the purchase is the refund of «На ОТХ» / «На возврат» only."""
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    order_manager.returned(conn, orders.get_card(conn, cid), "менеджер Бета", "2026-10-27T12:00:00")
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "at_point" and card["return_amount"] == 0


def test_the_journal_names_no_purchase_the_audit_keeps_it(tmp_path):
    conn = db(tmp_path)
    cid = returning(conn)
    order_oth.sign_act(conn, agreed(conn, cid), "Бета", "2026-10-27T12:00:00", "менеджер Бета")
    assert not [e for e in orders.list_events(conn, cid) if "16000" in e["text"]]
    assert order_oth.REFUND_LINE in [e["text"] for e in orders.list_events(conn, cid)]
    [row] = conn.execute("SELECT detail FROM audit WHERE action = 'oth_return_amount'").fetchall()
    assert '"amount": 16000.0' in row[0]


def test_signed_tyres_are_not_ours_on_the_oth_page_and_the_point_texts_follow_the_state(tmp_path, clock):
    app, conn, pid, cid, mine = on_page(tmp_path)
    assert 'data-oth-question="%d"' % cid not in mine.get("/my-point?tab=agree").get_data(as_text=True)     # E1: the seventh day asks nothing
    agreed(conn, cid)
    page = mine.get("/my-point?tab=return").get_data(as_text=True)
    assert 'data-return="%d"' % cid in page and "Шины отданы поставщику" not in page        # agreed: the act to sign, nothing given yet
    conn.execute("UPDATE order_item SET supplier_price = 0 WHERE order_id = ?", (cid,))
    conn.commit()
    assert order_oth.sign_act(conn, orders.get_card(conn, cid), "Бета", "2026-10-27T12:00:00", "менеджер Бета") == "signed"
    assert order_oth.stock(conn)["stock"] == [] and order_oth.goods_on_oth(conn) == {}
    page = mine.get("/my-point?tab=return").get_data(as_text=True)
    block = page[page.index('id="order-%d"' % cid):].split("</article>")[0]
    assert "Отдано, нужна сумма возврата" in block and "Шины отданы поставщику, менеджер заполнит сумму возврата" in block


def test_a_failed_migration_keeps_the_base_open_and_tries_again(tmp_path, monkeypatch):
    conn = db(tmp_path)
    old_flow(conn)
    monkeypatch.setattr(order_oth, "return_changes", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert order_oth.migrate(conn, "2026-11-04T06:00:00") == dict.fromkeys(("renamed", "closed", "waiting_amount", "to_return_given", "skipped"), 0)
    assert not store.get_state(conn, order_oth.FLOW_KEY) and orders.stage_name(conn, orders.RETURN_GIVEN) == order_oth.OLD_RETURN_NAME
    monkeypatch.undo()
    assert order_oth.migrate(conn, "2026-11-04T07:00:00")["closed"] == 1


def test_the_vk_returned_without_a_purchase_takes_agreeing_off_sale(tmp_path):
    conn = db(tmp_path)
    cid = returning(conn)
    conn.execute("UPDATE order_item SET supplier_price = 0 WHERE order_id = ?", (cid,))
    conn.commit()
    outcome, _answer = order_manager.returned(conn, orders.get_card(conn, cid), "менеджер Бета", "2026-10-27T12:00:00")
    assert "этап не изменён" in outcome
    assert orders.get_card(conn, cid)["stage"]["code"] == orders.RETURN_GIVEN and label(conn, cid) == "Отдано, нужна сумма возврата"
    assert oth_stock.pieces(conn, manual=False) == {}


# ---- 4: the board, «Рабочее место», the undo ----------------------------------------------------------------------------------------

def test_the_board_shows_the_label_and_filters_by_it(tmp_path):
    conn = db(tmp_path)
    wait, done = returning(conn), returning(conn)
    agreed(conn, done)
    plain = card_at(conn, "at_point", "2026-10-15T10:00:00")

    def cards(**kw):
        data = orders.board(conn, orders.datetime.fromisoformat("2026-10-27T12:00:00"), **kw)
        return {c["id"]: c["returning"] for col in data["columns"] for c in col["cards"]}
    shown = cards()
    assert shown[wait] == "Согласование возврата" and shown[done] == "Согласовано, отдать до 28.10" and shown[plain] == ""
    assert cards(returning=order_oth.AGREE) == {wait: "Согласование возврата"}
    assert cards(returning=order_oth.AGREED) == {done: "Согласовано, отдать до 28.10"}


def test_the_board_page_filter(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    cid = _card(conn, pid, webmim="700200", code="at_point")
    started(conn)
    orders.move_card(conn, cid, _stage(conn, "oth"), ENTERED, "o", changes={"cancel_reason": "Клиент не пришёл"})
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00")
    order_oth.answer_here(conn, orders.get_card(conn, cid), "менеджер Бета", DAY7)
    page = client.get("/orders?r=agree").get_data(as_text=True)
    assert 'data-return-filter' in page and 'data-return-label>Согласование возврата<' in page
    assert "№%d " % cid in page
    assert "№%d " % cid not in client.get("/orders?r=agreed").get_data(as_text=True)


@pytest.fixture
def clock(monkeypatch):
    moment = {"now": DAY7}
    monkeypatch.setattr(routes_mypoint, "local_iso", lambda: moment["now"])
    return moment


def on_page(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    cid = _card(conn, pid, webmim="700200", code="at_point")
    started(conn)
    orders.move_card(conn, cid, _stage(conn, "oth"), ENTERED, "o", changes={"cancel_reason": "Клиент не пришёл"})
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00")
    return app, conn, pid, cid, manager(app, conn, pid)


def test_the_point_s_return_tab_shows_the_stage_and_its_label(tmp_path, clock):
    app, conn, pid, cid, mine = on_page(tmp_path)
    assert orders.get_card(conn, cid)["stage"]["code"] == orders.RETURN_AGREE             # from the start, the question or not (2026-11-04; the stage 2026-11-13)
    assert 'id="order-%d"' % cid not in mine.get("/my-point?tab=oth").get_data(as_text=True)
    assert 'id="order-%d"' % cid not in mine.get("/my-point?tab=return").get_data(as_text=True)     # «На возврат»: the agreed only (2026-11-12)
    page = mine.get("/my-point?tab=agree").get_data(as_text=True)
    assert 'id="order-%d"' % cid in page and "data-return-label>Согласование возврата<" in page
    assert "/act_signed" not in page                                                      # no hand-over before the agreement


def test_the_act_on_the_page_closes_it_and_the_undo_puts_it_back(tmp_path, clock):
    app, conn, pid, cid, mine = on_page(tmp_path)
    assert 'data-oth-question="%d"' % cid not in mine.get("/my-point?tab=agree").get_data(as_text=True)     # E1: the seventh day asks nothing
    agreed(conn, cid)
    clock["now"] = "2026-10-27T12:00:00"
    _t, page = press(mine, cid, "act_signed")
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "return" and card["return_amount"] == 16000.0 and "возврат закрыт" in page
    assert 'action="/my-point/%d/undo"' % cid not in workplace(mine)    # the owner 2026-11-10: no list on the page; the office corrects it,
    clock["now"] = "2026-10-27T12:10:00"
    r = post(mine, "/my-point/%d/undo" % cid, "tok")
    assert "отменена" in mine.get(r.headers["Location"]).get_data(as_text=True)
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == orders.RETURN_GIVEN and card["return_amount"] == 0 and not card["return_kind"] and not card["return_reason"]
    assert order_oth.return_plan(conn, cid)["signed_at"] is None and label(conn, cid) == "Согласовано, отдать до 28.10"
    assert 'data-return="%d"' % cid in mine.get("/my-point?tab=return").get_data(as_text=True)     # «Подписал акт и отдал» again


# ---- 5: the orders of the old flow ----------------------------------------------------------------------------------------------

def old_flow(conn):
    """A base as it stood before 2026-11-03: return_given called «Возврат передан» with an order on it (the act signed, the refund awaited),
    one without a purchase, an order on «На ОТХ» the point said «На точке» of, one agreed, one only asked, one plain."""
    conn.execute("UPDATE order_stage SET name = ? WHERE code = ?", (order_oth.OLD_RETURN_NAME, orders.RETURN_GIVEN))
    conn.execute("DELETE FROM app_state WHERE key = ?", (order_oth.FLOW_KEY,))
    conn.commit()
    given, nopurchase, here, agreed_, asked, plain = (card_at(conn, "at_point", "2026-10-15T10:00:00") for _ in range(6))
    for cid in (given, nopurchase, here, agreed_, asked, plain):
        on_oth(conn, cid)
    for cid in (given, nopurchase, agreed_):
        conn.execute("INSERT INTO oth_return_plan (order_id, agreed_at, agreed_by, return_date) VALUES (?, ?, 'owner', '2026-10-21')",
                     (cid, "2026-10-21T10:00:00"))
    conn.execute("UPDATE oth_return_plan SET signed_at = '2026-10-21T12:00:00', signed_by = 'Бета' WHERE order_id IN (?, ?)", (given, nopurchase))
    conn.execute("UPDATE order_item SET supplier_price = 0 WHERE order_id = ?", (nopurchase,))
    conn.commit()
    for cid in (given, nopurchase):
        orders.move_card(conn, cid, stage(conn, orders.RETURN_GIVEN), "2026-10-21T12:00:00", "Бета")
    for cid, value in ((here, "here|2026-10-26T13:00:00"), (asked, "asked|2026-10-26T12:30:00")):
        conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?)", (order_oth._ask_key(orders.get_card(conn, cid)), value))
    conn.commit()
    return given, nopurchase, here, agreed_, asked, plain


def test_the_migration_moves_the_old_flow_once(tmp_path):
    conn = db(tmp_path)
    given, nopurchase, here, agreed_, asked, plain = old_flow(conn)
    assert order_oth.migrate(conn, "2026-11-04T06:00:00") == {"renamed": 1, "closed": 1, "waiting_amount": 1, "to_return_given": 2, "skipped": 0}
    code = lambda cid: orders.get_card(conn, cid)["stage"]["code"]                     # noqa: E731
    assert orders.stage_name(conn, orders.RETURN_GIVEN) == "На возврат"
    assert code(given) == "return" and orders.get_card(conn, given)["return_amount"] == 16000.0
    assert code(nopurchase) == orders.RETURN_GIVEN and label(conn, nopurchase) == "Отдано, нужна сумма возврата"
    assert code(here) == orders.RETURN_GIVEN and label(conn, here) == "Согласование возврата"
    assert code(agreed_) == orders.RETURN_GIVEN and label(conn, agreed_) == "Согласовано, отдать до 21.10"
    assert code(asked) == "oth" and code(plain) == "oth"
    assert oth_stock.pieces(conn, manual=False) == {"1": 8}                              # asked, plain: on sale; «На возврат» never (2026-11-04)
    assert conn.execute("SELECT COUNT(*) FROM chat_send_log").fetchone()[0] == 0
    assert order_oth.migrate(conn, "2026-11-04T07:00:00") == {"renamed": 0, "closed": 0, "waiting_amount": 0, "to_return_given": 0, "skipped": 0}
    assert code(here) == orders.RETURN_GIVEN


def test_the_migration_keeps_a_name_the_owner_gave(tmp_path):
    conn = db(tmp_path)
    old_flow(conn)
    conn.execute("UPDATE order_stage SET name = 'Отдали на возврат' WHERE code = ?", (orders.RETURN_GIVEN,))
    conn.commit()
    assert order_oth.migrate(conn, "2026-11-04T06:00:00")["renamed"] == 0
    assert orders.stage_name(conn, orders.RETURN_GIVEN) == "Отдали на возврат"


def test_the_migration_runs_from_the_schema_and_a_new_base_starts_with_the_new_name(tmp_path):
    conn = db(tmp_path)
    assert orders.stage_name(conn, orders.RETURN_GIVEN) == "На возврат"
    assert store.get_state(conn, order_oth.FLOW_KEY)                                    # store._migrate made it: nothing to move later
    old_flow(conn)
    conn.close()
    conn = store.connect(str(tmp_path / "x.db"))                                       # the next start migrates
    assert orders.stage_name(conn, orders.RETURN_GIVEN) == "На возврат" and store.get_state(conn, order_oth.FLOW_KEY)


# ---- 6: «Реализация» of the Google table --------------------------------------------------------------------------------------------

def test_the_sheet_marks_o_only_for_oth(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    assert order_sheet.realization(orders.get_card(conn, cid)) == order_sheet.REFUSED
    returning(conn, cid)
    assert order_sheet.realization(orders.get_card(conn, cid)) == ""
    order_oth.sign_act(conn, agreed(conn, cid), "Бета", "2026-10-27T12:00:00", "менеджер Бета")
    assert order_sheet.realization(orders.get_card(conn, cid)) == ""


def test_the_new_view_of_the_point_shows_na_vozvrat_with_its_label(tmp_path, clock):
    from test_point_new_view import flag, is_new
    app, conn, pid, cid, mine = on_page(tmp_path)
    flag(conn, "all")
    assert 'data-oth-question="%d"' % cid not in mine.get("/my-point?tab=agree").get_data(as_text=True)     # E1: the seventh day asks nothing
    page = mine.get("/my-point?tab=agree").get_data(as_text=True)                         # not agreed yet: «Согласование возврата» (2026-11-12)
    assert is_new(page) and 'id="order-%d"' % cid in page
    block = page[page.index('id="order-%d"' % cid):].split("</article>")[0]
    assert "data-return-label>Согласование возврата<" in block and "на возврат" in block and 'data-point-hint="returning"' in block
    agreed(conn, cid)
    page = mine.get("/my-point?tab=return").get_data(as_text=True)
    assert 'data-return="%d"' % cid in page and "сумма возврата — закупка шин" in page
