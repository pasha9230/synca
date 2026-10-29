"""The settings pages pile their changes up in the one working draft and apply them at once (the owner 2026-10-29): no jump to another page,
the bar «Не применено: N» on top says what the feed will get («войдёт, выйдет, цены изменятся у») and «Применить» stays on the page. The draft,
its versions and every check of /draft/N (rev, the removal guard, the percent limit) are the same ones."""
from __future__ import annotations

import re

from panel_helpers import post
from test_panel_apply_publish import setup
from tires_sync import staging, store
from tires_sync.panel import service
from tires_sync.settings_store import active_settings, confirm_draft, create_draft, get_active

T = "2026-10-07T10:00:00"


def bar_rev(html):
    return re.search(r'data-settings-bar.*?name="rev" value="([0-9a-f]+)"', html, re.S).group(1)


def published(conn):
    return {g: p for g, p in conn.execute("SELECT goods_id, price FROM published_state WHERE account_id = 1")}


def baseline(client, tok, conn):
    """A first publication: what the feed has before the changes."""
    staging.set_override(conn, "6", "off", T)
    post(client, "/draft/%d/confirm" % staging.summary(conn)["draft_id"], tok)
    assert len(published(conn)) == 5


def test_several_changes_of_several_settings_pages_pile_up_in_one_draft_without_leaving_the_page(tmp_path):
    app, client, tok, conn, _ = setup(tmp_path)
    r = post(client, "/settings/draft", tok, min_stock="5")
    assert r.status_code == 302 and r.headers["Location"].endswith("/settings")
    r = post(client, "/connections/suppliers/1/excluded-sizes", tok, diameter="16")   # «Исключаемые радиусы»
    assert r.headers["Location"].endswith("/settings/exclusions#excluded-sizes-1")
    r = post(client, "/settings/pricing", tok, scope="brand", supplier="1", brand="IKON TYRES", winter_small="-1", winter_large="-1")
    assert r.headers["Location"].endswith("/settings/brands#pricing-1")
    staged = staging.summary(conn)
    assert staged["count"] == 3 and staged["changes"]["min_stock"] == 5 and staged["changes"]["excluded_sizes"]
    assert conn.execute("SELECT COUNT(*) FROM draft WHERE status = 'open'").fetchone()[0] == 1
    for page in ("/settings", "/settings/brands", "/settings/exclusions", "/settings/brands/card?supplier=1&brand=IKON%20TYRES", "/economics/terms"):
        html = client.get(page).get_data(as_text=True)
        assert "data-settings-bar" in html and "Не применено: 3 правки" in html and ">Применить</button>" in html, page
        assert 'action="/catalog/cancel"' in html and 'href="/catalog/review">Подробнее' in html
    assert "Не применено" not in client.get("/orders").get_data(as_text=True)      # other pages keep «Посмотреть и применить»
    assert active_settings(conn).min_stock != 5                                         # nothing applied yet


def test_a_change_of_the_form_keeps_the_changes_already_waiting_and_the_form_shows_them(tmp_path):
    app, client, tok, conn, _ = setup(tmp_path)
    post(client, "/connections/suppliers/1/excluded-sizes", tok, diameter="16")   # «Исключаемые радиусы»
    post(client, "/settings/draft", tok, pack4_form="1", p4_add="1", p4_brand="IKON TYRES", p4_season="*", p4_d="*")
    post(client, "/settings/draft", tok, pack4_form="1", p4_add="1", p4_brand="*", p4_season="*", p4_d="16")      # a second rule: the first stays
    post(client, "/settings/draft", tok, min_stock="6")
    post(client, "/settings/pricing", tok, scope="brand", supplier="1", brand="IKON TYRES", winter_small="-1", winter_large="-1")
    changes = staging.summary(conn)["changes"]
    assert changes["excluded_sizes"] and changes["min_stock"] == 6
    assert changes["pack4_rules"] == ["*|*|16", "IKON TYRES|*|*"] and changes["pack4"] is True
    assert changes["pricing"]["IKON TYRES|winter"] == {"pct_small": -1.0, "pct_large": -1.0}
    html = client.get("/settings").get_data(as_text=True)
    assert 'name="min_stock" value="6"' in html and "IKON TYRES · все сезоны · все радиусы" in html
    post(client, "/settings/draft", tok, min_stock=str(get_active(conn)[1]["min_stock"]))   # back to the active value: it leaves the draft
    assert "min_stock" not in staging.summary(conn)["changes"] and staging.summary(conn)["changes"]["excluded_sizes"]


