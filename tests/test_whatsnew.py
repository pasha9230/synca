"""«Что нового» (the owner 2026-11-02, tires_sync.whatsnew, panel/routes_whatsnew): the shipped files all read; a bad file is skipped and
logged, never a fall of the panel; each role sees only its items (the owner and the partner all, working as a point the point's); an
update without items for a role is not shown to it; the dot of the menu while an update is unread, per person and role; opening the page
reads it; the client's page in its frame, without scripts."""
from __future__ import annotations

import logging
import re

import pytest
from markupsafe import escape

from panel_helpers import login, make_app, post
from test_roles import conn_of, person
from tires_sync import order_points as points, user_prefs, users, whatsnew

NOW = "2026-10-07T12:00:00"
DOT = 'class="wn-dot"'


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


BODY = "**Что сделано:** %s.\n\n**Как работает:**\n- правило.\n\n**Затрагивает разделы:** другие разделы не затрагивает.\n"


def update(number, *items, date="02.11.2026", title=None):
    text = "number: %d\ndate: %s\ntitle: %s\n" % (number, date, title or "Обновление панели №%d" % number)
    for head, roles in items:
        text += "\n## %s\nroles: %s\n%s" % (head, roles, BODY % head)
    return text


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "whatsnew"
    path.mkdir()
    (path / "0001.md").write_text(update(1, ("Цена", "owner, partner"), ("Карточка точки", "owner, partner, point")), encoding="utf-8")
    (path / "0002.md").write_text(update(2, ("Каталог", "client")), encoding="utf-8")
    return path


@pytest.fixture
def app(tmp_path, folder):
    return make_app(tmp_path, WHATSNEW_DIR=str(folder))


def page(client):
    response = client.get("/whats-new")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def dot_on(client, url):
    return DOT in client.get(url).get_data(as_text=True)


# --- the files --------------------------------------------------------------------------------------------------------------------------

# what a section of the panel is called, in the titles of the items: the menus (base.html, _point_nav.html), and the menu itself
SECTIONS = {"Главная", "Заказы", "Задачи", "Чаты", "Звонки", "CRM", "Уведомления", "Каталог", "Каталог для клиента", "Генерация", "Фиды", "На ОТХ",
            "Подключения", "Настройки заказов", "Шаблоны сообщений", "История", "Новый вид", "Пользователи", "Рабочее место", "Меню"}
# never in an item a point or a client reads (the owner 2026-11-05); and in no item at all, the repository is public
NOT_FOR_POINT = re.compile(r"закуп|наценк|прибыл|маржа|логин|аккаунт", re.IGNORECASE)
PUBLIC_NEVER = re.compile(r"\+?\d[\d ()-]{9,}\d|https?://|\b[\w-]+\.(?:ru|com|net|org)\b", re.IGNORECASE)


def shipped_items():
    return [(u["number"], item) for u in whatsnew.load() for item in u["items"]]


def test_every_shipped_file_reads_and_numbers_follow_one_another():
    names = sorted(p.name for p in whatsnew.DIR.glob("*"))
    shipped = whatsnew.load()
    assert len(shipped) == len(names) >= 1, "a shipped file of «Что нового» does not read: see the warning in the log"
    assert [u["number"] for u in shipped] == list(range(len(names), 0, -1))
    first = shipped[-1]
    assert first["title"] == "Обновление панели №1" and first["date"] == "02.11.2026"
    for u in shipped:
        for item in u["items"]:
            assert item["roles"] and str(item["body"]).strip()


def test_every_shipped_item_has_the_three_parts_and_a_real_section():
    for number, item in shipped_items():
        body = str(item["body"])
        assert all(body.count("<b>%s</b>" % part.strip("*")) == 1 for part in whatsnew.PARTS), (number, item["title"])
        section, sep, name = item["title"].partition(": ")
        assert sep and name.strip() and section in SECTIONS, (number, item["title"])


def test_no_shipped_item_tells_a_point_or_a_client_about_money_or_logins():
    for number, item in shipped_items():
        if item["roles"] & {"point", "client"}:
            assert not NOT_FOR_POINT.search(item["title"] + " " + str(item["body"])), (number, item["title"])


def test_no_shipped_text_holds_a_phone_or_an_address():
    for path in whatsnew.DIR.glob("*.md"):
        assert not PUBLIC_NEVER.search(path.read_text(encoding="utf-8")), path.name


