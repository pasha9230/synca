"""«Настройка → Шаблоны сообщений» (the owner 2026-11-02): every text the system sends by itself, editable; the senders use the owner's text,
«Вернуть исходный текст» brings the default back, a variable the text cannot have is refused, the journal of actions keeps before → after."""
from __future__ import annotations

from datetime import datetime

import pytest

from panel_helpers import login, make_app, post
from test_oth_seventh_day import office, on_oth
from test_arrived_wait import env  # noqa: F401  (the fixture)
from test_reserve_ask_point import at
from test_reserve_no_number import ready
from tires_sync import message_texts as mt, order_oth, order_reserve as reserve, order_templates as tpl, orders, store, users
from tires_sync.panel import texts

NOW = "2026-10-12T12:00:00"
ASK = "Заказ №{order_label}: клиент забрал? Ответьте «выдал {order_label}» или «на точке {order_label}». {order_summary}"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def test_the_list_has_every_text_that_goes_by_itself_with_whom_and_when():
    names = {t["name"] for t in mt.TEXTS}
    for sample in (tpl.SAMPLES[0], tpl.SAMPLES[1], tpl.ARRIVED_SAMPLE, tpl.RESERVE_SAMPLE, tpl.OTH_POINT_SAMPLE, tpl.OTH_ASK_SAMPLE):
        assert sample[0] in names
    assert {mt.RESERVE_ASK, mt.ASK_NUMBER, mt.MOVED_NOTE, mt.UNAGREED, mt.RETURN_AGREE, mt.UNTOLD_POINT} <= set(mt.BY_KEY)
    for t in mt.TEXTS:
        assert t["to"] in mt.TO and t["when"] and t["default"] and t["audience"] in tpl.AUDIENCES
        assert set(tpl.PLACEHOLDER.findall(t["default"])) <= set(t["variables"]), t["key"]     # the default passes its own check
        assert mt.check(t, t["default"])[1] == [], t["key"]
    assert len(names) == len(mt.TEXTS)                                     # one row of order_template per name


