"""«Что делать сейчас» (the owner 2026-10-30, onboarding): the hint of the current stage on the order's card and on «Рабочее место».
The guard: every button a hint names is a button of the panel's templates (no button that is not there); every stage of the pipeline has
its hint; «Свернуть» is the person's own setting and is kept."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from panel_helpers import login, make_app
from test_roles import person
from tires_sync import onboarding_texts as ot, order_points as points, orders, store, user_prefs, users

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "templates"
NOW = "2026-10-30T12:00:00"
# the inner text of what is drawn as a button: <button>, a link styled as a button or an item of a menu, the summary of «Ещё ⋯»
PRESSABLE = re.compile(r"<(button|a|summary)\b([^>]*)>(.*?)</\1>", re.S)


CARD = ["order_card.html"] + sorted(p.name for p in TEMPLATES.glob("_order_*.html"))     # the screen of the order's card
POINT_SCREEN = ["mypoint.html"]                                                               # «Рабочее место»


def _pressable_texts(names=None) -> list:
    found = []
    for path in [TEMPLATES / n for n in names] if names else sorted(TEMPLATES.glob("*.html")):
        for tag, attrs, inner in PRESSABLE.findall(path.read_text(encoding="utf-8")):
            if tag == "a" and not re.search(r'class="[^"]*\b(btn|menu-item)\b', attrs):
                continue
            inner = re.sub(r"\{\{\s*icon\([^}]*\}\}", "", inner)
            found.append(inner)
    return found


def _matches(label: str, inner: str) -> bool:
    """`label` is the text of the button `inner`: the same words, a Jinja expression standing for any words (a stage's name), or one of
    the quoted alternatives of an expression («"Готово" if … else "Выполнить"») or of {% if %} branches."""
    if label in re.findall(r'"([^"{}%]+)"', inner):
        return True
    branches = re.split(r"\{%[^%]*%\}", inner)
    for text in branches + [re.sub(r"\{%[^%]*%\}", "", inner)]:
        parts = re.split(r"\{\{.*?\}\}", text, flags=re.S)
        if len("".join(parts).strip()) < 2:                         # a button of an expression only ({{ s.name }}): it proves nothing
            continue
        parts[0], parts[-1] = parts[0].lstrip(), parts[-1].rstrip()
        pattern = ".*?".join(re.escape(p) for p in parts)
        if re.fullmatch(pattern, label, re.S):
            return True
    return False


def test_every_button_a_hint_names_is_a_button_of_its_screen():
    """A hint of a stage names the buttons of the order's card, or those the point presses on «Рабочее место» (a button the hints of the
    point name as well); a hint of «Рабочее место» names its buttons only."""
    card, point = _pressable_texts(CARD), _pressable_texts(POINT_SCREEN)
    assert len(card) > 30 and len(point) > 5                         # the guard looks where it should
    of_point = ot.buttons_of(ot.point_texts())
    of_stages = ot.buttons_of(ot.stage_texts())
    assert len(of_stages) >= 15 and len(of_point) >= 7
    missing = sorted(b for b in of_point if not any(_matches(b, t) for t in point))
    missing += sorted(b for b in of_stages if not any(_matches(b, t) for t in (point if b in of_point else card)))
    assert missing == [], "a hint names a button its screen has not: %s" % missing


def test_the_guard_catches_a_button_that_is_not_there():
    texts = _pressable_texts()
    assert not any(_matches("Отправить на Луну", t) for t in texts)
    assert any(_matches("Перевести в «Переговоры»", t) for t in texts)      # a button with a stage's name in it
    assert any(_matches("Выполнить", t) for t in texts) and any(_matches("Сторнировать продажу", t) for t in texts)
    assert not any(_matches("Подтвердить", t) for t in _pressable_texts(POINT_SCREEN))   # a word of another screen is not found there


def test_every_stage_of_the_pipeline_has_a_hint_of_two_to_four_steps():
    codes = {d[0] for d in orders.DEFAULT_STAGES} | {orders.FROM_POINT[0]}
    assert codes <= set(ot.STAGES), sorted(codes - set(ot.STAGES))
    for code, h in ot.STAGES.items():
        assert 2 <= len(h["steps"]) <= 4, code
        assert h["auto"], code
    assert ot.stage_hint(None) is None and ot.stage_hint("my_own_stage") is None


def test_a_hint_draws_the_buttons_bold_and_escapes_the_rest():
    h = ot.stage_hint("at_point")
    html = " ".join(str(s) for s in h["steps"])
    assert '<b class="hint-btn">' in html and "[" not in html
    assert str(ot._render("<x> [Кнопка]")) == '&lt;x&gt; <b class="hint-btn">Кнопка</b>'


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def test_the_card_shows_the_hint_of_its_stage_and_folding_is_kept(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    card = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, NOW, "o")
    page = client.get("/orders/%d" % card).get_data(as_text=True)
    assert 'data-stage-hint="new"' in page and "Что делать сейчас" in page and "Свернуть" in page
    r = client.post("/orders/hint/fold", data={"csrf": tok, "where": "stage", "fold": "1", "back": "/orders/%d" % card})
    assert r.status_code == 302 and r.headers["Location"].endswith("/orders/%d" % card)
    assert user_prefs.get(conn, "owner", "hint.stage") is True
    page = client.get("/orders/%d" % card).get_data(as_text=True)
    assert 'data-stage-hint="folded"' in page and "Развернуть" in page and "Панель сама" not in page
    client.post("/orders/hint/fold", data={"csrf": tok, "where": "stage", "fold": "0", "back": "/orders/%d" % card})
    assert 'data-stage-hint="new"' in client.get("/orders/%d" % card).get_data(as_text=True)


def test_fold_goes_back_only_inside_the_panel_and_knows_its_places(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    for back in ("https://evil.example/", "//evil.example/", "/\\evil.example", "/\t/evil.example", "/logout"):
        r = client.post("/orders/hint/fold", data={"csrf": tok, "where": "stage", "fold": "1", "back": back})
        assert r.status_code == 302 and r.headers["Location"] in ("/", "http://localhost/")
    client.post("/orders/hint/fold", data={"csrf": tok, "where": "nowhere", "fold": "1", "back": "/"})
    assert user_prefs.get(conn, "owner", "hint.nowhere") is None


def test_the_point_sees_a_short_hint_on_its_cards_and_folds_it(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    alfa = points.save_point(conn, {"name": "Альфа", "manager_name": "А", "channel": "panel"})
    stages = {s["code"]: s for s in orders.list_stages(conn)}
    card = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+79001112233", "point_id": str(alfa)}, NOW, "o")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stages["at_point"]["id"], card))
    conn.commit()
    client, _ = person(app, "point", alfa)
    page = client.get("/my-point").get_data(as_text=True)
    assert 'data-point-hint="here"' in page and "Свернуть подсказки" in page
    page = client.get("/my-point?tab=here").get_data(as_text=True)
    assert 'name="back" value="/my-point?tab=here"' in page                     # back to the same tab
    r = client.post("/orders/hint/fold", data={"csrf": "tok", "where": "point", "fold": "1", "back": "/my-point"})
    assert r.status_code == 302
    page = client.get("/my-point").get_data(as_text=True)
    assert "data-point-hint=" not in page and "Показать подсказки" in page


def test_the_point_on_the_order_card_reads_about_its_own_buttons_only(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    alfa = points.save_point(conn, {"name": "Альфа", "manager_name": "А", "channel": "panel"})
    stages = {s["code"]: s for s in orders.list_stages(conn)}
    card = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+79001112233", "point_id": str(alfa)}, NOW, "o")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stages["moving"]["id"], card))
    conn.commit()
    client, _ = person(app, "point", alfa)
    page = client.get("/orders/%d" % card).get_data(as_text=True)
    assert 'data-stage-hint="moving"' in page and "Приехал" in page and "Рабочем месте" in page
    assert "Панель сама" not in page and "Заказать в МИМ" not in page


def test_a_viewer_who_changes_nothing_sees_no_steps_of_buttons_not_theirs(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    card = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, NOW, "o")
    client, _ = person(app, "viewer")
    page = client.get("/orders/%d" % card).get_data(as_text=True)
    assert "data-stage-hint" not in page and "Что делать сейчас" not in page
