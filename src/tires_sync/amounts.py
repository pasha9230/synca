"""A money amount typed by a person: «19 240», «19 240,50», «19240.5 ₽», «1.500», «19,240.50»."""
from __future__ import annotations

import re

_SPACES = re.compile(r"[\s  ₽р.]*$")                          # nothing to strip inside the number except spaces and the sign
_THOUSANDS = re.compile(r"^\d{1,3}([.,]\d{3})+$")


def parse_amount(text) -> float:
    """The number in `text`. Spaces of every kind and a ruble sign are ignored. When both a point and a comma are present the last one is the
    decimal mark. A single separator followed by exactly three digits is a thousands separator («1.500» is 1500, not 1.5: kopecks have two digits),
    except after a leading zero («0.125»). Raises ValueError when there is no number."""
    cleaned = re.sub(r"[\s  ₽]", "", str(text if text is not None else ""))
    cleaned = re.sub(r"(руб\.?|р\.?)$", "", cleaned, flags=re.I)
    if not cleaned or not re.fullmatch(r"[-+]?[\d.,]+", cleaned):
        raise ValueError("not a number")
    sign = ""
    if cleaned[0] in "+-":
        sign, cleaned = cleaned[0], cleaned[1:]
    if "." in cleaned and "," in cleaned:
        last = max(cleaned.rfind("."), cleaned.rfind(","))
        whole, frac = cleaned[:last], cleaned[last + 1:]
        cleaned = re.sub(r"[.,]", "", whole) + "." + frac
    elif _THOUSANDS.match(cleaned) and not cleaned.startswith("0"):
        cleaned = re.sub(r"[.,]", "", cleaned)
    elif cleaned.count(".") + cleaned.count(",") > 1:
        raise ValueError("not a number")
    return float(sign + cleaned.replace(",", "."))
