"""The conditions of the clients (buyers, the owner 2026-10-29; plan client-role §3, §7.1): read and written for the owner's pages and for an
accepted invitation. Subject '*' is the role «Клиент» (the default for every client), else a client's login (over the role's).

A % is the markup over the purchase: 0-300 with at most two decimals (В5); empty = not set (the next step of panel/client_catalog acts). The
brand keys are kept bare (client_catalog.bare_brand: no «S<N>:», no case), so one brand of both warehouses is one row. A subject's rows are
written whole in one go (the old ones go), and every change is in the journal (client_terms_changed: the %s and the brands, no password).
How the conditions act (the order of the %s, the set of brands) is client_catalog's and is not changed here."""
from __future__ import annotations

import json
import re
from typing import Dict, Iterable, List, Optional, Tuple

from . import store

ROLE = "*"
PCT_MAX = 300
MAX_BRANDS = 2000
MODES = ("inherit", "own")
PCT = re.compile(r"^[0-9]{1,3}(?:\.[0-9]{1,2})?$")           # ASCII digits only (\d takes other scripts too)
PCT_TEXT = "Наценка: число от 0 до 300, не больше двух знаков после запятой."

Pct = Tuple[Optional[float], Optional[float]]


class TermsError(ValueError):
    """A refused condition; `field` is the form field it is about (the message goes under it)."""

    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.field = field


def bare(key: str) -> str:
    from .panel.client_catalog import bare_brand          # the one rule of the catalog (imported here: the panel imports this module's users)
    return bare_brand(key)


def parse_pct(text, field: str = "") -> Optional[float]:
    """«12,5» -> 12.5; empty -> None; anything else than 0-300 with at most two decimals is refused."""
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        text = repr(float(text))
        text = text[:-2] if text.endswith(".0") else text
    value = str(text).strip().replace(",", ".")
    if value == "":
        return None
    if not PCT.match(value) or float(value) > PCT_MAX:
        raise TermsError(PCT_TEXT, field)
    return float(value)


def read(conn, subject: str) -> dict:
    """{"small", "large", "mode", "brands": {bare key: (small, large)}} of a subject; no row: nothing set («как у роли»)."""
    row = conn.execute("SELECT pct_small, pct_large, brands_mode FROM client_terms WHERE subject = ?", (subject,)).fetchone()
    brands: Dict[str, Pct] = {}
    for key, small, large in conn.execute("SELECT brand_key, pct_small, pct_large FROM client_brand WHERE subject = ? ORDER BY brand_key",
                                          (subject,)):
        brands.setdefault(bare(key), (small, large))
    mode = "own" if subject == ROLE else (row[2] if row and row[2] in MODES else "inherit")
    return {"small": row[0] if row else None, "large": row[1] if row else None, "mode": mode, "brands": brands}


def _clean_brands(brands) -> Dict[str, Pct]:
    out: Dict[str, Pct] = {}
    for key, pcts in (brands.items() if isinstance(brands, dict) else ((k, (None, None)) for k in brands)):
        key = bare(str(key))
        if not key:
            continue
        small, large = pcts
        out[key] = (parse_pct(small), parse_pct(large))
    if len(out) > MAX_BRANDS:
        raise TermsError("Слишком много брендов: не больше %d." % MAX_BRANDS)
    return out


