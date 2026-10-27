"""The line marker {new_only}: a line that claims the tires are new of this year's production is not shown for a position whose
supplier name carries the age note («старше 3-х лет», feed.AGE_NOTE); for every other position the line stays byte for byte
(owner, 2026-10-26)."""
from __future__ import annotations

from datetime import date

from helpers import make_offer, make_settings
from tires_sync.candidates import Candidate
from tires_sync.description import ALLOWED, PLACEHOLDER_HELP, render_description, validate_template
from tires_sync.feed import render_ad

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
    t = "Шины {title}\nГод: 2025-2026{new_only} (свежие)\nКонец"
    assert render_description(t, cand(), 100) == "Шины Pirelli 265/55/19 V 109 Scorpion Winter (MO)\nГод: 2025-2026 (свежие)\nКонец"
    assert render_description(t, cand(AGED[0]), 100) == "Шины %s\nКонец" % AGED[0]


def test_marker_after_a_space_renders_as_the_unmarked_line():
    assert render_description(TEMPLATE % (LINE + " {new_only}"), cand(), 13370) == render_description(PLAIN, cand(), 13370)


def test_marker_with_an_empty_old_model_still_drops_the_line_for_everyone():
    t = "Шины {title}\nПреемник: {old_model}{new_only}\nКонец"
    assert render_description(t, cand(), 100) == render_description(t, cand(AGED[0]), 100).replace(AGED[0], cand().offer.name_orig)


def test_set_of_four_in_the_feed_follows_the_same_rule():
    for name, shown in ((AGED[0], False), ("Pirelli 265/55/19 V 109 Scorpion Winter (MO)", True)):
        ad = render_ad(cand(name), 13370, "1", make_settings(), date(2026, 10, 26), template=MARKED, pack=4)
        assert ("2025-2026" in ad.find("Description").text) is shown


# --- {old_model} prints «Прежнее название шин» itself (owner, 2026-10-27) ------------------------------------------------------------

FORMER = {("Pirelli", "Scorpion Winter"): "Nokian Hakkapeliitta 9 SUV"}


def test_old_model_alone_on_a_line_gets_the_words():
    text = render_description("Шины\n{old_model}\nКонец", cand(), 100, FORMER)
    assert text == "Шины\nПрежнее название шин Nokian Hakkapeliitta 9 SUV\nКонец"


def test_old_model_with_own_words_before_it_is_not_doubled():
    for before in ("Прежнее название шин ", "прежнее  название   ШИН ", "Прежнее название шин: ", "Прежнее название шин — "):
        text = render_description("Шины\n%s{old_model}\nКонец" % before, cand(), 100, FORMER)
        assert text.lower().count("название") == 1, before
        assert text.endswith("Nokian Hakkapeliitta 9 SUV\nКонец") and "шин Прежнее" not in text, before


def test_other_words_before_it_still_get_the_prefix():
    assert "Раньше: Прежнее название шин Nokian Hakkapeliitta 9 SUV" in render_description("Раньше: {old_model}", cand(), 100, FORMER)


def test_old_model_without_a_former_name_drops_the_line():
    for t in ("Шины\n{old_model}\nКонец", "Шины\nПрежнее название шин {old_model}\nКонец"):
        assert render_description(t, cand(), 100) == "Шины\nКонец"


def test_old_model_with_new_only_on_the_same_line():
    t = "Шины\n{old_model}{new_only}\nКонец"
    assert render_description(t, cand(), 100, FORMER) == "Шины\nПрежнее название шин Nokian Hakkapeliitta 9 SUV\nКонец"
    aged = render_description(t, cand(AGED[0]), 100, FORMER)
    assert "Nokian" not in aged and aged == "Шины\nКонец"
    t2 = "Шины\nПрежнее название шин {old_model} {new_only}\nКонец"
    assert render_description(t2, cand(), 100, FORMER) == "Шины\nПрежнее название шин Nokian Hakkapeliitta 9 SUV\nКонец"


def test_help_text_mentions_the_words():
    assert "Прежнее название шин" in PLACEHOLDER_HELP["old_model"]
