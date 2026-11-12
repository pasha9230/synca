"""A row of the Google table that went away comes back; one rewritten by hand stays alone (the owner 2026-11-11). The script from that day
tells «gone» (the hidden mark «tires_id» is nowhere: deleted by hand or by a failure) from «edited» (the mark is there, «Артикул» or
«Телефон» is another): «gone» is sent again at once with every column, «edited» is marked `_missing` as before. The script before that
says «missing» for both: the panel does as it did. A row the panel removed before 2026-11-11 (`_removed`) never comes back. The command
sheet-restore-missing asks once about the rows marked `_missing` before."""
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
REMADE = "Строка Google-таблицы пропала — создана заново"
EDITED = "Строка Google-таблицы изменена вручную (артикул/телефон) — проверьте её; обновления этой строки не идут"
OLD = "Строка Google-таблицы изменена или удалена вручную (другой артикул или строки нет)"


class NewScript(FakeScript):
    """The script of 2026-11-11: «gone» for a mark it has not, «edited» for a row whose code or phone is another; no «remove»."""

    def run(self, p):
        if p.get("rows") and any(r.get("remove") for r in p["rows"]):
            return {"ok": True, "results": [{"key": r["key"], "status": "error", "error": "удаление строк отключено"} for r in p["rows"]]}
        answer = super().run(p)
        for row, out in zip(p.get("rows") or [], answer.get("results") or []):
            if out["status"] == "missing":
                out["status"] = "edited" if row["key"] in self.rows else "gone"
        return answer


def written(conn, card_id):
    return json.loads(conn.execute("SELECT rows FROM sheet_order WHERE order_id = ?", (card_id,)).fetchone()[0])


def mark_missing(conn, card_id, marks):
    rows = written(conn, card_id)
    for m in marks:
        rows[m]["_missing"] = True
    conn.execute("UPDATE sheet_order SET rows = ? WHERE order_id = ?", (json.dumps(rows, ensure_ascii=False), card_id))
    conn.commit()


@pytest.fixture
def table(tmp_path):
    conn = db(tmp_path)
    box = SecretBox(Fernet.generate_key())
    turn_on(conn, box)
    return conn, box, NewScript()


