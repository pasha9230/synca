"""One set of stud rules (run «match3»): the feed (webmim.studded_in_text), the doubtful positions of tire_match and the line a client
waits for read from a chat title (order_wait.line_from_title) all read studs by webmim.studs_named."""
from __future__ import annotations

import itertools
import re
from dataclasses import replace

import pytest

from test_tire_match import mim, mim_offers, tochki_offers, tochki_row
from tires_sync import order_wait, tire_match, webmim

# the rule of the feed before run «match3», word for word: the feed must read every text as it did
_OLD_NOT_STUDDED = re.compile(r"(?<![а-яё])(?:(?:не|без|нет|под)\s*шип[а-яё]*|шипуем[а-яё]*|шипы?\s*[:\-–—]?\s*нет(?![а-яё]))\.?", re.I)


def old_studded_in_text(text):
    rest = _OLD_NOT_STUDDED.sub(" ", (text or "").lower())
    return "шип" in rest or "ш." in rest


PIECES = ["", " ", ".", "Ш.", "ш", "шип", "шип.", "нешип", "нешип.", "не шип", "без шипов", "под шип", "шипуемая", "Шипы: нет", "нет шипов",
          "липучка", "липучка.", "липучкаш.", "фрикцион", "фрикционная", "friction", "Friction.", "Внешипованная", "зимние", "(", ")", "XL"]


def test_the_feed_reads_every_text_as_before():
    """«липучка», «фрикцион», «friction» joined the negations: they hold no «шип» nor «ш.», so the feed's answer never changes."""
    checked = 0
    for a, b, c in itertools.product(PIECES, repeat=3):
        for text in (a + b + c, " ".join((a, b, c)), "IKON 205/55/16 Test %s %s %s" % (a, b, c)):
            assert webmim.studded_in_text(text) is old_studded_in_text(text), text
            checked += 1
    assert checked > 50000


@pytest.mark.parametrize("text, said", [
    ("IKON Test Ш.", True), ("шипованная", True), ("Внешипованная", True), ("нешип. Ш.", True),         # the mark «Ш.» wins
    ("нешип.", False), ("без шипов", False), ("под шип", False), ("Шипуемая", False), ("Шипы: нет", False), ("нет шипов", False),
    ("липучка", False), ("Фрикционная", False), ("friction", False),
    ("IKON Test", None), ("", None), (None, None), ("зимние 205/55 R16", None),
])
def test_studs_named_says_studs_no_studs_or_nothing(text, said):
    assert webmim.studs_named(text) is said


def test_tire_match_reads_the_name_by_the_one_rule():
    tochki = tochki_offers(tochki_row())
    for words in ("под шип", "шипуемая", "Шипы: нет", "липучка"):                    # no studs in the fields, «no studs» in the name: no doubt
        name = "Ikon Tyres 155/80/13 T 79 Ikon Character Ice 7 %s" % words
        offers = mim_offers(mim(name=name))
        assert offers["100"].studded is False
        assert "100" not in tire_match.pairs({**offers, **tochki}).doubtful, words
    made_for_studs = tochki_offers(tochki_row(name="IKON 155/80R13 79T Character Ice 7 (под шип)", thorn=False))
    assert tire_match.pairs(made_for_studs).doubtful == {}                        # was «в названии шипы, в полях без шипов»
    plain = mim_offers(mim(name="Ikon Tyres 155/80/13 T 79 Ikon Character Ice 7"))["100"]
    say_no = replace(plain, studded=True, name="Ikon Tyres 155/80/13 T 79 Ikon Character Ice 7 под шип")
    assert "без шипов" in tire_match.pairs({"100": say_no}).doubtful["100"]               # the fields say studs, the name «под шип»
    say_yes = replace(plain, studded=False, name="Ikon Tyres 155/80/13 T 79 Ikon Character Ice 7 нешип. Ш.")
    assert "в названии шипы" in tire_match.pairs({"100": say_yes}).doubtful["100"]         # the mark «Ш.» wins here as in the feed
    nothing = replace(plain, studded=False)
    assert tire_match.pairs({"100": nothing}).doubtful == {}


@pytest.mark.parametrize("title, studded", [
    ("Шины 205/55 R16 шипованные", "yes"), ("Шины 205/55 R16 Ш.", "yes"), ("Шины Внешипованная 205/55 R16", "yes"),
    ("Шины 205/55 R16 нешип. Ш.", "yes"),
    ("Шины 205/55 R16 нешипованная", "no"), ("Шины 205/55 R16 под шип", "no"), ("Шины 205/55 R16 шипуемая", "no"),
    ("Шины 205/55 R16 Шипы: нет", "no"), ("Шины 205/55 R16 липучка", "no"), ("Шины 205/55 R16 Friction", "no"),
    ("Шины 205/55 R16", None),
])
def test_the_title_of_a_chat_reads_studs_by_the_one_rule(title, studded):
    line = order_wait.line_from_title(title, [])
    assert line.get("studded") == studded
