"""No row of the Google table is ever removed (the owner 2026-11-11: «Не удаляй строки вообще», it overrides the owner's word of 2026-11-07 «удаляй
такие заказы из таблицы»): an order cancelled before its tyres came to the point keeps its rows and gets «Этап»: «Отмена» as any other; the
command sheet-cancel-remove only refuses. A row the panel removed before (`_removed`) is never made again: on «Отмена»
or back from it."""
from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from test_order_sheet import FakeScript, events, placed, stage, turn_on
from test_webmim_create import NOW, db
from tires_sync import cli, order_sheet as sheet, orders
from tires_sync.secretbox import SecretBox

LATER = "2026-10-23T12:30:00"
LATEST = "2026-10-23T13:00:00"


@pytest.fixture
def table(tmp_path):
    conn = db(tmp_path)
    box = SecretBox(Fernet.generate_key())
    turn_on(conn, box)
    return conn, box, FakeScript()


def cancel(conn, card_id, now=LATER):
    orders.move_card(conn, card_id, stage(conn, "cancel")["id"], now, "owner", {"cancel_reason": "передумал"})


def written(conn, card_id):
    return json.loads(conn.execute("SELECT rows FROM sheet_order WHERE order_id = ?", (card_id,)).fetchone()[0])


def mine_rows(fake, card_id):
    return {k: v for k, v in fake.rows.items() if k.startswith("tires:%d:" % card_id)}


def no_remove_sent(fake):
    return not any(r.get("remove") for p in fake.data_posts() for r in p["rows"])


@pytest.mark.parametrize("after_point", [False, True])
def test_a_cancel_keeps_the_rows_and_updates_the_stage_and_no_remove_goes(table, after_point):
    conn, box, fake = table
    mine = placed(conn)
    other = placed(conn, client_name="Пётр")
    sheet.flush(conn, box, fake, NOW)
    if after_point:
        orders.move_card(conn, mine, stage(conn, "at_point")["id"], NOW, "owner")
        sheet.flush(conn, box, fake, NOW)
    cancel(conn, mine)
    sheet.flush(conn, box, fake, LATER)
    rows = mine_rows(fake, mine)
    assert len(rows) == 2 and all(r["Этап"] == "Отмена" for r in rows.values())
    assert len(mine_rows(fake, other)) == 2
    assert no_remove_sent(fake)
    assert not any("удален" in e for e in events(conn, mine))
    assert any(e.startswith("Google-таблица обновлена:") and "Этап" in e for e in events(conn, mine))
    assert not any(v.get("_removed") for v in written(conn, mine).values())


def test_a_row_removed_before_the_change_is_never_made_again_on_cancel_or_off_it(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    cancel(conn, mine)
    sheet.flush(conn, box, fake, LATER)
    rows = written(conn, mine)                                             # as the panel of 2026-11-07 left it: the rows removed
    conn.execute("UPDATE sheet_order SET rows = ? WHERE order_id = ?", (json.dumps({m: {"_removed": True} for m in rows}), mine))
    conn.commit()
    for m in rows:
        del fake.rows[m]
    posts = len(fake.data_posts())
    orders.add_event(conn, mine, LATEST, "owner", "note", "ещё запись")
    sheet.flush(conn, box, fake, LATEST, full=True)
    assert len(fake.data_posts()) == posts and not mine_rows(fake, mine)  # never made again while on «Отмена»
    orders.move_card(conn, mine, stage(conn, "placed")["id"], LATEST, "owner")
    sheet.flush(conn, box, fake, LATEST)                                   # back from «Отмена»: still never made again (the owner 2026-11-11)
    assert not mine_rows(fake, mine) and not any(r["key"] in rows for p in fake.data_posts()[posts:] for r in p["rows"])
    assert all(v == {"_removed": True} for v in written(conn, mine).values())
    assert not any("снова записаны" in e or "убрана из заказа" in e for e in events(conn, mine))
    assert no_remove_sent(fake)


def test_the_command_refuses_and_changes_nothing(table, capsys):
    conn, box, fake = table
    old = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    cancel(conn, old)
    before = conn.execute("SELECT rows FROM sheet_order WHERE order_id = ?", (old,)).fetchone()[0]
    posts = len(fake.posts)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    for extra in ([], ["--dry-run"]):
        assert cli.main(["sheet-cancel-remove", "--db", db_path] + extra) == 1
        assert "удаление строк из Google-таблицы отключено решением владельца 2026-11-11" in capsys.readouterr().err
    assert conn.execute("SELECT rows FROM sheet_order WHERE order_id = ?", (old,)).fetchone()[0] == before
    assert len(fake.posts) == posts and len(mine_rows(fake, old)) == 2
    assert not hasattr(sheet, "remove_cancelled") and not hasattr(sheet, "cancelled_before_point")