def test_the_first_update_is_tagged_as_decided():
    first = {i["title"]: i["roles"] for i in whatsnew.load()[-1]["items"]}
    assert first["Каталог: цена в фиде округляется вверх до 50 ₽"] == {"owner", "partner"}
    assert first["Чаты: «Передать клиента» у точки"] == {"owner", "partner", "point"}
    assert first["Рабочее место: «отдал» в ВК значит выдачу"] == {"owner", "partner", "point"}
    assert first["Рабочее место: «Работать как точка»"] == {"owner", "partner"}
    assert first["Новый вид: оформление по группам"] == {"owner", "partner"}
    assert not any("client" in roles for roles in first.values())


def test_the_new_view_is_not_promised_to_those_who_may_not_have_it():
    """Off by default (routes_design.ui_state): an item about a look that is off does not go to the people who would not see it, and says it is off."""
    found = 0
    for number, item in shipped_items():
        if re.search(r"нов(ый|ом|ого|ые|ая|ую) вид", item["title"] + " " + str(item["body"]), re.IGNORECASE):
            found += 1
            assert item["roles"] == {"owner", "partner"}, (number, item["title"])
            assert "выкл" in str(item["body"]).lower(), (number, item["title"])
    assert found >= 5


def test_the_item_of_the_column_account_goes_to_the_owner_and_the_partner_only():
    """Names of people for the table (the lead 2026-11-05): nobody else reads about them."""
    [item] = [i for n, i in shipped_items() if n == 3 and "колонка «Аккаунт»" in i["title"]]
    assert item["roles"] == {"owner", "partner"}
    assert "Подпись в таблице заказов" in str(item["body"])


def test_the_fourth_update_is_shipped_and_tagged_as_decided():
    """№4 (the lead 2026-11-07): what came after №3, by role; WebMIM, the purchase and the profit never to a point (the owner 2026-10-30)."""
    [fourth] = [u for u in whatsnew.load() if u["number"] == 4]
    assert fourth["title"] == "Обновление панели №4" and fourth["date"] == "07.11.2026"
    roles = {i["title"]: i["roles"] for i in fourth["items"]}
    assert roles["Рабочее место: «ОТКАЗ» с причиной вместо «Клиент не пришёл»"] == {"owner", "partner", "point"}
    assert roles["Рабочее место: «Итого» на карточках заказов"] == {"owner", "partner", "point"}
    assert roles["Заказы: блок «Заказы WebMIM» в карточке"] == {"owner", "partner", "chats", "viewer"}
    assert roles["Заказы: «Заказать в МИМ» кнопкой"] == {"owner", "partner"}
    assert roles["Заказы: автосообщение клиенту в указанный день"] == {"owner", "partner", "chats"}
    assert roles["Уведомления: утренняя сводка и «Фото на проверке» в колокольчике"] == {"owner"}
    assert not any("client" in r for r in roles.values())
    assert not any(re.search(r"Письмо поставщику|Почта для возвратов", i["title"] + " " + str(i["body"])) for i in fourth["items"])   # removed (the owner 2026-11-07)
    for item in fourth["items"]:
        if "point" in item["roles"]:
            assert not re.search(r"webmim|\bmim\b|мим\b|закуп|прибыл", item["title"] + " " + str(item["body"]), re.IGNORECASE), item["title"]


def test_the_point_sees_only_its_items_of_the_fourth_update(tmp_path):
    app = make_app(tmp_path)
    pid = points.save_point(conn_of(app), {"name": "Янино", "manager_name": "А", "channel": "vk"})
    client, _conn = person(app, "point", point_id=pid)
    html = page(client)
    [fourth] = [u for u in whatsnew.load() if u["number"] == 4]
    shown = [i for i in fourth["items"] if str(escape(i["title"])) in html]
    assert shown and all("point" in i["roles"] for i in shown)
    assert len(shown) == sum("point" in i["roles"] for i in fourth["items"])
    for hidden in ("Заказы WebMIM", "Закупка изменилась", "Автосообщение клиенту"):
        assert hidden not in html, hidden


@pytest.mark.parametrize("role", ["owner", "partner", "point", "client", "chats", "viewer"])
def test_the_shipped_page_opens_for_a_role_with_its_own_items_only(tmp_path, role):
    app = make_app(tmp_path)
    if role == "owner":
        client = app.test_client()
        login(client)
    elif role == "point":
        pid = points.save_point(conn_of(app), {"name": "Янино", "manager_name": "А", "channel": "vk"})
        client, _conn = person(app, "point", point_id=pid)
    else:
        client, _conn = person(app, role)
    html = page(client)
    for _number, item in shipped_items():
        title = str(escape(item["title"]))
        assert (title in html) == (role in whatsnew.SEES_ALL or role in item["roles"]), (role, item["title"])


