"""«Сверить таблицу с панелью» (the owner 28.11): old versions of the sheet were brought back, its cells are not what the panel wrote. The
panel is the truth (nobody changes the sheet by hand): sheet-resync sends every row written whole, the script of 28.11 writes the cells
that differ — a row cleared or rewritten by hand too —, never «Примечание»; the profit of old orders is not counted again; a row without
a mark is made again for an open order (a closed one: only when asked). After it the rows are followed again and a second look finds
nothing. A script before 28.11 writes nothing."""
from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from panel_helpers import make_app, shared_mark_of
from test_order_sheet import FakeScript, events, placed, stage, turn_on
from test_roles import person
from test_sheet_repair_blank import RepairScript
from test_sheet_restore import NewScript, mark_missing, written
from test_webmim_create import NOW, db
from tires_sync import cli, order_sheet as sheet, orders, store, users
from tires_sync.panel import MOSCOW_AUDIT, texts
from tires_sync.secretbox import SecretBox

LATER = "2026-10-23T12:30:00"


def filled(cells):
    """The cells with something in them: an empty cell is the same as a value the panel leaves empty."""
    return {k: v for k, v in (cells or {}).items() if v not in ("", None)}


class SyncScript(RepairScript):
    """The script of 28.11 with «sync» (knowledge/…/sheet-script-2026-11-28.gs, syncRow): the cells that differ are written."""

    def run(self, p):
        if p.get("secret") == self.secret and not p.get("ping") and any(r.get("sync") for r in p.get("rows") or []):
            out, order = [], sorted(self.rows)
            for row in p["rows"]:
                cells = self.rows.get(row["key"])
                if cells is None:
                    out.append({"key": row["key"], "status": "gone", "sync": True})
                    continue
                values = dict(row["fields"], **(row.get("extra") or {}))
                changed, lacking = {}, []
                for name, value in values.items():
                    if name == "Примечание":
                        continue
                    if name not in self.columns:
                        lacking += [name] if name in row["fields"] else []
                        continue
                    if str(cells.get(name, "")).strip() != str(value).strip():
                        changed[name] = [str(cells.get(name, "")), str(value)]
                blank = all(not str(cells.get(c, "")).strip() for c in row["repair"])
                edited = not blank and any(cells.get(c) != v for c, v in row["verify"].items())
                grey = row["key"] in self.grey
                fill = (not grey) if row.get("shade") is True else (row.get("shade") is False and grey)
                if fill:
                    changed["заливка"] = ["серая" if grey else "нет", "серая" if row["shade"] else "нет"]
                if not row.get("probe"):
                    cells.update({k: values[k] for k in changed if k != "заливка"})
                    if fill:
                        (self.grey.add if row["shade"] else self.grey.discard)(row["key"])
                out.append({"key": row["key"], "status": ("differs" if row.get("probe") else "synced") if changed else "same", "changed": changed,
                            "lacking": lacking, "blank": blank, "edited": edited, "sync": True, "row": order.index(row["key"]) + 2})
            return {"ok": True, "results": out}
        return super().run(p)


@pytest.fixture
def table(tmp_path):
    conn = db(tmp_path)
    box = SecretBox(Fernet.generate_key())
    turn_on(conn, box)
    fake = SyncScript()
    staged, wiped, edited, gone_open, gone_closed = (placed(conn), placed(conn, client_name="Пётр"), placed(conn, client_name="Анна"),
                                                    placed(conn, client_name="Ольга"), placed(conn, client_name="Нина"))
    sheet.flush(conn, box, fake, NOW)
    orders.move_card(conn, gone_closed, stage(conn, "done")["id"], NOW, "owner")
    sheet.flush(conn, box, fake, NOW)
    first = lambda o: sorted(m for m in fake.rows if m.startswith("tires:%d:" % o))[0]
    rows = {"staged": first(staged), "wiped": first(wiped), "edited": first(edited), "gone_open": first(gone_open), "gone_closed": first(gone_closed)}
    orders_of = {"staged": staged, "wiped": wiped, "edited": edited, "gone_open": gone_open, "gone_closed": gone_closed}
    whole = {k: dict(fake.rows[m]) for k, m in rows.items()}
    fake.rows[rows["staged"]].update({"Этап": "Новый заказ", "Реализация": "старое"})       # an old version brought back
    fake.rows[rows["wiped"]] = {}                                                           # cleared, the mark left
    fake.rows[rows["edited"]]["Артикул"] = "999"                                            # rewritten by hand
    mark_missing(conn, edited, [rows["edited"]])
    del fake.rows[rows["gone_open"]], fake.rows[rows["gone_closed"]]                        # the mark gone too
    return conn, box, fake, rows, orders_of, whole


