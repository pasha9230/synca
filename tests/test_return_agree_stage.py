"""The owner 2026-11-13 («в панели этап как у точки: согласование возврата… статус меняем на этапы»; «Го» on the plan): «Согласование
возврата» (code return_agree) is a stage between «На ОТХ» and «На возврат», not a status of «На возврат».
- in: the seventh day on «На ОТХ», a position marked «На возврат» (the order whole), the office by hand; a move by hand to «На возврат»
  with nothing agreed goes on to the new stage;
- out: «Согласовано» (the date; one act for several orders too) → «На возврат»; the office's sale → «Выполнено» («Есть покупатель» for two
  days since the order came to the stage); «Снять согласование» → «На ОТХ», on sale; the last mark undone → «На ОТХ»;
- «На возврат» holds the agreed ones only: «Отменить согласование» puts the order back on «Согласование возврата»; the act → «Вернули поставщику»;
- the orders on the agreement now move once at the start (store._migrate), the agreed ones stay; a second run changes nothing;
- the feed: the tyres on the agreement are not ours on OTH (as before); the Google table: the new name in «Этап», no new row."""
from __future__ import annotations

import json
from datetime import date

import pytest
from cryptography.fernet import Fernet

from test_mypoint import manager
from test_order_sheet import FakeScript, turn_on
from test_point_oth_sell import ON_OTH, setup
from test_point_return_tabs import counts, events, has, item_of, tab
from test_vk_commands import _stage
from tires_sync import oth_point_return, oth_stock, order_oth, order_sheet as sheet, orders, tasks
from tires_sync.panel import routes_mypoint
from tires_sync.secretbox import SecretBox

T1 = "2026-10-16T11:00:00"
DAY7 = "2026-10-20T12:31:00"                       # ON_OTH 14.10 is the first day: the seventh from 12:30


@pytest.fixture
def clock(monkeypatch):
    moment = {"now": T1}
    monkeypatch.setattr(routes_mypoint, "local_iso", lambda: moment["now"])
    return moment


def code(conn, card) -> str:
    return orders.get_card(conn, card)["stage"]["code"]


def return_tasks(conn, card, open_only=True) -> list:
    return conn.execute("SELECT id FROM callback_task WHERE order_id = ? AND title IN (?, ?)" + (" AND done_at IS NULL AND cancelled_at IS NULL" if open_only else ""),
                        (card,) + order_oth.RETURN_TITLES).fetchall()


def to_agree_stage(conn, card, now="2026-10-15T10:00:00"):
    before = orders.get_card(conn, card)
    orders.move_card(conn, card, _stage(conn, orders.RETURN_AGREE), now, "o")
    order_oth.moved_by_hand(conn, before, now, "o")


def agree(conn, card, now="2026-10-15T12:00:00", day="2026-10-16"):
    return order_oth.agree(conn, orders.get_card(conn, card), date.fromisoformat(day), now, "owner")


def in_feed(conn, card) -> bool:
    return any(r["order_id"] == card for r in oth_stock._order_rows(conn))


# ---- the stage itself ----------------------------------------------------------------------------------------------------------------

