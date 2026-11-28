"""A row of the Google table wiped by hand keeps only its hidden mark (the owner 28.11): the script answered «edited» and the panel stopped
following it. sheet-repair-blank (and the owner's button) asks the script of 28.11 about every row written: a row whose «Артикул»,
«Телефон», «Наименование», «Кол-во» are all empty gets the panel's data again; a row corrected by hand is only listed; a script before
28.11 writes nothing and the panel says a new deployment is needed."""
from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from test_order_sheet import FakeScript, events, placed, stage, turn_on
from test_sheet_restore import NewScript, mark_missing, written
from test_webmim_create import NOW, db
from tires_sync import cli, order_sheet as sheet, orders
from tires_sync.panel import MOSCOW_AUDIT, texts
from tires_sync.secretbox import SecretBox

LATER = "2026-10-23T12:30:00"


class RepairScript(NewScript):
    """The script of 28.11: a row with «repair» is looked at by its blank cells (knowledge/…/sheet-script-2026-11-28.gs)."""

    def run(self, p):
        if p.get("secret") == self.secret and not p.get("ping") and any(isinstance(r.get("repair"), list) for r in p.get("rows") or []):
            out, order = [], sorted(self.rows)
            for row in p["rows"]:
                cells = self.rows.get(row["key"])
                if cells is None:
                    out.append({"key": row["key"], "status": "gone", "repair": True})
                elif any(str(cells.get(c, "") or "").strip() for c in row["repair"]):
                    same = all(cells.get(c) == v for c, v in (row.get("verify") or {}).items())
                    out.append({"key": row["key"], "status": "filled" if same else "edited", "repair": True, "row": order.index(row["key"]) + 2})
                elif row.get("probe"):
                    out.append({"key": row["key"], "status": "blank", "repair": True, "row": order.index(row["key"]) + 2})
                else:
                    cells.update({c: v for c, v in row["fields"].items() if c in self.columns})
                    cells.update({c: v for c, v in (row.get("extra") or {}).items() if c in self.columns})
                    if row.get("shade") in (True, False):
                        (self.grey.add if row["shade"] else self.grey.discard)(row["key"])
                    lacking = [c for c in row["fields"] if c not in self.columns]
                    if lacking:
                        out.append({"key": row["key"], "status": "error", "error": "нет колонок: " + ", ".join(lacking) + " (остальное записано)",
                                    "repair": True, "partial": True, "row": order.index(row["key"]) + 2})
                        continue
                    out.append({"key": row["key"], "status": "repaired", "repair": True, "row": order.index(row["key"]) + 2})
            return {"ok": True, "results": out}
        return super().run(p)


def wipe(fake, mark):
    """Every cell of the row cleared by hand: only the hidden mark is left."""
    fake.rows[mark] = {}


@pytest.fixture
def table(tmp_path):
    conn = db(tmp_path)
    box = SecretBox(Fernet.generate_key())
    turn_on(conn, box)
    fake = RepairScript()
    wiped, edited, whole = placed(conn), placed(conn, client_name="Пётр"), placed(conn, client_name="Анна")
    sheet.flush(conn, box, fake, NOW)
    marks = {o: sorted(m for m in fake.rows if m.startswith("tires:%d:" % o)) for o in (wiped, edited, whole)}
    before = dict(fake.rows[marks[wiped][0]])
    wipe(fake, marks[wiped][0])
    fake.rows[marks[edited][0]]["Телефон"] = "89990000000"                 # corrected by hand
    for o in (wiped, edited):
        mark_missing(conn, o, marks[o][:1])                                # as send_order left them («edited»)
    return conn, box, fake, {"wiped": (wiped, marks[wiped][0]), "edited": (edited, marks[edited][0]), "whole": (whole, marks[whole][0])}, before


