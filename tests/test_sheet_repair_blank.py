"""A row of the Google table wiped by hand keeps only its hidden mark (the owner 28.11): the script answered «edited» and the panel stopped
following it. sheet-repair-blank (and the owner's button) asks the script of 28.11 about every row written: a row whose «Артикул»,
«Телефон», «Наименование», «Кол-во» are all empty gets the panel's data again; a row corrected by hand is only listed; a script before
28.11 writes nothing and the panel says a new deployment is needed."""
from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from panel_helpers import make_app, shared_mark_of
from test_order_sheet import FakeScript, events, placed, turn_on
from test_roles import person
from test_sheet_restore import NewScript, mark_missing, written
from test_webmim_create import NOW, db
from tires_sync import cli, order_sheet as sheet, orders, store, users
from tires_sync.panel import MOSCOW_AUDIT, texts
from tires_sync.secretbox import SecretBox

LATER = "2026-10-23T12:30:00"


class RepairScript(NewScript):
    """The script of 28.11: a row with «repair» is looked at by its blank cells (knowledge/…/sheet-script-2026-11-28.gs)."""

    def run(self, p):
        if p.get("secret") == self.secret and not p.get("ping") and any(isinstance(r.get("repair"), list) for r in p.get("rows") or []):
            out = []
            for row in p["rows"]:
                cells = self.rows.get(row["key"])
                if cells is None:
                    out.append({"key": row["key"], "status": "gone", "repair": True})
                elif any(str(cells.get(c, "") or "").strip() for c in row["repair"]):
                    same = all(cells.get(c) == v for c, v in (row.get("verify") or {}).items())
                    out.append({"key": row["key"], "status": "filled" if same else "edited", "repair": True})
                elif row.get("probe"):
                    out.append({"key": row["key"], "status": "blank", "repair": True})
                else:
                    cells.update(row["fields"])
                    cells.update({c: v for c, v in (row.get("extra") or {}).items() if c in self.columns})
                    out.append({"key": row["key"], "status": "repaired", "repair": True})
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
    assert json.loads(audit[0]) == {"rows": 1, "orders": 1}
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
    assert "№%d · %s · пустая — будет записано: Артикул:" % rows["wiped"] in out and "(проверка: в таблицу и в базу ничего не записано)" in out
    assert fake.rows[rows["wiped"][1]] == {}
    assert cli.main(["sheet-repair-blank", "--db", db_path, "--key-file", str(key)]) == 0
    out = capsys.readouterr().out
    assert "№%d · %s · записана заново" % rows["wiped"] in out and fake.rows[rows["wiped"][1]] == before
    assert cli.main(["sheet-repair-blank", "--db", db_path]) == 1          # no key: nothing sent
    assert "Секрет Google-таблицы не задан" in capsys.readouterr().err


def test_the_button_is_the_owners_and_shows_the_result(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    box, fake = SecretBox(Fernet.generate_key()), RepairScript()
    app = make_app(tmp_path, SECRET_BOX=box, SHEET_HTTP=fake, SHEET_RUN=lambda work: work())
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    turn_on(conn, box)
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    mark = sorted(fake.rows)[0]
    whole = dict(fake.rows[mark])
    wipe(fake, mark)
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "owner", 0, "tok"
        session["pw"] = shared_mark_of(client.application)
    assert "Строки, стёртые вручную" in client.get("/connections/sheet").get_data(as_text=True)
    assert client.post("/connections/sheet/repair", data={"csrf": "tok", "dry": "1"}).status_code == 302
    page = client.get("/connections/sheet").get_data(as_text=True)
    assert "пустая — будет записано" in page and fake.rows[mark] == {}
    assert client.post("/connections/sheet/repair", data={"csrf": "tok", "dry": "0"}).status_code == 302
    assert fake.rows[mark] == whole and "записана заново" in client.get("/connections/sheet").get_data(as_text=True)
    partner, _ = person(app, "partner")
    assert partner.post("/connections/sheet/repair", data={"csrf": "tok", "dry": "1"}).status_code == 403
    assert "Строки, стёртые вручную" not in partner.get("/connections/sheet").get_data(as_text=True)
    assert mine
