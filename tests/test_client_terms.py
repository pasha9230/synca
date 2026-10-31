"""The conditions of the clients in the owner's panel and the client's invitation (the owner 2026-10-31): a client's own % of a brand
(up to R17 / from R18) with our % now beside the field, kept as a shift from ours — so it follows ours (ours 10 → 13: the client's
12 → 15); several of ours (seasons, warehouses): the field is the shift; none of ours: the client's own %, needed. −50…300 or nothing is
saved; «Применить» of our % that would take a client's out of it stops with the list; the journal says «было → стало»; only the owner
changes them; the preview is the client's own catalog and is not the client's to open."""
from __future__ import annotations

import json
import re

import pytest

from panel_helpers import login, post
from test_shop import make_shop, page, prices, set_ours, tochki_row
from tires_sync import client_terms as ct, store, users

NOW = "2026-10-29T12:00:00"
PASSWORD = "длинный-пароль-1"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def link_alive(conn, path: str) -> bool:
    """The invitation of the link is not used (the app's clock is not the test's: no expiry asked here)."""
    return conn.execute("SELECT used_at FROM panel_invite WHERE token_hash = ?", (users._token_hash(path.rsplit("/", 1)[1]),)).fetchone() == (None,)


def owner_of(app):
    owner = app.test_client()
    return owner, login(owner)


def as_user(app, conn, login_):
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = login_
        session["epoch"] = store.get_session_epoch(conn, login_)
        session["csrf"] = "tok"
    return client


def form_of(html: str, action: str) -> dict:
    """The fields a browser sends from the form `action` as it is drawn (the brand rows: key, tick, %s)."""
    part = html[html.index('action="%s"' % action):]
    part = part[:part.index("</form>")]
    data = {}
    for tag in re.findall(r"<input[^>]*>", part):
        name = re.search(r'name="([^"]+)"', tag)
        if not name:
            continue
        kind = (re.search(r'type="([^"]+)"', tag) or [None, "text"])[1]
        value = (re.search(r'value="([^"]*)"', tag) or [None, ""])[1]
        if kind in ("checkbox", "radio") and "checked" not in tag:
            continue
        data.setdefault(name.group(1), []).append(value)
    return data


def row_index(data: dict, key: str, prefix: str = "") -> int:
    return data[prefix + "bk"].index(key)


def fill(data: dict, key: str, small=None, large=None, prefix: str = "") -> dict:
    i = row_index(data, key, prefix)
    if small is not None:
        data["%ss_%d" % (prefix, i)] = [small]
    if large is not None:
        data["%sl_%d" % (prefix, i)] = [large]
    return data


def send(client, url, tok, data: dict):
    return client.post(url, data=dict(data, csrf=tok))


def profile_form(owner, login_="buyer"):
    return form_of(page(owner, "/client-terms/c/%s" % login_), "/client-terms/c/%s" % login_)


# --- a client's own % --------------------------------------------------------------------------------------------------------------

