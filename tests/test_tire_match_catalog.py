"""The catalog marks a position that the other supplier has too (tire_match): «есть у другого поставщика» with its price and stock."""
from __future__ import annotations

import re

from test_tochki_panel import app_with, row

BADGE = re.compile(r'<span class="badge badge--neutral" data-twins title="([^"]*)">есть у другого поставщика</span>')


def test_both_sides_of_a_pair_are_marked_with_the_other_supplier(tmp_path):
    app, client, tok, sid = app_with(tmp_path, rows=[row("T1", rest=41), row("T2", width="215", load="95")])   # T1 is WebMIM 100's tire
    html = client.get("/catalog").get_data(as_text=True)
    titles = BADGE.findall(html)
    assert len(titles) == 2                                                          # WebMIM 100 and 4tochki T1; T2 has no twin
    assert any("4 точки — цена —, остаток &gt;40" in t for t in titles)    # 4tochki before its switch: no price of ours
    assert any(re.search(r"WebMIM — [\d\s\xa0]+₽, остаток 10", t) for t in titles)


def test_the_mark_stays_on_a_tab_of_one_supplier(tmp_path):
    app, client, tok, sid = app_with(tmp_path, rows=[row("T1", rest=41)])
    only_mim = client.get("/catalog?sup=1").get_data(as_text=True)
    only_tochki = client.get("/catalog?sup=%d" % sid).get_data(as_text=True)
    assert len(BADGE.findall(only_mim)) == 1 and "4 точки" in BADGE.findall(only_mim)[0]
    assert len(BADGE.findall(only_tochki)) == 1 and "WebMIM" in BADGE.findall(only_tochki)[0]


def test_no_mark_without_a_twin(tmp_path):
    app, client, tok, sid = app_with(tmp_path, rows=[row("T1", width="225", load="98")])
    assert not BADGE.findall(client.get("/catalog").get_data(as_text=True))
