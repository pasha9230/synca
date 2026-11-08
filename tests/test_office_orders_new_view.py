"""«Новый вид» of the office's «Заказы» (lane port-office-orders, the prototype's role «Офис»: the board, «Список» with the side panel of the
selection, the card variant C): with the office group's flag on for the viewer the pages are drawn from templates/office2/; off — the pages
as before. The new view adds «Дата создания», the bulk tag / responsible / delete of the selected and the person's own columns of the card."""
from __future__ import annotations

import io
import json
import re
import sqlite3
from pathlib import Path

import pytest

from panel_helpers import login, make_app
from test_roles import PASSWORD, conn_of, sign_in
from tires_sync import order_points as points, orders, store, trash, user_prefs, users

NOW = "2026-10-07T12:00:00"
NEW = "office2.css"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def _order(conn, name, phone, created, webmim="", item=None):
    card = orders.create_card(conn, dict(client_name=name, client_phone=phone), created, "o")
    if webmim:
        orders.update_card(conn, card, dict(webmim_number=webmim), created, "o")
    if item:
        orders.add_item(conn, card, goods_id="1", name=item, brand_key="nokian", season="winter", diameter=16, territory="1", qty=4,
                        sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=created, author="o")
    return card


@pytest.fixture
def w(tmp_path):
    app = make_app(tmp_path)
    conn = conn_of(app)
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, {"name": "Янино", "manager_name": "А", "channel": "vk"})
    for login_, role in (("zaman", "partner"), ("chat1", "chats"), ("view1", "viewer"), ("pt1", "point")):
        users.add_user(conn, login_, PASSWORD, NOW, role=role, point_id=pid if role == "point" else None)
    ids = dict(a=_order(conn, "Анна", "+7 900 000-00-01", "2026-10-01T10:00:00", webmim="7712345", item="Nokian Hakkapeliitta 10 205/55 R16"),
               b=_order(conn, "Борис", "+7 900 000-00-02", "2026-10-05T10:00:00"),
               c=_order(conn, "Вера", "+7 900 000-00-03", "2026-10-07T09:00:00", item="Ikon Autograph Snow 3"))
    conn.commit()
    return {"app": app, "conn": conn, "pid": pid, "ids": ids}


def client_of(w, who):
    client = w["app"].test_client()
    if who == "owner":
        login(client)
    else:
        sign_in(client, w["conn"], who)
    return client


def flag(w, state):
    store.set_state(w["conn"], "ui_new.office", state)
    w["conn"].commit()


def html(client, page):
    r = client.get(page)
    assert r.status_code == 200, page
    return r.get_data(as_text=True)


def is_new(text):
    return NEW in text and "office2.js" in text


def pages(w):
    return ("/orders", "/orders?v=list", "/orders/%d" % w["ids"]["a"])


def csrf(client):
    with client.session_transaction() as s:
        return s.get("csrf") or "tok"


# --- who gets it ------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["", "off"])
def test_off_everybody_keeps_the_old_pages(w, state):
    if state:
        flag(w, state)
    for who in ("owner", "zaman", "chat1", "view1"):
        client = client_of(w, who)
        for page in pages(w):
            text = html(client, page)
            assert not is_new(text) and "o2-" not in text and "data-ccs" not in text, (who, page)
    assert 'class="tiles tiles--row"' in html(client_of(w, "owner"), "/orders")             # the old board itself
    assert 'class="ocard-layout"' in html(client_of(w, "owner"), "/orders/%d" % w["ids"]["a"])


def test_owner_only_is_the_owner_and_the_partner(w):
    flag(w, "owner_only")
    for who, new in (("owner", True), ("zaman", True), ("chat1", False), ("view1", False)):
        client = client_of(w, who)
        for page in pages(w):
            assert is_new(html(client, page)) is new, (who, page)


def test_all_is_every_office_role_never_a_point(w):
    flag(w, "all")
    for who in ("owner", "zaman", "chat1", "view1"):
        client = client_of(w, who)
        for page in pages(w):
            assert is_new(html(client, page)), (who, page)
    point = client_of(w, "pt1")
    assert not is_new(html(point, "/orders"))                                                  # the point's own «Заказы» stay its own
    store.set_state(w["conn"], "ui_new.point", "all")
    w["conn"].commit()
    assert not is_new(html(point, "/orders"))


