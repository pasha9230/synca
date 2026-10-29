"""The conditions of the clients in the owner's panel and the client's invitation (builds 3-4 of the role «Клиент», plan client-role §7.1,
§8, §9.4): the role's conditions act for every client without their own, a client's own only for them; an invitation carries the conditions
into the profile at once or not at all; a wrong % is refused under its field; only the owner changes them; the preview is the client's own
catalog and is not the client's to open; a forged POST never makes a client responsible for an order or on duty."""
from __future__ import annotations

import json
import re

import pytest

from panel_helpers import login, post
from test_shop import make_shop, page, prices, tochki_row
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


def set_brand(data: dict, key: str, on=None, small=None, large=None) -> dict:
    i = data["bk"].index(key)
    if on is True:
        data["on_%d" % i] = ["1"]
    elif on is False:
        data.pop("on_%d" % i, None)
    if small is not None:
        data["s_%d" % i] = [small]
    if large is not None:
        data["l_%d" % i] = [large]
    return data


def send(client, url, tok, data: dict):
    return client.post(url, data=dict(data, csrf=tok))


# --- the role's and a client's own conditions ----------------------------------------------------------------------------------------

def test_the_role_conditions_change_every_client_without_their_own_and_a_profile_only_its_client(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    users.add_user(conn, "buyer2", PASSWORD, NOW, role="client")
    users.add_user(conn, "buyer3", PASSWORD, NOW, role="client")
    ct.save(conn, "buyer3", 30, None, "inherit", {}, NOW, "owner")                      # buyer3: its own % up to R17
    b2, b3 = as_user(app, conn, "buyer2"), as_user(app, conn, "buyer3")
    assert sorted(prices(page(buyer))) == sorted(prices(page(b2))) == [13580, 65190]    # 12345 × 1.10, 54321 × 1.20
    assert sorted(prices(page(b3))) == [16050, 65190]                                   # 12345 × 1.30
    owner, tok = owner_of(app)
    data = form_of(page(owner, "/client-terms"), "/client-terms/role")
    data.update(small=["50"], large=["20"])
    assert send(owner, "/client-terms/role", tok, data).status_code == 302
    assert sorted(prices(page(buyer))) == sorted(prices(page(b2))) == [18520, 65190]    # 12345 × 1.50: every client without their own
    assert sorted(prices(page(b3))) == [16050, 65190]                                   # the own % is stronger
    data = form_of(page(owner, "/client-terms/c/buyer2"), "/client-terms/c/buyer2")
    data.update(small=["0"], large=[""])
    assert send(owner, "/client-terms/c/buyer2", tok, data).status_code == 302
    assert sorted(prices(page(b2))) == [12350, 65190]                                   # buyer2 alone: 0 % up to R17 set by hand
    assert sorted(prices(page(buyer))) == [18520, 65190] and sorted(prices(page(b3))) == [16050, 65190]


def test_the_brands_of_the_role_and_a_client_s_own_list_and_the_brand_pct(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    owner, tok = owner_of(app)
    html = page(owner, "/client-terms")
    data = form_of(html, "/client-terms/role")
    assert set(data["bk"]) >= {"ikon tyres", "laufenn"}
    set_brand(data, "laufenn", on=True, small="5")
    assert send(owner, "/client-terms/role", tok, data).status_code == 302
    assert ct.read(conn, ct.ROLE)["brands"] == {"ikon tyres": (None, None), "laufenn": (5.0, None)}
    assert 3500 in prices(page(buyer))                                                  # 3333 × 1.05 → 3500 (the brand's % over the general)
    users.add_user(conn, "buyer2", PASSWORD, NOW, role="client")
    data = form_of(page(owner, "/client-terms/c/buyer2"), "/client-terms/c/buyer2")
    data["mode"] = ["own"]
    set_brand(data, "ikon tyres", on=False)
    set_brand(data, "laufenn", on=True)
    assert send(owner, "/client-terms/c/buyer2", tok, data).status_code == 302
    b2 = as_user(app, conn, "buyer2")
    assert prices(page(b2)) == [3500]                                                   # its own list: Laufenn only, the role's % of it
    assert len(prices(page(buyer))) == 3
    data = form_of(page(owner, "/client-terms/c/buyer2"), "/client-terms/c/buyer2")
    data["mode"] = ["inherit"]
    set_brand(data, "laufenn", small="10")
    set_brand(data, "ikon tyres", on=True)                                              # a tick does not act «как у роли»: not kept
    assert send(owner, "/client-terms/c/buyer2", tok, data).status_code == 302
    assert ct.read(conn, "buyer2") == {"small": None, "large": None, "mode": "inherit", "brands": {"laufenn": (10.0, None)}}
    assert sorted(prices(page(b2))) == [3670, 13580, 65190]                             # the role's set, its own % of Laufenn
    data = form_of(page(owner, "/client-terms/c/buyer2"), "/client-terms/c/buyer2")      # «как у роли»: the ticks are the role's set
    assert sorted(data["bk"][int(k[3:])] for k in data if k.startswith("on_")) == ["ikon tyres", "laufenn"]


def test_a_brand_key_is_kept_bare_and_a_subject_s_rows_are_written_whole(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path)
    conn.execute("INSERT INTO client_brand (subject, brand_key, pct_small, pct_large) VALUES ('*', 'S2:IKON TYRES', 99, 99)")
    conn.commit()
    ct.save(conn, ct.ROLE, 10, 20, "own", {"S2:Ikon Tyres": (None, 15), "": (1, 1)}, NOW, "owner")
    assert conn.execute("SELECT brand_key, pct_small, pct_large FROM client_brand WHERE subject = '*'").fetchall() == [("ikon tyres", None, 15.0)]
    assert ct.read(conn, ct.ROLE)["mode"] == "own"
    ct.save(conn, ct.ROLE, 10, 20, "inherit", {}, NOW, "owner")                         # the role is always «own»
    assert conn.execute("SELECT brands_mode FROM client_terms WHERE subject = '*'").fetchone() == ("own",)
    with pytest.raises(ct.TermsError):                                                  # conditions only for a client
        ct.save(conn, "owner2", 1, 1, "inherit", {}, NOW, "owner")
    users.add_user(conn, "olya", PASSWORD, NOW, role="chats")
    with pytest.raises(ct.TermsError):
        ct.save(conn, "olya", 1, 1, "inherit", {}, NOW, "owner")
    assert not conn.execute("SELECT 1 FROM client_terms WHERE subject IN ('olya', 'owner2')").fetchone()


@pytest.mark.parametrize("value", ["-1", "300.01", "301", "abc", "1.234", "1e2", "10%", " 5 5", "nan", "inf", "+5", "١٢"])
def test_a_wrong_pct_is_refused_under_its_field_and_nothing_is_written(tmp_path, value):
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    owner, tok = owner_of(app)
    data = form_of(page(owner, "/client-terms"), "/client-terms/role")
    data["small"] = [value]
    response = send(owner, "/client-terms/role", tok, data)
    html = response.get_data(as_text=True)
    assert response.status_code == 400 and 'data-field-error="small"' in html and "от 0 до 300" in html
    assert 'name="small" value="%s"' % value in html                                     # what was typed comes back
    assert ct.read(conn, ct.ROLE)["small"] == 10.0
    data = form_of(page(owner, "/client-terms"), "/client-terms/role")
    set_brand(data, "ikon tyres", large=value)
    response = send(owner, "/client-terms/role", tok, data)
    i = data["bk"].index("ikon tyres")
    assert response.status_code == 400 and 'data-field-error="l_%d"' % i in response.get_data(as_text=True)
    assert ct.read(conn, ct.ROLE)["brands"]["ikon tyres"] == (None, None)
    assert not conn.execute("SELECT 1 FROM audit WHERE action = 'client_terms_changed'").fetchone()


@pytest.mark.parametrize("value, pct", [("0", 0.0), ("300", 300.0), ("12,5", 12.5), ("7.25", 7.25), ("", None), (" 15 ", 15.0)])
def test_the_pct_that_is_taken(value, pct):
    assert ct.parse_pct(value) == pct


def test_the_journal_has_the_author_and_the_changes_and_no_password(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    owner, tok = owner_of(app)
    data = form_of(page(owner, "/client-terms/c/buyer"), "/client-terms/c/buyer")
    data.update(small=["12"], mode=["own"])
    set_brand(data, "ikon tyres", on=True, large="7")
    assert send(owner, "/client-terms/c/buyer", tok, data).status_code == 302
    rows = conn.execute("SELECT author, detail FROM audit WHERE action = 'client_terms_changed'").fetchall()
    assert len(rows) == 1 and rows[0][0] == app.config["AUTHOR"]
    detail = json.loads(rows[0][1])
    assert detail["subject"] == "buyer" and detail["from"]["small"] is None and detail["to"] == {"small": 12.0, "large": None, "mode": "own"}
    assert detail["brands_added"] == ["ikon tyres"] and detail["brand_pct"] == {"ikon tyres": [None, 7.0]}   # a new brand's % too
    everything = " ".join(r[0] for r in conn.execute("SELECT detail FROM audit"))
    assert PASSWORD not in everything and "pbkdf2" not in everything
    from tires_sync.panel import texts
    assert texts.audit_text({"action": "client_terms_changed", "detail": detail}) == \
        "Условия клиента buyer: до R17 — → 12 %, бренды свой список, бренды +1 −0, свой % у брендов: 1"


# --- rights ----------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["partner", "chats", "viewer", "point", "client"])
def test_nobody_but_the_owner_changes_the_conditions(tmp_path, role):
    from tires_sync import order_points as points
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"}) if role == "point" else None
    if role != "client":
        users.add_user(conn, "who", PASSWORD, NOW, role=role, point_id=pid)
    other = as_user(app, conn, "buyer" if role == "client" else "who")
    for url, data in (("/client-terms/role", {"small": "0", "large": "0"}), ("/client-terms/c/buyer", {"small": "0", "mode": "inherit"}),
                      ("/users/invite", {"role": "client", "c_small": "0", "c_mode": "inherit"})):
        assert other.post(url, data=dict(data, csrf="tok")).status_code == 403, url
    assert ct.read(conn, ct.ROLE)["small"] == 10.0 and ct.read(conn, "buyer")["small"] is None
    assert not conn.execute("SELECT 1 FROM panel_invite").fetchone()
    reads = [other.get(u).status_code for u in ("/client-terms", "/client-terms/c/buyer", "/client-terms/c/buyer/preview")]
    assert reads == ([200, 200, 200] if role == "partner" else [403, 403, 403])          # the partner looks (section «users»: смотреть)
    if role == "partner":
        html = other.get("/client-terms").get_data(as_text=True)
        assert 'action="/client-terms/role"' not in html and "ro-form" in html


def test_the_pages_of_a_login_that_is_not_a_client_are_404(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path)
    users.add_user(conn, "olya", PASSWORD, NOW, role="chats")
    owner, tok = owner_of(app)
    for url in ("/client-terms/c/olya", "/client-terms/c/olya/preview", "/client-terms/c/nobody", "/client-terms/c/nobody/preview"):
        assert owner.get(url).status_code == 404, url
    assert owner.post("/client-terms/c/olya", data={"csrf": tok, "small": "1", "mode": "inherit"}).status_code == 404
    assert not conn.execute("SELECT 1 FROM client_terms WHERE subject = 'olya'").fetchone()


# --- the preview -----------------------------------------------------------------------------------------------------------------------

def test_the_preview_is_the_client_s_own_catalog_and_not_the_client_s_to_open(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path, tochki=[tochki_row("T1")], role=(10, 20))
    users.add_user(conn, "buyer2", PASSWORD, NOW, role="client")
    ct.save(conn, "buyer2", 30, 30, "inherit", {}, NOW, "owner")
    owner, tok = owner_of(app)
    preview = owner.get("/client-terms/c/buyer/preview")
    assert preview.status_code == 200 and "no-store" in preview.headers["Cache-Control"]
    html = preview.get_data(as_text=True)
    assert sorted(prices(html)) == sorted(prices(page(buyer)))
    assert sorted(prices(page(owner, "/client-terms/c/buyer2/preview"))) == sorted(prices(page(as_user(app, conn, "buyer2"))))
    assert "Склад 2" in html and "/shop/photo/" not in html
    for secret in ("12 345", "12345", "SECRETCODE", "77001", "cdn.supplier-one.test", "img.4tochki.test"):
        assert secret not in html[html.index("Найдено"):], secret
    filtered = page(owner, "/client-terms/c/buyer/preview?size=245/45R18")                # the filters and their links stay on the preview
    assert prices(filtered) == [65190] and 'action="/client-terms/c/buyer/preview"' in filtered and 'action="/shop"' not in filtered
    for url in ("/client-terms/c/buyer/preview", "/client-terms/c/buyer2/preview", "/client-terms", "/client-terms/c/buyer"):
        assert buyer.get(url).status_code == 403, url


# --- the invitation of a client --------------------------------------------------------------------------------------------------------

def invite(app, owner, tok, **data):
    response = owner.post("/users/invite", data=dict({"csrf": tok, "role": "client", "c_mode": "inherit"}, **data))
    assert response.status_code == 302
    html = owner.get("/users").get_data(as_text=True)
    found = re.search(r'value="(http://localhost/invite/[^"]+)"', html)
    return found.group(1).replace("http://localhost", "") if found else None


def accept(app, path, login_, password=PASSWORD):
    guest = app.test_client()
    opened = guest.get(path)
    if opened.status_code != 200:
        return opened
    tok = re.search(r'name="csrf" value="([^"]+)"', opened.get_data(as_text=True)).group(1)
    return guest.post(path, data={"csrf": tok, "login": login_, "password": password, "password2": password})


def test_an_invitation_with_conditions_puts_them_in_the_profile(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    owner, tok = owner_of(app)
    html = page(owner, "/users")
    form = html[html.index('action="/users/invite"'):].split("</form>")[0]
    assert 'value="client"' in form and 'name="c_small"' in form and 'value="laufenn"' in form
    path = invite(app, owner, tok, display_name="ООО Шины", c_small="12,5", c_large="", c_mode="own", c_brand=["laufenn"])
    assert path and "до R17 12.5 %" in page(owner, "/users")
    assert accept(app, path, "shina").status_code == 302
    assert users.get_role(conn, "shina")["role"] == "client" and users.get_role(conn, "shina")["display_name"] == "ООО Шины"
    assert ct.read(conn, "shina") == {"small": 12.5, "large": None, "mode": "own", "brands": {"laufenn": (None, None)}}
    shina = as_user(app, conn, "shina")
    assert prices(page(shina)) == [3750]                                                # 3333 × 1.125 → 3750, Laufenn only
    assert accept(app, path, "shina2").status_code == 404                               # one time
    plain = invite(app, owner, tok, c_small="", c_large="")
    assert accept(app, plain, "shina3").status_code == 302
    assert ct.read(conn, "shina3") == {"small": None, "large": None, "mode": "inherit", "brands": {}}
    assert sorted(prices(page(as_user(app, conn, "shina3")))) == sorted(prices(page(buyer)))   # «как у роли»


def test_a_taken_login_or_a_failure_of_the_conditions_rolls_everything_back_and_the_link_stays(tmp_path, monkeypatch):
    app, buyer, conn, _ = make_shop(tmp_path)
    owner, tok = owner_of(app)
    path = invite(app, owner, tok, c_small="12", c_mode="own", c_brand=["ikon tyres"])
    response = accept(app, path, "buyer")                                               # taken
    assert response.status_code == 400 and "Логин занят" in response.get_data(as_text=True)
    assert conn.execute("SELECT COUNT(*) FROM client_terms WHERE subject != '*'").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM client_brand WHERE subject != '*'").fetchone() == (0,)
    assert link_alive(conn, path)

    def broken(*args, **kwargs):                                                        # the last step fails: the claim and the user go too
        raise ct.TermsError("сбой")
    monkeypatch.setattr(ct, "save", broken)
    assert accept(app, path, "fresh").status_code == 400
    check = store.connect(app.config["DB_PATH"])
    assert not users.exists(check, "fresh") and link_alive(check, path)
    assert not check.execute("SELECT 1 FROM audit WHERE action = 'user_added' AND detail LIKE '%fresh%'").fetchone()
    monkeypatch.undo()
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    assert accept(app, path, "fresh").status_code == 302
    check = store.connect(app.config["DB_PATH"])
    assert ct.read(check, "fresh")["small"] == 12.0 and not link_alive(check, path)


def test_a_broken_conditions_of_an_invitation_refuse_it_and_keep_the_link(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path)
    owner, tok = owner_of(app)
    path = invite(app, owner, tok, c_small="12")
    conn.execute("UPDATE panel_invite SET client_terms = '{\"small\": -5, \"large\": null, \"brands\": null}'")
    conn.commit()
    response = accept(app, path, "fresh")
    assert response.status_code == 400 and "испорчены" in response.get_data(as_text=True)
    assert not users.exists(conn, "fresh") and link_alive(conn, path)


@pytest.mark.parametrize("field, value", [("c_small", "-1"), ("c_large", "301"), ("c_small", "много"), ("c_mode", "everything")])
def test_a_wrong_condition_of_an_invitation_is_refused_under_its_field(tmp_path, field, value):
    app, buyer, conn, _ = make_shop(tmp_path)
    owner, tok = owner_of(app)
    owner.post("/users/invite", data={"csrf": tok, "role": "client", "c_mode": "inherit", field: value})
    html = page(owner, "/users")
    assert 'data-field-error="%s"' % field in html
    assert not conn.execute("SELECT 1 FROM panel_invite").fetchone()


def test_the_conditions_of_an_invitation_are_kept_for_a_client_only(tmp_path):
    app, buyer, conn, _ = make_shop(tmp_path)
    owner, tok = owner_of(app)
    path = invite(app, owner, tok, role="viewer", c_small="-1", c_mode="own", c_brand=["ikon tyres"])   # not a client: not even checked
    assert conn.execute("SELECT client_terms FROM panel_invite").fetchone() == ("",)
    assert accept(app, path, "watcher").status_code == 302
    assert not conn.execute("SELECT 1 FROM client_terms WHERE subject = 'watcher'").fetchone()
    with pytest.raises(ct.TermsError):
        users.create_invite(conn, "client", None, "", NOW, "owner", '{"small": 999, "large": null, "brands": null}')
    users.create_invite(conn, "viewer", None, "", NOW, "owner", ct.invite_terms("5", "", "inherit", []))   # the module keeps it a client's only
    assert conn.execute("SELECT client_terms FROM panel_invite WHERE role = 'viewer'").fetchall() == [("",), ("",)]


@pytest.mark.parametrize("name", ["clients", "role", "active", "preview"])
def test_a_login_never_takes_the_address_of_the_conditions(tmp_path, name):
    """Review: the conditions lived under /users/clients/…, so /users/<login>/role of a user «clients» was the role's conditions (written
    empty) and a client «role» saved over the role's: now under /client-terms, apart from /users/<login>/…"""
    app, buyer, conn, _ = make_shop(tmp_path, role=(10, 20))
    users.add_user(conn, name, PASSWORD, NOW, role="viewer")
    owner, tok = owner_of(app)
    assert owner.post("/users/%s/role" % name, data={"csrf": tok, "role": "chats", "display_name": "Оля"}).status_code == 302
    assert users.get_role(conn, name)["role"] == "chats"
    assert owner.post("/users/%s/active" % name, data={"csrf": tok, "active": "0"}).status_code == 302
    assert not users.get_role(conn, name)["active"]
    assert ct.read(conn, ct.ROLE)["small"] == 10.0 and ct.read(conn, ct.ROLE)["brands"] == {"ikon tyres": (None, None)}
    assert not conn.execute("SELECT 1 FROM audit WHERE action = 'client_terms_changed'").fetchone()
    users.set_role(conn, name, "client", None, "", NOW, "owner")
    html = page(owner, "/client-terms/c/%s" % name)
    assert 'action="/client-terms/c/%s"' % name in html
    data = form_of(html, "/client-terms/c/%s" % name)
    data.update(small=["40"], mode=["own"])
    assert send(owner, "/client-terms/c/%s" % name, tok, data).status_code == 302
    assert ct.read(conn, name)["small"] == 40.0 and ct.read(conn, ct.ROLE)["small"] == 10.0
