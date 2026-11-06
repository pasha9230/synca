"""«Письмо поставщику» (решение владельца 07.11, lane/supplier-return-email): письмо для согласования возврата готовит система, отправляет оператор
из своей почты (mailto). Кнопка у задачи «Написать поставщику и согласовать возврат» и в заказе на «На возврат», плюс «Скопировать текст».
В тексте № MIM, дата счёта, позиции и количество, просьба согласовать возврат и назвать дату забора; ни закупки, ни прибыли. Адрес из
настроек поставщика; пустой: письмо без адреса и предупреждение. Нажатие пишется в журнал заказа, этап и задача не меняются. Точке не видно.
Сервер ничего не отправляет. Адреса в тестах только example.com."""
from __future__ import annotations

import re
import urllib.parse

import pytest

from panel_helpers import post
from test_mypoint import manager
from test_return_flow import on_page
from tires_sync import connections, message_texts, order_oth, orders, store, supplier_mail

EMAIL = "returns@example.com"
NOW = "2026-10-26T14:00:00"


def made(conn, cid, day="2026-10-12 11:30:00"):
    """The order's WebMIM snapshot: its «Дата счета»."""
    card = orders.get_card(conn, cid)
    conn.execute("INSERT INTO webmim_order (server, id, number, create_date, seen_at) VALUES ('prod', 'w1', ?, ?, ?)", (card["webmim_number"], day, NOW))
    conn.commit()


def test_the_letter_has_the_number_the_invoice_day_the_positions_and_no_money(tmp_path):
    app, conn, pid, cid, _point = on_page(tmp_path)
    made(conn, cid)
    supplier_mail.set_email(conn, 1, EMAIL, NOW, "owner")
    mail = supplier_mail.draft(conn, orders.get_card(conn, cid))
    assert mail["to"] == EMAIL and mail["warning"] == ""
    assert "700200" in mail["subject"]
    body = mail["body"]
    assert "700200" in body and "12.10.2026" in body and "Nokian Hakkapeliitta 10" in body and "4 шт" in body
    assert "согласовать возврат" in body.lower() and "дату забора" in body.lower()
    assert "4000" not in body and "4 000" not in body and "5000" not in body and "5 000" not in body      # no purchase, no sale
    assert "прибыл" not in body.lower() and "закуп" not in body.lower()
    href = mail["href"]
    assert href.startswith("mailto:" + EMAIL + "?")
    query = urllib.parse.parse_qs(href.split("?", 1)[1])
    assert query["subject"] == [mail["subject"]] and query["body"] == [body.replace("\n", "\r\n")]     # RFC 6068: CRLF


def test_without_an_address_the_letter_opens_empty_with_a_warning(tmp_path):
    app, conn, pid, cid, _point = on_page(tmp_path)
    mail = supplier_mail.draft(conn, orders.get_card(conn, cid))
    assert mail["to"] == "" and mail["href"].startswith("mailto:?") and "адрес" in mail["warning"].lower()


def test_the_address_is_checked_and_kept_in_the_supplier_settings(tmp_path):
    app, conn, pid, cid, _point = on_page(tmp_path)
    for bad in ("нет", "a@b", "x@example.com\nBcc: y@example.com", "a" * 250 + "@example.com"):
        try:
            supplier_mail.set_email(conn, 1, bad, NOW, "owner")
        except ValueError:
            continue
        raise AssertionError("принят адрес %r" % bad)
    supplier_mail.set_email(conn, 1, " Returns@Example.com ", NOW, "owner")
    assert supplier_mail.email_of(conn, 1) == "Returns@Example.com"
    supplier_mail.set_email(conn, 1, "", NOW, "owner")
    assert supplier_mail.email_of(conn, 1) == ""


def test_the_template_is_edited_in_the_message_texts(tmp_path):
    app, conn, pid, cid, _point = on_page(tmp_path)
    assert message_texts.entry(supplier_mail.TEXT_KEY)["to"] == "supplier"
    message_texts.save(conn, supplier_mail.TEXT_KEY, "Заказ {mim} от {invoice_date}:\n{items}\nВернём?", "owner", NOW)
    body = supplier_mail.draft(conn, orders.get_card(conn, cid))["body"]
    assert body.startswith("Заказ 700200 от ") and body.endswith("Вернём?") and "Nokian Hakkapeliitta 10" in body