def test_off_written_is_the_same_markup_as_nothing_written(w):
    client = client_of(w, "owner")

    def snap():
        return {p: re.sub(r'value="[0-9a-f]{24,}"', "", html(client, p)) for p in pages(w)}
    snap()                                                                # the first look marks things seen (the tour, the alerts)
    before = snap()
    flag(w, "off")
    assert snap() == before
    flag(w, "owner_only")
    assert snap() != before
    flag(w, "off")
    assert snap() == before


# --- the board and the list -------------------------------------------------------------------------------------------------------

def test_the_board_shows_the_webmim_number_first_and_the_model_bold(w):
    """The card of the prototype of the audit (the owner 08.11): left-aligned, a field per line — № WebMIM, the tyres, «4 шт.», the sum,
    the point, the account as a label, the days; no inner number, no «№ WebMIM ещё нет», no strip of numbers above the board."""
    flag(w, "all")
    text = html(client_of(w, "chat1"), "/orders")
    a = re.search(r'<a class="ocard o2-card[^"]*"[^>]*data-card="%d".*?</a>' % w["ids"]["a"], text, re.S).group(0)
    assert '<span class="o2-wm">№ 7712345</span>' in a and "внутр." not in a and "№ %d" % w["ids"]["a"] not in a
    assert '<b class="o2-model">Nokian Hakkapeliitta 10 205/55 R16</b>' in a and "4 шт." in a and "× 4" not in a
    assert a.index("o2-wm") < a.index("o2-model") < a.index("4 шт.") < a.index("дн.")
    b = re.search(r'data-card="%d".*?</a>' % w["ids"]["b"], text, re.S).group(0)
    assert "WebMIM ещё нет" not in b and "o2-wm" not in b
    assert 'id="board"' in text and 'data-stage="' in text and 'draggable="true"' in text          # app.js drag and drop: the same hooks
    assert 'class="o2-stats"' not in text and "Сумма в работе" not in text                          # audit 15: no strip of numbers
    assert 'name="from"' in text and 'name="to"' in text and 'name="s"' in text                    # «Дата создания» and «Этап»
    assert "data-filters-toggle" in text and 'class="o2-f-more"' in text                            # the rest under «Фильтры» (the owner 08.11)
    assert 'class="quick"' not in text and 'class="btn btn--secondary" href="/orders/inbox"' not in text    # no quick add, no button «Входящие» (the owner 08.11)


def test_the_date_of_creation_filters_the_board_the_list_and_the_file(w):
    flag(w, "all")
    client = client_of(w, "owner")
    text = html(client, "/orders?from=2026-10-04&to=2026-10-06")
    shown = set(int(x) for x in re.findall(r'data-card="(\d+)"', text))
    assert shown == {w["ids"]["b"]}
    assert "Фильтр включён: 1" in text
    listed = html(client, "/orders?v=list&from=2026-10-06")
    assert set(int(x) for x in re.findall(r'data-sel="(\d+)"', listed)) == {w["ids"]["c"]}
    flag(w, "off")                                                        # the old view has no such filter: an address with it shows all
    old = html(client, "/orders?from=2026-10-04&to=2026-10-06")
    assert {w["ids"]["a"], w["ids"]["b"], w["ids"]["c"]} <= set(int(x) for x in re.findall(r'data-card="(\d+)"', old))


def test_the_list_starts_with_the_stage_and_has_the_selection_panel(w):
    flag(w, "all")
    text = html(client_of(w, "zaman"), "/orders?v=list")
    head = re.search(r"<thead><tr>(.*?)</tr></thead>", text, re.S).group(1)
    assert re.findall(r"<th[^>]*>([^<]*)", head)[:4] == ["", "Этап", "№ WebMIM", "внутр. №"]
    assert 'data-sel-all' in text and 'data-sel="%d"' % w["ids"]["a"] in text and 'data-wm="7712345"' in text
    panel = re.search(r'<aside class="card o2-bulk".*?</aside>', text, re.S).group(0)
    for action in ('action="/orders/bulk/check"', 'action="/orders/bulk/tag"', 'action="/orders/bulk/responsible"', "data-bulk-xls",
                   'action="/orders/bulk/delete"'):
        assert action in panel, action
    assert "Отмена" in panel                                              # «Закрыть разом»'s targets


