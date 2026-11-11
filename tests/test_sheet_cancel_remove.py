"""The rows of the Google table are never deleted (the owner 2026-11-11, instead of the removal of 2026-11-07): an order cancelled before its
tyres came to the point keeps its rows, «Этап» goes to «Отмена» as any other change, no op «remove» is ever sent; a row removed earlier
(`_removed` in sheet_order.rows) is never made again; the command sheet-cancel-remove is gone."""
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


def test_a_cancel_before_the_point_keeps_the_rows_and_sends_the_stage(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    cancel(conn, mine)
    sheet.flush(conn, box, fake, LATER)
    rows = mine_rows(fake, mine)
    assert len(rows) == 2 and all(r["Этап"] == "Отмена" and r["Артикул"] for r in rows.values())
    last = fake.data_posts()[-1]["rows"]
    assert len(last) == 2 and all(not r["create"] and r["fields"].get("Этап") == "Отмена" for r in last)
    assert no_remove_sent(fake)
    assert "Google-таблица обновлена: Этап." in events(conn, mine)
    assert not any("удалены" in e for e in events(conn, mine))
    assert not any(v.get("_removed") for v in written(conn, mine).values())


def test_a_cancel_after_the_point_keeps_the_rows_as_before(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    orders.move_card(conn, mine, stage(conn, "at_point")["id"], LATER, "owner")
    sheet.flush(conn, box, fake, LATER)
    cancel(conn, mine, LATEST)
    sheet.flush(conn, box, fake, LATEST)
    rows = mine_rows(fake, mine)
    assert len(rows) == 2 and all(r["Этап"] == "Отмена" for r in rows.values())
    assert no_remove_sent(fake)


def test_a_row_removed_before_is_never_made_again(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    gone, kept = sorted(mine_rows(fake, mine))
    rows = written(conn, mine)
    rows[gone] = {"_removed": True}                                        # removed by the rule of 2026-11-07
    del fake.rows[gone]
    conn.execute("UPDATE sheet_order SET rows = ? WHERE order_id = ?", (json.dumps(rows, ensure_ascii=False), mine))
    conn.commit()
    cancel(conn, mine)
    sheet.flush(conn, box, fake, LATER)
    orders.move_card(conn, mine, stage(conn, "placed")["id"], LATEST, "owner")            # back from «Отмена»: still not made again
    sheet.flush(conn, box, fake, LATEST)
    assert set(mine_rows(fake, mine)) == {kept} and fake.rows[kept]["Этап"] == "Заказ оформлен"
    assert not any(r["key"] == gone for p in fake.data_posts()[1:] for r in p["rows"])
    assert written(conn, mine)[gone] == {"_removed": True}
    assert no_remove_sent(fake)
    assert not any("снова записаны" in e or "убрана из заказа" in e for e in events(conn, mine))


def test_the_command_sheet_cancel_remove_is_gone(table):
    conn, box, fake = table
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    with pytest.raises(SystemExit) as err:
        cli.main(["sheet-cancel-remove", "--db", db_path, "--dry-run"])
    assert err.value.code == 2
    assert not hasattr(sheet, "remove_cancelled") and not hasattr(sheet, "SINCE")
