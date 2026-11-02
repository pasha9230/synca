"""The order window saves the client's name, the phone, the point, the deal and the responsible by itself (the owner 2026-11-02):
POST /orders/<id>/autosave — one field, checked and written in the journal as «Сохранить» does, with its rights (a point and a viewer
cannot) and its CSRF; any other field is refused. «Время доставки» is out of the window; the column and the old values stay."""
from __future__ import annotations

import json

from test_order_window import created, setup
from test_roles import person, sign_in
from panel_helpers import post
from tires_sync import orders, users
from tires_sync import order_points as points


def autosave(client, cid, tok, field, value, was=None):
    data = {"csrf": tok, "field": field, field: value}
    if was is not None:
        data["was"] = was
    return client.post("/orders/%d/autosave" % cid, data=data, headers={"X-Requested-With": "fetch"})


def journal(conn, cid) -> list:
    return [r[0] for r in conn.execute("SELECT text FROM order_event WHERE order_id = ? AND kind = 'field' ORDER BY id", (cid,))]


def test_each_of_the_five_fields_saves_by_itself_with_the_record_save_writes(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", "2026-10-15T12:00:00")
    a = points.save_point(conn, {"name": "Альфа"})
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))
    before = journal(conn, cid)
    for field, value, stored in (("client_name", "Антон", "Антон"), ("client_phone", "+7 900 123-45-67", "+7 900 123-45-67"),
                                 ("point_id", str(a), a), ("deal_kind", "stock", "stock"), ("responsible", "worker2", "worker2")):
        r = autosave(client, cid, tok, field, value)
        assert r.status_code == 200 and r.get_json() == {"ok": True, "value": stored}, field
        assert orders.get_card(conn, cid)[field] == stored, field
    assert journal(conn, cid)[len(before):] == ["Клиент: Антон", "Телефон: +7 900 123-45-67", "Точка: %d" % a, "Тип сделки: stock",
                                                "Ответственный: worker2"]
    assert autosave(client, cid, tok, "client_name", "Антон").get_json() == {"ok": True, "value": "Антон"}      # the same again: no record
    assert len(journal(conn, cid)) - len(before) == 5


def test_the_records_are_those_of_save(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    one, two = (created(post(client, "/orders/new", tok, client_name="Иван")) for _ in range(2))
    assert autosave(client, one, tok, "client_name", "Пётр").status_code == 200
    page = client.get("/orders/%d" % two).get_data(as_text=True)
    post(client, "/orders/%d/save" % two, tok, client_name="Пётр")
    assert journal(conn, one)[-1] == journal(conn, two)[-1] == "%s: Пётр" % orders.LABELS["client_name"]
    assert 'name="client_name" value="Иван" maxlength="120" data-autosave' in page and 'name="point_id" data-autosave' in page
    # the phone only when the field is left (review 2026-11-02): ten digits of a number half typed are a «number» already — the order would
    # join a client of that wrong phone; the script waits for the pause only without data-autosave-whole
    assert 'maxlength="30" data-autosave data-autosave-whole' in page
    script = open(app.root_path + "/static/app.js", encoding="utf-8").read()
    assert 'if (!field.hasAttribute("data-autosave-whole")) field.addEventListener("input"' in script


def test_a_wrong_value_is_refused_with_its_message_and_nothing_is_saved(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))
    before = journal(conn, cid)
    for field, value in (("client_phone", "12"), ("deal_kind", "нет такого"), ("point_id", "999"), ("responsible", "чужой")):
        r = autosave(client, cid, tok, field, value)
        assert r.status_code == 400 and r.get_json()["ok"] is False and r.get_json()["error"], field
    card = orders.get_card(conn, cid)
    assert (card["client_phone"], card["deal_kind"], card["point_id"], card["responsible"]) == ("", "webmim", None, "owner")
    assert journal(conn, cid) == before


def test_only_the_five_fields_and_never_without_the_token(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))
    for field in ("comment", "delivery_time", "webmim_number", "stage_id", "tags"):
        assert autosave(client, cid, tok, field, "x").status_code == 400, field
    assert client.post("/orders/%d/autosave" % cid, data={"field": "client_name"}).status_code == 400              # «field» alone, no value
    assert autosave(client, cid, "чужой", "client_name", "Взломщик").status_code in (400, 403)
    assert orders.get_card(conn, cid)["client_name"] == "Иван" and orders.get_card(conn, cid)["comment"] == ""
    assert autosave(client, 99999, tok, "client_name", "x").status_code == 404


def test_a_point_and_a_viewer_cannot_autosave_and_see_no_autosaving_fields(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    pid = points.save_point(conn, {"name": "Альфа"})
    cid = created(post(client, "/orders/new", tok, client_name="Иван", point_id=str(pid)))
    for role in ("point", "viewer"):
        other, _ = person(app, role, pid if role == "point" else None)
        r = autosave(other, cid, "tok", "client_name", "Чужой")
        assert r.status_code == 403, role
        page = other.get("/orders/%d" % cid).get_data(as_text=True)
        assert "data-autosave" not in page, role
    assert orders.get_card(conn, cid)["client_name"] == "Иван"


def test_was_keeps_a_field_the_system_wrote_after_the_page_was_opened(tmp_path, monkeypatch):
    """As «Сохранить» (_touched): the value the page showed is not sent as a change; the answer's value goes into the page's «was»."""
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))
    shown = json.dumps({k: orders._row(conn, cid)[k] for k in orders.EDITABLE}, ensure_ascii=False)
    orders.update_card(conn, cid, {"client_name": "Иван Петров"}, "2026-10-12T09:00:00", "система")       # meanwhile, not on the page
    assert autosave(client, cid, tok, "client_name", "Иван", was=shown).get_json() == {"ok": True, "value": "Иван Петров"}
    assert orders.get_card(conn, cid)["client_name"] == "Иван Петров"


def test_the_delivery_time_is_out_of_the_card_its_old_value_stays_and_a_save_keeps_it(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    a = points.save_point(conn, {"name": "Альфа", "default_time": "до 18:00"})
    cid = created(post(client, "/orders/new", tok, client_name="Иван", point_id=str(a)))
    assert orders.get_card(conn, cid)["delivery_time"] == ""                   # a new order: empty, the point's time is not copied in
    conn.execute("UPDATE order_card SET delivery_time = 'после 18' WHERE id = ?", (cid,))
    conn.commit()
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert 'name="delivery_time"' not in page and "Время доставки" not in page
    post(client, "/orders/%d/save" % cid, tok, client_name="Пётр", was=json.dumps({k: orders._row(conn, cid)[k] for k in orders.EDITABLE},
                                                                                  ensure_ascii=False))
    assert orders.get_card(conn, cid)["delivery_time"] == "после 18"
