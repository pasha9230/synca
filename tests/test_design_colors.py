"""The colours of «Новый вид» (lane design-colors, the owner: «при новом дизайне ты оставил старые цвета»): a stage's colour is kept
as the old palette's code (order_stage.color) and drawn from the tokens of «Строгий» under body.ui-new (routes_design.stage_color); the
old view draws the stored code as before; office2-pages.css and point2.css carry no colour code outside their blocks of tokens."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from test_ui_new import STATIC, client_of, set_states, w  # noqa: F401  (the fixture)
from test_webmim_create import card
from tires_sync import orders
from tires_sync.panel import routes_design as design

OLD = set(design.STAGE_TOKENS)
CUSTOM = "#123456"                                     # a colour the owner chose himself in «Настройка воронки»


def old_codes(html: str) -> set:
    return {code for code in OLD if code.lower() in html.lower()}


def block(css: str, selector: str) -> str:
    found = re.search(r"(?m)^" + re.escape(selector) + r"\{(.*?)\n\}", css, re.S)
    assert found, selector
    return found.group(1)


def tokens(text: str) -> dict:
    return {name: value.strip() for name, value in re.findall(r"--([\w-]+):([^;]+);", text)}


@pytest.fixture
def board(w):  # noqa: F811
    """A card on every stage; one stage recoloured by the owner with a code of his own."""
    conn = w["conn"]
    stages = orders.list_stages(conn)
    ids = []
    for stage in stages:
        cid = card(conn, client_name="Клиент %s" % stage["code"], point_id=w["pid"])
        conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage["id"], cid))
        ids.append(cid)
    conn.execute("UPDATE order_stage SET color = ? WHERE code = 'talk'", (CUSTOM,))
    conn.execute("UPDATE order_stage SET on_board = 1")
    conn.commit()
    return dict(w, ids=ids, stages=stages)


def test_every_stored_code_has_a_token_and_the_others_are_neutral():
    stored = {s[2].upper() for s in orders.DEFAULT_STAGES} | {orders.FROM_POINT[2].upper(), "#98A2B3"}   # + the column's default
    assert stored == OLD
    for code, name in design.STAGE_TOKENS.items():
        assert design.stage_color(code, True) == "var(--stage-%s)" % name
        assert design.stage_color(code.lower(), True) == "var(--stage-%s)" % name           # <input type=color> gives lower case
        assert design.stage_color(code, False) == code                                    # the old view: as stored
    assert design.stage_color(CUSTOM, True) == "var(--stage-other)" and design.stage_color("", True) == "var(--stage-other)"
    assert design.stage_color(CUSTOM, False) == CUSTOM


def test_the_tokens_of_the_stages_in_both_palettes():
    css = (STATIC / "panel.css").read_text(encoding="utf-8")
    new, old = tokens(block(css, "body.ui-new")), tokens(block(css, ":root"))
    for code, name in design.STAGE_TOKENS.items():
        assert old["stage-" + name].upper() == code, name                                  # :root: the code itself
        assert re.fullmatch(r"#[0-9A-F]{6}", new["stage-" + name]) and new["stage-" + name] not in OLD, name   # «Строгий»: its own
    assert "stage-other" in new and "stage-other" in old
    for name in ("winter-50", "winter-200", "winter-700", "err-25"):                        # added for office2-pages.css: in both
        assert name in new and name in old


@pytest.mark.parametrize("page", ["/orders", "/orders?v=list"])
def test_the_new_view_of_the_office_draws_no_old_code(board, page):
    set_states(board, office="all")
    html = client_of(board, "owner").get(page).get_data(as_text=True)
    assert 'class="ui-new' in html or " ui-new" in html
    assert not old_codes(html) and CUSTOM.lower() not in html.lower()
    assert "var(--stage-violet)" in html and "var(--stage-other)" in html                   # «Новый заказ» and the owner's own colour


def test_the_new_view_of_the_card_draws_no_old_code(board):
    set_states(board, office="all")
    owner = client_of(board, "owner")
    for cid, stage in zip(board["ids"], board["stages"]):
        html = owner.get("/orders/%d" % cid).get_data(as_text=True)
        assert 'class="owin-stage is-' in html, stage["code"]          # the stage once, in the strip (the owner 08.11: no chip in the head)
        assert not old_codes(html), (stage["code"], old_codes(html))


def test_the_new_view_of_a_point_draws_no_old_code(board):
    set_states(board, point="all")
    html = client_of(board, "pt1").get("/orders").get_data(as_text=True)               # the point's board: its four columns
    assert " ui-new" in html and not old_codes(html)
    assert all("var(--stage-%s)" % name in html for name in ("violet", "amber", "green-dark", "red"))


@pytest.mark.parametrize("page", ["/orders", "/orders?v=list"])
def test_the_old_view_draws_the_stored_codes(board, page):
    html = client_of(board, "owner").get(page).get_data(as_text=True)
    assert " ui-new" not in html and "var(--stage-" not in html
    assert "#7F56D9" in html and CUSTOM in html and "#D92D20" in html


def test_the_old_view_of_the_card_and_the_setting_keep_the_codes(board):
    owner = client_of(board, "owner")
    assert "--stage-" not in owner.get("/orders/%d" % board["ids"][0]).get_data(as_text=True)
    set_states(board, office="all")
    page = owner.get("/orders/settings").get_data(as_text=True)                             # «Настройка воронки»: what is stored
    assert 'type="color" name="color" value="#7F56D9"' in page


@pytest.mark.parametrize("name", ["office2-pages.css", "point2.css"])
def test_no_colour_code_outside_the_blocks_of_tokens(name):
    css = re.sub(r"/\*.*?\*/", "", (STATIC / name).read_text(encoding="utf-8"), flags=re.S)
    loose = []
    for declaration in re.split(r"[;{}]", css):
        if re.search(r"#[0-9A-Fa-f]{3,8}\b", declaration) and not declaration.strip().startswith("--"):
            loose.append(declaration.strip())
    assert loose == []
    assert not re.search(r"var\(--[\w-]+,\s*#", css)                                       # no old code as a fallback either
