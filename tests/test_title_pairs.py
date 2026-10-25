"""The shortening of a title to 70 chars (`feed.compose_title`).

The golden file holds every supplier name of the live data of 2026-10-25 (WebMIM unload and the 4tochki snapshot) whose title was shortened on that day,
with the title it had then for one tire and for a set of 4: the feeds on that day's data stay byte for byte (taken before the change, run
`feed-tails` of 2026-10-25-tails3). Known half pairs of that day are pinned as they were on purpose (the owner decides on them separately):
«… (не для дорог общего в наличии». Changed on purpose (run title-age, owner 2026-10-25): the 18 names with the supplier's age note
(«старше 3-х лет», «старше 5-ти лет») have it no longer in the title, so «… (BMW) старше в наличии» is gone."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tires_sync.feed import compose_title

GOLDEN = json.loads(Path(__file__).with_name("test_title_pairs_golden.json").read_text(encoding="utf-8"))


def test_the_golden_holds_the_shortened_names_of_the_day():
    assert len(GOLDEN) == 28 and all(len(row) == 3 for row in GOLDEN)


@pytest.mark.parametrize("name, one, set_of_4", GOLDEN, ids=[str(i) for i in range(len(GOLDEN))])
def test_the_titles_of_the_day_stay_byte_for_byte(name, one, set_of_4):
    assert compose_title(name) == one
    assert compose_title(name, "", tail="комплект 4 шт.") == set_of_4


HEAD = "Ikon 205/55/16 Character "                                          # no indexes and no XL: the shortening goes straight to the words


def one_over(pair: str) -> str:
    """A name whose title is one char over the limit: without the pair's last word it would fit."""
    rest = len("Ikon 205 55 R16 Character " + " " + pair + " в наличии")
    return HEAD + "W" * (71 - rest) + " " + pair


@pytest.mark.parametrize("pair", ["Шипы: нет", "Шипы : нет", "Шипы — нет", "Шипы - нет", "Шипы нет", "шипы: да", "Шип есть",
                                  "без шипов", "под шип", "не шип.", "Ошиповка: да", "Шипы :нет", "Шипы -нет", "Шипы —нет", "Шипы- нет",
                                  "Шипы– нет", "(Шипы нет)", "Шипы нет)", "Шипы нет!", "«Шипы нет»", "(Шипы: нет)", "(без шипов)",
                                  "Шипы: нет.", "Шипы: Нет,"])
def test_the_shortening_takes_a_feature_and_its_value_off_together(pair):
    name = one_over(pair)
    assert len(compose_title(name, limit=10_000)) == 71
    got = compose_title(name)
    assert got == "Ikon 205 55 R16 Character %s в наличии" % ("W" * (71 - len("Ikon 205 55 R16 Character  %s в наличии" % pair))), got


@pytest.mark.parametrize("pair", ["Шипы: нет", "Шипы — нет", "без шипов"])
def test_a_pair_that_fits_stays_whole(pair):
    name = HEAD + "Ice 7 " + pair
    assert compose_title(name) == "Ikon 205 55 R16 Character Ice 7 %s в наличии" % pair


def test_ordinary_words_still_go_one_by_one():
    name = one_over("Ice 7")
    assert compose_title(name).endswith(" Ice в наличии")                  # «7» is not glued to «Ice»: only the last word goes
    over_by_6 = one_over("Ice 7 Nord").replace("W", "WWWWWW", 1)
    assert compose_title(over_by_6).endswith("W Ice в наличии")            # «Nord» and «7» go one by one, «Ice» stays


def test_a_long_non_studded_name_with_studs_no_does_not_stop_the_feed():
    """The case of the tail: «Шипы: нет» cut to «Шипы» reads as studs on a «Зимние нешипованные» ad and `studs_conflict` stops the feed."""
    from datetime import date

    from helpers import make_offer, make_settings
    from test_feed_check import ctx
    from tires_sync import feed_check
    from tires_sync.candidates import Candidate
    from tires_sync.feed import render_ad
    from tires_sync.webmim import studded_in_text

    name = one_over("Шипы: нет")
    assert studded_in_text(name) is False
    offer = make_offer(goods_id="100", stock=12, name=name, name_orig=name, studded=False)
    node = render_ad(Candidate("100", offer, 4810, "Ikon Tyres", "Test Model"), 4810, "P1", make_settings(), date(2026, 10, 14))
    assert node.findtext("TireType") == "Зимние нешипованные" and "Шип" not in node.findtext("Title")
    assert [f for f in feed_check.check_feed([node], ctx()) if f.rule == "studs_conflict"] == []
    node.find("Title").text = node.findtext("Title").replace(" в наличии", " Шипы: в наличии")     # what the old shortening left
    assert [f.rule for f in feed_check.check_feed([node], ctx()) if f.rule == "studs_conflict"] == ["studs_conflict"]


def test_a_lone_separator_never_takes_the_size_nor_a_whole_list():
    assert compose_title("Ikon 205/55/16 - " + "W" * 60) == "Ikon 205 55 R16 в наличии"          # the size stays, no «-» hangs
    listed = compose_title(HEAD + "W" * 25 + " A - B - C")                                       # one over: «C» alone would do
    assert listed == "Ikon 205 55 R16 Character %s A в наличии" % ("W" * 25), listed           # «B - C» as one pair, no «-» left


def test_the_shortening_of_the_day_is_checked_against_the_studs_rule_at_every_cut():
    """A non-studded name with a negation pair, cut at every place: no title keeps a «шип…» word the rule would read as studs."""
    from tires_sync.feed_check import STUDS_WORD
    from tires_sync.webmim import NOT_STUDDED, studded_in_text

    for pair in ("Шипы: нет", "Шипы — нет", "Шипы -нет", "(Шипы нет)", "без шипов", "под шип", "Шипы: нет)"):
        for pad in range(60):
            for after in ("", " XL", " старше 3-х лет"):
                name = HEAD + "W" * pad + " " + pair + after
                assert not studded_in_text(name)
                for tail in ("", "комплект 4 шт."):
                    title = compose_title(name, tail=tail)
                    assert not STUDS_WORD.search(NOT_STUDDED.sub(" ", title)), (name, title)
