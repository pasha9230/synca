"""The section of the point is «Рабочее место» (the owner): no text of the panel names it «Моя точка» any more (its address stays /my-point)."""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "tires_sync"
OLD = re.compile(r"Мо(я|ей|ю) точк", re.IGNORECASE)


def test_no_text_of_the_panel_names_the_section_moya_tochka():
    found = [str(p.relative_to(SRC)) for p in sorted(SRC.rglob("*")) if p.suffix in (".py", ".html", ".js", ".css") and OLD.search(p.read_text(encoding="utf-8"))]
    assert found == []
    assert (SRC / "panel" / "templates").is_dir() and (SRC / "panel" / "texts.py").is_file()       # the guard looks where it should