def test_the_look_lists_every_difference_and_writes_nothing(table):
    conn, box, fake, rows, orders_of, whole = table
    keep = json.dumps(fake.rows, sort_keys=True, ensure_ascii=False)
    base = {o: written(conn, o) for o in orders_of.values()}
    done = sheet.resync(conn, box, fake, LATER, dry_run=True)
    by = {d["key"]: d for d in done["rows"]}
    staged = by[rows["staged"]]
    assert staged["result"] == "будет исправлена" and "Этап: Новый заказ → %s" % whole["staged"]["Этап"] in staged["values"]
    assert staged["sheet_row"] == sorted(fake.rows).index(rows["staged"]) + 2
    assert by[rows["wiped"]]["result"] == "будет исправлена (стёрта вручную)"
    assert by[rows["edited"]]["result"] == "будет исправлена (артикул/телефон правили вручную)" and "Артикул: 999 → " in by[rows["edited"]]["values"]
    assert by[rows["gone_open"]]["result"].startswith("метки на листе нет — будет создана заново")
    assert by[rows["gone_closed"]]["result"].startswith("метки на листе нет, заказ закрыт — не создаём")
    s = done["summary"]
    assert (s["orders"], s["differs"], s["blank"], s["edited"], s["gone_open"], s["gone_closed"], s["errors"]) == (5, 3, 1, 1, 1, 1, 0)
    assert s["same"] == s["rows"] - 5
    line = sheet.summary_line(s, True)
    assert line.startswith("Заказов проверено: 5, строк: %d; как в панели: %d; расходятся: 3 (из них стёрты: 1, артикул/телефон правили: 1)" % (s["rows"], s["same"]))
    assert json.dumps(fake.rows, sort_keys=True, ensure_ascii=False) == keep and {o: written(conn, o) for o in orders_of.values()} == base
    probes = [r for p in fake.data_posts()[-1:] for r in p["rows"]]
    assert all(r["probe"] and r["sync"] and r["check"] == sheet.REPAIR_CHECK and "Примечание" not in r["fields"] for r in probes)


def test_the_fix_sets_every_row_right_and_a_second_look_finds_nothing(table):
    conn, box, fake, rows, orders_of, whole = table
    done = sheet.resync(conn, box, fake, LATER, author="owner")
    for k in ("staged", "wiped", "edited", "gone_open"):
        assert filled(fake.rows[rows[k]]) == filled(whole[k]), k
    assert rows["gone_closed"] not in fake.rows
    assert not written(conn, orders_of["edited"])[rows["edited"]].get("_missing")
    assert any(e.startswith("Google-таблица сверена с панелью: исправлено 1 стр.") for e in events(conn, orders_of["staged"]))
    assert any(e == sheet.REMADE % 1 for e in events(conn, orders_of["gone_open"]))
    assert (done["summary"]["fixed"], done["summary"]["created"]) == (3, 1)
    detail = json.loads(conn.execute("SELECT detail FROM audit WHERE action = 'sheet_resync'").fetchone()[0])
    assert detail["fixed"] == 3 and detail["created"] == 1 and "sheet_resync" in MOSCOW_AUDIT
    assert texts.audit_text({"action": "sheet_resync", "detail": detail}).startswith("Google-таблица сверена с панелью")
    again = sheet.resync(conn, box, fake, LATER, dry_run=True)["summary"]
    assert (again["differs"], again["gone_open"], again["gone_closed"], again["errors"]) == (0, 0, 1, 0)
    sheet.resync(conn, box, fake, LATER, closed_too=True)                   # the owner asked: the closed one too
    assert fake.rows[rows["gone_closed"]] == whole["gone_closed"]
    last = sheet.resync(conn, box, fake, LATER, dry_run=True)["summary"]
    assert (last["differs"], last["gone_open"], last["gone_closed"], last["same"]) == (0, 0, 0, last["rows"])
    assert conn.execute("SELECT COUNT(*) FROM sheet_order WHERE claim_until != ''").fetchone()[0] == 0


def test_a_row_set_right_follows_the_order_again(table):
    conn, box, fake, rows, orders_of, whole = table
    sheet.resync(conn, box, fake, LATER)
    order = orders_of["edited"]
    orders.move_card(conn, order, stage(conn, "at_point")["id"], LATER, "owner")
    sheet.flush(conn, box, fake, LATER)
    assert fake.rows[rows["edited"]]["Этап"] == "На точке, ждём клиента"


def test_the_note_is_never_written_and_old_profit_is_not_counted_again(table):
    conn, box, fake, rows, orders_of, whole = table
    mark, order = rows["staged"], orders_of["staged"]
    fake.rows[mark]["Примечание"] = "текст владельца"
    kept = written(conn, order)
    kept[mark]["Прибыль ₽"] = 111.0                                        # what was written before KEEPS_BEFORE
    conn.execute("UPDATE sheet_order SET rows = ? WHERE order_id = ?", (json.dumps(kept, ensure_ascii=False), order))
    conn.commit()
    fake.rows[mark]["Прибыль ₽"] = 999.0
    sheet.resync(conn, box, fake, LATER)
    assert fake.rows[mark]["Примечание"] == "текст владельца" and fake.rows[mark]["Прибыль ₽"] == 111.0
    assert all("Примечание" not in r["fields"] and "Примечание" not in (r.get("extra") or {}) for p in fake.data_posts() for r in p["rows"] if r.get("sync"))