def test_the_clients_own_percent_beside_ours_is_kept_as_a_shift_and_follows_ours(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    users.add_user(conn, "buyer2", PASSWORD, NOW, role="client")
    b2 = as_user(app, conn, "buyer2")
    assert sorted(prices(page(buyer))) == sorted(prices(page(b2))) == [3670, 13580, 65190]   # ours: 3333 and 12345 × 1.10, 54321 × 1.20
    owner, tok = owner_of(app)
    html = page(owner, "/client-terms/c/buyer2")
    assert "зима: 10 / 20 %" in html and "лето: 10 / 20 %" in html and "свой %" in html     # our % beside the fields
    data = fill(form_of(html, "/client-terms/c/buyer2"), "laufenn", "12", "18")
    assert send(owner, "/client-terms/c/buyer2", tok, data).status_code == 302
    assert ct.rows(conn, "buyer2") == {"laufenn": (2.0, False, -2.0, False)}                  # kept as the shift from ours
    assert sorted(prices(page(b2))) == [3740, 13580, 65190] and sorted(prices(page(buyer))) == [3670, 13580, 65190]
    assert 'value="12"' in page(owner, "/client-terms/c/buyer2")                                # shown as the client's %
    set_ours(conn, {"IKON TYRES|winter": (10, 20), "LAUFENN|summer": (13, 20)})              # our % moved: the client's moves with it
    assert 3840 in prices(page(b2)) and 'value="15"' in page(owner, "/client-terms/c/buyer2")   # 3333 × 1.15
    data = fill(profile_form(owner, "buyer2"), "laufenn", "", "")                             # emptied: as ours
    assert send(owner, "/client-terms/c/buyer2", tok, data).status_code == 302 and ct.rows(conn, "buyer2") == {}


def test_several_of_ours_make_the_field_a_shift_with_every_clients_percent_beside(tmp_path):
    app, buyer, conn, sid = make_shop(tmp_path, tochki=[tochki_row("T1")], role=(10, 20))
    set_ours(conn, {"IKON TYRES|winter": (10, 20), "IKON TYRES|summer": (12, 22), "S%d:IKON TYRES|winter" % sid: (11, 21), "LAUFENN|summer": (10, 20)})
    owner, tok = owner_of(app)
    html = page(owner, "/client-terms/c/buyer")
    assert "на сколько пунктов выше (+) или ниже (−) нашего" in html and "Склад 2, зима: 11 / 21 %" in html
    data = fill(form_of(html, "/client-terms/c/buyer"), "ikon tyres", "+2", "−1")
    assert send(owner, "/client-terms/c/buyer", tok, data).status_code == 302
    assert ct.rows(conn, "buyer") == {"ikon tyres": (2.0, False, -1.0, False)}
    html = page(owner, "/client-terms/c/buyer")
    assert 'value="+2"' in html and 'value="−1"' in html and "12 %" in html and "14 %" in html and "13 %" in html   # 10+2, 12+2, 11+2
    assert sorted(prices(page(buyer))) == [3670, 7920, 13830, 64650]                   # 12345 × 1.12; 7001 × 1.13; 54321 × 1.19


def test_a_brand_we_have_no_percent_of_needs_the_clients_own(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    set_ours(conn, {"IKON TYRES|winter": (10, 20)})
    owner, tok = owner_of(app)
    html = page(owner, "/client-terms/c/buyer")
    assert "нашего % нет" in html and "без него бренд скрыт" in html and 3670 not in prices(page(buyer))
    data = fill(form_of(html, "/client-terms/c/buyer"), "laufenn", "-10", "")
    assert send(owner, "/client-terms/c/buyer", tok, data).status_code == 302
    assert ct.rows(conn, "buyer") == {"laufenn": (-10.0, True, None, False)}
    assert 3000 in prices(page(buyer))                                                        # 3333 × 0.90: below the purchase, allowed
    set_ours(conn, {"IKON TYRES|winter": (10, 20), "LAUFENN|summer": (15, 20)})              # ours came later: the client's stays its own
    html = page(owner, "/client-terms/c/buyer")
    assert 3000 in prices(page(buyer)) and "свой -10 % (задан, когда нашего не было)" in html
    set_ours(conn, {"IKON TYRES|winter": (10, 20), "LAUFENN|summer": (15, 20), "LAUFENN|winter": (9, 20)})   # several of ours now
    data = profile_form(owner)                                                         # the review: saved untouched (another brand
    fill(data, "ikon tyres", "+1")                                                     # changed), the own % stays, never read as a shift
    assert send(owner, "/client-terms/c/buyer", tok, data).status_code == 302
    assert ct.rows(conn, "buyer")["laufenn"] == (-10.0, True, None, False) and 3000 in prices(page(buyer))
    data = fill(profile_form(owner), "laufenn", "+2")                                  # changed: read as the field reads now (a shift)
    assert send(owner, "/client-terms/c/buyer", tok, data).status_code == 302
    assert ct.rows(conn, "buyer")["laufenn"] == (2.0, False, None, False)


@pytest.mark.parametrize("value", ["-50.01", "300.01", "abc", "1.234", "1e2", "10%", "nan", "inf", "١٢", "--1"])
def test_a_wrong_percent_is_refused_under_its_field_and_nothing_is_written(tmp_path, value):
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    owner, tok = owner_of(app)
    data = fill(profile_form(owner), "laufenn", value)
    response = send(owner, "/client-terms/c/buyer", tok, data)
    html = response.get_data(as_text=True)
    assert response.status_code == 400 and 'data-field-error="s_%d"' % row_index(data, "laufenn") in html
    assert ct.rows(conn, "buyer") == {} and not conn.execute("SELECT 1 FROM audit WHERE action = 'client_pct_changed'").fetchone()


@pytest.mark.parametrize("value, ok", [("-50", True), ("300", True), ("0", True), ("−12,5", True), ("-50.5", False), ("301", False)])
def test_the_bounds_of_the_clients_percent(tmp_path, value, ok):
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    owner, tok = owner_of(app)
    assert send(owner, "/client-terms/c/buyer", tok, fill(profile_form(owner), "laufenn", value)).status_code == (302 if ok else 400)


def test_a_page_drawn_before_our_percent_changed_is_refused(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    owner, tok = owner_of(app)
    data = fill(profile_form(owner), "laufenn", "12")
    set_ours(conn, {"IKON TYRES|winter": (10, 20), "LAUFENN|summer": (13, 20)})              # 12 meant +2 over 10, not −1 over 13
    response = send(owner, "/client-terms/c/buyer", tok, data)
    assert response.status_code == 400 and "изменился" in response.get_data(as_text=True) and ct.rows(conn, "buyer") == {}


def test_the_journal_says_what_was_and_what_is(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    owner, tok = owner_of(app)
    send(owner, "/client-terms/c/buyer", tok, fill(profile_form(owner), "laufenn", "12"))
    send(owner, "/client-terms/c/buyer", tok, fill(profile_form(owner), "laufenn", "9", "25"))
    send(owner, "/client-terms/c/buyer", tok, profile_form(owner))                           # nothing changed: no line
    from tires_sync.panel import texts
    rows = [(r[0], json.loads(r[1])) for r in conn.execute("SELECT author, detail FROM audit WHERE action = 'client_pct_changed' ORDER BY id")]
    assert len(rows) == 2 and rows[1][0] == app.config["AUTHOR"]
    assert texts.audit_text({"action": "client_pct_changed", "detail": rows[1][1]}) == \
        "Свой % клиента buyer: LAUFENN: до R17 +2 п. к нашему, от R18 как у нас → до R17 −1 п. к нашему, от R18 +5 п. к нашему"
    everything = " ".join(r[0] for r in conn.execute("SELECT detail FROM audit"))
    assert PASSWORD not in everything and "pbkdf2" not in everything


def test_a_brand_key_is_kept_bare_and_only_a_client_gets_its_own(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path)
    ct.save_rows(conn, "buyer", {"S2:Ikon Tyres": (4, False, None, False), "": (1, False, None, False)}, NOW, "owner")
    assert conn.execute("SELECT brand_key, small FROM client_brand_pct").fetchall() == [("ikon tyres", 4.0)]
    users.add_user(conn, "olya", PASSWORD, NOW, role="chats")
    for login_ in ("olya", "owner2"):
        with pytest.raises(ct.TermsError):
            ct.save_rows(conn, login_, {"laufenn": (1, False, None, False)}, NOW, "owner")
    users.remove_user(conn, "buyer", NOW)
    assert not conn.execute("SELECT 1 FROM client_brand_pct").fetchone()


# --- «Применить» of our % -----------------------------------------------------------------------------------------------------------

def _draft(conn, pricing):
    from tires_sync import settings_store
    return settings_store.create_draft(conn, {"pricing": {k: {"pct_small": v[0], "pct_large": v[1]} for k, v in pricing.items()}}, NOW)


def test_our_percent_applied_moves_the_clients_and_the_draft_says_how(tmp_path):
    from tires_sync import settings_store
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 8))
    ct.save_rows(conn, "buyer", {"laufenn": (2, False, 2, False)}, NOW, "owner")              # the owner's Gislaved: ours 10/8, the client's 12/10
    draft = _draft(conn, {"IKON TYRES|winter": (10, 8), "LAUFENN|summer": (13, 11)})
    owner, tok = owner_of(app)
    html = page(owner, "/draft/%d" % draft)
    assert "Клиентам: 1" in html and "buyer, LAUFENN (лето) до R17: 12 % → 15 %" in html and "от R18: 10 % → 13 %" in html
    settings_store.confirm_draft(conn, draft, NOW, "owner", strong=True)
    assert 3840 in prices(page(buyer))                                                        # 3333 × 1.15
    detail = json.loads(conn.execute("SELECT detail FROM audit WHERE action = 'client_pct_followed'").fetchone()[0])
    from tires_sync.panel import texts
    assert "buyer, LAUFENN (лето) до R17: 12 % → 15 %" in texts.audit_text({"action": "client_pct_followed", "detail": detail})


def test_our_own_percent_out_of_bounds_stops_the_apply_for_the_clients_without_their_own(tmp_path):
    from tires_sync import settings_store
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    draft = _draft(conn, {"IKON TYRES|winter": (10, 20), "LAUFENN|summer": (-60, 20)})
    with pytest.raises(ct.FollowError, match="клиенты без своего %"):
        settings_store.confirm_draft(conn, draft, NOW, "owner", strong=True)


def test_our_percent_that_takes_a_clients_out_of_bounds_stops_the_apply(tmp_path):
    from tires_sync import settings_store
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    ct.save_rows(conn, "buyer", {"laufenn": (-55, False, None, False)}, NOW, "owner")         # 10 − 55 = −45: allowed
    version = settings_store.get_active(conn)[0]
    draft = _draft(conn, {"IKON TYRES|winter": (10, 20), "LAUFENN|summer": (4, 20)})          # 4 − 55 = −51: out
    owner, tok = owner_of(app)
    assert "Применить нельзя" in page(owner, "/draft/%d" % draft)
    with pytest.raises(ct.FollowError):
        settings_store.confirm_draft(conn, draft, NOW, "owner", strong=True)
    assert settings_store.get_active(conn)[0] == version and ct.rows(conn, "buyer") == {"laufenn": (-55.0, False, None, False)}
    with pytest.raises(ct.FollowError):                                                       # a rollback to such a version: the same
        conn.execute("INSERT INTO settings_version (created, author, note, json) SELECT created, author, note, "
                     "replace(json, '\"pct_small\": 10', '\"pct_small\": 4') FROM settings_version WHERE id = ?", (version,))
        conn.commit()
        settings_store.rollback(conn, conn.execute("SELECT MAX(id) FROM settings_version").fetchone()[0], NOW, "owner")
    assert settings_store.get_active(conn)[0] == version


# --- rights ----------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["partner", "chats", "viewer", "point", "client"])
def test_nobody_but_the_owner_changes_the_conditions(tmp_path, role):
    from tires_sync import order_points as points
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"}) if role == "point" else None
    if role != "client":
        users.add_user(conn, "who", PASSWORD, NOW, role=role, point_id=pid)
    other = as_user(app, conn, "buyer" if role == "client" else "who")
    for url, data in (("/client-terms/c/buyer", {"bk": "laufenn", "s_0": "5"}), ("/users/invite", {"role": "client", "c_bk": "laufenn", "c_s_0": "5"})):
        assert other.post(url, data=dict(data, csrf="tok")).status_code == 403, url
    assert ct.rows(conn, "buyer") == {} and not conn.execute("SELECT 1 FROM panel_invite").fetchone()
    reads = [other.get(u).status_code for u in ("/client-terms", "/client-terms/c/buyer", "/client-terms/c/buyer/preview")]
    assert reads == ([200, 200, 200] if role == "partner" else [403, 403, 403])          # the partner looks (section «users»: смотреть)
    if role == "partner":
        html = other.get("/client-terms/c/buyer").get_data(as_text=True)
        assert 'action="/client-terms/c/buyer"' not in html and "ro-form" in html


def test_the_pages_of_a_login_that_is_not_a_client_are_404(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path)
    users.add_user(conn, "olya", PASSWORD, NOW, role="chats")
    owner, tok = owner_of(app)
    for url in ("/client-terms/c/olya", "/client-terms/c/olya/preview", "/client-terms/c/nobody", "/client-terms/c/nobody/preview"):
        assert owner.get(url).status_code == 404, url
    assert owner.post("/client-terms/c/olya", data={"csrf": tok, "bk": "laufenn", "s_0": "1"}).status_code == 404
    assert owner.post("/client-terms/role", data={"csrf": tok, "small": "1"}).status_code in (404, 405)   # no general % of a role any more


# --- the preview -----------------------------------------------------------------------------------------------------------------------

def test_the_preview_is_the_client_s_own_catalog_and_not_the_client_s_to_open(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path, tochki=[tochki_row("T1")], role=(10, 20))
    users.add_user(conn, "buyer2", PASSWORD, NOW, role="client")
    ct.save_rows(conn, "buyer2", {"ikon tyres": (20, False, 20, False)}, NOW, "owner")
    owner, tok = owner_of(app)
    preview = owner.get("/client-terms/c/buyer/preview")
    assert preview.status_code == 200 and "no-store" in preview.headers["Cache-Control"]
    html = preview.get_data(as_text=True)
    assert sorted(prices(html)) == sorted(prices(page(buyer)))
    assert sorted(prices(page(owner, "/client-terms/c/buyer2/preview"))) == sorted(prices(page(as_user(app, conn, "buyer2"))))
    assert sorted(prices(page(owner, "/client-terms/c/buyer2/preview"))) != sorted(prices(html))
    assert "Склад 2" in html and "/shop/photo/" not in html
    for secret in ("12 345", "12345", "SECRETCODE", "77001", "cdn.supplier-one.test", "img.4tochki.test"):
        assert secret not in html[html.index("Найдено"):], secret
    filtered = page(owner, "/client-terms/c/buyer/preview?size=245/45R18")                # the filters and their links stay on the preview
    assert prices(filtered) == [65190] and 'action="/client-terms/c/buyer/preview"' in filtered and 'action="/shop"' not in filtered
    for url in ("/client-terms/c/buyer/preview", "/client-terms/c/buyer2/preview", "/client-terms", "/client-terms/c/buyer"):
        assert buyer.get(url).status_code == 403, url


# --- the invitation of a client --------------------------------------------------------------------------------------------------------

def invite(app, owner, tok, **data):
    response = owner.post("/users/invite", data=dict({"csrf": tok, "role": "client"}, **data))
    assert response.status_code == 302, response.get_data(as_text=True)[:300]
    html = owner.get("/users").get_data(as_text=True)
    found = re.search(r'value="(http://localhost/invite/[^"]+)"', html)
    return found.group(1).replace("http://localhost", "") if found else None


def invite_form(owner):
    return form_of(page(owner, "/users"), "/users/invite")


def accept(app, path, login_, password=PASSWORD):
    guest = app.test_client()
    opened = guest.get(path)
    if opened.status_code != 200:
        return opened
    tok = re.search(r'name="csrf" value="([^"]+)"', opened.get_data(as_text=True)).group(1)
    return guest.post(path, data={"csrf": tok, "login": login_, "password": password, "password2": password})


def _sent(data):
    return {k: v for k, v in data.items() if k != "csrf"}


def test_an_invitation_with_the_clients_own_percent_puts_it_in_the_profile(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path, tochki=[tochki_row("T1")], role=(10, 20))
    owner, tok = owner_of(app)
    data = invite_form(owner)
    assert {"laufenn", "ikon tyres"} <= set(data["c_bk"]) and len(data["c_bk"]) == len(set(data["c_bk"]))   # every brand, once
    assert "лето: 10 / 20 %" in page(owner, "/users")                                     # our % beside the fields
    fill(data, "laufenn", "15", None, "c_")
    data.update(role=["client"], display_name=["ООО Шины"])
    path = invite(app, owner, tok, **_sent(data))
    assert path and "свой %: LAUFENN" in page(owner, "/users")
    assert accept(app, path, "shina").status_code == 302
    assert users.get_role(conn, "shina")["display_name"] == "ООО Шины" and ct.rows(conn, "shina") == {"laufenn": (5.0, False, None, False)}
    assert 3840 in prices(page(as_user(app, conn, "shina")))                              # 3333 × 1.15
    assert accept(app, path, "shina2").status_code == 404                               # one time
    plain = invite(app, owner, tok)
    assert accept(app, plain, "shina3").status_code == 302 and ct.rows(conn, "shina3") == {}
    assert sorted(prices(page(as_user(app, conn, "shina3")))) == sorted(prices(page(buyer)))
    journal = [json.loads(r[0]) for r in conn.execute("SELECT detail FROM audit WHERE action = 'client_pct_changed'")]
    assert [j["subject"] for j in journal] == ["shina"]


def test_a_refused_invitation_comes_back_with_what_was_typed(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    owner, tok = owner_of(app)
    data = fill(invite_form(owner), "laufenn", "-51", None, "c_")
    data.update(role=["client"], display_name=["ООО Шины"])
    response = send(owner, "/users/invite", tok, _sent(data))
    html = response.get_data(as_text=True)
    i = row_index(data, "laufenn", "c_")
    assert response.status_code == 400 and 'data-field-error="c_s_%d"' % i in html
    assert 'name="c_s_%d" value="-51"' % i in html and 'value="ООО Шины"' in html and '<option value="client" selected' in html
    assert not conn.execute("SELECT 1 FROM panel_invite").fetchone()


def test_a_taken_login_or_a_failure_of_the_rows_rolls_everything_back_and_the_link_stays(tmp_path, monkeypatch):
    app, buyer, conn, _ = make_shop(tmp_path)
    owner, tok = owner_of(app)
    path = invite(app, owner, tok, **_sent(fill(invite_form(owner), "laufenn", "13", None, "c_")))
    response = accept(app, path, "buyer")                                               # taken
    assert response.status_code == 400 and "Логин занят" in response.get_data(as_text=True)
    assert link_alive(conn, path) and not conn.execute("SELECT 1 FROM client_brand_pct").fetchone()

    def broken(*args, **kwargs):                                                        # the last step fails: the claim and the user go too
        raise ct.TermsError("сбой")
    monkeypatch.setattr(ct, "save_rows", broken)
    assert accept(app, path, "fresh").status_code == 400
    check = store.connect(app.config["DB_PATH"])
    assert not users.exists(check, "fresh") and link_alive(check, path)
    assert not check.execute("SELECT 1 FROM audit WHERE action = 'user_added' AND detail LIKE '%fresh%'").fetchone()
    monkeypatch.undo()
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    assert accept(app, path, "fresh").status_code == 302
    check = store.connect(app.config["DB_PATH"])
    assert ct.rows(check, "fresh") == {"laufenn": (3.0, False, None, False)} and not link_alive(check, path)


def test_an_invitation_our_percent_outdated_refuses_the_link_and_keeps_it(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    owner, tok = owner_of(app)
    path = invite(app, owner, tok, **_sent(fill(invite_form(owner), "laufenn", "-40", None, "c_")))   # a shift of −50
    set_ours(conn, {"IKON TYRES|winter": (10, 20), "LAUFENN|summer": (-5, 20)})          # since the link: −5 − 50 < −50
    response = accept(app, path, "fresh")
    assert response.status_code == 400 and "устарели" in response.get_data(as_text=True)
    assert not users.exists(conn, "fresh") and link_alive(conn, path)


def test_broken_or_old_conditions_of_an_invitation(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path)
    owner, tok = owner_of(app)
    path = invite(app, owner, tok)
    conn.execute("UPDATE panel_invite SET client_terms = '{\"rows\": {\"laufenn\": [-500, true, null, false]}}'")
    conn.commit()
    response = accept(app, path, "fresh")
    assert response.status_code == 400 and "испорчены" in response.get_data(as_text=True)
    assert not users.exists(conn, "fresh") and link_alive(conn, path)
    conn.execute("UPDATE panel_invite SET client_terms = '{\"small\": 12, \"large\": null, \"brands\": [\"laufenn\"]}'")   # before 2026-10-31
    conn.commit()
    assert accept(app, path, "fresh").status_code == 302 and ct.rows(conn, "fresh") == {}


def test_the_conditions_of_an_invitation_are_kept_for_a_client_only(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path)
    owner, tok = owner_of(app)
    path = invite(app, owner, tok, role="viewer", c_bk="laufenn", c_s_0="-999")         # not a client: not even checked
    assert conn.execute("SELECT client_terms FROM panel_invite").fetchone() == ("",)
    assert accept(app, path, "watcher").status_code == 302
    assert not conn.execute("SELECT 1 FROM client_brand_pct WHERE login = 'watcher'").fetchone()
    with pytest.raises(ct.TermsError):
        users.create_invite(conn, "client", None, "", NOW, "owner", '{"rows": {"laufenn": [999, true, null, false]}}')
    users.create_invite(conn, "viewer", None, "", NOW, "owner", ct.invite_terms({"laufenn": (5, False, None, False)}))
    assert conn.execute("SELECT client_terms FROM panel_invite WHERE role = 'viewer'").fetchall() == [("",), ("",)]


@pytest.mark.parametrize("name", ["clients", "role", "active", "preview"])
def test_a_login_never_takes_the_address_of_the_conditions(tmp_path, name):
    """Review: the conditions lived under /users/clients/…, so /users/<login>/role of a user «clients» was the role's conditions; now
    under /client-terms, apart from /users/<login>/…"""
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    users.add_user(conn, name, PASSWORD, NOW, role="viewer")
    owner, tok = owner_of(app)
    assert owner.post("/users/%s/role" % name, data={"csrf": tok, "role": "chats", "display_name": "Оля"}).status_code == 302
    assert users.get_role(conn, name)["role"] == "chats"
    assert owner.post("/users/%s/active" % name, data={"csrf": tok, "active": "0"}).status_code == 302
    assert not users.get_role(conn, name)["active"]
    users.set_role(conn, name, "client", None, "", NOW, "owner")
    html = page(owner, "/client-terms/c/%s" % name)
    assert 'action="/client-terms/c/%s"' % name in html
    assert send(owner, "/client-terms/c/%s" % name, tok, fill(form_of(html, "/client-terms/c/%s" % name), "laufenn", "14")).status_code == 302
    assert ct.rows(conn, name) == {"laufenn": (4.0, False, None, False)}


def test_the_rules_are_written_on_the_invitation_and_the_profile(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    owner, tok = owner_of(app)
    for url in ("/users", "/client-terms/c/buyer"):
        html = page(owner, url)
        assert "<b>Правила</b>" in html and "закупка + наш % бренда + сдвиг клиента" in html, url
        assert "Выключенный в нашем фиде" in html and "Пустое поле — как у нас" in html and "от −50 до 300" in html and "«Применить» остановится" in html, url


def test_a_new_number_in_a_field_drawn_before_our_percent_changed_is_refused_however_it_was_drawn(tmp_path):
    """The lead's review: a field of the client's own % (drawn as «свой», ours one) got a new number while «Применить» gave the brand
    a second % of ours — it was read as a shift (35/37 in place of 25); a shift field typed «+5» while ours became one — read as «свой 5».
    Now a new number is read only the way it was drawn, else refused; the refused form shows what is saved for such a brand."""
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    owner, tok = owner_of(app)
    ct.save_rows(conn, "buyer", {"laufenn": (20, True, None, False)}, NOW, "owner")    # its own 20 % (kept absolute: ours came later)
    data = fill(profile_form(owner), "laufenn", "25")
    set_ours(conn, {"IKON TYRES|winter": (10, 20), "LAUFENN|summer": (10, 20), "LAUFENN|winter": (12, 20)})   # A: a second % of ours
    response = send(owner, "/client-terms/c/buyer", tok, data)
    html = response.get_data(as_text=True)
    assert response.status_code == 400 and "изменился" in html and ct.rows(conn, "buyer")["laufenn"] == (20.0, True, None, False)
    assert 'name="s_%d" value="20"' % row_index(data, "laufenn") in html                 # the saved own %, not the 25 drawn another way
    ct.save_rows(conn, "buyer", {"laufenn": (None, False, None, False)}, NOW, "owner")
    data = fill(profile_form(owner), "laufenn", "+5")                                    # B: drawn as a shift (two of ours)
    set_ours(conn, {"IKON TYRES|winter": (10, 20), "LAUFENN|summer": (10, 20)})          # ours became one
    assert send(owner, "/client-terms/c/buyer", tok, data).status_code == 400 and ct.rows(conn, "buyer") == {}