@pytest.mark.parametrize("text, why", [
    ("date: 02.11.2026\ntitle: Т\n\n## А\nroles: owner\nТекст\n", "no number"),
    ("number: 2\ndate: 02.11.2026\ntitle: Т\n\n## А\nroles: owner\nТекст\n", "number not the file's"),
    ("number: 3\ndate: 2026-11-02\ntitle: Т\n\n## А\nroles: owner\nТекст\n", "date"),
    ("number: 3\ndate: 02.11.2026\ntitle: Т\n\n## А\nroles: owner, boss\nТекст\n", "unknown role"),
    ("number: 3\ndate: 02.11.2026\ntitle: Т\n\n## А\nТекст\n", "no roles line"),
    ("number: 3\ndate: 02.11.2026\ntitle: Т\n\n## А\nroles: owner\n\n", "no text"),
    ("number: 3\ndate: 02.11.2026\ntitle: Т\n", "no items"),
    ("number: 3\ndate: 02.11.2026\ntitle: Т\n\n## А\nroles: owner\nПросто текст без частей.\n", "no parts at all"),
    ("number: 3\ndate: 02.11.2026\ntitle: Т\n\n## А\nroles: owner\n**Как работает:**\n- п\n\n**Затрагивает разделы:** нет\n", "no «Что сделано»"),
    ("number: 3\ndate: 02.11.2026\ntitle: Т\n\n## А\nroles: owner\n**Что сделано:** т\n\n**Затрагивает разделы:** нет\n", "no «Как работает»"),
    ("number: 3\ndate: 02.11.2026\ntitle: Т\n\n## А\nroles: owner\n**Что сделано:** т\n\n**Как работает:**\n- п\n", "no «Затрагивает разделы»"),
    ("number: 3\ndate: 02.11.2026\ntitle: Т\n\n## А\nroles: owner\n**Как работает:**\n- п\n\n**Что сделано:** т\n\n**Затрагивает разделы:** нет\n", "parts out of order"),
    ("number: 3\ndate: 02.11.2026\ntitle: Т\n\n## А\nroles: owner\n**Что сделано:**\n\n**Как работает:**\n- п\n\n**Затрагивает разделы:** нет\n", "«Что сделано» empty"),
    ("number: 3\ndate: 02.11.2026\ntitle: Т\n\n## А\nroles: owner\n**Что сделано:** т\n\n**Как работает:**\n\n**Затрагивает разделы:** нет\n", "«Как работает» empty"),
    ("number: 3\ndate: 02.11.2026\ntitle: Т\n\n## А\nroles: owner\n**Что сделано:** т\n\n**Как работает:**\n- п\n\n**Затрагивает разделы:**\n", "«Затрагивает разделы» empty"),
    ("number: 3\ndate: 02.11.2026\ntitle: Т\n\n## А\nroles: owner\nв тексте **Что сделано:** т, **Как работает:** п, **Затрагивает разделы:** нет\n", "the words inside a line are no parts"),
    ("number: 3\ndate: 02.11.2026\ntitle: Т\n\n## А\nroles: owner\nДо частей.\n\n**Что сделано:** т\n\n**Как работает:**\n- п\n\n**Затрагивает разделы:** нет\n", "text before the parts"),
    ("number: 3\ndate: 02.11.2026\ntitle: Т\n\n## А\nroles: owner\n**Что сделано:** т\n\n**Что сделано:** ещё\n\n**Как работает:**\n- п\n\n**Затрагивает разделы:** нет\n", "a part twice"),
    ("number: 3\ndate: 02.11.2026\ntitle: Т\n\n## А\nroles: owner\n**Что сделано:** т\n\n**Как работает:** абзац, а не список\n\n**Затрагивает разделы:** нет\n", "the rules not a list"),
])
def test_a_bad_file_is_skipped_and_logged_the_rest_shows(tmp_path, folder, caplog, text, why):
    (folder / "0003.md").write_text(text, encoding="utf-8")
    (folder / "notes.md").write_text(update(4, ("А", "owner")), encoding="utf-8")       # not NNNN.md
    with caplog.at_level(logging.WARNING, logger="tires_sync.whatsnew"):
        loaded = whatsnew.load(folder)
    assert [u["number"] for u in loaded] == [2, 1], why
    assert "0003.md skipped" in caplog.text and "notes.md skipped" in caplog.text
    client = make_app(tmp_path, WHATSNEW_DIR=str(folder)).test_client()
    login(client)
    assert "Обновление панели №1" in page(client)


