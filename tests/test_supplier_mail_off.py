"""«Письмо поставщику» убрано (слово владельца 07.11, lane/supplier-mail-off): кнопки «Письмо поставщику» и «Скопировать текст» нет ни
в карточке заказа на «На возврат», ни у задачи возврата в «Задачах» (оба вида), поля «Почта для возвратов» нет на странице поставщика,
шаблона письма нет в «Шаблонах сообщений», адреса кнопки и поля больше не отвечают. Данные, которые записала прежняя кнопка (адрес в
настройках поставщика, строки журнала), не удаляются: они просто не читаются и не показываются; старые строки журнала точке по-прежнему
не видны. Прежние тесты функции (tests/test_supplier_mail.py) удалены вместе с ней. Адреса в тестах только example.com."""
from __future__ import annotations

import pytest

from panel_helpers import login, post
from test_return_flow import on_page
from tires_sync import connections, message_texts, order_oth, orders, store

EMAIL = "returns@example.com"
NOW = "2026-10-26T14:00:00"
GONE = ("Письмо поставщику", "Скопировать текст", "data-mail-log", "mailto:", "data-supplier-mail", "Почта для возвратов", "return_email",
        EMAIL)


def stored_address(conn):
    """What the removed button kept in the supplier's settings: it stays in the base, nobody reads it."""
    connections.set_config(conn, "supplier_mail", 1, {"email": EMAIL}, NOW, "owner")


def owner_client(app):
    client = app.test_client()
    return client, login(client)


def assert_gone(page):
    for word in GONE:
        assert word not in page, word


def test_the_card_on_na_vozvrat_has_no_letter(tmp_path):
    app, conn, pid, cid, point = on_page(tmp_path)
    stored_address(conn)
    client, _tok = owner_client(app)
    assert orders.get_card(conn, cid)["stage"]["code"] == orders.RETURN_GIVEN
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert_gone(page)


@pytest.mark.parametrize("view", ("off", "all"))                                # the old look and «Новый вид» of the office
def test_the_return_task_in_tasks_has_no_letter(tmp_path, view):
    from tires_sync.panel.routes_design import UI_KEY
    app, conn, pid, cid, point = on_page(tmp_path)
    stored_address(conn)
    store.set_state(conn, UI_KEY % "office", view)
    client, _tok = owner_client(app)
    assert conn.execute("SELECT 1 FROM callback_task WHERE order_id = ? AND title = ?", (cid, order_oth.RETURN_TASK)).fetchone()
    page = client.get("/tasks?tab=all").get_data(as_text=True)
    assert order_oth.RETURN_TASK in page
    assert_gone(page)


def test_the_supplier_page_has_no_return_email_and_the_stored_address_stays(tmp_path):
    app, conn, pid, cid, point = on_page(tmp_path)
    stored_address(conn)
    client, _tok = owner_client(app)
    page = client.get("/connections/webmim/1").get_data(as_text=True)
    assert "WebMIM" in page
    assert_gone(page)
    assert connections.get_config(conn, "supplier_mail", 1) == {"email": EMAIL}        # the data is not deleted


def test_the_old_addresses_answer_nothing_and_write_nothing(tmp_path):
    app, conn, pid, cid, point = on_page(tmp_path)
    stored_address(conn)
    client, tok = owner_client(app)
    events = len(orders.list_events(conn, cid))
    assert post(client, "/orders/%d/supplier-mail" % cid, tok).status_code in (404, 405)
    assert post(client, "/connections/suppliers/1/email", tok, return_email="other@example.com").status_code in (404, 405)
    assert len(orders.list_events(conn, cid)) == events
    assert connections.get_config(conn, "supplier_mail", 1) == {"email": EMAIL}


def test_the_letter_template_is_not_in_the_message_texts(tmp_path):
    app, conn, pid, cid, point = on_page(tmp_path)
    client, _tok = owner_client(app)
    assert all(t["key"] != "supplier_return" for t in message_texts.TEXTS)
    response = client.get("/orders/templates")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Шаблоны сообщений" in page and "Поставщику: согласовать возврат" not in page


def test_an_old_journal_record_of_the_letter_is_still_not_the_points(tmp_path):
    app, conn, pid, cid, point = on_page(tmp_path)
    orders.add_event(conn, cid, NOW, "owner", "supplier_mail", "Письмо поставщику о возврате открыто в почте оператора (%s)" % EMAIL,
                     level="grey")
    conn.commit()
    client, _tok = owner_client(app)
    assert "Письмо поставщику о возврате открыто" in client.get("/orders/%d" % cid).get_data(as_text=True)   # the office's history stays
    page = point.get("/orders/%d" % cid).get_data(as_text=True)
    assert "Письмо поставщику" not in page and EMAIL not in page
