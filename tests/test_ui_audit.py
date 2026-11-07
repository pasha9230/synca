"""«Новый вид» after the review of the sections (the owner 2026-11-07, ui-audit; the prototype «Панель после правок»): no number, stage, date or sum
twice on one screen, no block of another stage or section. The old view and the point's card are not changed (their own tests)."""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from panel_helpers import login, make_app, post
from tires_sync import critical, order_notify, orders, order_points as points, store, users

NOW = "2026-11-07T10:00:00"


@pytest.fixture
def w(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 11, 7, 12, 0, 0))
    client = app.test_client()
    tok = login(client)
    client.get("/orders")                                                   # the pipeline
    conn = store.connect(app.config["DB_PATH"])
    store.set_state(conn, "ui_new.office", "all")
    pid = points.save_point(conn, {"name": "Точка Север", "manager_name": "Иван Пример"})
    conn.commit()
    stages = {s["code"]: s for s in orders.list_stages(conn)}
    return dict(app=app, client=client, tok=tok, conn=conn, pid=pid, stages=stages)


def card(w, code, **fields):
    conn = w["conn"]
    cid = orders.create_card(conn, {"client_name": "Анна", "client_phone": "+7 900 000-00-11", "point_id": w["pid"], "delivery_date": "2026-11-10"},
                             NOW, "owner", many_open=True)
    orders.add_item(conn, cid, goods_id="100", name="Ikon Tyres 205/55 R16 Test Model", brand_key="ikon", season="лето", diameter="16", territory="",
                    qty=4, sale_unit=5000, supplier_price=4000, terms="", fee_pct=0, promo_pct=0, now=NOW, author="owner")
    sets = dict(fields, stage_id=w["stages"][code]["id"])
    conn.execute("UPDATE order_card SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in sets), (*sets.values(), cid))
    conn.commit()
    return cid


def page(w, url):
    r = w["client"].get(url)
    assert r.status_code == 200, url
    return r.get_data(as_text=True)


def window(text):
    return text[text.index('<section class="card owin"'):text.index("</section>", text.index('id="ow-main"'))]


# ---- the order's card ------------------------------------------------------------------------------------------------------------------

def test_the_stage_is_not_repeated_in_the_window(w):
    text = page(w, "/orders/%d" % card(w, "placed", webmim_number="5316244"))
    assert 'class="o2c-stage"' in text and 'class="owin-stages"' in text                 # the header and the strip
    assert not re.search(r'<span class="badge badge--\w+">Заказ оформлен', window(text))   # no badge in the window's eyebrow
    assert "Этап «Заказ оформлен» с" not in text                                          # the time is the strip's title
    assert 'title="На этапе с 07.11 10:00"' in text


def test_the_number_of_webmim_is_text_with_fix_and_the_input_stays_in_the_form(w):
    text = page(w, "/orders/%d" % card(w, "placed", webmim_number="5316244", transfer_number="26R-1"))
    win = window(text)
    assert re.search(r'<span class="owin-fixv mono">5316244</span> <span class="owin-fixa">Исправить</span>', win)
    assert 'name="webmim_number" value="5316244"' in win and 'name="transfer_number" value="26R-1"' in win   # in <details>: still sent


def test_a_new_order_types_the_numbers_in_the_supplier_block(w):
    text = page(w, "/orders/%d" % card(w, "new"))
    assert 'name="webmim_number"' not in window(text)
    manual = text[text.index("data-supplier-manual"):]
    assert 'name="webmim_number" form="ow-main"' in manual and 'name="transfer_number" form="ow-main"' in manual
    assert 'value="%d">Сохранить и перевести на «Заказ оформлен»' % w["stages"]["placed"]["id"] in manual


def test_the_numbers_typed_in_the_supplier_block_are_saved(w):
    cid = card(w, "new")
    r = post(w["client"], "/orders/%d/save" % cid, w["tok"], webmim_number="5316300", transfer_number="26R-7")
    assert r.status_code == 302
    c = orders.get_card(w["conn"], cid)
    assert (c["webmim_number"], c["transfer_number"]) == ("5316300", "26R-7")


def test_a_field_left_out_of_the_window_is_kept_by_a_save(w):
    cid = card(w, "placed", webmim_number="5316244", webmim_id="777", webmim_status="в работе")
    assert 'name="webmim_id"' not in window(page(w, "/orders/%d" % cid))                   # moved to the block «WebMIM»
    post(w["client"], "/orders/%d/save" % cid, w["tok"], comment="позвонить")
    c = orders.get_card(w["conn"], cid)
    assert (c["webmim_id"], c["webmim_status"], c["comment"]) == ("777", "в работе", "позвонить")


def test_the_service_fields_of_webmim_are_in_its_block(w):
    text = page(w, "/orders/%d" % card(w, "placed", webmim_number="5316244", webmim_id="777"))
    block = text[text.index("data-webmim-fields"):]
    assert 'name="webmim_id" form="ow-main" value="777"' in block and 'name="webmim_status" form="ow-main"' in block


def test_the_late_stages_show_the_point_the_deal_and_the_date_as_text(w):
    win = window(page(w, "/orders/%d" % card(w, "at_point")))
    for label in ("Точка (ПВЗ)", "Тип сделки", "Дата доставки"):
        assert re.search(r'<div class="field owin-fix"><span class="label">%s</span><details class="owin-fixd">' % re.escape(label), win), label
    assert "Точка Север" in win and ">10.11<" in win
    early = window(page(w, "/orders/%d" % card(w, "new")))
    assert 'class="field owin-fix"' not in early


@pytest.mark.parametrize("code, hidden", [("new", ("На ОТХ", "На возврат")), ("moving", ("На ОТХ", "На возврат")), ("oth", ("На возврат",)),
                                          ("at_point", ())])
def test_the_strip_offers_oth_and_return_only_from_the_point_on(w, code, hidden):
    strip = page(w, "/orders/%d" % card(w, code))
    strip = strip[strip.index('class="owin-stages"'):strip.index("</div>", strip.index('class="owin-stages"'))]
    for name in ("На ОТХ", "На возврат"):
        assert (">%s</button>" % name in strip or ">%s · " % name in strip) == (name not in hidden), name


def test_the_blocks_of_other_stages_are_hidden(w):
    new = page(w, "/orders/%d" % card(w, "new"))
    assert 'id="wants"' in new and 'id="money"' not in new and "data-merge-open" in new and 'id="intercity"' in new
    oth = page(w, "/orders/%d" % card(w, "oth", webmim_number="5316244"))
    assert 'id="wants"' not in oth and 'id="intercity"' not in oth and "data-merge-open" not in oth and 'id="money"' in oth
    assert 'name="reserve_until"' not in oth and "Дата счета" not in oth
    moving = page(w, "/orders/%d" % card(w, "moving"))
    assert 'name="reserve_until"' in moving and 'name="codes_written_off"' not in moving
    cancel = page(w, "/orders/%d" % card(w, "cancel"))
    assert 'id="webmim"' not in cancel and 'id="money"' not in cancel


def test_no_third_sum_in_the_money_part(w):
    text = page(w, "/orders/%d" % card(w, "done"))
    assert 'class="ocard-sum"' not in text and "data-order-sum" in text


def test_a_closed_order_is_read_only_until_fix(w):
    cid = card(w, "done")
    text = page(w, "/orders/%d" % cid)
    assert 'id="ow-main" method="post"' not in text and "data-closed" in text and 'href="/orders/%d?edit=1">Исправить заказ…' % cid in text
    assert 'id="ow-main" method="post"' in page(w, "/orders/%d?edit=1" % cid)


def test_the_autosend_is_in_the_chat_column_with_a_chat_only(w):
    cid = card(w, "new")
    text = page(w, "/orders/%d" % cid)
    assert 'id="autosend"' not in text and "data-autosend-nochat" in text
    assert 'id="autosend"' not in page(w, "/orders/%d" % card(w, "done")) and "data-autosend-nochat" not in page(w, "/orders/%d" % card(w, "done"))


def test_the_header_has_no_client_and_the_reset_of_the_columns_is_in_the_column(w):
    text = page(w, "/orders/%d" % card(w, "new"))
    head = text[text.index('<header class="o2c-head">'):text.index("</header>", text.index('<header class="o2c-head">'))]
    assert "Анна" not in head and "o2c-phone" not in head and "data-cc-reset" not in head
    assert 'data-cc-reset title="Колонки по умолчанию' in text


def test_the_task_kinds_follow_the_stage(w):
    def kinds(text):
        box = text[text.index('name="kind"'):text.index("</select>", text.index('name="kind"'))]
        return re.findall(r'<option value="(\w+)"', box)
    assert kinds(page(w, "/orders/%d" % card(w, "new"))) == ["callback", "order", "other"]
    assert kinds(page(w, "/orders/%d" % card(w, "at_point"))) == ["callback", "oth", "return", "other"]
    assert kinds(page(w, "/orders/%d" % card(w, "done"))) == ["callback", "return", "other"]


# ---- the board, the main page, the menu ------------------------------------------------------------------------------------------------

def test_the_board_has_two_tiles_no_stage_filter_and_the_date_without_the_stage(w):
    card(w, "new")
    placed = card(w, "placed", webmim_number="5316244")
    text = page(w, "/orders")
    stats = text[text.index('<div class="o2-stats">'):text.index("</div>\n<form", text.index('<div class="o2-stats">'))]
    assert "Просрочено" in stats and "Ждут действия" in stats and "В работе" not in stats and "Сумма в работе" not in stats
    assert "data-stage-filter" not in text and "data-return-filter" not in text
    assert "data-stage-filter" in page(w, "/orders?v=list")
    one = text[text.index('data-card="%d"' % placed):]
    assert re.search(r"data-stage-date>с \d\d\.\d\d</div>", one[:one.index("</a>")])
    assert "№ WebMIM ещё нет" not in text                                                 # the first stages: no empty number


def test_the_main_page_has_one_line_of_the_feeds_and_no_second_update(w):
    text = page(w, "/")
    head = text[text.index('<header class="page-head">'):text.index("</header>", text.index('<header class="page-head">'))]
    assert 'href="/feed/update"' not in head
    assert "data-feeds-line" in text and "Последний запуск</h3>" not in text and "Готовность к публикации</h3>" not in text
    assert "Утренняя сводка" not in text


def test_the_summary_switch_is_on_the_telegram_tab_and_comes_back_there(w):
    assert "data-summary" in page(w, "/connections/telegram")
    r = post(w["client"], "/summary/toggle", w["tok"], on="1", back="telegram")
    assert r.status_code == 302 and r.headers["Location"].endswith("/connections/telegram")
    assert post(w["client"], "/summary/toggle", w["tok"], on="0").headers["Location"].endswith("/")      # elsewhere: as before


def test_one_item_settings_and_its_pages_as_tabs(w):
    text = page(w, "/orders/settings")
    side = text[text.index('<aside'):text.index("</aside>")]
    assert ">Настройка</a>" in side and ">Настройки заказов</a>" not in side and ">Шаблоны сообщений</a>" not in side
    tabs = text[text.index('class="tabs setup-tabs"'):text.index("</nav>", text.index('class="tabs setup-tabs"'))]
    for url in ("/settings", "/orders/settings", "/orders/templates", "/users", "/history", "/ui-new"):
        assert 'href="%s"' % url in tabs, url
    assert 'href="/orders/settings" class="is-active"' in tabs


def test_the_vk_page_of_a_person_is_saved_on_the_vk_tab_and_keeps_the_telegram_chat(w):
    conn = w["conn"]
    users.add_user(conn, "chat1", "pw-1234567890", NOW, role="chats")
    conn.commit()
    assert post(w["client"], "/connections/telegram/user-chat", w["tok"], login="chat1", chat_id="12345").status_code == 302
    assert "data-user-vk" in page(w, "/connections/vk") and 'name="vk_peer"' not in page(w, "/connections/telegram")
    r = post(w["client"], "/connections/telegram/user-chat", w["tok"], login="chat1", vk_peer="777", back="vk")
    assert r.headers["Location"].endswith("/connections/vk")
    assert order_notify.user_chats(conn).get("chat1") == "12345" and critical.vk_peers(conn).get("chat1") == "777"


def test_a_closed_order_can_be_deleted_without_fix(w):
    cid = card(w, "done")
    text = page(w, "/orders/%d" % cid)
    fieldset = text[text.index('<fieldset disabled'):text.index("</fieldset>", text.index('<fieldset disabled'))]
    assert 'form="ow-delete"' not in fieldset                                            # a disabled fieldset would disable it
    assert 'data-closed-delete><button class="btn btn--danger-outline btn--sm" form="ow-delete">' in text


def test_a_failed_save_does_not_show_the_numbers_twice(w):
    cid = card(w, "new")
    r = post(w["client"], "/orders/%d/save" % cid, w["tok"], transfer_number="26R-1", client_phone="12")     # a wrong phone: the window comes back
    text = r.get_data(as_text=True)
    assert text.count('name="transfer_number"') == 1 and text.count('name="webmim_number"') == 1


def test_the_old_view_keeps_every_task_kind(w):
    store.set_state(w["conn"], "ui_new.office", "off")
    w["conn"].commit()
    text = page(w, "/orders/%d" % card(w, "new"))
    at = text.index('<select class="select select--sm" name="kind">')                   # the form «Поставить задачу»
    box = text[at:text.index("</select>", at)]
    assert re.findall(r'<option value="(\w+)"', box) == ["callback", "order", "oth", "return", "other"]