def test_the_dry_run_lists_the_wiped_and_the_edited_and_writes_nothing(table):
    conn, box, fake, rows, before = table
    keep_rows = json.dumps(fake.rows, sort_keys=True, ensure_ascii=False)
    keep_base = {o: written(conn, o) for o, _ in rows.values()}
    done = sheet.repair_blank(conn, box, fake, LATER, dry_run=True)
    by_key = {d["key"]: d for d in done}
    wiped = by_key[rows["wiped"][1]]
    assert wiped["result"] == "пустая — будет записано" and "Артикул: %s" % before["Артикул"] in wiped["values"]
    assert wiped["sheet_row"] == sorted(fake.rows).index(rows["wiped"][1]) + 2           # the row of the sheet, for the owner to compare
    assert by_key[rows["edited"][1]]["result"].startswith("правлена вручную")
    assert rows["whole"][1] not in by_key                                  # a row as written is not listed
    assert json.dumps(fake.rows, sort_keys=True, ensure_ascii=False) == keep_rows
    assert {o: written(conn, o) for o, _ in rows.values()} == keep_base
    probes = [r for p in fake.data_posts()[-1:] for r in p["rows"]]
    assert probes and all(r["probe"] and r["fields"] == {} and r["create"] is False and r["check"] == sheet.REPAIR_CHECK for r in probes)


def test_a_wiped_row_gets_the_panel_data_back_and_is_followed_again(table):
    conn, box, fake, rows, before = table
    done = sheet.repair_blank(conn, box, fake, LATER, author="owner")
    by_key = {d["key"]: d["result"] for d in done}
    order, mark = rows["wiped"]
    assert by_key[mark] == "записана заново"
    assert fake.rows[mark] == before and "Примечание" not in fake.rows[mark]
    assert not written(conn, order)[mark].get("_missing")
    assert any(e == sheet.REPAIRED % 1 for e in events(conn, order))
    assert by_key[rows["edited"][1]].startswith("правлена вручную")
    assert fake.rows[rows["edited"][1]]["Телефон"] == "89990000000" and written(conn, rows["edited"][0])[rows["edited"][1]]["_missing"] is True
    audit = conn.execute("SELECT detail FROM audit WHERE action = 'sheet_repair_blank'").fetchone()
    assert json.loads(audit[0]) == {"rows": 1, "orders": 1, "remade": 0}
    assert "sheet_repair_blank" in MOSCOW_AUDIT
    assert texts.audit_text({"action": "sheet_repair_blank", "detail": json.loads(audit[0])}).startswith("Google-таблица: строки, стёртые вручную")
    again = sheet.repair_blank(conn, box, fake, LATER)                     # again: only the edited one is left
    assert [d["key"] for d in again] == [rows["edited"][1]]


def test_the_old_script_writes_nothing_and_says_a_new_deployment(table):
    conn, box, fake, rows, before = table
    old = NewScript()
    old.rows = fake.rows
    keep = json.dumps(fake.rows, sort_keys=True, ensure_ascii=False)
    with pytest.raises(sheet.SheetError, match="новое развертывание"):
        sheet.repair_blank(conn, box, old, LATER)
    oldest = FakeScript()                                                  # before 2026-11-11 too
    oldest.rows = fake.rows
    with pytest.raises(sheet.SheetError, match="новое развертывание"):
        sheet.repair_blank(conn, box, oldest, LATER)
    assert json.dumps(fake.rows, sort_keys=True, ensure_ascii=False) == keep
    assert written(conn, rows["wiped"][0])[rows["wiped"][1]]["_missing"] is True


