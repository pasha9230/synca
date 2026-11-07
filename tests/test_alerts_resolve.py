"""A red record «не подготовлена / не отправлено / не хватает …» closes by itself (order_alerts.resolve: for everybody, no new record) once the
same thing is done: the message of the same template (and the same rule) of the order went or was ticked «Отправлено вручную», a position
was added after «Позиция не добавлена». What is not fixed stays red (the owner 2026-11-07, orders 540 and 542). Fakes only."""
from __future__ import annotations

from test_order_placed import Deliver, ready, stage_rule, tasks
from test_webmim_create import FakeWebmim, NOW, db, place
from tires_sync import order_alerts as alerts, order_engine as engine, order_placed, order_templates as templates, orders

LATER = "2026-10-23T12:30:00"


def reds(conn, mine, login="owner"):
    return [e["text"] for e in alerts.open_alerts(conn, login, mine) if e["level"] == "red"]


def wait_for_number(conn, audience="client", name=order_placed.CLIENT_TEMPLATE):
    template = next(t for t in templates.list_templates(conn) if t["name"] == name and t["audience"] == audience)
    templates.update_template(conn, template["id"], template["name"], audience, "Заказ {webmim_number}, перемещение {transfer_number}")
    return template


def test_the_hooks_unsent_message_closes_once_it_is_sent(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    wait_for_number(conn)
    place(conn, mine, FakeWebmim())
    order_placed.tell_client(conn, mine, "1001", NOW, "owner", Deliver())
    assert any(t.startswith("Сообщение клиенту «Клиенту: заказ оформлен» не отправлено: не хватает") for t in reds(conn, mine))
    orders.update_card(conn, mine, {"transfer_number": "26R-1"}, LATER, "owner")
    [task] = tasks(conn, mine)
    engine.send_task(conn, task["id"], "Заказ 5001, перемещение 26R-1", Deliver(), LATER, "Мария")
    assert not any("Клиенту: заказ оформлен" in t for t in reds(conn, mine))
    assert not any("Клиенту: заказ оформлен" in t for t in reds(conn, mine, "someone"))     # for everybody, as «исправлено»
    who = conn.execute("SELECT author FROM order_event_ack WHERE login = ?", (alerts.EVERYBODY,)).fetchall()
    assert who and all(a == "Мария" for (a,) in who)


def test_the_rules_task_not_prepared_closes_with_the_hooks_record(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    wait_for_number(conn)
    stage_rule(conn)
    place(conn, mine, FakeWebmim())                                    # the press moves the card to «Заказ оформлен»: the rule's task
    order_placed.tell_client(conn, mine, "1001", NOW, "owner", Deliver())
    red = reds(conn, mine)
    assert "Задача не подготовлена: Клиенту: заказ оформлен" in red
    assert any(t.startswith("Сообщение клиенту «Клиенту: заказ оформлен» не отправлено") for t in red)
    orders.update_card(conn, mine, {"transfer_number": "26R-1"}, LATER, "owner")
    [task] = [t for t in engine.list_tasks(conn, mine) if t["kind"] == "message" and t["title"] == order_placed.CLIENT_TEMPLATE
              and t["status"] in ("pending", "error")]
    engine.send_task(conn, task["id"], "Заказ 5001, перемещение 26R-1", Deliver(), LATER, "owner")
    assert not any("Клиенту: заказ оформлен" in t for t in reds(conn, mine))


def unsent_after_placing(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    wait_for_number(conn)
    place(conn, mine, FakeWebmim())
    order_placed.tell_client(conn, mine, "1001", NOW, "owner", Deliver())
    [task] = tasks(conn, mine)
    return conn, mine, task


def test_sent_by_hand_closes_it_too(tmp_path):
    conn, mine, task = unsent_after_placing(tmp_path)
    engine.sent_by_hand(conn, task["id"], LATER, "owner")              # «Отправлено вручную», still lacking the number
    assert not any("Клиенту: заказ оформлен" in t for t in reds(conn, mine))


def test_a_skip_fixes_nothing(tmp_path):
    conn, mine, task = unsent_after_placing(tmp_path)
    engine.skip_task(conn, task["id"], LATER, "owner")                 # skipped: nothing was sent, the red stays for «Разобрался»
    assert any("Клиенту: заказ оформлен" in t for t in reds(conn, mine))


def test_sent_by_hand_from_not_delivered_closes_it(tmp_path):
    from tires_sync import unsent
    conn = db(tmp_path)
    mine = ready(conn, chat=False)                                     # no chat: the text waits in «Не дошло клиенту»
    place(conn, mine, FakeWebmim())
    order_placed.tell_client(conn, mine, "1001", NOW, "owner", Deliver())
    assert any(t.startswith("Сообщение клиенту «Клиенту: заказ оформлен» не отправлено: У заказа нет чата") for t in reds(conn, mine))
    [task] = tasks(conn, mine)
    unsent.sent_by_hand(conn, mine, "task", task["id"], LATER, "owner")    # «Отправил вручную» of «Не дошло клиенту»
    assert engine.get_task(conn, task["id"])["status"] == "done"
    assert not any("Клиенту: заказ оформлен" in t for t in reds(conn, mine))


def test_another_message_or_another_order_stays_red(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    wait_for_number(conn)
    place(conn, mine, FakeWebmim())
    order_placed.tell_client(conn, mine, "1001", NOW, "owner", Deliver())
    orders.add_event(conn, mine, NOW, order_placed.AUTHOR, "chat", "Сообщение точке «Клиенту: заказ оформлен» не отправлено: нет ВК.", level="red")
    orders.add_event(conn, mine, NOW, "правило №7", "rule", "Задача не подготовлена: Клиенту: заказ оформлен", level="red")
    orders.add_event(conn, mine, NOW, "owner", "chat", "Сообщение клиенту «Клиенту: другое» не отправлено: нет чата.", level="red")
    conn.commit()
    orders.update_card(conn, mine, {"transfer_number": "26R-1"}, LATER, "owner")
    [task] = tasks(conn, mine)
    engine.send_task(conn, task["id"], "Заказ 5001, перемещение 26R-1", Deliver(), LATER, "owner")
    left = reds(conn, mine)
    assert not any(t.startswith("Сообщение клиенту «Клиенту: заказ оформлен»") for t in left)
    assert "Сообщение точке «Клиенту: заказ оформлен» не отправлено: нет ВК." in left           # another audience
    assert "Задача не подготовлена: Клиенту: заказ оформлен" in left                             # another rule's task (№7), not this one
    assert "Сообщение клиенту «Клиенту: другое» не отправлено: нет чата." in left                # another template


def test_a_position_added_later_closes_position_not_added(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    orders.add_event(conn, mine, NOW, "owner", "chat", "Позиция не добавлена: позиции 9 нет в выгрузке")
    orders.add_event(conn, mine, NOW, "owner", "chat", "Позиция не определена: объявление Авито №5 не найдено среди наших объявлений")
    orders.add_event(conn, mine, NOW, "owner", "chat", "Сообщение клиенту не отправлено: Авито отказал", level="red")
    conn.commit()
    assert len(reds(conn, mine)) == 3
    orders.add_item(conn, mine, goods_id="1", name="Nokian", brand_key="nokian", season="winter", diameter=16, territory="1", qty=4,
                    sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=LATER, author="owner")
    assert reds(conn, mine) == ["Сообщение клиенту не отправлено: Авито отказал"]