def test_a_file_that_is_not_text_is_skipped(folder):
    (folder / "0003.md").write_bytes(b"\xff\xfe\x00bad")
    assert [u["number"] for u in whatsnew.load(folder)] == [2, 1]


def test_the_text_is_escaped_and_only_the_small_markup_works(folder):
    (folder / "0003.md").write_text("number: 3\ndate: 02.11.2026\ntitle: <b>Т</b>\n\n## <img src=x onerror=alert(1)>\nroles: owner\n"
                                    "**Что сделано:** <script>alert(1)</script>\n\n**Как работает:**\n- раз\n- два\n\n**Затрагивает разделы:** нет\n", encoding="utf-8")
    client_app_body = str(whatsnew.load(folder)[0]["items"][0]["body"])
    assert client_app_body == ("<p><b>Что сделано:</b> &lt;script&gt;alert(1)&lt;/script&gt;</p><p><b>Как работает:</b></p><ul><li>раз</li><li>два</li></ul>"
                               "<p><b>Затрагивает разделы:</b> нет</p>")


def test_titles_are_escaped_on_the_page(tmp_path, folder):
    (folder / "0003.md").write_text(update(3, ("<img src=x onerror=alert(1)>", "owner"), title="<i>Т</i>"), encoding="utf-8")
    client = make_app(tmp_path, WHATSNEW_DIR=str(folder)).test_client()
    login(client)
    html = page(client)
    assert "<img src=x" not in html and "&lt;img src=x" in html and "<i>Т</i>" not in html


# --- who sees what -----------------------------------------------------------------------------------------------------------------------

def test_the_owner_sees_every_item_with_its_roles(app):
    client = app.test_client()
    login(client)
    html = page(client)
    assert all(t in html for t in ("Цена", "Карточка точки", "Каталог")) and html.index("№2") < html.index("№1")
    assert '<span class="badge">Клиент</span>' in html and '<span class="badge">Менеджер точки</span>' in html


def test_the_partner_sees_every_item_too(app):
    client, _conn = person(app, "partner")
    assert all(t in page(client) for t in ("Цена", "Карточка точки", "Каталог"))


def test_the_point_sees_its_items_only_and_not_an_update_without_them(app):
    pid = points.save_point(conn_of(app), {"name": "Янино", "manager_name": "А", "channel": "vk"})
    client, _conn = person(app, "point", point_id=pid)
    html = page(client)
    assert "Карточка точки" in html and "Цена" not in html and "Каталог" not in html and "№2" not in html
    assert 'class="badge"' not in html and 'data-point-nav' in html


def test_the_client_sees_the_catalog_item_only_in_its_frame_without_scripts(app):
    client, _conn = person(app, "client")
    response = client.get("/whats-new")
    html = response.get_data(as_text=True)
    assert response.status_code == 200 and "Каталог" in html and "Цена" not in html and "Карточка точки" not in html
    assert 'class="shop-top"' in html and "<script" not in html and 'class="sidebar"' not in html
    assert "script-src" not in response.headers["Content-Security-Policy"] and "default-src 'none'" in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize("role", ["chats", "viewer"])
def test_a_role_with_no_items_sees_nothing_new_and_no_dot(app, role):
    client, _conn = person(app, role)
    assert "Пока ничего нового" in page(client) and not dot_on(client, "/")


def test_working_as_a_point_shows_the_points_items_and_its_own_mark(app):
    conn = conn_of(app)
    pid = points.save_point(conn, {"name": "Янино", "manager_name": "А", "channel": "vk"})
    client = app.test_client()
    login(client)
    page(client)                                                     # read as the owner: 2
    assert post(client, "/as-point", "tok", point_id=str(pid)).status_code == 302
    assert dot_on(client, "/my-point")                               # as the point: №1 not read yet as a point
    html = page(client)
    assert "Карточка точки" in html and "Цена" not in html and "Каталог" not in html
    assert user_prefs.get(conn, "owner", whatsnew.SEEN_KEY) == {"owner": 2, "point": 1}
    assert not dot_on(client, "/my-point")


# --- the dot -------------------------------------------------------------------------------------------------------------------------------

