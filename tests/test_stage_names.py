"""The new names of the stages (the owner 2026-11-03): renamed once by code while the old default name is kept, a journal line each, the
owner's templates naming an old stage only listed; the texts name the stages by their current names; «Показывать на доске»."""
from __future__ import annotations

import json
import re
from datetime import datetime

import panel_helpers
from panel_helpers import login, make_app, post
from test_order_search import REF, UNLOAD
from tires_sync import onboarding_texts, order_manager as mgr, order_oth, order_points as points, orders, store
from tires_sync.panel import routes_alerts

NOW = "2026-11-03T12:00:00"
NEW = {"moving": "Едет на точку", "at_point": "На точке, ждём клиента", "shipped": "Отправлен в ТК", "cancel": "Отмена",
       "return": "Вернули поставщику"}


def old_db(tmp_path):
    """A pipeline as on the server before 2026-11-03: the old default names, «Отмена» and «Возврат» on the board, the migrations not run."""
    path = str(tmp_path / "x.db")
    conn = store.connect(path)
    orders.ensure_pipeline(conn, NOW)
    for code, old in orders.OLD_NAMES_2026_11_03.items():
        conn.execute("UPDATE order_stage SET name = ? WHERE code = ?", (old, code))
    conn.execute("UPDATE order_stage SET on_board = 1")
    conn.execute("DELETE FROM app_state WHERE key IN (?, ?)", (orders.RENAMED_KEY, orders.BOARD_KEY))
    conn.execute("DELETE FROM audit")
    conn.commit()
    return path, conn


def by_code(conn, code) -> dict:
    return next(s for s in orders.list_stages(conn) if s["code"] == code)


def names(conn) -> dict:
    return {s["code"]: s["name"] for s in orders.list_stages(conn)}


def audit(conn, action) -> list:
    return [json.loads(r[0]) for r in conn.execute("SELECT detail FROM audit WHERE action = ? ORDER BY id", (action,))]