def save(conn, subject: str, small, large, mode: str, brands, now: str, author: str, commit: bool = True) -> dict:
    """Write the whole conditions of a subject: the general %s, the mode of the brands and the brand rows ({key: (small, large)}, or keys).
    The role is always «own». A client must be a user with the role «Клиент». Nothing is written when anything is refused.
    `commit=False`: the caller's transaction (an invitation is accepted at once or not at all). Returns the journal's detail."""
    from . import users
    if subject != ROLE:
        subject = users.normalize(subject)
        if not users.exists(conn, subject) or users.get_role(conn, subject)["role"] != "client":
            raise TermsError("Условия ставятся только клиенту.")
    if mode not in MODES:
        raise TermsError("Бренды: «как у роли» или «свой список».", "mode")
    mode = "own" if subject == ROLE else mode
    small, large = parse_pct(small, "small"), parse_pct(large, "large")
    rows = _clean_brands(brands)
    old = read(conn, subject)
    conn.execute("INSERT INTO client_terms (subject, pct_small, pct_large, brands_mode, updated, updated_by) VALUES (?,?,?,?,?,?) "
                 "ON CONFLICT(subject) DO UPDATE SET pct_small = excluded.pct_small, pct_large = excluded.pct_large, "
                 "brands_mode = excluded.brands_mode, updated = excluded.updated, updated_by = excluded.updated_by",
                 (subject, small, large, mode, now, author))
    conn.execute("DELETE FROM client_brand WHERE subject = ?", (subject,))
    conn.executemany("INSERT INTO client_brand (subject, brand_key, pct_small, pct_large) VALUES (?,?,?,?)",
                     [(subject, key, s, l) for key, (s, l) in sorted(rows.items())])
    detail = {"subject": subject,
              "from": {"small": old["small"], "large": old["large"], "mode": old["mode"]},
              "to": {"small": small, "large": large, "mode": mode},
              "brands_added": sorted(set(rows) - set(old["brands"])), "brands_removed": sorted(set(old["brands"]) - set(rows)),
              "brand_pct": {k: list(v) for k, v in sorted(rows.items()) if old["brands"].get(k, (None, None)) != v}}   # new or changed %s
    audit(conn, now, author, "client_terms_changed", detail, commit)
    return detail


def audit(conn, now: str, author: str, action: str, detail: dict, commit: bool) -> None:
    """store.write_audit commits; inside a caller's transaction the row is written without it."""
    if commit:
        store.write_audit(conn, now, author, action, detail)
    else:
        conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)", (now, author, action, json.dumps(detail, ensure_ascii=False)))


# ---- an invitation of a client -------------------------------------------------------------------------------------------------------

def invite_terms(small, large, mode: str, brands: Iterable[str]) -> str:
    """The conditions an invitation of a client puts in the profile, checked now (the fields «c_small», «c_large», «c_mode»), as JSON:
    {"small", "large", "brands": null («как у роли») | [bare keys]}."""
    if mode not in MODES:
        raise TermsError("Бренды: «как у роли» или «свой список».", "c_mode")
    data = {"small": parse_pct(small, "c_small"), "large": parse_pct(large, "c_large"),
            "brands": sorted(_clean_brands(list(brands))) if mode == "own" else None}
    return json.dumps(data, ensure_ascii=False)


def from_invite(text: str) -> Optional[dict]:
    """The checked conditions of an invitation (None: not a client's). A broken one is refused: the invitation is not accepted."""
    if not text:
        return None
    try:
        data = json.loads(text)
        brands = data["brands"]
        if brands is not None and not isinstance(brands, list):
            raise ValueError
        return {"small": parse_pct(data["small"]), "large": parse_pct(data["large"]),
                "mode": "inherit" if brands is None else "own", "brands": list(_clean_brands([str(b) for b in brands or []]))}
    except (ValueError, KeyError, TypeError) as err:
        raise TermsError("Условия приглашения испорчены: попросите новую ссылку.") from err


def describe(terms: dict) -> List[str]:
    """Short lines of conditions for the owner's lists: «до R17 12 %», «бренды: как у роли» …"""
    out = []
    for key, label in (("small", "до R17"), ("large", "с R18")):
        out.append("%s %s" % (label, ("%g %%" % terms[key]) if terms.get(key) is not None else "как у роли"))
    if terms.get("mode") == "own":
        out.append("бренды: свои (%d)" % len(terms.get("brands") or ()))
    else:
        out.append("бренды: как у роли")
    return out