def test_the_fill_follows_the_order(table):
    conn, box, fake, rows, orders_of, whole = table
    fake.grey.add(rows["staged"])                                          # our grey left on a placed order
    order = orders_of["wiped"]
    orders.move_card(conn, order, stage(conn, "new")["id"], LATER, "owner")
    sheet.flush(conn, box, fake, LATER)
    fake.grey.discard(rows["wiped"])
    sheet.resync(conn, box, fake, LATER)
    assert rows["staged"] not in fake.grey and rows["wiped"] in fake.grey
    assert written(conn, order)[rows["wiped"]]["_grey"] is True


def test_the_old_script_writes_nothing(table):
    conn, box, fake, rows, orders_of, whole = table
    for old in (NewScript(), FakeScript()):
        old.rows = fake.rows
        keep = json.dumps(fake.rows, sort_keys=True, ensure_ascii=False)
        with pytest.raises(sheet.SheetError, match="новое развертывание"):
            sheet.resync(conn, box, old, LATER)
        assert json.dumps(fake.rows, sort_keys=True, ensure_ascii=False) == keep


def test_a_failed_chunk_is_an_error_of_its_own_rows_only(table, monkeypatch):
    conn, box, fake, rows, orders_of, whole = table
    monkeypatch.setattr(sheet, "REPAIR_CHUNK", 1)
    probes = len(sheet.repair_candidates(conn))
    fake.fail = [None] * probes + ["500"]                                  # the first row written fails, the others go
    done = sheet.resync(conn, box, fake, LATER)
    assert done["summary"]["errors"] == 1 and done["summary"]["fixed"] == 2
    assert sum(filled(fake.rows.get(rows[k])) == filled(whole[k]) for k in ("staged", "wiped", "edited")) == 2


def test_the_command(table, monkeypatch, capsys, tmp_path):
    conn, box, fake, rows, orders_of, whole = table
    key = tmp_path / "secret.key"
    key.write_text("заглушка", encoding="utf-8")
    monkeypatch.setattr(cli, "load_box", lambda key_file: box)
    monkeypatch.setattr(cli, "_http", lambda: fake)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    assert cli.main(["sheet-resync", "--db", db_path, "--key-file", str(key), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "№%d · %s · строка листа " % (orders_of["staged"], rows["staged"]) in out and "Этап: Новый заказ → " in out
    assert "Заказов проверено: 5" in out and "(проверка: в таблицу и в базу ничего не записано)" in out
    assert cli.main(["sheet-resync", "--db", db_path, "--key-file", str(key)]) == 0
    assert "Исправлено строк: 3, создано заново: 1." in capsys.readouterr().out
    assert cli.main(["sheet-resync", "--db", db_path, "--key-file", str(key), "--closed-too"]) == 0
    assert fake.rows[rows["gone_closed"]] == whole["gone_closed"]


def test_the_button_is_the_owners_and_shows_the_totals(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    box, fake = SecretBox(Fernet.generate_key()), SyncScript()
    app = make_app(tmp_path, SECRET_BOX=box, SHEET_HTTP=fake, SHEET_RUN=lambda work: work())
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    turn_on(conn, box)
    placed(conn)
    sheet.flush(conn, box, fake, NOW)
    mark = sorted(fake.rows)[0]
    whole = dict(fake.rows[mark])
    fake.rows[mark]["Этап"] = "Новый заказ"
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "owner", 0, "tok"
        session["pw"] = shared_mark_of(client.application)
    assert "Сверить таблицу с панелью" in client.get("/connections/sheet").get_data(as_text=True)
    assert client.post("/connections/sheet/resync", data={"csrf": "tok", "dry": "1"}).status_code == 302
    page = client.get("/connections/sheet").get_data(as_text=True)
    assert "Заказов проверено: 1" in page and "расходятся: 1" in page and "Этап: Новый заказ → " in page and fake.rows[mark]["Этап"] == "Новый заказ"
    assert "По последнему поиску: расходятся 1 стр." in page
    assert client.post("/connections/sheet/resync", data={"csrf": "tok", "dry": "0"}).status_code == 302
    assert fake.rows[mark] == whole and "Исправлено строк: 1" in client.get("/connections/sheet").get_data(as_text=True)
    partner, _ = person(app, "partner")
    assert partner.post("/connections/sheet/resync", data={"csrf": "tok", "dry": "1"}).status_code == 403
    assert "Сверить таблицу с панелью" not in partner.get("/connections/sheet").get_data(as_text=True)
