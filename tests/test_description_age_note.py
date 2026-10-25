"""The line marker {new_only}: a line that claims the tires are new of this year's production is not shown for a position whose
supplier name carries the age note («старше 3-х лет», feed.AGE_NOTE); for every other position the line stays byte for byte
(owner, 2026-10-26)."""
from __future__ import annotations

from helpers import make_offer
from tires_sync.candidates import Candidate
from tires_sync.description import ALLOWED, PLACEHOLDER_HELP, render_description, validate_template

# the shape of account 1's template (its year line spelled with Latin «o» and «p», as the owner typed it)
LINE = "Все позиции нoвые: 2025-2026 года пpoизводства"
TEMPLATE = """В продаже новые шины {title}

Шины {brand_full}, модель {model}
Размер: {size} ({indexes})
Артикул: {code}

%s
❤️ Добавьте объявление в избранное, чтобы не потерять

Как купить:
— напишите в чат Авито

Варианты написания размера: {search}"""
MARKED = TEMPLATE % (LINE + "{new_only}")
PLAIN = TEMPLATE % LINE


def cand(name="Pirelli 265/55/19 V 109 Scorpion Winter (MO)", name_ru=None):
    offer = make_offer(name=name_ru if name_ru is not None else name, name_orig=name)
    return Candidate(offer.goods_id, offer, 4810, "Pirelli", "Scorpion Winter")


AGED = ["Pirelli  265/55/19  V 109 Scorpion Winter  (MO)  старше 3-х лет",
        "Pirelli  275/40/20  V 106 WSZ s3  XL Run Flat (BMW)  старше 5-ти лет"]


def test_marker_is_allowed_and_explained():
    assert "new_only" in ALLOWED and "старше" in PLACEHOLDER_HELP["new_only"]
    assert validate_template(MARKED) == []


def test_without_the_marker_nothing_changes_for_any_position():
    for name in AGED + ["Pirelli 265/55/19 V 109 Scorpion Winter (MO)"]:
        assert LINE in render_description(PLAIN, cand(name), 13370)


def test_marked_line_is_byte_for_byte_for_a_position_without_the_note():
    c = cand()
    assert render_description(MARKED, c, 13370) == render_description(PLAIN, c, 13370)


def test_marked_line_is_dropped_for_an_aged_position():
    for name in AGED:
        text = render_description(MARKED, cand(name), 13370)
        assert "2025-2026" not in text and "{new_only}" not in text
        assert render_description(PLAIN, cand(name), 13370).replace(LINE + "\n", "") == text
        assert "Артикул: TS1\n\n❤️" in text and "\n\n\n" not in text


def test_note_in_the_russian_name_only_also_counts():
    c = cand("Pirelli 265/55/19 V 109 Scorpion Winter (MO)", name_ru="Пирелли 265/55/19 V 109 Scorpion Winter (MO) старше 3-х лет")
    assert "2025-2026" not in render_description(MARKED, c, 13370)


def test_marker_mid_line_is_removed_and_other_lines_stay():
    t = "Шины {title}\nГод: 2025-2026 {new_only}(свежие)\nКонец"
    assert render_description(t, cand(), 100) == "Шины Pirelli 265/55/19 V 109 Scorpion Winter (MO)\nГод: 2025-2026 (свежие)\nКонец"
    assert render_description(t, cand(AGED[0]), 100) == "Шины %s\nКонец" % AGED[0]