def test_the_dot_is_on_until_the_page_is_opened_and_per_person(app, folder):
    owner = app.test_client()
    login(owner)
    partner, conn = person(app, "partner")
    assert dot_on(owner, "/") and dot_on(partner, "/")
    page(owner)
    assert not dot_on(owner, "/") and dot_on(partner, "/")          # one person's reading is not another's
    assert user_prefs.get(conn, "owner", whatsnew.SEEN_KEY) == {"owner": 2}
    (folder / "0003.md").write_text(update(3, ("Ещё", "owner")), encoding="utf-8")
    assert dot_on(owner, "/")                                        # a newer update lights it again


def test_an_update_not_for_the_role_does_not_light_the_dot(app, folder):
    pid = points.save_point(conn_of(app), {"name": "Янино", "manager_name": "А", "channel": "vk"})
    client, _conn = person(app, "point", point_id=pid)
    assert dot_on(client, "/my-point")
    page(client)
    assert not dot_on(client, "/my-point")
    (folder / "0003.md").write_text(update(3, ("Только офис", "owner, partner")), encoding="utf-8")
    assert not dot_on(client, "/my-point")


def test_the_clients_dot_in_its_frame(app):
    client, _conn = person(app, "client")
    assert dot_on(client, "/shop")
    page(client)
    assert not dot_on(client, "/shop")


def test_a_broken_seen_value_is_read_as_nothing_seen(app):
    client = app.test_client()
    login(client)
    conn = conn_of(app)
    user_prefs.put(conn, "owner", whatsnew.SEEN_KEY, ["junk"], NOW)
    assert dot_on(client, "/")
    page(client)
    assert user_prefs.get(conn, "owner", whatsnew.SEEN_KEY) == {"owner": 2} and not dot_on(client, "/")


def test_no_updates_no_dot_and_the_page_still_opens(tmp_path):
    empty = tmp_path / "none"
    empty.mkdir()
    client = make_app(tmp_path, WHATSNEW_DIR=str(empty)).test_client()
    login(client)
    assert not dot_on(client, "/") and "Пока ничего нового" in page(client)


def test_the_shipped_updates_light_the_dot_of_the_office(tmp_path):
    client = make_app(tmp_path).test_client()
    login(client)
    assert dot_on(client, "/") and "Обновление панели №1" in page(client) and not dot_on(client, "/")


def test_a_file_with_a_bom_and_windows_line_ends_reads(folder):
    (folder / "0003.md").write_bytes(("﻿" + update(3, ("А", "owner"), ("Б", "chats"))).replace("\n", "\r\n").encode("utf-8"))
    third = whatsnew.load(folder)[0]
    assert third["number"] == 3 and [i["title"] for i in third["items"]] == ["А", "Б"] and "\r" not in str(third["items"][1]["body"])


@pytest.mark.parametrize("role", ["chats", "viewer"])
def test_a_role_sees_an_item_naming_it_and_gets_the_dot(app, folder, role):
    (folder / "0003.md").write_text(update(3, ("Для офиса", "owner, chats, viewer"), ("Только владельцу", "owner")), encoding="utf-8")
    client, _conn = person(app, role)
    assert dot_on(client, "/")
    html = page(client)
    assert "Для офиса" in html and "Только владельцу" not in html and "Цена" not in html and not dot_on(client, "/")


def test_the_office_keeps_its_scripts_on_the_page(app):
    client = app.test_client()
    login(client)
    response = client.get("/whats-new")
    assert "script-src 'self'" in response.headers["Content-Security-Policy"] and "navfold.js" in response.get_data(as_text=True)


def test_a_mark_that_cannot_be_saved_still_opens_the_page(app, monkeypatch):
    def busy(*_args, **_kwargs):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(user_prefs, "put", busy)
    client = app.test_client()
    login(client)
    assert "Цена" in page(client) and dot_on(client, "/")


@pytest.mark.parametrize("state", ["off", "all"])
def test_the_client_has_the_item_and_the_dot_in_the_old_and_the_new_look(app, state):
    from tires_sync import store
    conn = conn_of(app)
    store.set_state(conn, "ui_new.client", state)
    client, _conn = person(app, "client")
    for url in ("/shop", "/shop/cart", "/shop/orders", "/shop/profile"):
        html = client.get(url).get_data(as_text=True)
        assert 'href="/whats-new"' in html and DOT in html, (state, url)
        assert ("ui-new" in html.split("<body", 1)[1].split(">", 1)[0]) == (state == "all")
    html = page(client)
    assert "Каталог" in html and "<script" not in html and not dot_on(client, "/shop")
