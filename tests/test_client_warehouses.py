"""The warehouses of a client (the owner 2026-11-10): two switches «Склад 1» / «Склад 2» per client, «Склад 2» off until the owner
switches it on (a new client, and every client without the setting); never both off. A position of a warehouse off is seen nowhere: the
catalog, the search, the lists of the filters, the cart, the checkout, the owner's preview, the photos. One warehouse on: no filter and
no column «Склад», no step of the tour about the warehouses."""
from __future__ import annotations

import json
import re

from panel_helpers import post
from test_client_terms import _sent, accept, as_user, form_of, invite, invite_form, owner_of
from test_shop import NOW, cc_tochki_gid, make_shop, page, tochki_row
from test_shop_cart import checkout, keys
from tires_sync import client_terms as ct, shop_cart, user_prefs, users
from tires_sync.panel import client_catalog as cc, shop_photos, texts


def make_shop_default(tmp_path):
    app, client, conn, sid = make_shop(tmp_path, tochki=[tochki_row("T1", rest=3)])
    conn.execute("DELETE FROM user_pref WHERE login = 'buyer' AND key = ?", (ct.WAREHOUSES_PREF,))      # as a client made before the switches
    conn.execute("DELETE FROM audit WHERE action = 'client_warehouses_changed'")
    conn.commit()
    return app, client, conn, sid


def test_a_client_without_the_setting_sees_only_the_first_warehouse(tmp_path):
    app, client, conn, sid = make_shop_default(tmp_path)
    assert ct.warehouses(conn, "buyer") == (1,) and user_prefs.get(conn, "buyer", ct.WAREHOUSES_PREF) is None
    html = page(client)
    assert "Найдено 3" in html and "Склад 2" not in html and "Склад 1" not in html      # one warehouse: it is not named at all
    assert 'id="f-wh"' not in html and 'name="wh"' not in html and "<th>Склад</th>" not in html and 'class="c-wh"' not in html
    assert 'value="wh"' not in html                                                    # «Колонки» has no tick of it
    assert "Найдено 3" in page(client, "/shop?wh=2")                                   # an old link: not a filter any more
    assert "Найдено 3" in page(client, "/shop?v=tiles") and "Склад" not in page(client, "/shop?v=tiles")
    assert "Склад 1 и Склад 2" not in html                                             # the step of the tour
    owner, _ = owner_of(app)
    preview = page(owner, "/client-terms/c/buyer/preview")
    assert "Найдено 3" in preview and "Склад 2</td>" not in preview and 'id="f-wh"' not in preview


def test_a_junk_setting_reads_as_the_default(tmp_path):
    app, client, conn, sid = make_shop_default(tmp_path)
    for junk in ([], [3], "2", {"2": 1}, [True], None):
        user_prefs.put(conn, "buyer", ct.WAREHOUSES_PREF, junk, NOW)
        assert ct.warehouses(conn, "buyer") == (1,), junk


def test_the_second_warehouse_switched_on_shows_both_and_the_filter(tmp_path):
    app, client, conn, sid = make_shop_default(tmp_path)
    owner, tok = owner_of(app)
    data = form_of(page(owner, "/client-terms/c/buyer"), "/client-terms/c/buyer")
    assert data["wh"] == ["1"] and data["wh_sent"] == ["1"]
    data["wh"] = ["1", "2"]
    assert owner.post("/client-terms/c/buyer", data=dict(_sent(data), csrf=tok)).status_code == 302
    assert ct.warehouses(conn, "buyer") == (1, 2)
    html = page(client)
    assert "Найдено 4" in html and 'id="f-wh"' in html and "<th>Склад</th>" in html and "Склад 2</td>" in html and 'value="wh"' in html
    assert "Найдено 1" in page(client, "/shop?wh=2") and "Найдено 3" in page(client, "/shop?wh=1")
    assert "Склад 1 и Склад 2" in html                                                 # the tour tells about the warehouses again
    journal = [json.loads(r[0]) for r in conn.execute("SELECT detail FROM audit WHERE action = 'client_warehouses_changed'")]
    assert journal == [{"subject": "buyer", "was": [1], "now": [1, 2]}]
    assert texts.audit_text({"action": "client_warehouses_changed", "detail": journal[0]}) == "Склады клиента buyer: Склад 1 → Склад 1, Склад 2"
    assert owner.post("/client-terms/c/buyer", data=dict(_sent(data), csrf=tok)).status_code == 302      # saved again: no new line
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'client_warehouses_changed'").fetchone()[0] == 1
    data["wh"] = ["2"]                                                                 # only the second: the first is hidden
    assert owner.post("/client-terms/c/buyer", data=dict(_sent(data), csrf=tok)).status_code == 302
    html = page(client)
    assert "Найдено 1" in html and 'id="f-wh"' not in html and "<th>Склад</th>" not in html


