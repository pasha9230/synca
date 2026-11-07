"""An order cancelled before its tyres came to the point leaves the Google table (the owner 2026-11-07: «удаляй такие заказы из таблицы, он
же не пришёл на точку»): its rows are removed by the script (the op «remove»), a record of the journal says so; back from «Отмена», they are
written again. A cancel after the point and every other stage: as before. Old cancelled orders: only by the command, its dry run first."""
from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from test_order_sheet import FakeScript, events, placed, stage, turn_on
from test_webmim_create import NOW, card, db
from tires_sync import cli, order_sheet as sheet, orders
from tires_sync.secretbox import SecretBox

LATER = "2026-10-23T12:30:00"
LATEST = "2026-10-23T13:00:00"


class RemovingScript(FakeScript):
    """The script with the op «remove» (to be deployed by the owner): the row found by its mark, its CHECK cells as written, deleted."""

    def run(self, p):
        if p.get("secret") != self.secret or p.get("ping"):
            return super().run(p)
        out, rest = [], []
        for row in p["rows"]:
            if not row.get("remove"):
                rest.append(row)
                continue
            mine = self.rows.get(row["key"])
            if mine is None or any(mine.get(c) != v for c, v in (row.get("check") or {}).items()):
                out.append({"key": row["key"], "status": "missing"})
                continue
            del self.rows[row["key"]]
            out.append({"key": row["key"], "status": "removed"})
        answer = super().run(dict(p, rows=rest)) if rest else {"ok": True, "results": []}
        return dict(answer, results=out + answer["results"])


@pytest.fixture
def table(tmp_path):
    conn = db(tmp_path)
    box = SecretBox(Fernet.generate_key())
    turn_on(conn, box)
    return conn, box, RemovingScript()


def cancel(conn, card_id, now=LATER):
    orders.move_card(conn, card_id, stage(conn, "cancel")["id"], now, "owner", {"cancel_reason": "передумал"})


def written(conn, card_id):
    return json.loads(conn.execute("SELECT rows FROM sheet_order WHERE order_id = ?", (card_id,)).fetchone()[0])


def test_a_cancel_before_the_point_removes_the_rows_and_says_so(table):
    conn, box, fake = table
    mine = placed(conn)
    other = placed(conn, client_name="Пётр")
    sheet.flush(conn, box, fake, NOW)
    assert len(fake.rows) == 4
    cancel(conn, mine)
    sheet.flush(conn, box, fake, LATER)
    assert {k for k in fake.rows if k.startswith("tires:%d:" % mine)} == set()
    assert len([k for k in fake.rows if k.startswith("tires:%d:" % other)]) == 2                # the other order's rows stay
    assert "Заказ отменён до прихода на точку: строки удалены из Google-таблицы: 2 стр." in events(conn, mine)
    assert all(v == {"_removed": True} for v in written(conn, mine).values())
    removes = [r for r in fake.data_posts()[-1]["rows"] if r.get("remove")]
    assert len(removes) == 2 and all(set(r["check"]) == set(sheet.CHECK) and not r["create"] for r in removes)
    posts = len(fake.data_posts())
    orders.add_event(conn, mine, LATEST, "owner", "note", "ещё запись")
    sheet.flush(conn, box, fake, LATEST)
    assert len(fake.data_posts()) == posts and not [k for k in fake.rows if k.startswith("tires:%d:" % mine)]   # never made again on «Отмена»