def test_the_line_of_the_bar_is_what_the_application_does_to_the_feed_and_apply_stays_on_the_page(tmp_path):
    app, client, tok, conn, folder = setup(tmp_path)
    baseline(client, tok, conn)
    before = published(conn)
    staging.set_override(conn, "6", None, T)                                                                             # 6 comes back
    staging.set_override(conn, "1", "off", T)                                                                            # 1 leaves
    post(client, "/settings/pricing", tok, scope="brand", supplier="1", brand="IKON TYRES", winter_small="-1", winter_large="-1")
    pending = client.get("/settings/pending", headers={"X-Requested-With": "fetch"}).get_json()
    assert pending["open"] and pending["line"] == "В фиде: войдёт 1, выйдет 1, цены изменятся у 4."
    assert not pending["guard"]["over"] and not pending["strong"]
    page = client.get("/settings/brands").get_data(as_text=True)
    assert bar_rev(page) == pending["rev"]
    r = post(client, "/draft/%d/confirm" % pending["draft"], tok, rev=pending["rev"], next="/settings/brands")
    assert r.status_code == 302 and r.headers["Location"].endswith("/settings/brands")
    after = published(conn)
    assert len(set(after) - set(before)) == 1 and len(set(before) - set(after)) == 1
    assert len([g for g in set(before) & set(after) if before[g] != after[g]]) == 4
    shown = client.get("/settings/brands").get_data(as_text=True)
    assert "Настройки применены." in shown and "Фид обновлён для Авито" in shown and 'id="pendbar" hidden data-settings-bar' in shown
    assert staging.summary(conn)["draft_id"] is None


def test_apply_from_the_bar_keeps_every_check_of_the_confirmation(tmp_path):
    app, client, tok, conn, _ = setup(tmp_path, guard_min=0)
    confirm_draft(conn, create_draft(conn, {"guard_fraction": 0.0, "guard_min": 1}, T), T, "owner")
    baseline(client, tok, conn)
    for gid in ("1", "2", "3"):
        staging.set_override(conn, gid, "off", T)
    pending = client.get("/settings/pending").get_json()
    assert pending["guard"]["over"] and pending["guard"]["gone"] == 3
    page = client.get("/settings/exclusions").get_data(as_text=True)
    assert 'data-check-understand' in page and 'name="understand"' in page
    r = post(client, "/draft/%d/confirm" % pending["draft"], tok, rev=pending["rev"], next="/settings/exclusions")
    assert r.headers["Location"].endswith("/draft/%d" % pending["draft"])          # not applied: the page of the draft asks
    assert len(published(conn)) == 5
    old = bar_rev(page)
    staging.set_override(conn, "4", "off", T)                                       # changed meanwhile (another tab, a partner)
    r = post(client, "/draft/%d/confirm" % pending["draft"], tok, rev=old, understand="on", next="/settings/exclusions")
    assert r.headers["Location"].endswith("/draft/%d" % pending["draft"]) and len(published(conn)) == 5
    fresh = client.get("/settings/pending").get_json()
    r = post(client, "/draft/%d/confirm" % fresh["draft"], tok, rev=fresh["rev"], understand="on", agreed_gone="3", next="/settings/exclusions")
    assert r.headers["Location"].endswith("/draft/%d" % fresh["draft"]) and len(published(conn)) == 5     # the tick was for 3, now 4 go
    r = post(client, "/draft/%d/confirm" % fresh["draft"], tok, rev=fresh["rev"], understand="on", agreed_gone=str(fresh["guard"]["gone"]),
             next="/settings/exclusions")
    assert r.headers["Location"].endswith("/settings/exclusions") and len(published(conn)) == 1
    assert client.get("/draft/%d" % pending["draft"]).status_code in (200, 404)


def test_cancel_of_the_bar_drops_the_whole_draft_and_stays_on_the_page(tmp_path):
    app, client, tok, conn, _ = setup(tmp_path)
    post(client, "/settings/draft", tok, min_stock="5")
    post(client, "/connections/suppliers/1/excluded-sizes", tok, diameter="16")   # «Исключаемые радиусы»
    r = post(client, "/catalog/cancel", tok, next="/settings")
    assert r.headers["Location"].endswith("/settings") and staging.summary(conn)["draft_id"] is None
    assert 'id="pendbar" hidden data-settings-bar' in client.get("/settings").get_data(as_text=True)