def test_the_panel_offers_only_what_the_role_may(w):
    flag(w, "all")
    chats = re.search(r'<aside class="card o2-bulk".*?</aside>', html(client_of(w, "chat1"), "/orders?v=list"), re.S).group(0)
    assert "data-bulk-xls" in chats and "/orders/bulk/" not in chats      # no «bulk», no «delete»: Excel only
    viewer = re.search(r'<aside class="card o2-bulk".*?</aside>', html(client_of(w, "view1"), "/orders?v=list"), re.S).group(0)
    assert "/orders/bulk/" not in viewer


# --- the bulk actions -------------------------------------------------------------------------------------------------------------

def _ids(w, *keys):
    return [str(w["ids"][k]) for k in keys]


def test_bulk_tag_on_and_off(w):
    flag(w, "all")
    client = client_of(w, "zaman")
    r = client.post("/orders/bulk/tag", data={"csrf": csrf(client), "id": _ids(w, "a", "b"), "tag": "предоплата", "mode": "on",
                                              "back": "/orders?v=list&q=x"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/orders?v=list&q=x")
    assert "предоплата" in orders.get_card(w["conn"], w["ids"]["a"])["tags"] and "предоплата" in orders.get_card(w["conn"], w["ids"]["b"])["tags"]
    assert "предоплата" not in orders.get_card(w["conn"], w["ids"]["c"])["tags"]
    assert any("Теги" in e["text"] or "тег" in e["text"].lower() for e in orders.list_events(w["conn"], w["ids"]["a"]))    # each order's journal
    client.post("/orders/bulk/tag", data={"csrf": csrf(client), "id": _ids(w, "a"), "tag": "предоплата", "mode": "off"})
    assert "предоплата" not in orders.get_card(w["conn"], w["ids"]["a"])["tags"]
    assert "предоплата" in orders.get_card(w["conn"], w["ids"]["b"])["tags"]


def test_bulk_responsible_names_only_staff(w):
    flag(w, "all")
    client = client_of(w, "zaman")
    client.post("/orders/bulk/responsible", data={"csrf": csrf(client), "id": _ids(w, "a", "c"), "responsible": "chat1"})
    assert [orders.get_card(w["conn"], w["ids"][k])["responsible"] for k in "ac"] == ["chat1", "chat1"]
    users.add_user(w["conn"], "buyer", PASSWORD, NOW, role="client")
    client.post("/orders/bulk/responsible", data={"csrf": csrf(client), "id": _ids(w, "b"), "responsible": "buyer"})
    assert orders.get_card(w["conn"], w["ids"]["b"])["responsible"] != "buyer"           # a forged POST never names a client


def test_bulk_delete_goes_to_the_trash_and_needs_both_rights(w):
    flag(w, "all")
    chats = client_of(w, "chat1")
    r = chats.post("/orders/bulk/delete", data={"csrf": csrf(chats), "id": _ids(w, "a")})
    assert r.status_code == 403
    viewer = client_of(w, "view1")
    assert viewer.post("/orders/bulk/tag", data={"csrf": csrf(viewer), "id": _ids(w, "a"), "tag": "x"}).status_code == 403
    point = client_of(w, "pt1")
    assert point.post("/orders/bulk/tag", data={"csrf": csrf(point), "id": _ids(w, "a"), "tag": "x"}).status_code == 403
    partner = client_of(w, "zaman")
    partner.post("/orders/bulk/delete", data={"csrf": csrf(partner), "id": _ids(w, "a", "b"), "confirmed": "1"})
    left = {r[0] for r in w["conn"].execute("SELECT id FROM order_card")}
    assert w["ids"]["a"] not in left and w["ids"]["b"] not in left and w["ids"]["c"] in left
    assert len(trash.list_deleted(w["conn"], "order")) == 2


def test_bulk_stage_is_the_check_of_close_at_once(w):
    client = client_of(w, "zaman")
    cancel = next(s["id"] for s in orders.list_stages(w["conn"]) if s["code"] == "cancel")
    r = client.post("/orders/bulk/check", data={"csrf": csrf(client), "id": _ids(w, "a", "b"), "to": str(cancel), "reason": "тест"})
    assert r.status_code == 200 and "orders/bulk/run" in r.get_data(as_text=True)              # the same confirmation as before


def test_excel_of_the_selected_only(w):
    openpyxl = pytest.importorskip("openpyxl")
    flag(w, "all")
    client = client_of(w, "owner")
    r = client.get("/orders/export.xlsx?ids=%s" % ",".join(_ids(w, "a", "c")))
    assert r.status_code == 200
    sheet = openpyxl.load_workbook(io.BytesIO(r.data)).active
    names = {str(c.value) for row in sheet.iter_rows(min_row=2) for c in row if c.value}
    assert "Анна" in names and "Вера" in names and "Борис" not in names
    everything = openpyxl.load_workbook(io.BytesIO(client.get("/orders/export.xlsx").data)).active
    assert everything.max_row == 4                                       # without ids: every order, as before


# --- the card variant C -----------------------------------------------------------------------------------------------------------

def test_the_card_has_three_columns_and_the_live_window(w):
    flag(w, "all")
    text = html(client_of(w, "owner"), "/orders/%d" % w["ids"]["a"])
    assert [int(x) for x in re.findall(r'data-cc="(\d)"', text)] == [0, 1, 2]
    assert 'value="7712345"' in text and "внутр." not in text and "Колонки по умолчанию" not in text    # the head: «← Заказы» only (the owner 08.11)
    for hook in ("form[data-owin]".replace("form[data-owin]", "data-owin"), 'class="owin-stages"', "data-autosave", 'action="/orders/%d/note"' % w["ids"]["a"],
                 "data-feed-tabs", 'id="wants"'):
        assert hook in text, hook
    assert 'id="money"' not in text and "Продажа и расчёты" not in text and "Что делать сейчас" not in text
    assert json.loads(re.search(r"data-layout='([^']*)'", text).group(1)) == {"w": [1.15, 1.0, 0.85], "off": [False, False, False]}


def test_the_columns_are_one_s_own(w):
    flag(w, "all")
    owner, partner = client_of(w, "owner"), client_of(w, "zaman")
    layout = {"w": [1.5, 0.7, 0.8], "off": [False, False, True]}
    r = owner.post("/orders/card/layout", data={"csrf": csrf(owner), "layout": json.dumps(layout)})
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    assert user_prefs.get(w["conn"], "owner", "order.card.layout") == layout
    text = html(owner, "/orders/%d" % w["ids"]["b"])
    assert json.loads(re.search(r"data-layout='([^']*)'", text).group(1)) == layout
    assert '<section class="o2c-col is-off" data-cc="2"' in text
    assert "is-off" not in re.search(r'<div class="o2c-body".*', html(partner, "/orders/%d" % w["ids"]["b"]), re.S).group(0).split("data-cc=\"2\"")[0][-80:]
    for bad in ({"w": [1, 1], "off": [False, False]}, {"w": [1, 1, 99], "off": [False] * 3}, {"w": [1, 1, 1], "off": [True] * 3},
                {"w": [1, 1, 1], "off": [0, 0, 0]}, "nonsense"):
        r = owner.post("/orders/card/layout", data={"csrf": csrf(owner), "layout": bad if isinstance(bad, str) else json.dumps(bad)})
        assert r.status_code == 400, bad
    assert user_prefs.get(w["conn"], "owner", "order.card.layout") == layout
    assert owner.post("/orders/card/layout", data={"layout": json.dumps(layout)}).status_code == 400       # no csrf


# --- after the review ----------------------------------------------------------------------------------------------------------------

def test_a_date_that_finds_nothing_is_not_an_empty_panel(w):
    flag(w, "all")
    text = html(client_of(w, "owner"), "/orders?from=2030-01-01")
    assert "Заказов пока нет" not in text and "Фильтр включён: 1" in text


def test_bulk_tag_with_a_comma_is_refused(w):
    flag(w, "all")
    client = client_of(w, "zaman")
    client.post("/orders/bulk/tag", data={"csrf": csrf(client), "id": _ids(w, "a"), "tag": "a,b", "mode": "on"})
    assert orders.get_card(w["conn"], w["ids"]["a"])["tags"] == []


def test_a_deeply_nested_layout_is_a_400_not_a_500(w):
    flag(w, "all")
    owner = client_of(w, "owner")
    r = owner.post("/orders/card/layout", data={"csrf": csrf(owner), "layout": "[" * 50000 + "]" * 50000})
    assert r.status_code == 400


# --- the lead's conditions (2026-11-04) ------------------------------------------------------------------------------------------------

def test_the_bulk_actions_are_the_new_view_s_only(w):
    partner = client_of(w, "zaman")                                      # «Офис» off: no panel, no bulk, the file as before
    for url, extra in (("/orders/bulk/tag", {"tag": "x"}), ("/orders/bulk/responsible", {"responsible": "chat1"}),
                       ("/orders/bulk/delete", {"confirmed": "1"})):
        assert partner.post(url, data=dict(extra, csrf=csrf(partner), id=_ids(w, "a"))).status_code == 403, url
    assert orders.get_card(w["conn"], w["ids"]["a"])["tags"] == []
    assert "data-bulk" not in html(partner, "/orders?v=list")
    openpyxl = pytest.importorskip("openpyxl")
    sheet = openpyxl.load_workbook(io.BytesIO(partner.get("/orders/export.xlsx?ids=%s&from=2030-01-01" % w["ids"]["a"]).data)).active
    assert sheet.max_row == 4                                            # ids and the date are ignored: every order, as before


def test_bulk_delete_asks_with_the_numbers_first(w):
    flag(w, "all")
    partner = client_of(w, "zaman")
    r = partner.post("/orders/bulk/delete", data={"csrf": csrf(partner), "id": _ids(w, "a", "b"), "back": "/orders?v=list"})
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "Удалить 2 заказа?" in page
    listed = re.search(r"<table[^>]*data-bulk-delete-list.*?</table>", page, re.S).group(0)
    assert "7712345" in listed and "№%d" % w["ids"]["a"] in listed and "№%d" % w["ids"]["b"] in listed and "Вера" not in listed
    assert 'name="confirmed" value="1"' in page
    assert w["conn"].execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 3        # nothing deleted by the question
    partner.post("/orders/bulk/delete", data={"csrf": csrf(partner), "id": _ids(w, "a", "b"), "confirmed": "1"})
    assert w["conn"].execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 1
    each = w["conn"].execute("SELECT COUNT(*) FROM audit WHERE action = 'order_delete'").fetchone()[0]
    whole = [json.loads(r[0]) for r in w["conn"].execute("SELECT detail FROM audit WHERE action = 'orders_bulk_delete'")]
    assert each == 2 and whole == [{"orders": sorted([w["ids"]["a"], w["ids"]["b"]]), "refused": 0}]
    restored = trash.list_deleted(w["conn"], "order")
    assert len(restored) == 2                                            # restorable from «Удалённые»


def test_a_refused_delete_leaves_the_order_and_says_why(w):
    flag(w, "all")
    orders.move_card(w["conn"], w["ids"]["a"], next(s["id"] for s in orders.list_stages(w["conn"]) if s["code"] == "talk"), NOW, "o")
    w["conn"].execute("UPDATE order_card SET sale_ids = '[1]' WHERE id = ?", (w["ids"]["a"],))
    w["conn"].commit()
    partner = client_of(w, "zaman")
    r = partner.post("/orders/bulk/delete", data={"csrf": csrf(partner), "id": _ids(w, "a", "b"), "confirmed": "1"}, follow_redirects=True)
    text = r.get_data(as_text=True)
    left = {r[0] for r in w["conn"].execute("SELECT id FROM order_card")}
    assert w["ids"]["a"] in left and w["ids"]["b"] not in left
    assert "Не изменены (1)" in text and "продажа" in text


def test_at_most_200_orders_a_press(w):
    flag(w, "all")
    partner = client_of(w, "zaman")
    many = [str(i) for i in range(1, 202)]
    r = partner.post("/orders/bulk/tag", data={"csrf": csrf(partner), "id": many, "tag": "x"}, follow_redirects=True)
    assert "не больше 200" in r.get_data(as_text=True)
    assert all("x" not in orders.get_card(w["conn"], i)["tags"] for i in w["ids"].values())
    r = partner.post("/orders/bulk/delete", data={"csrf": csrf(partner), "id": many, "confirmed": "1"}, follow_redirects=True)
    assert w["conn"].execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 3


def test_the_layout_is_not_saved_working_as_a_point(w):
    flag(w, "all")
    owner = client_of(w, "owner")
    owner.post("/as-point", data={"csrf": csrf(owner), "point_id": str(w["pid"])})
    r = owner.post("/orders/card/layout", data={"csrf": csrf(owner), "layout": json.dumps({"w": [1, 1, 1], "off": [False] * 3})})
    assert r.status_code in (403, 409)
    assert user_prefs.get(w["conn"], "owner", "order.card.layout") is None


# --- after the second review ---------------------------------------------------------------------------------------------------------

def test_a_locked_database_mid_delete_says_what_is_left_and_keeps_the_record(w, monkeypatch):
    flag(w, "all")
    real, calls = trash.delete_order, []

    def locked_on_the_second(conn, card_id, *args, **kwargs):
        calls.append(card_id)
        if len(calls) == 2:
            raise sqlite3.OperationalError("database is locked")
        return real(conn, card_id, *args, **kwargs)
    monkeypatch.setattr(trash, "delete_order", locked_on_the_second)
    partner = client_of(w, "zaman")
    r = partner.post("/orders/bulk/delete", data={"csrf": csrf(partner), "id": _ids(w, "a", "b", "c"), "confirmed": "1"}, follow_redirects=True)
    assert r.status_code == 200 and "База была занята" in r.get_data(as_text=True)
    assert calls == sorted(calls)[:2] and len(calls) == 2                 # stopped at once, the third was not tried
    assert w["conn"].execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 2
    whole = [json.loads(x[0]) for x in w["conn"].execute("SELECT detail FROM audit WHERE action = 'orders_bulk_delete'")]
    assert whole == [{"orders": [min(w["ids"].values())], "refused": 0, "stopped": True}]


def test_a_press_on_orders_already_gone_leaves_no_record(w):
    flag(w, "all")
    partner = client_of(w, "zaman")
    for _ in range(2):                                                    # «Назад» and the same press again
        partner.post("/orders/bulk/delete", data={"csrf": csrf(partner), "id": _ids(w, "a"), "confirmed": "1"})
    assert w["conn"].execute("SELECT COUNT(*) FROM audit WHERE action = 'orders_bulk_delete'").fetchone()[0] == 1


def test_a_locked_database_on_reading_an_order_stops_the_press_too(w, monkeypatch):
    flag(w, "all")
    real, second, thrown = orders.get_card, w["ids"]["b"], []

    def locked_once(conn, card_id, *args, **kwargs):
        if card_id == second and not thrown:
            thrown.append(card_id)
            raise sqlite3.OperationalError("database is locked")
        return real(conn, card_id, *args, **kwargs)
    monkeypatch.setattr(orders, "get_card", locked_once)
    partner = client_of(w, "zaman")
    r = partner.post("/orders/bulk/tag", data={"csrf": csrf(partner), "id": _ids(w, "a", "b", "c"), "tag": "срочно", "mode": "on"}, follow_redirects=True)
    assert r.status_code == 200 and "База была занята" in r.get_data(as_text=True)
    monkeypatch.undo()
    assert "срочно" in orders.get_card(w["conn"], w["ids"]["a"])["tags"]               # what is done stays done
    assert "срочно" not in orders.get_card(w["conn"], w["ids"]["c"])["tags"]           # the rest is not touched


def test_a_click_beside_the_checkbox_is_skipped_by_the_row_handler_itself():
    root = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static"
    assert 't.closest("a,button,input,label,select,.o2-chk")' in (root / "app.js").read_text(encoding="utf-8")
    assert "stopPropagation" not in (root / "office2.js").read_text(encoding="utf-8")    # the menus close on a click outside


# --- the window by the prototype of the audit (the owner 08.11) ----------------------------------------------------------------------

def test_the_button_to_webmim_is_right_of_the_responsible_and_the_check_lists_the_problems_only(w):
    flag(w, "all")
    owner = client_of(w, "owner")
    empty = html(owner, "/orders/%d" % w["ids"]["b"])
    grid = empty[empty.index('name="responsible"'):empty.index('id="items"')]
    assert "data-owin-wm" in grid and re.search(r'<button type="button" class="btn btn--primary" disabled title="В заказе нет позиций">Оформить в WebMIM</button>', grid)
    sup = empty[empty.index('id="supplier"'):]
    assert "✗" in sup and "Можно и вручную" not in sup and "Заказать в МИМ" not in sup        # the problems listed; the button moved up
    assert "в порядке</span>" not in sup.split("Проверить снова")[0].replace("не в порядке", "")    # no line «в порядке»


def test_the_check_of_the_supplier_is_one_line_when_all_is_well(w, monkeypatch):
    from tires_sync import order_supplier
    real = order_supplier.view

    def all_well(*args, **kwargs):
        v = real(*args, **kwargs)
        return dict(v, lines=[dict(l, state="ok") for l in v["lines"]], blocked=False)
    monkeypatch.setattr(order_supplier, "view", all_well)
    flag(w, "all")
    text = html(client_of(w, "owner"), "/orders/%d" % w["ids"]["c"])
    assert "data-supplier-ok" in text and "всё сходится" in text and "Сверка с WebMIM: 1 позиция, цены и остатки в порядке" in text
    assert not re.findall(r'<li class="supplier-line', text)              # no line «в порядке» (audit 24)
    flag(w, "off")
    old = html(client_of(w, "owner"), "/orders/%d" % w["ids"]["c"])
    assert "data-supplier-ok" not in old and re.findall(r'<li class="supplier-line is-ok"', old)    # the old view: line by line, as before


def test_the_auto_message_is_in_the_chat_column_only_with_a_chat(w):
    flag(w, "all")
    text = html(client_of(w, "owner"), "/orders/%d" % w["ids"]["a"])
    assert 'id="autosend"' not in text                                     # no chat: no auto message (audit 20)
    head = text[text.index('<header class="o2c-head">'):text.index("</header>")]
    assert re.sub(r"<[^>]+>|\s+", "", head) == "Заказы"                    # «← Заказы» only (the owner 08.11)


def test_the_hint_of_the_positions_is_one_line(w):
    flag(w, "all")
    text = html(client_of(w, "owner"), "/orders/%d" % w["ids"]["a"])
    assert "появятся позиции с остатком и ценой объявления.</p>" in text and "Код позиции из «Каталога»" not in text
    flag(w, "off")
    assert "Код позиции из «Каталога»" in html(client_of(w, "owner"), "/orders/%d" % w["ids"]["a"])


def test_the_new_order_page_says_create(w):
    flag(w, "all")
    text = html(client_of(w, "owner"), "/orders/new")
    assert "data-create-head" in text and text.count(">Создать заказ</button>") == 2
    assert text.index('<button class="sr-only"') < text.index("data-create-head")       # Enter still presses the first, hidden button
    flag(w, "off")
    old = html(client_of(w, "owner"), "/orders/new")
    assert "Создать заказ</button>" not in old and '<button class="btn btn--primary">Сохранить</button>' in old


def test_the_sale_keeps_its_one_line_with_its_buttons(w):
    """No «Продажа и расчёты» (the owner 08.11), but «Записать продажу» and «Сторнировать» are made nowhere else: one line on the window."""
    flag(w, "all")
    conn, a = w["conn"], w["ids"]["a"]
    done = next(s["id"] for s in orders.list_stages(conn) if s["code"] == "done")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (done, a))
    conn.commit()
    text = html(client_of(w, "owner"), "/orders/%d" % a)
    assert "data-sale-line" in text and 'action="/orders/%d/sale"' % a in text and "Записать продажу" in text
    conn.execute("UPDATE order_card SET sale_ids = '[7]' WHERE id = ?", (a,))
    conn.commit()
    text = html(client_of(w, "owner"), "/orders/%d" % a)
    assert 'action="/orders/%d/sale/reverse"' % a in text and "Сторнировать продажу" in text and "Продажа и расчёты" not in text
    open_ = html(client_of(w, "owner"), "/orders/%d" % w["ids"]["b"])
    assert "data-sale-line" not in open_                                     # an open stage, no sale: nothing


def test_the_inbox_stays_in_more(w, monkeypatch):
    monkeypatch.setattr("tires_sync.order_manager.new_count", lambda conn: 3)
    flag(w, "all")
    text = html(client_of(w, "owner"), "/orders")
    menu = text[text.index('<details class="menu orders-more">'):text.index("</details>", text.index('<details class="menu orders-more">'))]
    assert '<a class="menu-item" href="/orders/inbox">Входящие (3)</a>' in menu
    assert text.count('href="/orders/inbox"') == 1                         # no button on the head (the owner 08.11)
