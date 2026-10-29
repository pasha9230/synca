"""«Рабочее место» of a point (the owner 2026-10-29): the panel's frame with the menu, no CRM, the stages of the point's orders, one search
line, «Отменить передачу» while the manager has not taken the client, the return of «На ОТХ» with the act МХ-3, «История точек»."""
from __future__ import annotations

import re
from datetime import date

from panel_helpers import post
from test_mypoint import hand_over, handed, manager
from test_roles import person
from test_vk_commands import NOW, _card, _stage, ready
from tires_sync import order_manager as mgr, order_oth, order_points as points, orders, point_log


def _text(r):
    return r.get_data(as_text=True)


def _other(conn):
    return points.save_point(conn, dict(name="Гамма", channel="vk", vk_peer_id="777"))


# ---- 1. the menu ------------------------------------------------------------------------------------------------------------------

def test_the_point_works_in_the_panels_frame_with_its_own_menu(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    assert mine.get("/").headers["Location"].endswith("/my-point")                        # the start page stays
    page = _text(mine.get("/my-point"))
    assert 'class="sidebar"' in page and '<title>Рабочее место' in page and "<h1>Рабочее место · Бета" in page
    assert 'href="/my-point">' in page and "Рабочее место</a>" in page and "mypoint.css" in page
    assert 'href="/orders"' in page and 'href="/chats"' in page and 'href="/clients"' not in page           # its sections, no CRM
    assert 'href="/points/history"' not in page and "Моя точка" not in page
    assert page.count("notify.js") == 1                                                    # the bell's sound: once, from the frame


# ---- 2. no CRM -------------------------------------------------------------------------------------------------------------------

def test_the_point_has_no_crm_and_its_order_card_still_opens(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    client_id = conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", (card,)).fetchone()
    for url in ["/clients", "/clients/duplicates"] + (["/clients/%d" % client_id[0]] if client_id else []):
        assert mine.get(url).status_code == 403, url
    page = _text(mine.get("/orders/%d" % card))
    assert "Татьяна" in page and "карточка клиента" not in page
    groups = mine.get("/search.json", query_string={"q": "+79001112233"}).get_json()["groups"]
    assert [g["key"] for g in groups] == ["orders"]                                         # the top search finds no client either
    assert 'href="/clients"' in _text(client.get("/orders"))                              # the owner keeps CRM


# ---- 3. the stages ---------------------------------------------------------------------------------------------------------------

def test_the_stages_count_the_own_orders_and_lead_to_the_list_of_the_stage(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    here = _card(conn, pid, webmim="700200", code="at_point")
    _card(conn, _other(conn), webmim="700300", code="at_point")                          # another point's: never counted
    mine = manager(app, conn, pid)
    page = _text(mine.get("/my-point"))
    counts = dict(re.findall(r'data-stage-count="(\d+)"><span class="dot"[^>]*></span><span class="mp-stage-name">[^<]*</span><b>(\d+)</b>', page))
    names = [s for s in orders.list_stages(conn)]
    assert len(counts) == len(names)                                                       # every stage of the owner's pipeline
    assert counts[str(_stage(conn, "at_point"))] == "1" and counts[str(_stage(conn, "moving"))] == "1" and counts[str(_stage(conn, "new"))] == "0"
    listed = _text(mine.get("/orders?v=list&s=%d" % _stage(conn, "at_point")))
    assert 'href="/orders/%d"' % here in listed and 'href="/orders/%d"' % card not in listed and "Только этап «Товар на точке»" in listed
    assert 'href="/orders/%d"' % card in _text(mine.get("/orders?v=list&s=999"))           # an unknown stage: no filter


# ---- 4. the search ---------------------------------------------------------------------------------------------------------------

def test_the_search_line_finds_the_own_order_by_number_transfer_and_phone_only(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    orders.update_card(conn, card, dict(transfer_number="26R-004417"), NOW, "o")
    theirs = _card(conn, _other(conn), webmim="700300")
    orders.update_card(conn, theirs, dict(transfer_number="26R-009999", client_phone="+7 911 000 00 00"), NOW, "o")
    mine = manager(app, conn, pid)

    def found(q):
        page = _text(mine.get("/my-point", query_string={"q": q}))
        return set(int(n) for n in re.findall(r'<a class="mp-name" href="/orders/(\d+)"', page))

    assert found(str(card)) == {card} and found("№%d" % card) == {card}
    assert found("26R-004417") == {card} and found("004417") == {card} and found("26r-0044") == {card}
    assert found("+7 900 111-22-33") == {card} and found("89001112233") == {card}
    assert found("26R-009999") == set() and found("+79110000000") == set() and found(str(theirs)) == set()
    assert "Среди заказов точки ничего не нашлось" in _text(mine.get("/my-point", query_string={"q": "26R-009999"}))
    urls = {i["url"] for g in mine.get("/search.json", query_string={"q": "26R-004417"}).get_json()["groups"] for i in g["items"]}
    assert urls == {"/orders/%d" % card}                                                    # the top bar finds the transfer number too
    assert mine.get("/search.json", query_string={"q": "26R-009999"}).get_json()["groups"] == []


# ---- 5. «Отменить передачу» ----------------------------------------------------------------------------------------------------

def _handed_over(app, conn, pid, phone="+7 900 555-66-77", login_="gamma"):
    mgr.set_duty(conn, {d: "worker2" for d in range(7)}, (10, 20), "", "2026-10-13")
    mine = manager(app, conn, pid, login_=login_)
    _tok, r = hand_over(mine, phone, "зима 205/55 R16")
    assert r.status_code == 302
    return mine, handed(conn)[-1][0]


def test_the_point_cancels_its_handoff_not_taken_and_the_manager_is_told(tmp_path, monkeypatch):
    app, client, tok, conn, pid, card = ready(tmp_path)
    told = []
    from tires_sync.panel import routes_vk
    monkeypatch.setattr(routes_vk, "channel", lambda *a: (None, None, lambda c, text: told.append((c["id"], c["responsible"], text)) or "отправлено"))
    mine, order_id = _handed_over(app, conn, pid)
    page = _text(mine.get("/my-point"))
    assert 'data-handoff-cancel="%d"' % order_id in page
    r = post(mine, "/my-point/handoff/%d/cancel" % order_id, "tok")
    assert r.status_code == 302
    got = orders.get_card(conn, order_id)
    assert got["stage"]["code"] == "cancel" and got["cancel_reason"] == mgr.CANCEL_REASON
    assert any(e["level"] == "yellow" and "отменила передачу клиента" in e["text"] for e in orders.list_events(conn, order_id))
    assert told[0][0] == told[-1][0] == order_id and told[-1][1] == told[0][1] and "отменила передачу" in told[-1][2]   # whom the handoff told
    page = _text(mine.get("/my-point"))
    assert 'data-status="cancelled"' in page and "отменён точкой" in page and 'data-handoff-cancel="%d"' % order_id not in page
    assert mgr.take(conn, order_id, "открыл", "worker2", NOW) is False                       # nobody takes a cancelled client
    before = len(told)
    r = post(mine, "/my-point/handoff/%d/cancel" % order_id, "tok")                         # again: refused, nothing more
    assert "уже не отменить" in _text(mine.get(r.headers["Location"])) and len(told) == before
    assert [r["kind"] for r in point_log.history(conn, pid)][:2] == ["handoff_cancel", "handoff"]


def test_a_taken_handoff_or_another_points_cannot_be_cancelled(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine, order_id = _handed_over(app, conn, pid)
    assert mgr.take(conn, order_id, "открыл заказ", "worker2", NOW)
    assert 'data-handoff-cancel="%d"' % order_id not in _text(mine.get("/my-point"))
    post(mine, "/my-point/handoff/%d/cancel" % order_id, "tok")
    assert orders.get_card(conn, order_id)["stage"]["code"] == "from_point"
    stranger = manager(app, conn, _other(conn), login_="delta")
    _m2, second = _handed_over(app, conn, pid, phone="+7 900 444-55-66", login_="gamma2")
    assert post(stranger, "/my-point/handoff/%d/cancel" % second, "tok").status_code in (403, 404)
    assert orders.get_card(conn, second)["stage"]["code"] == "from_point"


def test_the_cancel_and_a_take_at_once_one_wins(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    _mine, order_id = _handed_over(app, conn, pid)
    point = points.get_point(conn, pid)
    conn.execute("UPDATE order_handoff_source SET taken_at = ?, taken_by = 'worker2' WHERE order_id = ?", (NOW, order_id))   # taken a moment ago
    try:
        mgr.cancel_handoff(conn, order_id, point, "gamma", NOW)
        raise AssertionError("cancelled a taken client")
    except orders.OrderError:
        pass
    assert orders.get_card(conn, order_id)["stage"]["code"] == "from_point"


# ---- 6. the return of «На ОТХ» -------------------------------------------------------------------------------------------------

def test_the_nearest_odd_date():
    assert order_oth.return_date(date(2026, 10, 29)) == "2026-10-29"                       # odd: today
    assert order_oth.return_date(date(2026, 10, 28)) == "2026-10-29"
    assert order_oth.return_date(date(2026, 4, 30)) == "2026-05-01"                        # the end of a month: the 1st
    assert order_oth.return_date(date(2026, 2, 28)) == "2026-03-01"


def _on_oth(conn, pid, webmim="700500"):
    card = _card(conn, pid, webmim=webmim, code="at_point")
    assert orders.move_card(conn, card, _stage(conn, "oth"), NOW, "o")
    return card


def test_agreed_return_the_point_signs_the_act_and_the_order_is_given_back(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    oth = _on_oth(conn, pid)
    mine = manager(app, conn, pid)
    assert "Подписал акт и отдал" not in _text(mine.get("/my-point"))                     # not agreed: nothing to sign
    assert "Согласовано с поставщиком" in _text(client.get("/orders/%d" % oth))
    assert post(mine, "/orders/%d/oth-agreed" % oth, "tok").status_code == 403            # the point never agrees
    r = post(client, "/orders/%d/oth-agreed" % oth, "tok")
    assert r.status_code == 302
    plan = order_oth.return_plan(conn, oth)
    day = date.fromisoformat(plan["return_date"])
    assert day.day % 2 == 1 and plan["signed_at"] is None
    assert "Передать на возврат %s" % day.strftime("%d.%m") in _text(client.get("/orders/%d" % oth))     # marked in the card
    post(client, "/orders/%d/oth-agreed" % oth, "tok")                                     # twice: one plan, one record
    assert sum("согласован с поставщиком" in e["text"] for e in orders.list_events(conn, oth)) == 1
    page = _text(mine.get("/my-point"))
    assert 'data-return="%d"' % oth in page and "Возврат %s" % day.strftime("%d.%m") in page and "Подпишите акт МХ-3" in page
    token = re.search(r'action="/my-point/%d/act_signed".*?name="token" value="([0-9a-f]+)"' % oth, page, re.S).group(1)
    r = post(mine, "/my-point/%d/act_signed" % oth, "tok", token=token)
    assert r.status_code == 302
    got = orders.get_card(conn, oth)
    assert got["stage"]["code"] == orders.RETURN_GIVEN and got["stage"]["name"] == "Возврат передан" and got["stage"]["kind"] == "open"
    assert order_oth.return_plan(conn, oth)["signed_at"]
    assert any(e["level"] == "yellow" and e["text"] == order_oth.SIGNED_ASK for e in orders.list_events(conn, oth))
    assert conn.execute("SELECT 1 FROM client_order WHERE order_id = ?", (oth,)).fetchone() is None     # the refused client stays unlinked
    assert 'data-return="%d"' % oth not in _text(mine.get("/my-point"))
    assert "Акт МХ-3 подписан" in _text(client.get("/orders/%d" % oth))
    again = re.search(r'name="token" value="([0-9a-f]+)"', _text(mine.get("/my-point"))).group(1)
    post(mine, "/my-point/%d/act_signed" % oth, "tok", token=again)                        # a press of an old page: nothing more
    assert sum(e["text"] == order_oth.SIGNED_ASK for e in orders.list_events(conn, oth)) == 1
    assert [r["kind"] for r in point_log.history(conn, pid)][0] == "act_signed"
    return_stage = _stage(conn, "return")                                                  # the manager closes it with the amount, as before
    orders.move_card(conn, oth, return_stage, NOW, "o", changes={"return_kind": "supplier", "return_reason": "ОТХ", "return_amount": "20000"})
    assert orders.get_card(conn, oth)["stage"]["code"] == "return"


def test_the_old_return_button_still_works(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    here = _card(conn, pid, webmim="700200", code="at_point")
    mine = manager(app, conn, pid)
    page = _text(mine.get("/my-point"))
    token = re.search(r'action="/my-point/%d/returned".*?name="token" value="([0-9a-f]+)"' % here, page, re.S).group(1)
    post(mine, "/my-point/%d/returned" % here, "tok", token=token)
    assert any(e["text"] == mgr.RETURN_LINE for e in orders.list_events(conn, here))


def test_the_stage_is_added_once_right_after_oth_to_an_old_pipeline(tmp_path):
    from tires_sync import store
    conn = store.connect(str(tmp_path / "x.db"))
    for position, (code, name, color, kind) in enumerate(c for c in orders.DEFAULT_STAGES if c[0] != orders.RETURN_GIVEN):
        conn.execute("INSERT INTO order_stage (code, name, color, position, kind) VALUES (?,?,?,?,?)", (code, name, color, position, kind))
    conn.commit()
    orders.ensure_pipeline(conn, NOW)
    orders.ensure_pipeline(conn, NOW)
    codes = [s["code"] for s in orders.list_stages(conn)]
    assert codes.count(orders.RETURN_GIVEN) == 1 and codes[codes.index("oth") + 1] == orders.RETURN_GIVEN
    assert [s["position"] for s in orders.list_stages(conn)] == list(range(len(codes)))


# ---- 7. «История точек» --------------------------------------------------------------------------------------------------------

def test_the_history_shows_the_points_actions_to_the_owner_and_the_partner_only(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    other = _other(conn)
    mine = manager(app, conn, pid)
    page = _text(mine.get("/my-point"))
    token = re.search(r'action="/my-point/%d/arrived".*?name="token" value="([0-9a-f]+)"' % card, page, re.S).group(1)
    post(mine, "/my-point/%d/arrived" % card, "tok", token=token)
    point_log.record(conn, NOW, "delta", other, "issued", None, "чужая точка")
    rows = point_log.history(conn, pid)
    assert [(r["kind"], r["order_id"], r["author"]) for r in rows] == [("arrived", card, "gamma")]
    page = _text(client.get("/points/history?p=%d" % pid))
    assert "принял заказ (приехал)" in page and "gamma" in page and "чужая точка" not in page and 'href="/orders/%d"' % card in page
    assert "чужая точка" in _text(client.get("/points/history"))
    partner, _ = person(app, "partner")
    assert partner.get("/points/history").status_code == 200
    for role in ("chats", "viewer"):
        assert person(app, role)[0].get("/points/history").status_code == 403, role
    assert mine.get("/points/history").status_code == 403
    assert 'href="/points/history"' in _text(client.get("/my-point"))


def test_a_move_on_the_board_by_the_point_is_in_the_history(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    r = post(mine, "/orders/%d/move" % card, "tok", stage_id=str(_stage(conn, "at_point")))
    assert r.status_code == 302 and orders.get_card(conn, card)["stage"]["code"] == "at_point"
    [row] = point_log.history(conn, pid)
    assert row["kind"] == "move" and "Товар на точке" in row["text"]


def test_a_new_stay_on_oth_needs_a_new_agreement_and_the_window_never_skips_the_act(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    oth = _on_oth(conn, pid)
    order_oth.agree(conn, orders.get_card(conn, oth), date(2026, 10, 28), NOW, "owner")
    window = _text(client.get("/orders/%d" % oth))
    assert "Перевести в «Возврат передан»" not in window                                    # the act is the point's press, not a click
    assert order_oth.sign_act(conn, orders.get_card(conn, oth), "Бета", NOW, "gamma")
    assert "Перевести в «Возврат»" in _text(client.get("/orders/%d" % oth))                 # then the amount closes it
    assert orders.move_card(conn, oth, _stage(conn, "oth"), NOW, "o")                       # back on «На ОТХ» (a mistake): a new stay
    assert order_oth.return_plan(conn, oth) is None and order_oth.to_return(conn, pid) == []
    stranger = manager(app, conn, _other(conn), login_="delta")
    order_oth.agree(conn, orders.get_card(conn, oth), date(2026, 10, 28), NOW, "owner")
    assert post(stranger, "/my-point/%d/act_signed" % oth, "tok", token="a" * 24).status_code in (403, 404)
    assert orders.get_card(conn, oth)["stage"]["code"] == "oth"


def test_a_take_between_the_check_and_the_cancel_wins(tmp_path, monkeypatch):
    """The manager takes the client after cancellable() said yes: the one-statement mark refuses the cancel, the order stays."""
    app, client, tok, conn, pid, card = ready(tmp_path)
    _mine, order_id = _handed_over(app, conn, pid)
    conn.execute("UPDATE order_handoff_source SET taken_at = ?, taken_by = 'worker2' WHERE order_id = ?", (NOW, order_id))
    conn.commit()
    monkeypatch.setattr(mgr, "cancellable", lambda *a: True)
    try:
        mgr.cancel_handoff(conn, order_id, points.get_point(conn, pid), "gamma", NOW)
        raise AssertionError("cancelled a taken client")
    except orders.OrderError as err:
        assert "взяли" in str(err)
    got = orders.get_card(conn, order_id)
    assert got["stage"]["code"] == "from_point"
    assert conn.execute("SELECT taken_by FROM order_handoff_source WHERE order_id = ?", (order_id,)).fetchone()[0] == "worker2"


# ---- 8. a refusal of an order at the point: «Шины остаются на точке — на ОТХ?» (the owner 2026-10-29) -------------------------------

def test_a_refusal_at_the_point_asks_first_and_yes_goes_to_oth_with_the_reason(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    here = _card(conn, pid, webmim="700200", code="at_point")
    r = post(client, "/orders/%d/move" % here, "tok", stage_id=str(_stage(conn, "cancel")), cancel_reason="клиент передумал")
    assert r.status_code == 302 and r.headers["Location"].endswith("/orders/%d#oth-offer" % here)
    assert orders.get_card(conn, here)["stage"]["code"] == "at_point"                       # nothing moved yet
    page = _text(client.get("/orders/%d" % here))
    assert "Шины остаются на точке — перевести на «На ОТХ»?" in page and "клиент передумал" in page
    post(client, "/orders/%d/move" % here, "tok", stage_id=str(_stage(conn, "oth")), oth_answer="yes", cancel_reason="клиент передумал")
    got = orders.get_card(conn, here)
    assert got["stage"]["code"] == "oth" and got["cancel_reason"] == "клиент передумал"
    assert "Шины остаются на точке" not in _text(client.get("/orders/%d" % here))
    assert here in {r["order_id"] for r in order_oth.stock(conn)["stock"]}                  # how the feed lane reads «на ОТХ»: stage oth + items


def test_no_to_the_question_moves_as_before_with_the_reason(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    here = _card(conn, pid, webmim="700200", code="at_point")
    r = client.post("/orders/%d/move" % here, data={"csrf": "tok", "stage_id": str(_stage(conn, "refused")), "cancel_reason": "не выкупил"},
                    headers={"X-Requested-With": "fetch"})                                     # the board's drag
    assert r.get_json() == {"ok": False, "message": "Шины остаются на точке — перевести на «На ОТХ»?", "go": "/orders/%d#oth-offer" % here}
    post(client, "/orders/%d/move" % here, "tok", stage_id=str(_stage(conn, "refused")), oth_answer="no", cancel_reason="не выкупил")
    got = orders.get_card(conn, here)
    assert got["stage"]["code"] == "refused" and got["cancel_reason"] == "не выкупил"


def test_an_order_not_at_the_point_is_cancelled_without_the_question(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)                                    # card: «Перемещение на точку»
    post(client, "/orders/%d/move" % card, "tok", stage_id=str(_stage(conn, "cancel")), cancel_reason="передумал")
    assert orders.get_card(conn, card)["stage"]["code"] == "cancel"
    post(client, "/orders/%d/save" % card, "tok", stage_id=str(_stage(conn, "new")))       # (back to work: a window move is not asked either)
    assert "Шины остаются на точке" not in _text(client.get("/orders/%d" % card))


def test_the_window_asks_too_and_the_reason_typed_is_kept(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    here = _card(conn, pid, webmim="700200", code="at_point")
    r = post(client, "/orders/%d/save" % here, "tok", stage_id=str(_stage(conn, "cancel")), cancel_reason="дорого")
    assert r.headers["Location"].endswith("#oth-offer") and orders.get_card(conn, here)["stage"]["code"] == "at_point"
    assert orders.get_card(conn, here)["cancel_reason"] == "дорого"                          # the window's fields are saved


def test_the_point_after_no_show_is_asked_and_moves_the_order_to_oth(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    here = _card(conn, pid, webmim="700200", code="at_point")
    mine = manager(app, conn, pid)
    page = _text(mine.get("/my-point"))
    assert 'action="/my-point/%d/to_oth"' % here not in page                                 # only after «Клиент не пришёл»
    bad = re.search(r'action="/my-point/%d/no_show".*?name="token" value="([0-9a-f]+)"' % here, page, re.S).group(1)
    r = post(mine, "/my-point/%d/to_oth" % here, "tok", token=bad[::-1])                      # no «не пришёл» yet: refused
    assert orders.get_card(conn, here)["stage"]["code"] == "at_point"
    r = post(mine, "/my-point/%d/no_show" % here, "tok", token=bad)
    assert "oth=%d" % here in r.headers["Location"]
    page = _text(mine.get(r.headers["Location"]))
    assert 'data-oth-offer="%d"' % here in page and "Шины остаются на точке — перевести на «На ОТХ»?" in page
    assert 'data-oth-offer' not in _text(mine.get("/my-point"))                              # «Нет»: the page without the question
    token = re.search(r'action="/my-point/%d/to_oth".*?name="token" value="([0-9a-f]+)"' % here, page, re.S).group(1)
    post(mine, "/my-point/%d/to_oth" % here, "tok", token=token)
    got = orders.get_card(conn, here)
    assert got["stage"]["code"] == "oth" and got["cancel_reason"] == "Клиент не пришёл"
    assert point_log.history(conn, pid)[0]["kind"] == "to_oth"


# ---- review fixes -------------------------------------------------------------------------------------------------------------------

def test_the_sheet_says_refused_for_a_return_given_and_the_point_bell_stays_quiet(tmp_path):
    from tires_sync import order_sheet
    from tires_sync.panel import routes_alerts
    app, client, tok, conn, pid, card = ready(tmp_path)
    oth = _on_oth(conn, pid)
    order_oth.agree(conn, orders.get_card(conn, oth), date(2026, 10, 28), NOW, "owner")
    order_oth.sign_act(conn, orders.get_card(conn, oth), "Бета", NOW, "gamma")
    assert order_sheet.realization(orders.get_card(conn, oth)) == order_sheet.REFUSED
    assert routes_alerts.is_return_ask({"kind": "point", "text": order_oth.SIGNED_ASK})
    mine, order_id = _handed_over(app, conn, pid, login_="gamma3")
    post(mine, "/my-point/handoff/%d/cancel" % order_id, "tok")
    other = manager(app, conn, pid, login_="gamma4")                                         # another user of the same point
    feed = other.get("/my-point/feed").get_json()["items"]
    assert not any("отменила передачу" in i["text"] or "заполните сумму" in i["text"] for i in feed)


def test_the_history_of_a_quiet_point_is_not_lost_behind_the_others(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    other = _other(conn)
    point_log.record(conn, "2026-10-01T10:00:00", "gamma", pid, "issued", card)
    for n in range(point_log.SHOWN * 2):
        point_log.record(conn, NOW, "delta", other, "arrived", None)
    assert [r["kind"] for r in point_log.history(conn, pid)] == ["issued"]
    assert "«Бета»" in __import__("tires_sync.panel.texts", fromlist=["x"]).audit_text(
        {"action": "point_action", "detail": {"point": pid, "name": "Бета", "kind": "issued", "order": card}})


# ---- the tabs of «Рабочее место» and the compact card (the owner 2026-10-29) ---------------------------------------------------------

def _tab_counts(page):
    return {k: int(v) for k, v in re.findall(r'data-tab-count="(\w+)">(\d+)</b>', page)}


def _listed(page):
    block = page[page.index('id="mp-orders-h"'):]
    return set(int(n) for n in re.findall(r'id="order-(\d+)"', block))


def test_the_tabs_count_and_list_the_points_orders(tmp_path):
    from tires_sync.panel import routes_mypoint
    app, client, tok, conn, pid, card = ready(tmp_path)                                    # card: moving
    placed = _card(conn, pid, webmim="700300", code="placed")
    ending = _card(conn, pid, webmim="700400", code="at_point")
    later = _card(conn, pid, webmim="700500", code="at_point")
    oth = _on_oth(conn, pid, webmim="700600")
    _card(conn, _other(conn), webmim="700700", code="at_point")                          # another point's: nowhere
    with app.test_request_context():
        today = routes_mypoint.local_iso()[:10]
    orders.update_card(conn, ending, {"reserve_until": today}, NOW, "o")
    orders.update_card(conn, later, {"reserve_until": "2099-01-01"}, NOW, "o")
    conn.commit()
    mine = manager(app, conn, pid)
    page = _text(mine.get("/my-point"))
    assert _tab_counts(page) == {"all": 4, "moving": 2, "here": 2, "ending": 1, "asked": 0, "return": 1}     # «Едут»: placed and moving
    assert _listed(page) == {card, placed, ending, later}                                    # «Все»: as the page always showed
    for tab, wanted in (("moving", {card, placed}), ("here", {ending, later}), ("ending", {ending}), ("asked", set()), ("return", {oth})):
        shown = _text(mine.get("/my-point?tab=%s" % tab))
        assert _listed(shown) == wanted, tab
        assert 'data-tab="%s" aria-current="page"' % tab in shown, tab                       # the tab is kept in the address
    assert _listed(_text(mine.get("/my-point?tab=nonsense"))) == {card, placed, ending, later}


def test_the_card_shows_the_top_and_the_buttons_and_hides_the_rest_under_more(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    orders.update_card(conn, card, dict(transfer_number="26R-1447431"), NOW, "o")
    bare = _card(conn, pid, webmim="700200", code="at_point")
    page = _text(manager(app, conn, pid).get("/my-point"))
    for order_id in (card, bare):
        article = re.search(r'<article class="mp-card[^"]*" id="order-%d".*?</article>' % order_id, page, re.S).group(0)
        top, more = article.split('<details class="mp-more">')
        assert "№ " in top and "mp-tel" in top and 'href="tel:+79001112233"' in top and "Татьяна" in top
        assert "/my-point/%d/issued" % order_id in top                                          # the buttons: never folded
        assert "<summary>Подробнее</summary>" in more and "Nokian Hakkapeliitta 10" in more and "Nokian Hakkapeliitta 10" not in top
    first = re.search(r'id="order-%d".*?</article>' % card, page, re.S).group(0)
    assert re.search(r"26R-1447431 от \d\d\.\d\d\.\d{4}", first.split("<details")[0])
    assert "Перемещение: ещё нет · заказ от" in re.search(r'id="order-%d".*?</article>' % bare, page, re.S).group(0).split("<details")[0]


def test_a_press_brings_back_to_the_tab_it_came_from(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    page = _text(mine.get("/my-point?tab=moving"))
    token = re.search(r'action="/my-point/%d/arrived".*?name="token" value="([0-9a-f]+)"' % card, page, re.S).group(1)
    r = mine.post("/my-point/%d/arrived" % card, data={"csrf": "tok", "token": token}, headers={"Referer": "http://localhost/my-point?tab=moving"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/my-point?tab=moving")
    assert 'name="tab" value="moving"' in page                                               # the search keeps the tab too