def test_a_new_pipeline_has_the_new_names_and_no_column_of_cancel_and_return(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    got = names(conn)
    assert {code: got[code] for code in NEW} == NEW and got["return_given"] == "Возврат передан" and got["done"] == "Выполнено"
    assert {s["code"] for s in orders.list_stages(conn) if not s["on_board"]} == {"cancel", "return"}


def test_the_old_default_names_are_renamed_once_by_code_with_a_journal_line_each(tmp_path):
    path, conn = old_db(tmp_path)
    conn.execute("UPDATE order_stage SET name = 'Едут к клиенту' WHERE code = 'moving'")             # the owner's own name: kept
    conn.execute("INSERT INTO order_stage (code, name, position, kind) VALUES ('', 'Товар на точке', 99, 'open')")   # a stage of the owner's
    conn.commit()
    conn = store.connect(path)
    got = names(conn)
    assert got["moving"] == "Едут к клиенту"
    assert [got[c] for c in ("at_point", "shipped", "cancel", "return")] == [NEW[c] for c in ("at_point", "shipped", "cancel", "return")]
    assert conn.execute("SELECT COUNT(*) FROM order_stage WHERE code = '' AND name = 'Товар на точке'").fetchone()[0] == 1
    assert [d["stage"] for d in audit(conn, "stage_renamed")] == ["at_point", "shipped", "cancel", "return"]
    assert audit(conn, "stage_renamed")[0] == {"stage": "at_point", "from": "Товар на точке", "to": "На точке, ждём клиента"}
    conn.execute("UPDATE order_stage SET name = 'Товар на точке' WHERE code = 'at_point'")         # the owner wants the old name back later
    conn.commit()
    conn = store.connect(path)                                                                     # a restart: nothing more
    assert names(conn)["at_point"] == "Товар на точке" and len(audit(conn, "stage_renamed")) == 4


def test_the_owners_templates_naming_an_old_stage_are_listed_and_never_rewritten(tmp_path):
    path, conn = old_db(tmp_path)
    body = "Заказ №{order_label} переведён в «Возврат», а не в «Возвраты»."
    tid = conn.execute("INSERT INTO order_template (name, audience, body) VALUES ('мой', 'client', ?)", (body,)).lastrowid
    conn.execute("INSERT INTO order_template (name, audience, body) VALUES ('чистый', 'client', 'Возвратите шины, пожалуйста.')")
    conn.commit()
    conn = store.connect(path)
    assert audit(conn, "stage_names_templates") == [{"templates": [{"id": tid, "name": "мой", "stages": ["Возврат"]}]}]
    assert conn.execute("SELECT body FROM order_template WHERE id = ?", (tid,)).fetchone()[0] == body


def test_cancel_and_return_leave_the_board_once_and_the_owner_may_show_them_again(tmp_path):
    path, conn = old_db(tmp_path)
    conn = store.connect(path)
    assert {s["code"] for s in orders.list_stages(conn) if not s["on_board"]} == {"cancel", "return"}
    assert [d["stage"] for d in audit(conn, "stage_off_board")] == ["cancel", "return"]
    orders.set_on_board(conn, by_code(conn, "cancel")["id"], True)
    conn = store.connect(path)
    assert {s["code"] for s in orders.list_stages(conn) if not s["on_board"]} == {"return"}


def test_the_texts_name_the_stages_by_their_current_names(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    conn.execute("UPDATE order_stage SET name = 'Закрыт возвратом' WHERE code = 'return'")
    conn.execute("UPDATE order_stage SET name = 'Ждёт клиента' WHERE code = 'at_point'")
    conn.commit()
    assert orders.stage_name(conn, "return") == "Закрыт возвратом" and orders.stage_name(conn, "from_point") == "Передано с точки"
    assert points.move_refused(conn) == "Свою продажу точка переводит только между этапами «Новый заказ», «Заказ оформлен», «Выполнено» и «Отмена»."
    hint = onboarding_texts.stage_hint("moving", names=names(conn))
    assert "заказ сам переходит на «Ждёт клиента»" in str(hint["auto"][0]) and not any("{" in str(t) for t in hint["steps"] + hint["auto"])
    assert not any("{" in t for t in onboarding_texts.stage_texts())
    assert "«На точке, ждём клиента»" in onboarding_texts.stage_texts()[[i for i, t in enumerate(onboarding_texts.stage_texts())
                                                                          if "заказ сам переходит на" in t][0]]
    for text in (mgr.RETURN_ASK % "Закрыт возвратом", mgr.RETURN_GAPS % ("причина возврата", "Закрыт возвратом"),
                 order_oth.SIGNED_ASK % "Закрыт возвратом",
                 "Точка отдала шины поставщику (возврат): заполните сумму возврата и переведите в «Возврат».",        # the records written before
                 "Точка отдала шины с ОТХ поставщику по акту МХ-3: заполните сумму возврата и переведите заказ в «Возврат»."):
        assert routes_alerts.is_return_ask({"kind": "point", "text": text}), text
    assert not routes_alerts.is_return_ask({"kind": "point", "text": mgr.RETURN_LINE})


# ---- «Показывать на доске» in the panel -------------------------------------------------------------------------------------------

def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(panel_helpers, "UNLOAD", UNLOAD)
    monkeypatch.setattr(panel_helpers, "REF_TEXT", REF)
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 11, 3, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    client.get("/orders")
    return client, tok, store.connect(app.config["DB_PATH"])


def new_card(client, tok) -> int:
    r = post(client, "/orders/new", tok, client_name="Иван", item_goods_id=["100"], item_qty=["4"])
    assert r.status_code == 302, r.get_data(as_text=True)[:300]
    return int(re.search(r"/orders/(\d+)$", r.headers["Location"]).group(1))


def column(page, stage_id):
    return re.search(r'<section class="col[^"]*" data-stage="%d"' % stage_id, page)


def test_a_stage_off_the_board_has_no_column_but_its_orders_are_in_the_list_the_filter_and_the_link(tmp_path, monkeypatch):
    client, tok, conn = setup(tmp_path, monkeypatch)
    card = new_card(client, tok)
    cancel = by_code(conn, "cancel")
    assert orders.move_card(conn, card, cancel["id"], NOW, "o", changes={"cancel_reason": "отменил клиент"})
    board = client.get("/orders").get_data(as_text=True)
    assert not column(board, cancel["id"]) and 'href="/orders/%d"' % card not in board
    assert '<option value="%d">Отмена (нет на доске)</option>' % cancel["id"] in board
    assert "/orders/%d" % card in client.get("/orders?v=list").get_data(as_text=True)
    assert "/orders/%d" % card in client.get("/orders?v=list&q=Иван").get_data(as_text=True)
    only = client.get("/orders?s=%d" % cancel["id"]).get_data(as_text=True)                         # chosen in «Этап»: its column shows
    assert column(only, cancel["id"]) and "/orders/%d" % card in only and "selected>Отмена (нет на доске)" in only
    assert client.get("/orders/%d" % card).status_code == 200


def test_the_box_of_the_pipeline_settings_shows_and_hides_a_stage(tmp_path, monkeypatch):
    client, tok, conn = setup(tmp_path, monkeypatch)
    done = by_code(conn, "done")
    page = client.get("/orders/settings").get_data(as_text=True)
    assert page.count('name="on_board" value="1" checked') == len(orders.list_stages(conn)) - 2
    form = dict(name=done["name"], color=done["color"], kind=done["kind"], hint="", limit_hours="0", board_shown="1")
    assert post(client, "/orders/settings/stage/%d/update" % done["id"], tok, **form).status_code == 302
    assert not orders.get_stage(conn, done["id"])["on_board"] and not column(client.get("/orders").get_data(as_text=True), done["id"])
    post(client, "/orders/settings/stage/%d/update" % done["id"], tok, on_board="1", **form)
    assert orders.get_stage(conn, done["id"])["on_board"] and column(client.get("/orders").get_data(as_text=True), done["id"])
    form.pop("board_shown")                                                                        # a form without the box changes nothing
    post(client, "/orders/settings/stage/%d/update" % done["id"], tok, **form)
    assert orders.get_stage(conn, done["id"])["on_board"]


def test_the_journal_names_the_renamings_and_the_templates_in_words(tmp_path):
    from tires_sync.panel import texts
    path, conn = old_db(tmp_path)
    conn.execute("INSERT INTO order_template (name, audience, body) VALUES ('мой', 'client', 'В «Возврат».')")
    conn.commit()
    conn = store.connect(path)
    said = [texts.audit_text(r) for r in store.list_audit(conn, limit=50)]
    assert "Этап переименован: «Товар на точке» → «На точке, ждём клиента»" in said
    assert any(t.startswith("Шаблоны сообщений называют этапы по-старому") and "№1 «мой»" in t and "«Возврат»" in t for t in said)
    assert any(t.startswith("Этап «Отмена» убран с доски") for t in said) and not any("«cancel»" in t for t in said)


def test_the_time_on_the_stages_counts_the_old_name_of_a_renamed_stage_as_the_stage(tmp_path):
    from tires_sync import analytics
    path, conn = old_db(tmp_path)
    card = conn.execute("INSERT INTO order_card (stage_id, created_at, created_by, updated_at, entered_at) VALUES (?, ?, 'o', ?, ?)",
                        (by_code(conn, "at_point")["id"], NOW, NOW, NOW)).lastrowid
    orders.add_event(conn, card, "2026-11-01T10:00:00", "o", "stage", "Этап: «Перемещение на точку» → «Товар на точке»")
    orders.add_event(conn, card, "2026-11-01T14:00:00", "o", "stage", "Этап: «Товар на точке» → «Выполнено»")
    conn.commit()
    conn = store.connect(path)
    stays = analytics.stage_times(conn, "2026-11-01", "2026-11-03", NOW)["stays"]
    here = next(s for s in stays if s["stage_id"] == by_code(conn, "at_point")["id"])
    assert here["name"] == "На точке, ждём клиента" and here["count"] == 1 and here["mean_hours"] == 4
    assert not any(s["name"] == "Товар на точке" for s in stays)


def test_a_search_on_the_board_shows_what_it_found_on_a_stage_off_the_board(tmp_path, monkeypatch):
    client, tok, conn = setup(tmp_path, monkeypatch)
    card = new_card(client, tok)
    cancel = by_code(conn, "cancel")
    assert orders.move_card(conn, card, cancel["id"], NOW, "o", changes={"cancel_reason": "отменил клиент"})
    found = client.get("/orders?q=Иван").get_data(as_text=True)
    assert column(found, cancel["id"]) and 'href="/orders/%d"' % card in found
    assert not column(client.get("/orders?q=Никто").get_data(as_text=True), cancel["id"])      # nothing found there: no column