def test_both_warehouses_cannot_be_switched_off(tmp_path):
    app, client, conn, sid = make_shop_default(tmp_path)
    owner, tok = owner_of(app)
    data = form_of(page(owner, "/client-terms/c/buyer"), "/client-terms/c/buyer")
    data.pop("wh")
    i = data["bk"].index("laufenn")
    data["s_%d" % i] = ["15"]
    response = owner.post("/client-terms/c/buyer", data=dict(_sent(data), csrf=tok))
    assert response.status_code == 400 and "Оставьте хотя бы один склад" in response.get_data(as_text=True)
    assert ct.warehouses(conn, "buyer") == (1,) and ct.rows(conn, "buyer") == {}       # refused whole: the % typed beside is not saved
    assert not conn.execute("SELECT 1 FROM audit WHERE action IN ('client_warehouses_changed', 'client_pct_changed')").fetchone()
    invite_data = invite_form(owner)
    invite_data.pop("c_wh")
    response = owner.post("/users/invite", data=dict(_sent(invite_data), csrf=tok, role="client"))
    assert response.status_code == 400 and "Оставьте хотя бы один склад" in response.get_data(as_text=True)
    assert not conn.execute("SELECT 1 FROM panel_invite").fetchone()
    for bad in ((), (3,), None):
        try:
            ct.set_warehouses(conn, "buyer", bad, NOW, "o")
            raise AssertionError(bad)
        except ct.TermsError:
            pass
    users.add_user(conn, "staff1", "длинный-пароль-1", NOW, role="partner")
    try:
        ct.set_warehouses(conn, "staff1", (1, 2), NOW, "o")
        raise AssertionError("not a client")
    except ct.TermsError:
        pass


def test_a_bad_percent_keeps_the_warehouses_as_they_were(tmp_path):
    app, client, conn, sid = make_shop_default(tmp_path)
    owner, tok = owner_of(app)
    data = form_of(page(owner, "/client-terms/c/buyer"), "/client-terms/c/buyer")
    data["wh"] = ["1", "2"]
    data["s_%d" % data["bk"].index("laufenn")] = ["999"]
    response = owner.post("/client-terms/c/buyer", data=dict(_sent(data), csrf=tok))
    assert response.status_code == 400 and ct.warehouses(conn, "buyer") == (1,)
    again = form_of(response.get_data(as_text=True), "/client-terms/c/buyer")
    assert again["wh"] == ["1", "2"]                                                   # the form comes back as it was ticked


def test_a_new_client_gets_the_first_warehouse_and_the_invitation_may_switch_the_second_on(tmp_path):
    app, buyer, conn, sid = make_shop_default(tmp_path)
    owner, tok = owner_of(app)
    html = page(owner, "/users")
    assert html.index("data-client-warehouses") < html.index('id="invite-brands"')     # above the % of the brands
    data = invite_form(owner)
    assert data["c_wh"] == ["1"]                                                       # by default: «Склад 1» on, «Склад 2» off
    data.update(role=["client"], display_name=["ООО Первый"])
    path = invite(app, owner, tok, **_sent(data))
    assert accept(app, path, "first").status_code == 302
    assert ct.warehouses(conn, "first") == (1,) and "Найдено 3" in page(as_user(app, conn, "first"))
    old = invite(app, owner, tok)                                                      # a form without the switches: the default
    assert accept(app, old, "plain").status_code == 302 and ct.warehouses(conn, "plain") == (1,)
    data["c_wh"] = ["1", "2"]
    path = invite(app, owner, tok, **_sent(data))
    assert "Склад 1, Склад 2 ·" in page(owner, "/users")                               # the list of the invitations says it
    assert accept(app, path, "second").status_code == 302
    assert ct.warehouses(conn, "second") == (1, 2) and "Найдено 4" in page(as_user(app, conn, "second"))
    journal = [json.loads(r[0]) for r in conn.execute("SELECT detail FROM audit WHERE action = 'client_warehouses_changed'")]
    assert journal == [{"subject": "second", "was": [1], "now": [1, 2]}]
    assert ct.from_invite(json.dumps({"rows": {}})) == {"rows": {}, "warehouses": (1,)}     # an invitation made before the switches
    for broken in ({"rows": {}, "warehouses": []}, {"rows": {}, "warehouses": [3]}, {"rows": {}, "warehouses": "2"}):
        try:
            ct.from_invite(json.dumps(broken))
            raise AssertionError(broken)
        except ct.TermsError:
            pass


