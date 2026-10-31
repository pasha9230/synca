"""«Обзор» at the first sign-in (the owner 2026-10-30, panel/tour.py): the steps of each role point at elements of the role's start page,
a role never gets a step about a section it cannot open, «Пропустить»/«Готово» are kept in the person's account (user_pref), «Пройти
обзор заново» shows it again; the client's catalog keeps no script."""
from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from panel_helpers import login, make_app, post
from test_mypoint import manager
from test_roles import person
from test_shop import make_shop
from test_vk_commands import NOW, _stage, ready
from test_workplace import _on_oth
from tires_sync import order_oth, orders, store, user_prefs, users
from tires_sync.panel import tour

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []


class _Tree(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.root = self.cur = _Node("#root", (), None)
        self.all = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        self.all.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_endtag(self, tag):
        node = self.cur
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.cur = node.parent


COMPOUND = re.compile(r'(?P<part>\.[\w-]+|#[\w-]+|\[[\w-]+=(?:[\w-]+|"[^"]*")\]|:first-of-type|[a-z][a-z0-9]*)')


def _fits(node, compound):
    parts = [m.group("part") for m in COMPOUND.finditer(compound)]
    assert "".join(parts) == compound, "the test does not read this selector: " + compound
    for p in parts:
        if p.startswith("."):
            ok = p[1:] in node.attrs.get("class", "").split()
        elif p.startswith("#"):
            ok = node.attrs.get("id") == p[1:]
        elif p.startswith("["):
            name, value = p[1:-1].split("=")
            ok = node.attrs.get(name) == value.strip('"')
        elif p == ":first-of-type":
            ok = next(c for c in node.parent.children if c.tag == node.tag) is node
        else:
            ok = node.tag == p
        if not ok:
            return False
    return True


def select(html, selector):
    """The elements of `html` the selector of a step lights (the descendant and the child combinators, classes, ids, [a=b], :first-of-type)."""
    tokens = selector.replace(">", " > ").split()
    found = []
    for node in _Tree(html).all:
        if not _fits(node, tokens[-1]):
            continue
        i, cur, ok = len(tokens) - 2, node, True
        while i >= 0 and ok:
            if tokens[i] == ">":
                cur = cur.parent
                ok = cur is not None and cur.tag != "#root" and _fits(cur, tokens[i - 1])
                i -= 2
            else:
                cur = cur.parent
                while cur is not None and cur.tag != "#root" and not _fits(cur, tokens[i]):
                    cur = cur.parent
                ok = cur is not None and cur.tag != "#root"
                i -= 1
        if ok:
            found.append(node)
    return found


def shown(html):
    """The titles of the steps the page shows (the tour's window), in order."""
    return re.findall(r'<h3 class="tour-title">([^<]+)</h3>', html)


def text(r):
    return r.get_data(as_text=True)


# ---- the page of each role ---------------------------------------------------------------------------------------------------------

def _staff(tmp_path, role):
    app = make_app(tmp_path)
    if role == "owner":
        client = app.test_client()
        login(client)
        return client, store.connect(app.config["DB_PATH"]), "owner"
    client, conn = person(app, role)
    return client, conn, role + "1"


@pytest.mark.parametrize("role", ["owner", "partner", "chats", "viewer"])
def test_every_step_of_the_staff_lights_an_element_of_the_home_page_and_only_its_own_sections(tmp_path, role):
    client, conn, _ = _staff(tmp_path, role)
    html = text(client.get("/"))
    steps = tour.steps_for(role)
    assert shown(html) == [s["title"] for s in steps]
    for s in steps:
        assert len(select(html, s["sel"])) == 1, (role, s["sel"])
        assert users.level(role, s["need"]) >= users.VIEW
    for s in tour.STEPS["staff"]:
        if s not in steps:                                              # a section the role cannot open: no step, no element to light
            assert select(html, s["sel"]) == [], (role, s["sel"])
    titles = {s["title"] for s in steps}
    if role in ("chats", "viewer"):
        assert "Настройки" not in titles and "История" not in titles and "Главная" in titles and "Чаты" in titles
    else:
        assert {"Главная", "Заказы", "Чаты", "Каталог", "Настройки", "История"} <= titles


def test_every_step_of_the_point_lights_an_element_of_its_workplace(tmp_path):
    app, owner, tok, conn, pid, card = ready(tmp_path)
    oth = _on_oth(conn, pid)
    assert post(owner, "/orders/%d/oth-agreed" % oth, "tok").status_code == 302            # a return with the act МХ-3 on the page
    assert order_oth.return_plan(conn, oth)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")                    # a card on «На точке», the tab the page opens on
    mine = manager(app, conn, pid)
    html = text(mine.get("/my-point"))
    steps = tour.steps_for("point")
    assert shown(html) == [s["title"] for s in steps] == ["Рабочее место", "Вкладки", "Поиск", "Передать клиента", "Карточка заказа",
                                                          "Возврат поставщику", tour.AGAIN["title"]]
    for s in steps:
        assert len(select(html, s["sel"])) == 1, s["sel"]
    assert "МХ-3" in next(s for s in steps if s["sel"] == ".mp-returns")["text"]


def test_every_step_of_the_client_lights_an_element_of_the_catalog(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    response = client.get("/shop")
    html = text(response)
    steps = tour.steps_for("client")
    assert shown(html) == [s["title"] for s in steps] == ["Каталог", "Фильтры", "Склад 1 и Склад 2", "Корзина и заказ", tour.AGAIN["title"]]
    for s in steps:
        assert len(select(html, s["sel"])) == 1, s["sel"]
        if "phone" in s:                                       # the filters sit in a closed sheet on a phone: its button is lit there
            assert len(select(html, s["phone"])) == 1, s["phone"]
            assert "@media (max-width:%dpx){body:has(#tour-s%d:checked) %s{" % (tour.PHONE, steps.index(s) + 1, s["phone"]) in html
    assert [s["title"] for s in steps if s.get("phone") == ".shop-ft-open"] == ["Фильтры", "Склад 1 и Склад 2"]
    assert "<script" not in html and "script-src" not in response.headers["Content-Security-Policy"]     # the catalog keeps no script


def test_no_role_gets_a_step_about_a_section_it_cannot_open():
    for role in users.ROLES:
        for s in tour.steps_for(role):
            assert users.level(role, s["need"]) >= users.VIEW, (role, s["title"])
    assert [s["title"] for s in tour.steps_for("point")][0] == "Рабочее место"
    assert all(s["need"] in ("shop", "self") for s in tour.steps_for("client"))
    assert all(s["need"] in ("mypoint", "self") for s in tour.steps_for("point"))
    for steps in tour.STEPS.values():
        assert all(tour.SELECTOR.match(s["sel"]) and s["text"] and s["title"] for s in steps)


def test_the_tour_is_shown_on_the_start_page_only(tmp_path):
    client, conn, _ = _staff(tmp_path, "owner")
    assert shown(text(client.get("/")))
    assert not shown(text(client.get("/orders"))) and not shown(text(client.get("/catalog")))


# ---- passed, skipped, again -------------------------------------------------------------------------------------------------------

def test_skip_is_kept_in_the_account_of_the_person_only_and_again_shows_it_once_more(tmp_path):
    app = make_app(tmp_path)
    one, conn = person(app, "partner", login_="anna")
    two, _ = person(app, "partner", login_="boris")
    r = post(one, "/tour/done", "tok", how="skip", next="/")
    assert r.status_code == 302 and r.headers["Location"] == "/"
    assert user_prefs.get(conn, "anna", tour.PREF)["how"] == "skip"
    assert not shown(text(one.get("/"))) and shown(text(two.get("/")))                 # the other person still has it
    other = app.test_client()                                                         # another browser of the same person: passed too
    from test_roles import sign_in
    sign_in(other, conn, "anna")
    assert not shown(text(other.get("/")))
    r = post(one, "/tour/again", "tok")
    assert r.status_code == 302 and r.headers["Location"] == "/"
    assert user_prefs.get(conn, "anna", tour.PREF) is None and shown(text(one.get("/")))


def test_the_last_step_ends_it_and_comes_back_to_the_same_page_never_elsewhere(tmp_path):
    client, conn, login_ = _staff(tmp_path, "owner")
    r = post(client, "/tour/done", "tok", how="end", next="/?x=1")
    assert r.headers["Location"] == "/?x=1" and user_prefs.get(conn, login_, tour.PREF)["how"] == "end"
    post(client, "/tour/again", "tok")
    assert post(client, "/tour/done", "tok", how="end", next="https://evil.example/").headers["Location"] == "/"


def test_the_point_and_the_client_pass_and_repeat_it_on_their_own_page(tmp_path):
    app, owner, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    assert shown(text(mine.get("/my-point")))
    assert post(mine, "/tour/done", "tok", how="skip", next="/my-point").status_code == 302
    assert not shown(text(mine.get("/my-point")))
    assert post(mine, "/tour/again", "tok").headers["Location"] == "/my-point"
    assert shown(text(mine.get("/my-point")))
    (tmp_path / "shop").mkdir()
    app, client, conn, _ = make_shop(tmp_path / "shop")
    assert 'action="/tour/again"' in text(client.get("/shop"))
    assert post(client, "/tour/done", "tok", how="end", next="/shop").status_code == 302
    assert not shown(text(client.get("/shop")))
    assert post(client, "/tour/again", "tok").headers["Location"] == "/shop"
    assert shown(text(client.get("/shop")))


def test_the_marks_need_the_csrf_and_a_sign_in(tmp_path):
    client, conn, login_ = _staff(tmp_path, "owner")
    assert client.post("/tour/done", data={"how": "skip"}).status_code == 400
    assert user_prefs.get(conn, login_, tour.PREF) is None
    stranger = client.application.test_client()
    with stranger.session_transaction() as session:
        session["csrf"] = "tok"
    assert post(stranger, "/tour/again", "tok").headers["Location"].endswith("/login")


def test_the_menu_of_every_signed_in_page_has_the_again_button(tmp_path):
    client, conn, _ = _staff(tmp_path, "chats")
    for url in ("/", "/orders", "/chats"):
        assert 'action="/tour/again"' in text(client.get(url)), url


# ---- a page without the part a step is about ---------------------------------------------------------------------------------------

def test_a_new_point_gets_no_step_about_a_card_or_a_return_it_does_not_have(tmp_path):
    app, owner, tok, conn, pid, card = ready(tmp_path)
    orders.move_card(conn, card, _stage(conn, "done"), NOW, "o")            # nothing on the way, nothing to give back
    mine = manager(app, conn, pid)
    html = text(mine.get("/my-point"))
    assert shown(html) == ["Рабочее место", "Вкладки", "Поиск", "Передать клиента", tour.AGAIN["title"]]
    for s in tour.steps_for("point"):
        if s["title"] in shown(html):
            assert len(select(html, s["sel"])) == 1, s["sel"]
        else:
            assert select(html, s["sel"]) == [], s["sel"]


def test_an_empty_catalog_shows_no_tour_and_keeps_it_for_later(tmp_path):
    app, client, conn, _ = make_shop(tmp_path, role=(None, None))
    html = text(client.get("/shop"))
    assert "Ассортимент ещё не настроен" in html and not shown(html) and "tour-radio" not in html
    assert user_prefs.get(conn, "buyer", tour.PREF) is None                 # not passed: it comes once the catalog is there


def test_a_catalog_found_empty_keeps_its_steps_and_the_cart(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    html = text(client.get("/shop?q=nothing-like-this"))                    # nothing found: the filters and the table head are still there
    assert shown(html) == ["Каталог", "Фильтры", "Склад 1 и Склад 2", "Корзина и заказ", tour.AGAIN["title"]]
    for s in tour.steps_for("client"):
        assert len(select(html, s["sel"])) == 1, s["sel"]
    assert "оформите заказ" in next(s for s in tour.STEPS["client"] if s["title"] == "Корзина и заказ")["text"]   # the owner 2026-10-30: with the cart


def test_enter_on_a_step_sends_nothing_the_radio_buttons_are_outside_the_forms(tmp_path):
    client, conn, _ = _staff(tmp_path, "owner")
    html = text(client.get("/"))
    tree = _Tree(html)
    radios = [n for n in tree.all if "tour-radio" in n.attrs.get("class", "").split()]
    assert len(radios) == len(tour.steps_for("owner"))
    for radio in radios:
        node = radio.parent
        while node is not None and node.tag != "form":
            node = node.parent
        assert node is None
    forms = [n for n in tree.all if n.tag == "form" and n.attrs.get("action") == "/tour/done"]
    assert len(forms) == len(radios)                                        # each step its own «Пропустить» / «Готово»