def test_a_gone_row_is_made_again_at_once_with_every_column_and_said(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    lost = sorted(fake.rows)[0]
    whole = dict(fake.rows[lost])
    del fake.rows[lost]                                                    # deleted by hand
    orders.move_card(conn, mine, stage(conn, "at_point")["id"], LATER, "pavel")
    sheet.flush(conn, box, fake, LATER)
    again = fake.data_posts()[-1]["rows"]
    assert [r["key"] for r in again] == [lost] and again[0]["create"] is True and set(again[0]["fields"]) == set(sheet.SENT)
    assert fake.rows[lost] == dict(whole, Этап="На точке, ждём клиента")
    assert any(e.startswith(REMADE) and e.endswith("1 стр.") for e in events(conn, mine))
    assert not any(e.startswith(OLD) or e.startswith(EDITED) for e in events(conn, mine))
    assert not written(conn, mine)[lost].get("_missing")
    orders.move_card(conn, mine, stage(conn, "done")["id"], LATEST, "pavel")       # the next change follows the row as any other
    sheet.flush(conn, box, fake, LATEST)
    assert fake.rows[lost]["Этап"] == fake.rows[sorted(fake.rows)[1]]["Этап"] != "На точке, ждём клиента"
    assert sum(e.startswith(REMADE) for e in events(conn, mine)) == 1


def test_a_gone_row_of_a_grey_order_comes_back_grey(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    lost = sorted(fake.rows)[0]
    del fake.rows[lost]
    orders.move_card(conn, mine, stage(conn, "talk")["id"], LATER, "pavel")         # back before «Заказ оформлен»: the fill goes
    sheet.flush(conn, box, fake, LATER)
    assert lost in fake.rows and lost in fake.grey and written(conn, mine)[lost]["_grey"] is True


def test_a_failed_second_send_leaves_the_row_to_go_again(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    lost = sorted(fake.rows)[0]
    del fake.rows[lost]
    orders.move_card(conn, mine, stage(conn, "at_point")["id"], LATER, "pavel")
    fake.fail = [None, "timeout"]                                          # the check answers, the creation does not
    sheet.flush(conn, box, fake, LATER)
    assert lost not in fake.rows and not written(conn, mine)[lost].get("_missing")
    assert conn.execute("SELECT failing FROM sheet_order WHERE order_id = ?", (mine,)).fetchone()[0] == 1
    sheet.flush(conn, box, fake, LATEST, full=True)                         # the retry: asked again, gone again, made
    assert lost in fake.rows and fake.rows[lost]["Этап"] == "На точке, ждём клиента"
    assert any(e.startswith(REMADE) for e in events(conn, mine))


def test_an_edited_row_is_marked_left_alone_and_said(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    first = sorted(fake.rows)[0]
    fake.rows[first]["Артикул"] = "чужой"
    orders.move_card(conn, mine, stage(conn, "at_point")["id"], LATER, "pavel")
    sheet.flush(conn, box, fake, LATER)
    assert fake.rows[first]["Артикул"] == "чужой" and fake.rows[first]["Этап"] == "Заказ оформлен"
    assert written(conn, mine)[first]["_missing"] is True
    assert any(e.startswith(EDITED) for e in events(conn, mine))
    assert not any(e.startswith(REMADE) or e.startswith(OLD) for e in events(conn, mine))
    posts = len(fake.data_posts())
    orders.move_card(conn, mine, stage(conn, "done")["id"], LATEST, "pavel")
    sheet.flush(conn, box, fake, LATEST)
    assert first not in [r["key"] for p in fake.data_posts()[posts:] for r in p["rows"]]     # no updates of that row


def test_missing_of_the_old_script_does_as_before(tmp_path):
    conn = db(tmp_path)
    box = SecretBox(Fernet.generate_key())
    turn_on(conn, box)
    fake = FakeScript()                                                    # the script before 2026-11-11
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    lost = sorted(fake.rows)[0]
    del fake.rows[lost]
    orders.move_card(conn, mine, stage(conn, "at_point")["id"], LATER, "pavel")
    posts = len(fake.data_posts())
    sheet.flush(conn, box, fake, LATER)
    assert len(fake.data_posts()) == posts + 1 and lost not in fake.rows   # one send, nothing made again
    assert written(conn, mine)[lost]["_missing"] is True
    assert any(e.startswith(OLD) for e in events(conn, mine))
    assert not any(e.startswith(REMADE) or e.startswith(EDITED) for e in events(conn, mine))


def test_a_row_removed_before_never_comes_back(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    rows = written(conn, mine)
    conn.execute("UPDATE sheet_order SET rows = ? WHERE order_id = ?",
                 (json.dumps({m: dict(v, _removed=True, _missing=True) for m, v in rows.items()}, ensure_ascii=False), mine))
    conn.commit()
    for m in rows:
        del fake.rows[m]
    posts = len(fake.posts)
    orders.move_card(conn, mine, stage(conn, "at_point")["id"], LATER, "pavel")
    sheet.flush(conn, box, fake, LATER, full=True)
    assert sheet.restore_candidates(conn) == []
    assert sheet.restore_missing(conn, box, fake, LATER) == []
    assert len(fake.posts) == posts and not fake.rows


def command_table(tmp_path, monkeypatch):
    conn = db(tmp_path)
    box = SecretBox(Fernet.generate_key())
    turn_on(conn, box)
    fake = NewScript()
    key = tmp_path / "secret.key"
    key.write_text("заглушка", encoding="utf-8")                           # load_box is replaced: the box of the test
    monkeypatch.setattr(cli, "load_box", lambda key_file: box)
    monkeypatch.setattr(cli, "_http", lambda: fake)
    gone_one, edited_one, back_one, closed = placed(conn), placed(conn, client_name="Пётр"), placed(conn, client_name="Анна"), placed(conn, client_name="Ольга")
    sheet.flush(conn, box, fake, NOW)
    marks = {o: sorted(m for m in fake.rows if m.startswith("tires:%d:" % o)) for o in (gone_one, edited_one, back_one, closed)}
    for o in (gone_one, edited_one, back_one, closed):
        mark_missing(conn, o, marks[o][:1])                                # as the old script left them
    del fake.rows[marks[gone_one][0]]
    del fake.rows[marks[closed][0]]
    fake.rows[marks[edited_one][0]]["Телефон"] = "89990000000"
    orders.move_card(conn, closed, stage(conn, "done")["id"], NOW, "owner")
    sheet.flush(conn, box, fake, NOW)
    return conn, fake, str(key), {"gone": (gone_one, marks[gone_one][0]), "edited": (edited_one, marks[edited_one][0]),
                                   "back": (back_one, marks[back_one][0]), "closed": (closed, marks[closed][0])}


def test_the_command_dry_run_lists_and_sends_nothing(tmp_path, monkeypatch, capsys):
    conn, fake, key, rows = command_table(tmp_path, monkeypatch)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    before = {o: written(conn, o) for o, _ in rows.values()}
    posts = len(fake.posts)
    assert cli.main(["sheet-restore-missing", "--db", db_path, "--dry-run"]) == 0
    out = capsys.readouterr().out
    for name in ("gone", "edited", "back"):
        assert "№%d · %s · проверка не отправлялась" % rows[name] in out
    assert "№%d ·" % rows["closed"][0] not in out                          # a closed order is not looked at
    assert "строк: 3 (проверка: ничего не отправлено и не записано)" in out
    assert len(fake.posts) == posts and {o: written(conn, o) for o, _ in rows.values()} == before


def test_the_command_makes_the_gone_leaves_the_edited_and_frees_the_one_back(tmp_path, monkeypatch, capsys):
    conn, fake, key, rows = command_table(tmp_path, monkeypatch)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    assert cli.main(["sheet-restore-missing", "--db", db_path, "--key-file", key]) == 0
    out = capsys.readouterr().out
    assert "№%d · %s · создана заново" % rows["gone"] in out
    assert "№%d · %s · изменена вручную, оставлена" % rows["edited"] in out
    assert "№%d · %s · на месте, обновления снова идут" % rows["back"] in out
    asked = [r for p in fake.data_posts() for r in p["rows"] if r["key"] in {m for _, m in rows.values()} and not r["fields"]]
    assert asked and all(r["create"] is False and set(r["check"]) == set(sheet.CHECK) for r in asked)   # the check creates nothing
    gone_order, gone_mark = rows["gone"]
    assert gone_mark in fake.rows and not written(conn, gone_order)[gone_mark].get("_missing")
    assert any(e.startswith(REMADE) for e in events(conn, gone_order))
    assert written(conn, rows["edited"][0])[rows["edited"][1]]["_missing"] is True
    assert fake.rows[rows["edited"][1]]["Телефон"] == "89990000000"
    assert not written(conn, rows["back"][0])[rows["back"][1]].get("_missing")
    assert rows["closed"][1] not in fake.rows                              # a closed order is not asked about
    assert cli.main(["sheet-restore-missing", "--db", db_path, "--key-file", key]) == 0      # again: only the edited one is left
    out = capsys.readouterr().out
    assert "строк: 1" in out and "№%d · %s · изменена вручную, оставлена" % rows["edited"] in out


def test_the_command_with_the_old_script_changes_nothing(tmp_path, monkeypatch, capsys):
    conn, fake, key, rows = command_table(tmp_path, monkeypatch)
    old = FakeScript()
    old.rows = fake.rows
    monkeypatch.setattr(cli, "_http", lambda: old)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    assert cli.main(["sheet-restore-missing", "--db", db_path, "--key-file", key]) == 0
    out = capsys.readouterr().out
    assert "№%d · %s · старый скрипт: не различает, оставлена" % rows["gone"] in out
    assert rows["gone"][1] not in fake.rows and written(conn, rows["gone"][0])[rows["gone"][1]]["_missing"] is True


def test_the_command_without_a_key_sends_nothing(tmp_path, monkeypatch, capsys):
    conn, fake, key, rows = command_table(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "load_box", lambda key_file: None)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    posts = len(fake.posts)
    assert cli.main(["sheet-restore-missing", "--db", db_path]) == 1
    assert "Секрет Google-таблицы не задан" in capsys.readouterr().err and len(fake.posts) == posts
