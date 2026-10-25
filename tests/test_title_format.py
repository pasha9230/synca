"""The title of an ad (owner's format of 2026-10-14): size «185 55 R15», no «Ш.», no country, the brand not repeated, «в наличии» at the end."""
from __future__ import annotations

import pytest

from tires_sync.feed import compose_title


@pytest.mark.parametrize("name, want", [
    ("Ikon Tyres  185/55/15  T 86 Ikon Character Ice 5  XL", "Ikon Tyres 185 55 R15 T 86 Character Ice 5 XL в наличии"),
    ("Kumho  215/60/17  T 100 WI32  XL Ш. CN", "Kumho 215 60 R17 T 100 WI32 XL в наличии"),
    ("Kumho 225/60/18 T 104 WI32 XL Ш. KOREA", "Kumho 225 60 R18 T 104 WI32 XL в наличии"),
    ("Pirelli 195/65/15 T 95 WINTER ICE ZERO FR XL", "Pirelli 195 65 R15 T 95 WINTER ICE ZERO FR XL в наличии"),
    ("Gislaved  185/55/15  T 86 SOFT FROST 200  XL", "Gislaved 185 55 R15 T 86 SOFT FROST 200 XL в наличии"),
    ("Yokohama 205/55/R16 V 91 BluEarth", "Yokohama 205 55 R16 V 91 BluEarth в наличии"),
    ("Pirelli Formula 205/60/16 T 96 Formula Ice XL Ш.", "Pirelli Formula 205 60 R16 T 96 Ice XL в наличии"),
])
def test_the_title_follows_the_owner_s_format(name, want):
    got = compose_title(name, "Корея")                                   # the country is not written any more
    assert got == want and len(got) <= 70 and "Ш" not in got and "/" not in got


def test_a_long_name_loses_marks_then_indexes_then_words_but_keeps_the_tail():
    got = compose_title("Pirelli 315/30/22 H 107 SCORPION ICE ZERO 2 XL NEW SEASON EDITION SPECIAL Ш.", "")
    assert len(got) <= 70 and got.endswith("в наличии") and got.startswith("Pirelli 315 30 R22")


def test_the_size_is_written_with_spaces_and_r_only_once():
    assert compose_title("Ikon 205/55/16 T 94 Character", "").startswith("Ikon 205 55 R16 T 94")
    assert "RR" not in compose_title("Ikon 205/55/R16 T 94", "")