def test_the_command_dry_run_and_run(table, monkeypatch, capsys, tmp_path):
    conn, box, fake, rows, before = table
    key = tmp_path / "secret.key"
    key.write_text("заглушка", encoding="utf-8")
    monkeypatch.setattr(cli, "load_box", lambda key_file: box)
    monkeypatch.setattr(cli, "_http", lambda: fake)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    assert cli.main(["sheet-repair-blank", "--db", db_path, "--key-file", str(key), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "№%d · %s · строка листа " % rows["wiped"] in out and "пустая — будет записано: Артикул:" in out and "(проверка: в таблицу и в базу ничего не записано)" in out
    assert fake.rows[rows["wiped"][1]] == {}
    assert cli.main(["sheet-repair-blank", "--db", db_path, "--key-file", str(key)]) == 0
    out = capsys.readouterr().out
    assert "№%d · %s · строка листа " % rows["wiped"] in out and "записана заново" in out and fake.rows[rows["wiped"][1]] == before
    assert cli.main(["sheet-repair-blank", "--db", db_path]) == 1          # no key: nothing sent
    assert "Секрет Google-таблицы не задан" in capsys.readouterr().err


def test_a_row_without_its_mark_is_made_again_at_the_end(table):
    conn, box, fake, rows, before = table
    order, mark = rows["whole"]
    kept = dict(fake.rows.pop(mark))                                       # the hidden cell cleared too: the mark is nowhere
    dry = {d["key"]: d for d in sheet.repair_blank(conn, box, fake, LATER, dry_run=True)}
    assert dry[mark]["result"].startswith("метки на листе нет — будет создана заново") and dry[mark]["sheet_row"] is None
    assert mark not in fake.rows
    done = {d["key"]: d["result"] for d in sheet.repair_blank(conn, box, fake, LATER)}
    assert done[mark] == sheet.REMADE_SAID and fake.rows[mark] == kept
    assert any(e == sheet.REMADE % 1 for e in events(conn, order))
    detail = json.loads(conn.execute("SELECT detail FROM audit WHERE action = 'sheet_repair_blank'").fetchone()[0])
    assert detail == {"rows": 2, "orders": 2, "remade": 1}
    assert "создано в конце листа (метки не было): 1" in texts.audit_text({"action": "sheet_repair_blank", "detail": detail})


def test_a_row_without_its_mark_of_a_closed_order_is_only_listed(table):
    conn, box, fake, rows, before = table
    order, mark = rows["whole"]
    fake.rows.pop(mark)
    orders.move_card(conn, order, stage(conn, "done")["id"], LATER, "owner")
    sheet.flush(conn, box, fake, LATER)                                    # the move goes as any other (the row is gone: made again)
    fake.rows.pop(mark, None)                                              # and wiped again, mark and all
    for dry in (True, False):
        done = {d["key"]: d["result"] for d in sheet.repair_blank(conn, box, fake, LATER, dry_run=dry)}
        assert done[mark] == sheet.GONE_CLOSED and mark not in fake.rows


def test_a_failed_chunk_is_an_error_of_its_own_rows_only(table, monkeypatch):
    conn, box, fake, rows, before = table
    second = rows["whole"]
    wipe(fake, second[1])
    mark_missing(conn, second[0], [second[1]])
    monkeypatch.setattr(sheet, "REPAIR_CHUNK", 1)
    probes = len(sheet.repair_candidates(conn))
    fake.fail = [None] * probes + [None, "500"]                            # the second chunk of writing fails
    done = {d["key"]: d["result"] for d in sheet.repair_blank(conn, box, fake, LATER)}
    first = rows["wiped"]
    assert done[first[1]] == "записана заново" and not written(conn, first[0])[first[1]].get("_missing")
    assert done[second[1]].startswith("ошибка") and written(conn, second[0])[second[1]]["_missing"] is True
    assert fake.rows[second[1]] == {}


def test_the_fill_follows_the_order_both_ways(table):
    conn, box, fake, rows, before = table
    order, mark = rows["wiped"]
    fake.grey.add(mark)                                                    # a fill left on the wiped row of a placed order
    sheet.repair_blank(conn, box, fake, LATER)
    assert mark not in fake.grey and written(conn, order)[mark]["_grey"] is False
    back = rows["whole"]
    orders.move_card(conn, back[0], stage(conn, "new")["id"], LATER, "owner")
    sheet.flush(conn, box, fake, LATER)
    wipe(fake, back[1])
    fake.grey.discard(back[1])                                             # the fill taken off by hand with the values
    sheet.repair_blank(conn, box, fake, LATER)
    assert back[1] in fake.grey and written(conn, back[0])[back[1]]["_grey"] is True


def test_the_note_is_never_written_by_the_repair(table, monkeypatch):
    conn, box, fake, rows, before = table
    order, mark = rows["wiped"]
    real = sheet.rows_of

    def with_note(conn_, card_id, placed_at, **kw):                        # a sale from OTH: the panel's «Примечание» (NOTE_FLAG)
        out = real(conn_, card_id, placed_at, **kw)
        if card_id == order:
            out[mark] = dict(out[mark], **{sheet.NOTE_FLAG: True, "Примечание": "продано с ОТХ"})
        return out
    monkeypatch.setattr(sheet, "rows_of", with_note)
    sheet.repair_blank(conn, box, fake, LATER)
    sent = [r for p in fake.data_posts() for r in p["rows"] if r["key"] == mark and not r.get("probe")]
    assert sent and all("Примечание" not in r["fields"] and "Примечание" not in (r.get("extra") or {}) for r in sent)
    assert "Примечание" not in fake.rows[mark] and written(conn, order)[mark]["Примечание"] == "продано с ОТХ"
    before_posts = len(fake.data_posts())
    sheet.flush(conn, box, fake, LATER)                                    # the next send does not write it either
    assert all("Примечание" not in (r.get("extra") or {}) for p in fake.data_posts()[before_posts:] for r in p["rows"] if r["key"] == mark)


def test_a_row_written_without_some_column_is_said_and_listed_later(table):
    conn, box, fake, rows, before = table
    order, mark = rows["wiped"]
    fake.columns.remove("ПВЗ")
    done = {d["key"]: d["result"] for d in sheet.repair_blank(conn, box, fake, LATER)}
    assert done[mark].startswith("записана не полностью (нет колонок: ПВЗ")
    assert fake.rows[mark]["Артикул"] == before["Артикул"] and not written(conn, order)[mark].get("_missing")
    later = {d["key"]: d["result"] for d in sheet.repair_blank(conn, box, fake, LATER, dry_run=True)}
    assert later[mark].startswith("записана не полностью")                 # never «filled» in silence
    assert "ПВЗ" not in written(conn, order)[mark] and "ПВЗ" not in fake.rows[mark]   # not kept as written: it goes again
    still = {d["key"]: d["result"] for d in sheet.repair_blank(conn, box, fake, LATER)}
    assert still[mark].startswith("записана не полностью")                 # the column is still not there
    fake.columns.append("ПВЗ")                                             # the owner put the column back
    after = {d["key"]: d["result"] for d in sheet.repair_blank(conn, box, fake, LATER)}
    assert after[mark] == "дописана: ПВЗ" and fake.rows[mark] == before
    assert sheet.PARTIAL not in written(conn, order)[mark] and written(conn, order)[mark]["ПВЗ"] == before["ПВЗ"]
    assert mark not in {d["key"] for d in sheet.repair_blank(conn, box, fake, LATER)}   # done: no longer listed
    assert conn.execute("SELECT COUNT(*) FROM sheet_order WHERE claim_until != ''").fetchone()[0] == 0


def test_the_claims_are_freed_when_the_writing_fails(table, monkeypatch):
    conn, box, fake, rows, before = table
    real = sheet._repair_write

    def broken(*a, **kw):
        raise sheet.SheetError("сбой")
    monkeypatch.setattr(sheet, "_repair_write", broken)
    with pytest.raises(sheet.SheetError):
        sheet.repair_blank(conn, box, fake, LATER)
    assert conn.execute("SELECT COUNT(*) FROM sheet_order WHERE claim_until != ''").fetchone()[0] == 0
    assert real
