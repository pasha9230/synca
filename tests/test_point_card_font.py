"""The order card of «Рабочее место» (the owner 2026-11-07, lane point-card-font): the quantity, the client's name and «Итого N ₽» are of the
size, the weight and the main text colour of the tyre's name, in the new view (point2.css) and in the old one (point.css). The number on the
left, the hint, the buttons, the tags and the grey «p2-sub» of the question stay as they were."""
from __future__ import annotations

import re
from pathlib import Path

from test_point_new_view import flag, w  # noqa: F401  (the fixture)

STATIC = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static"


def rule(css: str, selector: str) -> dict:
    """The declarations of the rule whose selector list holds `selector` exactly (the last one wins, as in the browser)."""
    found = {}
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in [s.strip() for s in selectors.split(",")]:
            found.update({k.strip(): v.strip() for k, v in re.findall(r"([\w-]+)\s*:\s*([^;]+)", body)})
    return found


def same_as_tyre(css: str, tyre: str, others, text_token: str):
    looks = rule(css, tyre)
    for other in others:
        mine = rule(css, other)
        assert mine.get("font-weight") == looks.get("font-weight"), other
        assert mine.get("color") == "var(%s)" % text_token, other
        assert mine.get("font-size", "inherit") == looks.get("font-size", "inherit"), other


def test_the_new_view_draws_them_as_the_tyre():
    css = (STATIC / "point2.css").read_text(encoding="utf-8")
    assert rule(css, ".p2-g") == {"font-weight": "600", "color": "var(--p2-text)"}
    same_as_tyre(css, ".p2-g", (".p2-g .p2-q", ".p2-who", ".p2-sum"), "--p2-text")
    assert rule(css, ".p2-sum b") == {"font-weight": "inherit"}              # «Итого <b>N ₽</b>»: the sum too
    assert rule(css, ".p2-sub")["color"] == "var(--p2-text-3)"          # the question's grey line keeps its look


def test_the_old_view_draws_them_as_the_tyre():
    css = (STATIC / "point.css").read_text(encoding="utf-8")
    tyre = rule(css, ".pt .mp-tyres")
    assert tyre["font-weight"] == "500"
    for other in (".pt .mp-client .mp-name", ".pt .mp-client .mp-sum"):
        mine = rule(css, other)
        assert mine["font-weight"] == "500" and mine["color"] == "var(--pt-text)" and mine["font-size"] == "inherit", other
    assert rule(css, ".pt .mp-client .mp-sum b") == {"font-weight": "inherit"}


def test_the_new_cards_mark_the_sum_and_keep_the_rest(w):  # noqa: F811
    flag(w["conn"], "all")
    html = w["point"].get("/my-point").get_data(as_text=True)
    assert 'class="p2-sum" data-point-sum' in html and 'class="p2-sub" data-point-sum' not in html
    assert 'class="p2-who mp-name"' in html and 'class="p2-q"' in html
    assert '<span class="mp-no" data-no>' in html and 'class="p2-sub">Сегодня последний день хранения' in html


def test_the_old_cards_mark_the_sum(w):  # noqa: F811
    flag(w["conn"], "")
    html = w["point"].get("/my-point").get_data(as_text=True)
    assert "point2.css" not in html
    assert 'class="mp-sub mp-sum" data-point-sum' in html and '<span class="mp-name">' in html
