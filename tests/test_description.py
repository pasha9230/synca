from __future__ import annotations

from datetime import date

from helpers import make_offer, make_settings
from tires_sync.candidates import Candidate
from tires_sync.description import (
    ALLOWED, DEFAULT_TEMPLATE, MAX_LENGTH, PLACEHOLDER_HELP, render_description, search_variants, validate_template,
)
from tires_sync.feed import render_ad


def cand(**offer_kw):
    offer = make_offer(**offer_kw)
    return Candidate(offer.goods_id, offer, 4810, "Ikon Tyres", "Test Model")


def test_search_variants_match_the_owners_previous_ads():
    assert search_variants(205, 55, 16) == (
        "205-55-16, 205/55/16, 205 55 16, 205 55 R16, 205-55r16, 205/55/R16, 20555r16, 205/55 R16, "
        "205 55 Р16, 205-55-Р16, 205/55/Р16, 20555р16, 205/55R16")


def test_default_template_renders_every_known_field():
    text = render_description(DEFAULT_TEMPLATE, cand(country="ТАИЛАНД"), 4810)
    assert "Ikon  205/55/16  T 94 Test Model" in text
    assert "Ikon Tyres / Айкон Тайерс" in text
    assert "Модель: Test Model" in text and "Размер: 205/55 R16" in text and "Сезон: зимняя" in text
    assert "Артикул: TS1" in text and "▸ Страна происхождения бренда: ТАИЛАНД" in text and "Страна производства" not in text   # owner 2026-10-21
    assert "Цена: 4810 руб. за 1 шт." in text
    assert "Для поиска: 205-55-16, 205/55/16" in text
    assert "{" not in text


def test_lines_with_an_empty_value_are_dropped_and_blank_runs_collapse():
    text = render_description(DEFAULT_TEMPLATE, cand(country=""), 4810)
    assert "Страна происхождения бренда" not in text
    assert "\n\n\n" not in text and not text.startswith("\n") and not text.endswith("\n")


def test_unknown_brand_has_no_russian_name_but_the_line_stays():
    offer = make_offer(brand_raw="Laufenn", brand_key="LAUFENN")
    c = Candidate("100", offer, 4810, "Laufenn", "G Fit")
    text = render_description("НОВЫЕ ШИНЫ {brand_full} для авто", c, 4810)
    assert text == "НОВЫЕ ШИНЫ Laufenn для авто"


def test_season_words_and_indexes():
    from tires_sync.models import Season
    summer = render_description("{season}|{indexes}", cand(season=Season.SUMMER), 1)
    assert summer == "летняя|T 94"
    assert render_description("{season}", cand(season=Season.ALL), 1) == "всесезонная"


def test_validation_reports_unknown_placeholders_empty_and_too_long_templates():
    assert validate_template(DEFAULT_TEMPLATE) == []
    assert any("{nope}" in e for e in validate_template("Привет {nope}"))
    assert validate_template("   ") != []
    assert any("символов" in e for e in validate_template("x" * (MAX_LENGTH + 1)))
    assert validate_template("Цена {price} и {price}") == []
    assert set(ALLOWED) >= {"title", "brand_full", "model", "size", "season", "code", "country", "price", "search"}


def test_literal_braces_that_are_not_placeholders_are_kept():
    assert render_description("a {1, 2} b {code}", cand(), 1) == "a {1, 2} b TS1"


def test_feed_uses_the_template_given_and_the_default_otherwise():
    settings = make_settings()
    ad = render_ad(cand(), 4810, "P1", settings, date(2026, 10, 10), template="Артикул {code}")
    assert ad.findtext("Description") == "Артикул TS1"
    default = render_ad(cand(), 4810, "P1", settings, date(2026, 10, 10))
    assert "Для поиска:" in default.findtext("Description")


def test_control_characters_are_rejected_in_templates_and_stripped_from_feed_values():
    import xml.etree.ElementTree as ET
    from tires_sync.feed import assemble_feed

    assert any("управляющ" in e for e in validate_template("a\x01b {title}"))
    assert any("управляющ" in e for e in validate_template("мягкий\x0bперенос"))
    assert validate_template("строка\nстрока\tтаб") == []
    ad = render_ad(cand(name_orig="Ikon\x0b205/55\x01"), 4810, "P1", make_settings(), date(2026, 10, 10))
    parsed = ET.fromstring(assemble_feed([ad]))
    assert parsed.findtext("Ad/Title") == "Ikon205/55 в наличии"
    assert "\x0b" not in parsed.findtext("Ad/Description")


def test_stock_placeholder_shows_the_exact_supplier_stock_only_in_the_description():
    from tires_sync.description import validate_template
    c = Candidate("100", make_offer(stock=12), 4810, "Ikon Tyres", "Test Model")
    assert validate_template("В наличии: {stock} шт.") == []
    ad = render_ad(c, 4810, "P1", make_settings(), date(2026, 10, 6), template="В наличии: {stock} шт.")
    assert ad.findtext("Description") == "В наличии: 12 шт."
    assert ad.find("Quantity") is None


def test_the_russian_title_and_the_known_russian_brands_are_available():
    from helpers import make_offer
    from tires_sync.candidates import Candidate
    from tires_sync.description import BRAND_RU, render_description, validate_template
    cand = Candidate("1", make_offer(goods_id="1", name="Гиславед  185/55/15  T 86 SOFT FROST 200  XL", name_orig="Gislaved  185/55/15  T 86 SOFT FROST 200  XL"),
                     5340, "Gislaved", "Soft Frost 200")
    assert validate_template("🔥 {title_ru}\n▸ НОВЫЕ ШИНЫ {brand_full} для авто") == []
    assert render_description("🔥 {title_ru}\n▸ НОВЫЕ ШИНЫ {brand_ru} для авто", cand, 5340) == "🔥 Гиславед  185/55/15  T 86 SOFT FROST 200  XL\n▸ НОВЫЕ ШИНЫ Гиславед для авто"
    assert BRAND_RU["Cordiant"] == "Кордиант" and BRAND_RU["Pirelli"] == "Пирелли" and BRAND_RU["Nexen"] == "Нексен"


def test_a_brand_written_whole_the_way_the_owner_writes_it():
    from helpers import make_offer
    from tires_sync.candidates import Candidate
    from tires_sync.description import render_description
    for brand, want in (("Gislaved", "Гиславед"), ("Formula", "Pirelli Formula / Пирелли Формула"), ("Cordiant", "Cordiant / Кордиант")):
        cand = Candidate("1", make_offer(goods_id="1"), 5000, brand, "M")
        assert render_description("{brand_full}", cand, 5000) == want


def test_the_old_and_the_new_label_of_the_country_both_render_the_suppliers_country():
    for label in ("Производитель", "Страна происхождения бренда"):                          # account 1 keeps its own text until the owner edits it
        assert render_description("%s: {country}" % label, cand(country="КОРЕЯ"), 1) == "%s: КОРЕЯ" % label
    assert PLACEHOLDER_HELP["country"] == "страна происхождения бренда (из выгрузки поставщика)"