def test_back_from_cancel_the_rows_are_written_again(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    cancel(conn, mine)
    sheet.flush(conn, box, fake, LATER)
    orders.move_card(conn, mine, stage(conn, "placed")["id"], LATEST, "owner")
    sheet.flush(conn, box, fake, LATEST)
    mine_rows = {k: v for k, v in fake.rows.items() if k.startswith("tires:%d:" % mine)}
    assert len(mine_rows) == 2 and all(r["Этап"] == "Заказ оформлен" and r["Артикул"] for r in mine_rows.values())
    assert "Заказ вернули из «Отмены»: строки снова записаны в Google-таблицу: 2 стр." in events(conn, mine)
    assert not any(v.get("_removed") for v in written(conn, mine).values())


def test_a_cancel_after_the_point_keeps_the_rows_as_before(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    orders.move_card(conn, mine, stage(conn, "at_point")["id"], LATER, "owner")
    sheet.flush(conn, box, fake, LATER)
    cancel(conn, mine, LATEST)
    sheet.flush(conn, box, fake, LATEST)
    rows = [v for k, v in fake.rows.items() if k.startswith("tires:%d:" % mine)]
    assert len(rows) == 2 and all(r["Этап"] == "Отмена" for r in rows)
    assert not any(r.get("remove") for p in fake.data_posts() for r in p["rows"])


@pytest.mark.parametrize("how", ["oth", "back_to_moving"])
def test_an_order_that_was_at_the_point_or_on_oth_keeps_its_rows(table, how):
    conn, box, fake = table
    mine = placed(conn)
    orders.move_card(conn, mine, stage(conn, "at_point")["id"], NOW, "owner")
    if how == "oth":
        orders.move_card(conn, mine, stage(conn, "oth")["id"], NOW, "owner", {"cancel_reason": "не пришёл"})
    orders.move_card(conn, mine, stage(conn, "moving")["id"], NOW, "owner")                # back before the point: it was there all the same
    sheet.flush(conn, box, fake, NOW)
    cancel(conn, mine)
    sheet.flush(conn, box, fake, LATER)
    assert len([k for k in fake.rows if k.startswith("tires:%d:" % mine)]) == 2


def test_a_row_the_owner_rewrote_is_not_removed(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    mark = sorted(k for k in fake.rows if k.startswith("tires:%d:" % mine))[0]
    fake.rows[mark]["Артикул"] = "чужой"
    cancel(conn, mine)
    sheet.flush(conn, box, fake, LATER)
    assert list(k for k in fake.rows if k.startswith("tires:%d:" % mine)) == [mark]
    assert any("строки удалены из Google-таблицы: 1 стр." in e for e in events(conn, mine))
    assert any("изменена или удалена вручную" in e for e in events(conn, mine))


def test_a_script_without_remove_keeps_the_rows_with_the_stage_and_says_it_once(tmp_path):
    conn = db(tmp_path)
    box = SecretBox(Fernet.generate_key())
    turn_on(conn, box)
    fake = FakeScript()                                                    # the script of today: no «remove»
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    cancel(conn, mine)
    sheet.flush(conn, box, fake, LATER)
    rows = [v for k, v in fake.rows.items() if k.startswith("tires:%d:" % mine)]
    assert len(rows) == 2 and all(r["Этап"] == "Отмена" for r in rows)
    said = [e for e in events(conn, mine) if "не умеет удалять строки" in e]
    assert len(said) == 1 and "новое развертывание Apps Script" in said[0]
    assert conn.execute("SELECT failing FROM sheet_order WHERE order_id = ?", (mine,)).fetchone()[0] == 0     # no failure, no alarm
    posts = len(fake.data_posts())
    orders.add_event(conn, mine, LATEST, "owner", "note", "ещё запись")
    sheet.flush(conn, box, fake, LATEST)
    assert len(fake.data_posts()) == posts                                 # not asked again every time


def test_merged_taken_from_oth_or_from_the_points_stock_are_left_alone(table):
    conn, box, fake = table
    mine = placed(conn, deal_kind="stock")
    sheet.flush(conn, box, fake, NOW)
    cancel(conn, mine)
    sheet.flush(conn, box, fake, LATER)
    assert len([k for k in fake.rows if k.startswith("tires:%d:" % mine)]) == 2


def test_an_order_cancelled_before_the_change_is_only_for_the_command(table, capsys, monkeypatch):
    conn, box, fake = table
    old = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    cancel(conn, old)                                                      # cancelled before the code: the rows stay with «Отмена»
    conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (sheet.SINCE, LATEST))
    conn.commit()
    sheet.flush(conn, box, fake, LATEST)
    assert len([k for k in fake.rows if k.startswith("tires:%d:" % old)]) == 2
    assert [c["id"] for c in sheet.cancelled_before_point(conn)] == [old]
    found = sheet.remove_cancelled(conn, box, fake, LATEST, apply=False)
    assert found == {old: "посчитано, без изменений"} and len([k for k in fake.rows if k.startswith("tires:%d:" % old)]) == 2
    found = sheet.remove_cancelled(conn, box, fake, LATEST, apply=True)
    assert "строки удалены из Google-таблицы: 2 стр." in found[old] and not [k for k in fake.rows if k.startswith("tires:%d:" % old)]
    assert sheet.cancelled_before_point(conn) == []
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    assert cli.main(["sheet-cancel-remove", "--db", db_path, "--dry-run"]) == 0
    assert "заказов: 0" in capsys.readouterr().out


def test_the_dry_run_of_the_command_lists_and_changes_nothing(table, capsys):
    conn, box, fake = table
    old = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    cancel(conn, old)
    conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (sheet.SINCE, LATEST))
    conn.commit()
    before = conn.execute("SELECT rows FROM sheet_order WHERE order_id = ?", (old,)).fetchone()[0]
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    assert cli.main(["sheet-cancel-remove", "--db", db_path, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "№%d" % old in out and "заказов: 1" in out and "ничего не изменено" in out
    assert conn.execute("SELECT rows FROM sheet_order WHERE order_id = ?", (old,)).fetchone()[0] == before and len(fake.posts) == 1
