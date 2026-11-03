"""The new names of the stages (the owner 2026-11-03): renamed once by code while the old default name is kept, a journal line each, the
owner's templates naming an old stage only listed; the texts name the stages by their current names."""
from __future__ import annotations

import json

from tires_sync import onboarding_texts, order_manager as mgr, order_oth, order_points as points, orders, store
from tires_sync.panel import routes_alerts

NOW = "2026-11-03T12:00:00"
NEW = {"moving": "Едет на точку", "at_point": "На точке, ждём клиента", "shipped": "Отправлен в ТК", "cancel": "Отмена",
       "return": "Вернули поставщику"}


def old_db(tmp_path):
    """A pipeline as on the server before 2026-11-03: the old default names, the renaming not run."""
    path = str(tmp_path / "x.db")
    conn = store.connect(path)
    orders.ensure_pipeline(conn, NOW)
    for code, old in orders.OLD_NAMES_2026_11_03.items():
        conn.execute("UPDATE order_stage SET name = ? WHERE code = ?", (old, code))
    conn.execute("DELETE FROM app_state WHERE key = ?", (orders.RENAMED_KEY,))
    conn.execute("DELETE FROM audit")
    conn.commit()
    return path, conn


def by_code(conn, code) -> dict:
    return next(s for s in orders.list_stages(conn) if s["code"] == code)


def names(conn) -> dict:
    return {s["code"]: s["name"] for s in orders.list_stages(conn)}


def audit(conn, action) -> list:
    return [json.loads(r[0]) for r in conn.execute("SELECT detail FROM audit WHERE action = ? ORDER BY id", (action,))]


def test_a_new_pipeline_has_the_new_names(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    got = names(conn)
    assert {code: got[code] for code in NEW} == NEW and got["return_given"] == "Возврат передан" and got["done"] == "Выполнено"


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
