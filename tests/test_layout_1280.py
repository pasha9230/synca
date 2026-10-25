"""Orders and the catalog on a 1280 screen (review 2026-10-21: 2.12, 2.13, 2.14, 2.7, 2.5, 1.0f, 1.4): the head of «Заказы» keeps the view and
the main button, the rest is in «Ещё»; closed stages are folded; the window moves an order by one button; destructive buttons are red; boards are wide."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import panel_helpers
from panel_helpers import login, make_app, post
from test_order_cleanup import base as cleanup_base
from test_order_search import REF, UNLOAD
from tires_sync import orders, store

STATIC = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static"


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(panel_helpers, "UNLOAD", UNLOAD)
    monkeypatch.setattr(panel_helpers, "REF_TEXT", REF)
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 21, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    client.get("/orders")                                                                       # the pipeline
    return client, tok, store.connect(app.config["DB_PATH"])


def new_card(client, tok, name="Иван") -> int:
    r = post(client, "/orders/new", tok, client_name=name, item_goods_id=["100"], item_qty=["4"])
    assert r.status_code == 302, r.get_data(as_text=True)[:300]
    return int(re.search(r"/orders/(\d+)$", r.headers["Location"]).group(1))


def head(page: str) -> str:
    return page[page.index('<header class="page-head">'):page.index("</header>")]


def test_the_orders_head_keeps_the_view_and_the_main_button_and_folds_the_rest_into_more(tmp_path, monkeypatch):
    client, tok, conn = setup(tmp_path, monkeypatch)
    page = client.get("/orders").get_data(as_text=True)
    top = head(page)
    menu = top[top.index('<details class="menu orders-more">'):top.index("</details>")]
    outside = top.replace(menu, "")
    assert '<summary class="btn btn--secondary">Ещё ⋯</summary>' in menu
    for href, label in (("/orders/report", "Отчёт"), ("/orders/bulk", "Закрыть разом"), ("/orders/templates", "Шаблоны"),
                        ("/orders/settings", "Настроить воронку"), ("/deleted", "Удалённые"), ("/orders/inbox", "Входящие")):
        assert '<a class="menu-item" href="%s">%s' % (href, label) in menu, label
        assert 'href="%s"' % href not in outside, label                                        # not a button of its own any more
    assert re.search(r'<a class="menu-item" href="/orders/export\.xlsx\?[^"]*"[^>]*>Выгрузить в Excel</a>', menu)
    assert "export.xlsx" not in page.replace(menu, "")                                          # Excel left the filters row
    assert 'class="seg" role="group" aria-label="Вид"' in outside and '>Доска</a>' in outside and '>Список</a>' in outside
    assert re.search(r'<a class="btn btn--primary" href="/orders/new">.*Новый заказ</a>', outside)
    assert '<div class="page page--wide">' in page and '<div class="tiles tiles--row">' in page
    assert len(re.findall(r'<div class="tile[ "]', page)) == 5                                     # the five tiles, one row


def test_the_inbox_shows_itself_in_the_head_only_when_something_came(tmp_path, monkeypatch):
    client, tok, conn = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("tires_sync.order_manager.new_count", lambda conn: 3)
    top = head(client.get("/orders").get_data(as_text=True))
    assert '<a class="btn btn--secondary" href="/orders/inbox"' in top and '<span class="sb-count sb-count--red">3</span>' in top
    assert "Входящие (3)" in top                                                               # and in the menu with the count


def test_closed_stages_are_folded_columns_and_open_ones_are_not(tmp_path, monkeypatch):
    client, tok, conn = setup(tmp_path, monkeypatch)
    new_card(client, tok)
    page = client.get("/orders").get_data(as_text=True)
    stages = {s["code"]: s for s in orders.list_stages(conn)}
    for code, s in stages.items():
        col = re.search(r'<section class="col([^"]*)" data-stage="%d"([^>]*)>(.*?)</section>' % s["id"], page, re.S)
        assert col, code
        folded = s["kind"] != "open"
        assert ("is-folded" in col.group(1)) == folded and ("data-fold" in col.group(2)) == folded, code
        if folded:
            assert '<button type="button" class="col-head" data-fold-toggle aria-expanded="false" aria-controls="col-body-%d"' % s["id"] in col.group(3)
            assert '<span class="col-name">%s</span>' % s["name"] in col.group(3)
        else:
            assert "data-fold-toggle" not in col.group(3)
    assert {c for c, s in stages.items() if s["kind"] != "open"} == {"done", "cancel", "refused", "return"}
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '[data-fold-toggle]' in js and 'classList.toggle("is-folded")' in js and '"aria-expanded"' in js
    css = (STATIC / "panel.css").read_text(encoding="utf-8")
    assert ".col.is-folded .sum,.col.is-folded .col-body{display:none}" in css and ".page--wide{max-width:none" in css


def window(page: str) -> str:
    return page[page.index('<section class="card owin"'):page.index("</section>", page.index('<section class="card owin"'))]


def next_button(page: str):
    return re.search(r'<button class="btn btn--primary btn--sm owin-next" formaction="([^"]+)" name="stage_id" value="(\d+)"[^>]*>Перевести в «([^»]+)»', page)


def test_the_window_moves_an_order_to_the_next_stage_by_one_button_that_saves_first(tmp_path, monkeypatch):
    client, tok, conn = setup(tmp_path, monkeypatch)
    cid = new_card(client, tok)
    stages = {s["code"]: s for s in orders.list_stages(conn)}
    page = window(client.get("/orders/%d" % cid).get_data(as_text=True))
    found = next_button(page)
    assert found and found.groups() == ("/orders/%d/save" % cid, str(stages["talk"]["id"]), "Переговоры")
    main = page[page.index('<form id="ow-main"'):page.index("</form>")]
    assert main.index('<button class="sr-only"') < main.index("owin-next")                      # Enter in a field still saves, it never moves
    post(client, "/orders/%d/save" % cid, tok, stage_id=found.group(2), client_name="Иван")       # the button posts what a chip of the strip posts
    assert orders.get_card(conn, cid)["stage"]["code"] == "talk"
    found = next_button(window(client.get("/orders/%d" % cid).get_data(as_text=True)))
    assert found.group(3) == "Ожидает наличие"


def test_the_last_open_stage_leads_to_done_and_closed_stages_have_no_next_button(tmp_path, monkeypatch):
    client, tok, conn = setup(tmp_path, monkeypatch)
    cid = new_card(client, tok)
    stages = {s["code"]: s for s in orders.list_stages(conn)}
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stages["oth"]["id"], cid))   # a test base: put the card on the last open stage
    conn.commit()
    found = next_button(window(client.get("/orders/%d" % cid).get_data(as_text=True)))
    assert found and found.group(2) == str(stages["done"]["id"]) and found.group(3) == "Выполнено"
    for code in ("done", "cancel", "return"):
        conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stages[code]["id"], cid))
        conn.commit()
        assert "owin-next" not in window(client.get("/orders/%d" % cid).get_data(as_text=True)), code


def test_the_new_order_window_has_no_next_button(tmp_path, monkeypatch):
    client, tok, conn = setup(tmp_path, monkeypatch)
    assert "owin-next" not in client.get("/orders/new").get_data(as_text=True)


def test_destructive_actions_of_the_window_are_red(tmp_path, monkeypatch):
    client, tok, conn = setup(tmp_path, monkeypatch)
    cid = new_card(client, tok)
    page = window(client.get("/orders/%d" % cid).get_data(as_text=True))
    assert '<a class="menu-item menu-item--danger" href="#finish" data-open-modal="finish-window">Отменить заказ или оформить возврат…</a>' in page
    assert '<button class="menu-item menu-item--danger" form="ow-delete">Удалить заказ…</button>' in page


def test_the_catalog_is_wide_its_names_end_in_one_line_and_mass_removal_is_red(tmp_path, monkeypatch):
    client, tok, conn = setup(tmp_path, monkeypatch)
    page = client.get("/catalog").get_data(as_text=True)
    assert '<div class="page page--wide">' in page
    assert '<th class="t-namecol">Название</th>' in page and '<th class="t-actions" style="width:1%">Действия</th>' in page
    assert re.search(r'<td class="t-namecol"><div class="t-name"><span title="([^"]+)" class="gname">\1</span>', page)   # the whole name is the hint
    assert '<button name="bulk" value="off" class="btn btn--danger-outline btn--sm">Снять выбранные</button>' in page
    css = (STATIC / "panel.css").read_text(encoding="utf-8")
    assert ".table--items .gname{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}" in css
    assert ".bulkbar{flex-wrap:wrap}" in css


def test_a_form_page_stays_narrow(tmp_path, monkeypatch):
    client, tok, conn = setup(tmp_path, monkeypatch)
    page = client.get("/orders/new").get_data(as_text=True)
    assert '<div class="page">' in page and "page--wide" not in page


def test_the_cleanup_button_is_red(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 19, 9, 0, 0))
    cleanup_base(app.config["DB_PATH"])
    client = app.test_client()
    login(client)
    page = client.get("/orders/cleanup").get_data(as_text=True)
    assert re.search(r'<button class="btn btn--danger">Убрать \d+ карточек</button>', page)


def test_the_next_stage_follows_the_owners_order_and_skips_a_custom_refusal_stage(tmp_path, monkeypatch):
    client, tok, conn = setup(tmp_path, monkeypatch)
    cid = new_card(client, tok)
    stages = {s["code"]: s for s in orders.list_stages(conn)}
    lost = orders.add_stage(conn, "Отказ клиента", "#D92D20", "lost", "", "")
    for sid, position in ((lost, 1), (stages["wait"]["id"], 2), (stages["talk"]["id"], 3)):      # the owner put a refusal and «Ожидает наличие» first
        conn.execute("UPDATE order_stage SET position = ? WHERE id = ?", (position, sid))
    conn.commit()
    found = next_button(window(client.get("/orders/%d" % cid).get_data(as_text=True)))
    assert found and found.group(2) == str(stages["wait"]["id"]) and found.group(3) == "Ожидает наличие"


def test_a_filter_unfolds_the_closed_stages_so_what_was_found_shows(tmp_path, monkeypatch):
    client, tok, conn = setup(tmp_path, monkeypatch)
    new_card(client, tok)
    for url in ("/orders?q=Иван", "/orders?t=важный"):
        page = client.get(url).get_data(as_text=True)
        assert "is-folded" not in page and "data-fold-toggle" not in page, url
    css = (STATIC / "panel.css").read_text(encoding="utf-8")
    assert ".col.is-folded.is-over{" in css                                                        # a folded column still shows where a card drops