# --- the panel --------------------------------------------------------------------------------------------------------------------------

def owner_client(app):
    from panel_helpers import login
    client = app.test_client()
    return client, login(client)


def test_the_card_on_na_vozvrat_has_the_button_and_the_copy_and_a_press_is_journalled_only(tmp_path):
    app, conn, pid, cid, point = on_page(tmp_path)
    client, tok = owner_client(app)
    supplier_mail.set_email(conn, 1, EMAIL, NOW, "owner")
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert "Письмо поставщику" in page and 'href="mailto:' + EMAIL in page.replace("&amp;", "&")
    assert "Скопировать текст" in page and 'data-mail-log="/orders/%d/supplier-mail"' % cid in page
    stage_before = orders.get_card(conn, cid)["stage"]["code"]
    tasks_before = conn.execute("SELECT id, done_at, cancelled_at FROM callback_task WHERE order_id = ?", (cid,)).fetchall()
    assert post(client, "/orders/%d/supplier-mail" % cid, tok).status_code == 204
    assert any(e["text"].startswith("Письмо поставщику о возврате открыто") and EMAIL in e["text"] for e in orders.list_events(conn, cid))
    assert orders.get_card(conn, cid)["stage"]["code"] == stage_before
    assert conn.execute("SELECT id, done_at, cancelled_at FROM callback_task WHERE order_id = ?", (cid,)).fetchall() == tasks_before


def test_a_card_on_another_stage_has_no_button(tmp_path):
    app, conn, pid, cid, point = on_page(tmp_path)
    client, tok = owner_client(app)
    conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE code = 'oth') WHERE id = ?", (cid,))
    conn.commit()
    assert "Письмо поставщику" not in client.get("/orders/%d" % cid).get_data(as_text=True)
    assert post(client, "/orders/%d/supplier-mail" % cid, tok).status_code == 409


@pytest.mark.parametrize("view", ("off", "all"))                                # the old look and «Новый вид» of the office
def test_the_return_task_in_tasks_has_the_button(tmp_path, view):
    from tires_sync.panel.routes_design import UI_KEY
    app, conn, pid, cid, point = on_page(tmp_path)
    store.set_state(conn, UI_KEY % "office", view)
    client, tok = owner_client(app)
    assert conn.execute("SELECT 1 FROM callback_task WHERE order_id = ? AND title = ?", (cid, order_oth.RETURN_TASK)).fetchone()
    page = client.get("/tasks?tab=all").get_data(as_text=True)
    assert ("o2-tk-what" in page) == (view == "all")
    assert order_oth.RETURN_TASK in page and "Письмо поставщику" in page and 'data-mail-log="/orders/%d/supplier-mail"' % cid in page


def test_a_point_sees_no_button_and_cannot_press_it(tmp_path):
    app, conn, pid, cid, point = on_page(tmp_path)
    supplier_mail.set_email(conn, 1, EMAIL, NOW, "owner")
    client, tok = owner_client(app)
    assert post(client, "/orders/%d/supplier-mail" % cid, tok).status_code == 204          # the office pressed: the record is not the point's
    events = len(orders.list_events(conn, cid))
    page = point.get("/orders/%d" % cid).get_data(as_text=True)
    assert "Письмо поставщику" not in page and EMAIL not in page and "mailto:" not in page
    assert post(point, "/orders/%d/supplier-mail" % cid, "tok").status_code == 403
    assert len(orders.list_events(conn, cid)) == events                         # nothing written by the point's press


def test_the_supplier_page_saves_the_address(tmp_path):
    app, conn, pid, cid, point = on_page(tmp_path)
    client, tok = owner_client(app)
    assert 'name="return_email"' in client.get("/connections/webmim/1").get_data(as_text=True)
    assert post(client, "/connections/suppliers/1/email", tok, return_email=EMAIL).status_code == 302
    assert supplier_mail.email_of(conn, 1) == EMAIL
    post(client, "/connections/suppliers/1/email", tok, return_email="не адрес")
    assert supplier_mail.email_of(conn, 1) == EMAIL                           # refused: the old one stays
    assert post(point, "/connections/suppliers/1/email", "tok", return_email="x@example.com").status_code == 403


def test_the_server_sends_nothing(tmp_path):
    source = open(supplier_mail.__file__, encoding="utf-8").read()
    assert not re.search(r"smtplib|urlopen|requests\.|http\.client", source)