def test_the_stage_stands_between_oth_and_na_vozvrat_on_the_board(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    codes = [s["code"] for s in orders.list_stages(conn)]
    assert codes.index("oth") + 1 == codes.index(orders.RETURN_AGREE) == codes.index(orders.RETURN_GIVEN) - 1
    stage = next(s for s in orders.list_stages(conn) if s["code"] == orders.RETURN_AGREE)
    assert stage["name"] == "Согласование возврата" and stage["kind"] == "open" and stage["on_board"]
    assert ("return_agree", "Согласование возврата", "#F79009", "open") in orders.DEFAULT_STAGES


def test_an_old_pipeline_gets_the_stage_once_right_after_oth(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    conn.execute("DELETE FROM order_stage WHERE code = ?", (orders.RETURN_AGREE,))
    for position, (sid,) in enumerate(conn.execute("SELECT id FROM order_stage ORDER BY position, id").fetchall()):
        conn.execute("UPDATE order_stage SET position = ? WHERE id = ?", (position, sid))
    conn.commit()
    for _ in range(2):
        orders.ensure_pipeline(conn, T1)
    codes = [s["code"] for s in orders.list_stages(conn)]
    assert codes.count(orders.RETURN_AGREE) == 1 and codes.index(orders.RETURN_AGREE) == codes.index("oth") + 1
    assert codes.index(orders.RETURN_GIVEN) == codes.index(orders.RETURN_AGREE) + 1
    assert [s["position"] for s in orders.list_stages(conn)] == list(range(len(codes)))


# ---- in ------------------------------------------------------------------------------------------------------------------------------

def test_in_the_seventh_day_puts_the_order_on_the_stage_with_the_task(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (order_oth.SINCE_KEY, "2026-10-01T00:00:00"))
    conn.commit()
    assert order_oth.ask_seventh_day(conn, DAY7) == 1
    card_now = orders.get_card(conn, card)
    assert card_now["stage"]["code"] == orders.RETURN_AGREE and card_now["entered_at"] == DAY7
    assert order_oth.agreeing(conn, card_now) and order_oth.agreeing_since(conn, card_now) == DAY7
    assert len(return_tasks(conn, card)) == 1                                       # the task of the responsible, as before
    assert tasks.get(conn, return_tasks(conn, card)[0][0])["title"] == order_oth.RETURN_TASK
    assert order_oth.ask_seventh_day(conn, "2026-10-21T13:00:00") == 0 and len(return_tasks(conn, card)) == 1


def test_in_a_position_marked_na_vozvrat_takes_the_order_whole(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    stay = orders.get_card(conn, card)["entered_at"]
    assert order_oth.item_to_return(conn, item_of(conn, card), T1, "owner", 2)        # 2 of 4
    card_now = orders.get_card(conn, card)
    assert card_now["stage"]["code"] == orders.RETURN_AGREE and card_now["entered_at"] == T1
    assert order_oth.oth_stay(conn, card_now) == stay                                 # the mark is kept by the stay on «На ОТХ»
    assert order_oth.return_parts(conn, card_now) == {item_of(conn, card): 2}         # «Согласовано» sends back the marked pieces by default
    assert len(return_tasks(conn, card)) == 1 and "Ожидает согласование" not in " ".join(events(conn, card))
    assert "На возврат: " in " ".join(events(conn, card)) and "Заказ целиком на «Согласование возврата»" in " ".join(events(conn, card))
    assert not order_oth.item_to_return(conn, item_of(conn, card), T1, "owner", 1)   # marked already: nothing new


def test_in_by_hand_the_card_button_and_a_move_to_na_vozvrat_without_an_agreement(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    page = owner.get("/orders/%d" % card).get_data(as_text=True)
    assert 'name="stage_id" value="%d"' % _stage(conn, orders.RETURN_AGREE) in page   # «На возврат» of the card: the new stage
    to_agree_stage(conn, card)
    assert code(conn, card) == orders.RETURN_AGREE and len(return_tasks(conn, card)) == 1
    (tmp_path / "second").mkdir()
    app2, owner2, conn2, pid2, card2 = setup(tmp_path / "second")
    before = orders.get_card(conn2, card2)
    orders.move_card(conn2, card2, _stage(conn2, orders.RETURN_GIVEN), T1, "o")      # the board: «На возврат» with nothing agreed
    order_oth.moved_by_hand(conn2, before, T1, "o")
    assert code(conn2, card2) == orders.RETURN_AGREE and len(return_tasks(conn2, card2)) == 1
    assert any(t.startswith("«На возврат» — только согласованные возвраты") for t in events(conn2, card2))


def test_rules_never_move_to_the_stage(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    from tires_sync import order_rules
    with pytest.raises(orders.OrderError, match="Согласование возврата"):
        order_rules._check(conn, _stage(conn, "at_point"), "enter", 0, "move", None, str(_stage(conn, orders.RETURN_AGREE)), "auto", None)


# ---- out -----------------------------------------------------------------------------------------------------------------------------

def test_out_agreed_goes_to_na_vozvrat_with_the_date(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    to_agree_stage(conn, card)
    assert agree(conn, card) == "2026-10-16"
    card_now = orders.get_card(conn, card)
    assert card_now["stage"]["code"] == orders.RETURN_GIVEN and not order_oth.agreeing(conn, card_now)
    assert order_oth.state_of(order_oth.return_plan(conn, card)) == order_oth.AGREED and not return_tasks(conn, card)
    assert order_oth.sign_act(conn, card_now, "Юнона", "2026-10-16T10:00:00", "gamma") == "return"     # as before


def test_out_agreed_a_part_goes_to_na_vozvrat_the_rest_back_on_its_stay_on_oth(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    item = item_of(conn, card)
    order_oth.item_to_return(conn, item, T1, "owner", 1)
    order_oth.agree(conn, orders.get_card(conn, card), date.fromisoformat("2026-10-19"), "2026-10-16T12:00:00", "owner")
    [new] = order_oth.split_of(conn, card)                                            # the marked piece, as on «На ОТХ» before the stage
    assert code(conn, new) == orders.RETURN_GIVEN and sum(int(i["qty"]) for i in orders.get_card(conn, new)["items"]) == 1
    rest = orders.get_card(conn, card)
    assert rest["stage"]["code"] == "oth" and rest["entered_at"] == ON_OTH and sum(int(i["qty"]) for i in rest["items"]) == 3
    assert not return_tasks(conn, card) and not return_tasks(conn, new)


def test_out_one_act_for_several_orders_takes_them_to_na_vozvrat(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    to_agree_stage(conn, card)
    pdf = b"%PDF-1.4\n%test\n"
    done = oth_point_return.act_for_orders(conn, [card], "2026-10-16", "act.pdf", pdf, date.fromisoformat("2026-10-15"), "2026-10-15T12:00:00", "owner")
    assert done["agreed"] == [card] and code(conn, card) == orders.RETURN_GIVEN


def test_out_the_buyer_for_two_days_since_the_stage_and_the_office_sells(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    to_agree_stage(conn, card, "2026-10-15T10:00:00")
    card_now = orders.get_card(conn, card)
    assert oth_point_return.buyer_until(conn, card_now) == "2026-10-17T10:00:00"
    assert oth_point_return.can_tell_buyer(conn, card_now, "2026-10-17T09:59:59") and not oth_point_return.can_tell_buyer(conn, card_now, "2026-10-17T10:00:00")
    assert order_oth.sell(conn, card_now, "gamma", T1)[2] is False                   # the point does not sell on the agreement
    outcome, answer, done = order_oth.sell(conn, card_now, "owner", T1, office=True)
    assert done and code(conn, card) == "done" and not return_tasks(conn, card)


def test_out_unagree_on_the_stage_goes_back_to_oth_on_sale(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    stay = orders.get_card(conn, card)["entered_at"]
    order_oth.item_to_return(conn, item_of(conn, card), "2026-10-15T10:00:00", "owner", 2)
    assert _csrf(owner, card)                                                         # «Снять согласование» in the card
    assert order_oth.unagree(conn, orders.get_card(conn, card), "2026-10-16T10:00:00", "owner")
    card_now = orders.get_card(conn, card)
    assert card_now["stage"]["code"] == "oth" and card_now["entered_at"] == stay      # the same stay: its seven days count on
    assert not return_tasks(conn, card) and in_feed(conn, card)                       # the tasks closed, the tyres in the feed again
    assert order_oth.BACK_TO_OTH % "На ОТХ" in events(conn, card)
    assert order_oth._kept(conn, card_now)                                            # the seventh day leaves the stay alone
    page = tab(manager(app, conn, pid), "oth")
    assert has(page, card) and 'action="/my-point/%d/oth_sold"' % card in page


def test_out_the_last_mark_undone_goes_back_to_oth(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    item = item_of(conn, card)
    order_oth.item_to_return(conn, item, T1, "owner", 2)
    order_oth.unmark_returned(conn, item, "2026-10-16T12:00:00", "owner")
    card_now = orders.get_card(conn, card)
    assert card_now["stage"]["code"] == "oth" and card_now["entered_at"] == ON_OTH and not order_oth._kept(conn, card_now)
    assert not order_oth.agreeing(conn, card_now) and not return_tasks(conn, card)


def test_unagree_on_na_vozvrat_puts_the_order_back_on_the_stage_with_the_task(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    to_agree_stage(conn, card)
    agree(conn, card)
    assert order_oth.unagree(conn, orders.get_card(conn, card), "2026-10-15T13:00:00", "owner")
    card_now = orders.get_card(conn, card)
    assert card_now["stage"]["code"] == orders.RETURN_AGREE and order_oth.return_plan(conn, card) is None
    assert order_oth.agreeing(conn, card_now) and len(return_tasks(conn, card)) == 1


def _csrf(client, card) -> str:
    import re
    page = client.get("/orders/%d" % card).get_data(as_text=True)
    found = re.search(r'action="/orders/%d/oth-unagree".*?name="csrf" value="([^"]+)"' % card, page, re.S)
    assert found, "no «Снять согласование» in the card"
    return found.group(1)


# ---- the point's tabs ----------------------------------------------------------------------------------------------------------------

def test_the_tabs_follow_the_stage(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    mine = manager(app, conn, pid)
    to_agree_stage(conn, card)
    page = tab(mine, "agree")
    assert has(page, card) and (counts(page)["oth"], counts(page)["agree"], counts(page)["return"]) == (0, 1, 0)
    agree(conn, card)
    page = tab(mine, "return")
    assert 'data-return="%d"' % card in page and (counts(page)["oth"], counts(page)["agree"], counts(page)["return"]) == (0, 0, 1)


# ---- the feed: no change ------------------------------------------------------------------------------------------------------------

def test_the_feed_takes_oth_and_not_the_agreement_nor_na_vozvrat(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    assert in_feed(conn, card)
    to_agree_stage(conn, card)
    assert not in_feed(conn, card) and card not in {r["order_id"] for r in oth_stock._rows(conn)}
    assert not order_oth.goods_on_oth(conn)                                           # nor the mark of the catalog
    agree(conn, card)
    assert not in_feed(conn, card)


# ---- the Google table: the new name, no new row ---------------------------------------------------------------------------------------

def test_the_table_names_the_stage_and_makes_no_row(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    box, fake = SecretBox(Fernet.generate_key()), FakeScript()
    turn_on(conn, box)
    sheet.enroll(conn, card, T1)
    sheet.flush(conn, box, fake, T1)
    made = len(fake.rows)
    assert made >= 1
    to_agree_stage(conn, card, "2026-10-16T12:00:00")
    sheet.flush(conn, box, fake, "2026-10-16T12:05:00")
    assert len(fake.rows) == made and not any(r["create"] for r in fake.data_posts()[-1]["rows"])
    assert all(r["Этап"] == "Согласование возврата" for r in fake.rows.values())
    assert orders.RETURN_AGREE in sheet.LISTED_CODES and orders.RETURN_AGREE in sheet.OPEN_STAGES


# ---- the migration of the orders on the agreement now -------------------------------------------------------------------------------

def _old_state(conn, card_given, card_marked, card_agreed):
    """The statuses of 2026-11-12: «Согласуем» on «На возврат», a mark «На возврат» on «На ОТХ», one agreed on «На возврат»."""
    given = _stage(conn, orders.RETURN_GIVEN)
    conn.execute("UPDATE order_card SET stage_id = ?, entered_at = ? WHERE id IN (?, ?)", (given, "2026-10-20T12:31:00", card_given, card_agreed))
    conn.execute("INSERT INTO oth_return_plan (order_id, agreed_at, agreed_by, return_date) VALUES (?, ?, ?, ?)",
                 (card_agreed, "2026-10-21T10:00:00", "owner", "2026-10-23"))
    item = item_of(conn, card_marked)
    stay = orders.get_card(conn, card_marked)["entered_at"]
    conn.execute("INSERT INTO oth_return (item_id, order_id, qty, at, author) VALUES (?, ?, ?, ?, ?)", (item, card_marked, 2, "2026-10-15T09:30:00", "owner"))
    task = order_oth._return_task(conn, orders.get_card(conn, card_marked), "2026-10-15T09:30:00", "owner", commit=False)     # its task, open
    conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?)", (order_oth._item_key(item, stay), json.dumps({"qty": 2, "at": "2026-10-15T09:30:00", "by": "owner", "task": task})))
    conn.execute("DELETE FROM app_state WHERE key = ?", (order_oth.AGREE_STAGE_KEY,))
    conn.commit()


def test_the_migration_moves_the_orders_on_the_agreement_once(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    cards = [card] + [orders.create_card(conn, {"point_id": pid, "client_name": "Тест %d" % n}, ON_OTH, "o", require_contact=False,
                                         stage=next(s for s in orders.list_stages(conn) if s["code"] == "oth")) for n in (1, 2)]
    cols = ", ".join(r[1] for r in conn.execute("PRAGMA table_info(order_item)") if r[1] not in ("id", "order_id"))
    for extra in cards[1:]:                                                           # the same tyres as the first order
        conn.execute("INSERT INTO order_item (order_id, %s) SELECT ?, %s FROM order_item WHERE order_id = ?" % (cols, cols), (extra, card))
    conn.commit()
    given, marked, agreed = cards[1], card, cards[2]
    _old_state(conn, given, marked, agreed)
    done = order_oth.migrate_agree_stage(conn, "2026-11-13T09:00:00")
    assert done == {"from_return_given": 1, "from_oth": 1, "skipped": 0}
    assert code(conn, given) == code(conn, marked) == orders.RETURN_AGREE and code(conn, agreed) == orders.RETURN_GIVEN
    assert orders.get_card(conn, given)["entered_at"] == "2026-10-20T12:31:00"       # the stay began when it came to the agreement
    assert orders.get_card(conn, marked)["entered_at"] == "2026-10-15T09:30:00"
    assert order_oth.return_parts(conn, orders.get_card(conn, marked)) == {item_of(conn, marked): 2}     # the mark is still read
    for mine, was in ((given, "Согласование возврата на «На возврат»"), (marked, "на «На ОТХ» с позицией «На возврат»")):
        assert order_oth.AGREE_MIGRATED % ("Согласование возврата", was) in events(conn, mine)
    assert not any("Согласование возврата (2026-11-13)" in t for t in events(conn, agreed))
    before = {c: (orders.get_card(conn, c)["stage"]["code"], len(events(conn, c))) for c in cards}
    assert order_oth.migrate_agree_stage(conn, "2026-11-13T10:00:00") == {"from_return_given": 0, "from_oth": 0, "skipped": 0}
    assert {c: (orders.get_card(conn, c)["stage"]["code"], len(events(conn, c))) for c in cards} == before


def test_the_migration_runs_at_the_start_of_the_base(tmp_path):
    """store._migrate calls order_oth.migrate at every start: it runs the move of 2026-11-13 too, once."""
    app, owner, conn, pid, card = setup(tmp_path)
    assert conn.execute("SELECT 1 FROM app_state WHERE key = ?", (order_oth.AGREE_STAGE_KEY,)).fetchone()     # a new base: done at its start
    to_return = _stage(conn, orders.RETURN_GIVEN)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (to_return, card))                       # «Согласуем» of before
    conn.execute("DELETE FROM app_state WHERE key = ?", (order_oth.AGREE_STAGE_KEY,))
    conn.commit()
    order_oth.migrate(conn, "2026-11-13T09:00:00")
    assert code(conn, card) == orders.RETURN_AGREE
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (to_return, card))
    conn.commit()
    order_oth.migrate(conn, "2026-11-13T10:00:00")
    assert code(conn, card) == orders.RETURN_GIVEN                                    # once: the key stays