def test_a_position_of_a_warehouse_off_is_not_put_in_the_cart_nor_ordered_nor_drawn(tmp_path):
    app, client, conn, sid = make_shop(tmp_path, tochki=[tochki_row("T1", rest=3)])     # both on: the tyre of «Склад 2» goes in the cart
    gid = cc_tochki_gid(app, conn)
    key = cc.photo_token(app.secret_key, gid)
    assert key in keys(page(client))
    assert post(client, "/shop/cart/add", "tok", key=key, qty="2").status_code == 302 and shop_cart.cart(conn, "buyer") == {gid: 2}
    shop_photos.drain()                                                                 # the photo is fetched in the background
    assert client.get("/shop/photo/" + key).status_code == 200
    assert "<th>Склад</th>" in page(client, "/shop/cart") and "Склад 2" in page(client, "/shop/cart")
    ct.set_warehouses(conn, "buyer", (1,), NOW, "o")                                    # the owner switches «Склад 2» off
    html = page(client, "/shop/cart")
    assert "Шина больше не продаётся" in html and "Склад" not in html                  # one warehouse: the cart names none
    assert checkout(client).status_code == 400 and not conn.execute("SELECT 1 FROM shop_order").fetchone()
    assert post(client, "/shop/cart/set", "tok", key=key, qty="1").status_code == 302 and shop_cart.cart(conn, "buyer") == {gid: 2}
    assert client.get("/shop/photo/" + key).status_code == 404
    assert key not in keys(page(client)) and "Найдено 3" in page(client)
    assert post(client, "/shop/cart/set", "tok", key=key, drop="1").status_code == 302 and shop_cart.cart(conn, "buyer") == {}
    response = post(client, "/shop/cart/add", "tok", key=key, qty="1")                  # a direct POST with the key remembered
    assert response.status_code == 302 and shop_cart.cart(conn, "buyer") == {}
    assert "Этой шины уже нет в наличии" in page(client)


def test_the_brands_and_sizes_of_a_warehouse_off_are_not_in_the_filters(tmp_path):
    row = dict(tochki_row("T9", rest=3), brand="OnlySecond", marka="OnlySecond", width=315, diametr=22)
    app, client, conn, sid = make_shop(tmp_path, tochki=[row], role=(10, 20))
    from test_shop import set_ours
    set_ours(conn, {"IKON TYRES|winter": (10, 20), "LAUFENN|summer": (10, 20), "S%d:ONLYSECOND|winter" % sid: (10, 20)})
    html = page(client)
    assert "OnlySecond" in html and 'value="315"' in html
    ct.set_warehouses(conn, "buyer", (1,), NOW, "o")
    html = page(client)
    assert "onlysecond" not in html.lower() and 'value="315"' not in html and 'value="22"' not in html
    assert "Найдено 0" in page(client, "/shop?q=onlysecond") and "Найдено 3" in page(client, "/shop?b=onlysecond")   # not a brand to filter by


def test_the_columns_chosen_keep_the_warehouse_column_while_one_warehouse_is_on(tmp_path):
    app, client, conn, sid = make_shop_default(tmp_path)
    assert post(client, "/shop/columns", "tok", show=["season", "stock", "price"]).status_code == 302
    assert "wh" not in cc.hidden_columns(conn, "buyer")                                 # the form had no tick of «Склад»: not hidden by it
    ct.set_warehouses(conn, "buyer", (1, 2), NOW, "o")
    assert "<th>Склад</th>" in page(client)
    assert post(client, "/shop/columns", "tok", show=["season"]).status_code == 302 and "wh" in cc.hidden_columns(conn, "buyer")
    ct.set_warehouses(conn, "buyer", (1,), NOW, "o")
    assert post(client, "/shop/columns", "tok", show=["season", "stock"]).status_code == 302
    assert "wh" in cc.hidden_columns(conn, "buyer")                                     # hidden by the client before: stays hidden


def test_the_new_view_draws_no_warehouse_with_one_on(tmp_path):
    from test_shop_new_view import new_view
    app, client, conn, sid = make_shop_default(tmp_path)
    new_view(conn)
    for url in ("/shop", "/shop?v=tiles", "/shop?wh=2"):
        html = page(client, url)
        assert "найдено 3" in html.lower() and "Склад" not in html and 'id="f-wh"' not in html and 'class="c-wh"' not in html, url
    ct.set_warehouses(conn, "buyer", (1, 2), NOW, "o")
    html = page(client)
    assert "найдено 4" in html.lower() and 'id="f-wh"' in html and "<th>Склад</th>" in html and "Склад 2" in page(client, "/shop?v=tiles")