def test_the_defaults_are_the_texts_the_code_sent_before(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    assert mt.text(conn, mt.RESERVE_ASK, order_label="77", order_summary="Иван, 205/55 R16") == \
        "Заказ №77 (Иван, 205/55 R16) ещё на точке? Ответьте «выдал 77» или «на точке 77»."
    assert mt.text(conn, mt.ASK_NUMBER, order_no=5, client="Иван") == "Укажите номер заказа WebMIM по заказу 5 (Иван)"
    assert mt.text(conn, mt.MOVED_NOTE, order_label="77") == "Заказ №77 переведён на «На ОТХ»: клиент не ответил на напоминание о сроке резерва"
    assert mt.text(conn, mt.UNAGREED, order_label="77") == "Возврат по заказу №77 поставщику отменён, шины остаются на точке"
    assert mt.text(conn, mt.UNTOLD_POINT, order_label="77", why="нет чата") == \
        "Заказ №77: клиенту не сообщили, что шины на точке — нет чата. Напишите клиенту в чате Авито."


def test_the_question_to_the_point_and_the_number_request_go_with_the_owners_text_and_back_after_reset(tmp_path):
    conn, cid, _chat, deliver, vk, notify = ready(tmp_path)
    assert mt.save(conn, mt.RESERVE_ASK, ASK, "owner", NOW) == []
    mt.save(conn, mt.ASK_NUMBER, "Нет номера WebMIM: заказ {order_no}, {client}", "owner", NOW)
    reserve.check(conn, at(10), notify, deliver, vk)
    assert [t for _p, t in vk.sent] == ["(бот) Заказ №%d: клиент забрал? Ответьте «выдал %d» или «на точке %d». Иван, 205/55 R16" % (cid, cid, cid)]
    assert (cid, "Нет номера WebMIM: заказ %d, Иван" % cid) in notify.sent
    assert mt.reset(conn, mt.RESERVE_ASK, "owner", NOW) and mt.body(conn, mt.RESERVE_ASK) == mt.BY_KEY[mt.RESERVE_ASK]["default"]
    assert not mt.reset(conn, mt.RESERVE_ASK, "owner", NOW)                 # already the default


def test_the_return_alert_goes_with_the_owners_text(tmp_path):
    from test_order_reserve import card_at, db
    conn = db(tmp_path)
    office(conn)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00")
    mt.save(conn, mt.RETURN_AGREE, "Возврат: заказ {order_label}, {point}, {link}", "owner", NOW)
    told = []
    order_oth.answer_here(conn, orders.get_card(conn, cid), "менеджер Бета", "2026-10-26T13:00:00", lambda logins, text: told.append(text))
    assert told == ["Возврат: заказ 5200001, Бета, /orders/%d" % cid]


def test_a_sample_template_is_edited_in_its_own_row_and_reset_to_the_sample(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    tpl.add_samples(conn)
    name = tpl.ARRIVED_SAMPLE[0]
    mt.save(conn, "arrived", "Заказ {webmim_number} на точке.", "owner", NOW)
    [row] = [t for t in tpl.list_templates(conn) if t["name"] == name]
    assert row["body"] == "Заказ {webmim_number} на точке."
    from tires_sync import order_manager
    assert order_manager.template_body(conn, tpl.ARRIVED_SAMPLE) == "Заказ {webmim_number} на точке."     # the sender reads it
    mt.reset(conn, "arrived", "owner", NOW)
    assert order_manager.template_body(conn, tpl.ARRIVED_SAMPLE) == tpl.ARRIVED_SAMPLE[2]
    assert len([t for t in tpl.list_templates(conn) if t["name"] == name]) == 1


def test_an_unknown_variable_is_refused_and_lost_answer_words_only_warn(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    with pytest.raises(orders.OrderError, match=r"\{client_name\}"):
        mt.save(conn, mt.RESERVE_ASK, "Заказ {order_label} у {client_name}?", "owner", NOW)    # the order's variable is not this text's
    with pytest.raises(orders.OrderError):
        mt.save(conn, mt.ASK_NUMBER, "   ", "owner", NOW)
    assert mt.body(conn, mt.RESERVE_ASK) == mt.BY_KEY[mt.RESERVE_ASK]["default"]
    [warning] = mt.save(conn, mt.RESERVE_ASK, "Заказ №{order_label} ещё у вас?", "owner", NOW)
    assert "«выдал {order_label}», «на точке {order_label}»" in warning and mt.body(conn, mt.RESERVE_ASK) == "Заказ №{order_label} ещё у вас?"     # saved anyway


def test_the_code_texts_are_not_offered_to_the_rules(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 12, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    mt.save(conn, mt.RESERVE_ASK, ASK, "owner", NOW)
    page = client.get("/orders/settings").get_data(as_text=True)
    assert mt.BY_KEY[mt.RESERVE_ASK]["name"] not in page
    assert tok


def test_the_page_lists_saves_resets_and_journals_and_only_the_owner_changes(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 12, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    page = client.get("/orders/templates").get_data(as_text=True)
    for t in mt.TEXTS:
        assert 'id="t-%s"' % t["key"] in page and t["when"] in page, t["key"]
    assert 'href="/orders/templates"' in page and "Шаблоны сообщений</a>" in page                       # in «Настройка»
    assert 'data-insert="{order_label}"' in page and "templates.js" in page
    post(client, "/orders/templates/text", tok, key=mt.MOVED_NOTE, body="Заказ {order_label} ушёл на ОТХ")
    assert mt.body(conn, mt.MOVED_NOTE) == "Заказ {order_label} ушёл на ОТХ"
    page = client.get("/orders/templates").get_data(as_text=True)
    assert "Вернуть исходный текст" in page and "Изменён" in page
    post(client, "/orders/templates/text", tok, key=mt.MOVED_NOTE, body="Заказ {nope}")
    assert mt.body(conn, mt.MOVED_NOTE) == "Заказ {order_label} ушёл на ОТХ"
    assert "Неизвестная подстановка: {nope}" in client.get("/orders/templates").get_data(as_text=True)
    post(client, "/orders/templates/text/reset", tok, key=mt.MOVED_NOTE)
    assert mt.body(conn, mt.MOVED_NOTE) == mt.BY_KEY[mt.MOVED_NOTE]["default"]
    entries = [e for e in store.list_audit(conn, 50) if e["action"] == mt.CHANGED]
    assert len(entries) == 2 and all(e["author"] == "owner" for e in entries)
    said = [texts.audit_text(e) for e in entries]
    assert any(s.startswith("Шаблону «%s» возвращён исходный текст: «Заказ {order_label} ушёл на ОТХ» → «Заказ №" % mt.BY_KEY[mt.MOVED_NOTE]["name"]) for s in said)
    assert any(s.startswith("Шаблон «%s» изменён: «Заказ №{order_label} переведён" % mt.BY_KEY[mt.MOVED_NOTE]["name"]) for s in said)
    for role in ("partner", "point", "chats"):                                # «Настройка» to change: the owner only (setup EDIT)
        users.add_user(conn, role + "1", "длинный-пароль-1", NOW, role=role,
                       point_id=conn.execute("INSERT INTO point (name) VALUES ('Т')").lastrowid if role == "point" else None)
        conn.commit()
        other = app.test_client()
        with other.session_transaction() as session:
            session.update(user=role + "1", epoch=store.get_session_epoch(conn, role + "1"), csrf="tok")
        assert other.post("/orders/templates/text", data={"csrf": "tok", "key": mt.MOVED_NOTE, "body": "x"}).status_code in (302, 403), role
        assert mt.body(conn, mt.MOVED_NOTE) == mt.BY_KEY[mt.MOVED_NOTE]["default"], role
    partner = app.test_client()
    with partner.session_transaction() as session:
        session.update(user="partner1", epoch=store.get_session_epoch(conn, "partner1"), csrf="tok")
    shown = partner.get("/orders/templates").get_data(as_text=True)          # the partner looks (setup VIEW), the forms are closed
    assert 'id="t-%s"' % mt.MOVED_NOTE in shown and 'action="/orders/templates/text"' not in shown


def test_the_moved_note_goes_with_the_owners_text(tmp_path):
    from test_reserve_noon import NEXT, told
    conn, cid, _chat, deliver, vk = told(tmp_path)
    mt.save(conn, mt.MOVED_NOTE, "Заказ {order_label} — на ОТХ", "owner", NOW)
    got = []
    deliver.at = NEXT + "T09:07:00"
    reserve.check(conn, NEXT + "T09:07:00", lambda card, text: (got.append(text), "ok")[1], deliver, vk)
    assert "Заказ 5200001 — на ОТХ" in got


def test_the_cancelled_return_goes_to_the_point_with_the_owners_text(tmp_path, monkeypatch):
    from test_oth_fixes import T, _quiet, _webmim, card_at, panel
    app, owner, tok, conn = panel(tmp_path)
    oth = card_at(conn, "oth", T)
    told = _quiet(monkeypatch)
    mt.save(conn, mt.UNAGREED, "Отбой возврата №{order_label}", "owner", NOW)
    post(owner, "/orders/%d/oth-agreed" % oth, tok)
    post(owner, "/orders/%d/oth-unagree" % oth, tok)
    assert told == [(oth, "Отбой возврата №%s" % _webmim(conn, oth))]


def test_the_untold_arrival_goes_to_the_point_with_the_owners_text(env, monkeypatch):
    from test_arrived_alert import LATE
    from tires_sync import order_arrived, order_points
    conn, _chat_row, card = env
    monkeypatch.setattr(order_points, "of_account", lambda c, order_id: {"id": 1})
    mt.save(conn, mt.UNTOLD_POINT, "№{order_label}: не ушло ({why})", "owner", NOW)
    point = []
    order_arrived.check(conn, LATE, lambda logins, text: None, lambda c, text: point.append(text))
    assert len(point) == 1 and point[0].startswith("№") and point[0].endswith(")") and ": не ушло (" in point[0]


def test_return_agree_default_is_the_old_text(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    assert mt.text(conn, mt.RETURN_AGREE, order_label="77", point="Бета", link="/orders/3") == \
        "Заказ №77 (точка «Бета») 7 дней на ОТХ, шины на точке: согласуйте возврат с поставщиком — /orders/3"


def test_a_placed_template_without_its_row_is_shown_as_not_sent_and_save_brings_it_back(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    [shown] = [t for t in mt.listing(conn) if t["key"] == "placed_client"]
    assert shown["off"]                                                    # order_placed sends nothing without the row
    mt.save(conn, "placed_client", tpl.SAMPLES[0][2], "owner", NOW)       # the sample as it is: the row is made
    assert [t["body"] for t in tpl.list_templates(conn) if t["name"] == tpl.SAMPLES[0][0]] == [tpl.SAMPLES[0][2]]
    assert not [t for t in mt.listing(conn) if t["key"] == "placed_client"][0]["off"]
    [entry] = [e for e in store.list_audit(conn, 5) if e["action"] == mt.CHANGED]
    assert texts.audit_text(entry).startswith("Шаблон «Клиенту: заказ оформлен» добавлен")


def test_the_rules_templates_are_journaled_and_may_not_take_a_systems_name(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 12, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    post(client, "/orders/templates/save", tok, name="Свой", audience="client", body="Заказ {order_no}")
    [t] = tpl.list_templates(conn)
    post(client, "/orders/templates/save", tok, id=str(t["id"]), name="Свой 2", audience="manager", body="Заказ {order_no}!")
    for name in (mt.BY_KEY[mt.MOVED_NOTE]["name"], tpl.ARRIVED_SAMPLE[0]):              # neither a new one nor a rename
        post(client, "/orders/templates/save", tok, name=name, audience="manager", body="x")
        post(client, "/orders/templates/save", tok, id=str(t["id"]), name=name, audience="manager", body="x")
    assert [(r["name"], r["body"]) for r in tpl.list_templates(conn)] == [("Свой 2", "Заказ {order_no}!")]
    assert "занято текстом, который система отправляет сама" in client.get("/orders/templates").get_data(as_text=True)
    post(client, "/orders/templates/%d/delete" % t["id"], tok)
    said = [texts.audit_text(e) for e in store.list_audit(conn, 50) if e["action"] == mt.CHANGED]
    assert said == ["Шаблон «Свой 2» удалён: «Заказ {order_no}!» → —",
                    "Шаблон «Свой 2» изменён (было имя «Свой»), адресат: менеджеру: «Заказ {order_no}» → «Заказ {order_no}!»",
                    "Шаблон «Свой» добавлен: — → «Заказ {order_no}»"]
