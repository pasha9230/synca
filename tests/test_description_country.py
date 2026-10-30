from __future__ import annotations

from datetime import date

import pytest

from helpers import make_offer, make_settings
from tires_sync.candidates import Candidate
from tires_sync.description import (
    ALLOWED, DEFAULT_TEMPLATE, PLACEHOLDER_HELP, brand_country, render_description, validate_template,
)
from tires_sync.feed import render_ad

MADE_IN_LINE = "{made_in}"                      # the whole line (the owner 2026-10-30)
MADE_IN = "Страна производства: КНР"


def kumho(name_orig="Kumho 205/55/16 H 91 WI51", country="КОРЕЯ, РЕСПУБЛИКА", **kw):
    offer = make_offer(name_orig=name_orig, brand_raw="Kumho", brand_key="KUMHO", country=country, **kw)
    return Candidate(offer.goods_id, offer, 4810, "Kumho", "WinterCraft WI51")


def brand(avito_brand, brand_key, country=""):
    offer = make_offer(brand_raw=avito_brand, brand_key=brand_key, country=country)
    return Candidate(offer.goods_id, offer, 4810, avito_brand, "Model")


# {made_in}: the country of production, the whole line «Страна производства: КНР» when the supplier's name ends with «CN»
# (owner, 2026-10-27; a whole line since 2026-10-30)

def test_cn_at_the_end_of_the_suppliers_name_prints_the_made_in_line():
    text = render_description(MADE_IN_LINE, kumho("Kumho 205/55/16 H 91 WI51 CN"), 4810)
    assert text == MADE_IN


def test_the_line_may_carry_its_own_mark_before_it():
    assert render_description("▸ {made_in}", kumho("Kumho 205/55/16 H 91 WI51 CN"), 4810) == "▸ " + MADE_IN


@pytest.mark.parametrize("name", ["Kumho 205/55/16 H 91 WI51 CN ", "Kumho 205/55/16 H 91 WI51  CN", "Kumho 205/55/16 H 91 WI51 (CN)"])
def test_cn_word_with_spaces_or_brackets_around_it_still_counts(name):
    assert render_description(MADE_IN_LINE, kumho(name), 4810) == MADE_IN


def test_cn_before_the_suppliers_age_note_still_counts():
    name = "Kumho 215/60/17 T 100 WI32 XL Ш. CN старше 3-х лет"       # the age note stands after the origin code
    assert render_description(MADE_IN_LINE, kumho(name), 4810) == MADE_IN


@pytest.mark.parametrize("name", [
    "Kumho 205/55/16 H 91 WI51",                 # no mark
    "Kumho 205/55/16 H 91 CN WI51",              # CN not at the end
    "Kumho 205/55/16 H 91 WI51CN",               # part of another word
    "Kumho 205/55/16 H 91 WI51 cn",              # lower case is not the supplier's mark
])
def test_no_cn_word_at_the_end_drops_the_made_in_line(name):
    text = render_description("Шина\n" + MADE_IN_LINE + "\nКонец", kumho(name), 4810)
    assert text == "Шина\nКонец"


def test_brand_country_stays_the_brands_own_for_a_tire_made_in_china():
    text = render_description("▸ Страна происхождения бренда: {country}\n" + MADE_IN_LINE, kumho("Kumho 205/55/16 H 91 WI51 CN"), 4810)
    assert text == "▸ Страна происхождения бренда: Корея\n" + MADE_IN


def test_made_in_is_a_known_placeholder_with_help_and_not_in_the_default_template():
    assert "made_in" in ALLOWED
    assert MADE_IN in PLACEHOLDER_HELP["made_in"] and "CN" in PLACEHOLDER_HELP["made_in"] and "целиком" in PLACEHOLDER_HELP["made_in"]
    assert validate_template(MADE_IN_LINE) == []
    assert "{made_in}" not in DEFAULT_TEMPLATE              # the owner adds the line to his templates himself


# {country}: the supplier's uppercase names in a human form

@pytest.mark.parametrize("raw, shown", [
    ("КИТАЙ", "КНР"), ("КОРЕЯ", "Корея"), ("КОРЕЯ, РЕСПУБЛИКА", "Корея"), ("СОЕДИНЕННЫЕ ШТАТЫ АМЕРИКИ", "США"),
    ("РОССИЯ", "Россия"), ("ИТАЛИЯ", "Италия"), ("ГЕРМАНИЯ", "Германия"), ("ЯПОНИЯ", "Япония"), ("ТАИЛАНД", "Таиланд"),
    ("БЕЛАРУСЬ", "Беларусь"), ("ФРАНЦИЯ", "Франция"), ("ВЕНГРИЯ", "Венгрия"),
    (" корея, республика ", "Корея"), ("Соединенные Штаты Америки", "США"),   # spaces and case of the supplier's text do not matter
    ("ФИНЛЯНДИЯ", "Финляндия"), ("ЮЖНАЯ АФРИКА", "Южная Африка"),            # anything else: the supplier's words in title case
])
def test_supplier_country_names_are_shown_as_normal_words(raw, shown):
    c = brand("Laufenn", "LAUFENN", raw)
    assert brand_country(c) == shown
    assert render_description("▸ Страна происхождения бренда: {country}", c, 1) == "▸ Страна происхождения бренда: " + shown


# brand overrides: the owner's word and brands with no country in the unload

@pytest.mark.parametrize("avito_brand, key, raw, shown", [
    ("Gislaved", "GISLAVED", "ГЕРМАНИЯ", "Россия"),     # the owner's word wins over the supplier's value
    ("Gislaved", "GISLAVED", "", "Россия"),
    ("BARS", "BARS", "", "Россия"),
    ("Haida", "HAIDA", "", "КНР"),
    ("Attar", "ATTAR", "", "Казахстан"),
    ("PACE", "PACE", "", "КНР"),
    ("Pace", "S2:PACE", "", "КНР"),                    # other case of the brand
    ("Хайда", "S2:HAIDA", "", "КНР"),                  # the brand is known by another supplier's key only
])
def test_brand_overrides(avito_brand, key, raw, shown):
    assert brand_country(brand(avito_brand, key, raw)) == shown


def test_brand_with_no_country_and_no_override_drops_the_line():
    c = brand("Satoya", "SATOYA", "")
    assert brand_country(c) == ""
    assert render_description("А\n▸ Страна происхождения бренда: {country}\nБ", c, 1) == "А\nБ"


# titles of live ads do not change

@pytest.mark.parametrize("c, title", [
    (kumho("Kumho 205/55/16 H 91 WI51 CN"), "Kumho 205 55 R16 H 91 WI51 в наличии"),
    (brand("Gislaved", "GISLAVED", "ГЕРМАНИЯ"), "Ikon 205 55 R16 T 94 Test Model в наличии"),
    (brand("Laufenn", "LAUFENN", "КИТАЙ"), "Ikon 205 55 R16 T 94 Test Model в наличии"),
])
def test_titles_do_not_change(c, title):
    settings, today = make_settings(), date(2026, 10, 27)
    assert render_ad(c, 4810, "Москва", settings, today).findtext("Title") == title          # no country, the origin code is cut as before
    own = render_ad(c, 4810, "Москва", settings, today, title_template="{brand} {country}").findtext("Title")
    assert own == "%s %s" % (c.avito_brand, c.offer.country.title())        # the title template keeps the supplier's text, no mapping