def test_the_pending_count_of_a_point_or_a_viewer_is_closed(tmp_path):
    from tires_sync import users
    app, client, tok, conn, _ = setup(tmp_path)
    users.HASH_METHOD = "pbkdf2:sha256:1000"
    users.add_user(conn, "viewer1", "длинный-пароль-1", T, role="viewer")
    c = app.test_client()
    with c.session_transaction() as session:
        session.update(user="viewer1", epoch=store.get_session_epoch(conn, "viewer1"), csrf="tok")
    assert c.get("/settings/pending").status_code == 403


def test_the_line_counts_come_from_the_same_review_as_the_page_of_the_draft(tmp_path):
    app, client, tok, conn, _ = setup(tmp_path)
    baseline(client, tok, conn)
    staging.set_override(conn, "1", "off", T)
    pending = client.get("/settings/pending").get_json()
    draft = client.get("/draft/%d" % pending["draft"]).get_data(as_text=True)
    assert 'name="rev" value="%s"' % pending["rev"] in draft
    assert service.pending_line({"preview": type("P", (), {"appear": [1, 2], "disappear": [], "price_changes": [1]})()}) == \
        "В фиде: войдёт 2, выйдет 0, цены изменятся у 1."


def test_a_second_tab_of_the_form_general_does_not_silently_undo_a_change_waiting_in_the_draft(tmp_path):
    """Two tabs (or the owner and the partner) on «Общие»: the form sends every field, the older one must not put back what the other changed
    meanwhile (the review 2026-10-30): it is refused, nothing is lost."""
    app, client, tok, conn, _ = setup(tmp_path)
    old_tab = client.get("/settings").get_data(as_text=True)
    rev = re.search(r'name="draft_rev" value="([0-9a-f]*)"', old_tab).group(1)
    active = get_active(conn)[1]
    post(client, "/settings/draft", tok, min_stock="3", draft_rev=rev)              # the other tab
    assert staging.summary(conn)["changes"] == {"min_stock": 3}
    r = post(client, "/settings/draft", tok, min_stock=str(active["min_stock"]), portion_limit="77", draft_rev=rev)   # the old tab
    assert r.headers["Location"].endswith("/settings")
    assert staging.summary(conn)["changes"] == {"min_stock": 3}
    assert "изменился" in client.get("/settings").get_data(as_text=True)
    fresh = re.search(r'name="draft_rev" value="([0-9a-f]*)"', client.get("/settings").get_data(as_text=True)).group(1)
    post(client, "/settings/draft", tok, min_stock="3", portion_limit="77", draft_rev=fresh)
    assert staging.summary(conn)["changes"] == {"min_stock": 3, "portion_limit": 77}


def test_apply_from_the_bar_asks_the_strong_confirmation_of_a_percent_beyond_the_limit(tmp_path):
    app, client, tok, conn, _ = setup(tmp_path)
    post(client, "/settings/pricing", tok, scope="brand", supplier="1", brand="IKON TYRES", winter_small="30", winter_large="30")
    pending = client.get("/settings/pending").get_json()
    assert pending["strong"] and 'data-check-strong' in client.get("/settings/brands").get_data(as_text=True)
    r = post(client, "/draft/%d/confirm" % pending["draft"], tok, rev=pending["rev"], next="/settings/brands")
    assert r.headers["Location"].endswith("/draft/%d" % pending["draft"]) and staging.summary(conn)["draft_id"] == pending["draft"]
    r = post(client, "/draft/%d/confirm" % pending["draft"], tok, rev=pending["rev"], strong="on", next="/settings/brands")
    assert r.headers["Location"].endswith("/settings/brands") and staging.summary(conn)["draft_id"] is None


def test_the_bar_counts_one_change_of_the_pack4_rules_once(tmp_path):
    app, client, tok, conn, _ = setup(tmp_path)
    post(client, "/settings/draft", tok, pack4_form="1", p4_add="1", p4_brand="*", p4_season="*", p4_d="16")
    assert staging.summary(conn)["count"] == 1


def test_a_pack4_rule_added_and_taken_back_over_the_old_bare_switch_leaves_nothing_in_the_draft(tmp_path):
    app, client, tok, conn, _ = setup(tmp_path)
    confirm_draft(conn, create_draft(conn, {"pack4": True}, T), T, "owner")          # the old form: the switch only, no rules
    assert "pack4_rules" not in get_active(conn)[1]
    post(client, "/settings/draft", tok, pack4_form="1", p4_add="1", p4_brand="*", p4_season="*", p4_d="16")
    assert staging.summary(conn)["count"] == 1
    post(client, "/settings/draft", tok, pack4_form="1", p4_del="*|*|16")
    assert staging.summary(conn)["draft_id"] is None
