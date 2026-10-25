"""The supplier's terms per brand, season and size: the numbers unit economics is made of."""
from __future__ import annotations

import csv
import io
import math
import re
from datetime import date
from typing import Dict, List, Optional, Tuple

from . import store
from .economics import pick_terms

SEASONS = ("winter", "summer", "all")
SIZE_CLASSES = ("small", "large", "all")
LIMITS = {"markdown_pct": (-50.0, 50.0), "discount_pct": (0.0, 50.0), "bonus_pct": (0.0, 50.0), "monthly_pct": (0.0, 50.0)}
LABELS = {"markdown_pct": "Уценка или наценка", "discount_pct": "Скидка", "bonus_pct": "Бонус", "monthly_pct": "ЕЖ"}
COLUMNS = "id, supplier_id, brand_key, season, size_class, markdown_pct, discount_pct, bonus_pct, monthly_pct, valid_from, created, author"


def _clean_number(value) -> str:
    """«-8,5 %», «−8,5», «13 %» (Excel's way to write percents) → «-8.5», «13»."""
    text = str(value).replace("\u2212", "-").replace("%", "")
    text = re.sub(r"[\s\u00a0\u202f]", "", text)
    return text.replace(",", ".")


def _number(value, name: str) -> float:
    try:
        number = float(_clean_number(value) or 0)
    except ValueError:
        raise ValueError("%s: нужно число, получено «%s»" % (LABELS[name], value))
    low, high = LIMITS[name]
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError("%s: допустимо от %g до %g %%" % (LABELS[name], low, high))
    return number


def _day(value: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError("Дата начала: нужен формат ГГГГ-ММ-ДД.")
    try:
        date.fromisoformat(text)
    except ValueError:
        raise ValueError("Дата начала: такой даты нет.")
    return text


def _prepare(row: dict) -> tuple:
    brand = re.sub(r"\s+", " ", str(row.get("brand_key") or "").strip()).upper()
    if not brand:
        raise ValueError("Укажите бренд.")
    if row.get("season") not in SEASONS:
        raise ValueError("Сезон: зима, лето или всесезонные.")
    if row.get("size_class") not in SIZE_CLASSES:
        raise ValueError("Размер: до R17, выше R17 или все радиусы.")
    values = {name: _number(row.get(name, 0), name) for name in LIMITS}
    start = _day(row.get("valid_from"))
    return (int(row.get("supplier_id") or 1), brand, row["season"], row["size_class"], values["markdown_pct"], values["discount_pct"],
            values["bonus_pct"], values["monthly_pct"], start)


def _write(conn, prepared: tuple, now: str, author: str) -> None:
    conn.execute(
        "INSERT INTO supplier_terms (supplier_id, brand_key, season, size_class, markdown_pct, discount_pct, bonus_pct, monthly_pct,"
        " valid_from, created, author) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT (supplier_id, brand_key, season, size_class, valid_from) DO UPDATE SET markdown_pct = excluded.markdown_pct,"
        " discount_pct = excluded.discount_pct, bonus_pct = excluded.bonus_pct, monthly_pct = excluded.monthly_pct,"
        " created = excluded.created, author = excluded.author", prepared + (now, author))


def save_term(conn, row: dict, now: str, author: str) -> None:
    prepared = _prepare(row)
    with conn:
        _write(conn, prepared, now, author)
    store.write_audit(conn, now, author, "term_saved", {"brand_key": prepared[1], "season": prepared[2], "size_class": prepared[3],
                                                        "valid_from": prepared[8]})


def update_term(conn, term_id: int, row: dict, now: str, author: str) -> None:
    """Change a saved term in place; a change that lands on another saved term is refused, not merged."""
    old = conn.execute("SELECT %s FROM supplier_terms WHERE id = ?" % COLUMNS, (int(term_id),)).fetchone()
    if old is None:
        raise ValueError("Таких условий уже нет.")
    before = dict(zip(COLUMNS.split(", "), old))
    prepared = (before["supplier_id"],) + _prepare(row)[1:]
    with conn:
        clash = conn.execute("SELECT 1 FROM supplier_terms WHERE supplier_id = ? AND brand_key = ? AND season = ? AND size_class = ?"
                             " AND valid_from = ? AND id != ?", (prepared[0], prepared[1], prepared[2], prepared[3], prepared[8], int(term_id))).fetchone()
        if clash:
            raise ValueError("Такие условия уже есть (бренд, сезон, радиус и дата те же): измените их напрямую.")
        conn.execute("UPDATE supplier_terms SET brand_key = ?, season = ?, size_class = ?, markdown_pct = ?, discount_pct = ?, bonus_pct = ?,"
                     " monthly_pct = ?, valid_from = ?, created = ?, author = ? WHERE id = ?", prepared[1:] + (now, author, int(term_id)))
    store.write_audit(conn, now, author, "term_updated", {"brand_key": prepared[1], "season": prepared[2], "size_class": prepared[3],
                                                          "valid_from": prepared[8], "before": {k: before[k] for k in (
                                                              "brand_key", "season", "size_class", "markdown_pct", "discount_pct", "bonus_pct",
                                                              "monthly_pct", "valid_from")}})


def list_terms(conn) -> List[dict]:
    names = COLUMNS.split(", ")
    rows = conn.execute("SELECT %s FROM supplier_terms ORDER BY valid_from, brand_key, season, size_class" % COLUMNS).fetchall()
    return [dict(zip(names, r)) for r in rows]


def delete_term(conn, term_id: int, now: str, author: str) -> None:
    row = conn.execute("SELECT brand_key, season, size_class, valid_from FROM supplier_terms WHERE id = ?", (int(term_id),)).fetchone()
    if row is None:
        raise ValueError("Таких условий уже нет.")
    with conn:
        conn.execute("DELETE FROM supplier_terms WHERE id = ?", (int(term_id),))
    store.write_audit(conn, now, author, "term_deleted", {"brand_key": row[0], "season": row[1], "size_class": row[2], "valid_from": row[3]})


# --- CSV in the owner's layout -------------------------------------------------------------------------------------------

_SEASON_WORDS = {"зима": "winter", "лето": "summer", "всесезон": "all", "всесезонные": "all", "всесезонная": "all", "winter": "winter",
                 "summer": "summer", "all": "all"}
STOP_WORDS = {"TYRES", "TYRE", "TIRES", "TIRE", "ШИНЫ"}


def _season(text: str) -> Optional[str]:
    return _SEASON_WORDS.get(text.strip().lower())


def _size(text: str) -> Optional[str]:
    """«до R17 вкл.» small, «выше R17» / «R18+» / «R20» large, «все радиусы» all, a range that crosses R17 all."""
    low = re.sub(r"\s+", " ", text.strip().lower())
    if low in ("all", "small", "large"):
        return low
    if "все" in low or re.search(r"\ball\b", low):
        return "all"
    numbers = [int(n) for n in re.findall(r"(\d{2})", low)]
    if any(word in low for word in ("выше", "более", "+", ">", "≥", "больше")):
        return "large"
    if "до" in low and numbers and max(numbers) <= 17:
        return "small"
    if not numbers:
        return None
    if max(numbers) <= 17:
        return "small"
    if min(numbers) > 17:
        return "large"
    return "all"


def resolve_brand(name: str, keys: List[str]) -> Optional[str]:
    """The panel's brand key for a name from the table: the exact name first, then the only key that has all the words of the name
    (the words «Tyres» and so on do not count; of two suppliers' keys the first supplier's one wins)."""
    wanted = re.sub(r"\s+", " ", name.strip().upper())
    if not wanted:
        return None
    exact = [k for k in keys if k.upper() == wanted]
    if exact:
        return exact[0]
    words = {w for w in wanted.split() if w not in STOP_WORDS}
    if not words:
        return None
    found = [k for k in keys if words <= {w for w in re.sub(r"^S\d+:", "", k.upper()).split() if w not in STOP_WORDS}]
    if len(found) > 1:
        plain = [k for k in found if not re.match(r"^S\d+:", k)]
        found = plain if len(plain) == 1 else found
    return found[0] if len(found) == 1 else None


def _header_index(header: List[str], *needles: str) -> Optional[int]:
    for i, cell in enumerate(header):
        if any(n in cell.strip().lower() for n in needles):
            return i
    return None


def import_csv(conn, text: str, valid_from: str, brand_keys: List[str], now: str, author: str, monthly_default: float = 10.0) -> Tuple[int, List[str]]:
    """Terms from a table. The rows of that start date are replaced as a whole (one transaction, one journal entry); rows with
    mistakes are listed and skipped."""
    start = _day(valid_from)
    text = text.lstrip("\ufeff")
    first = text.splitlines()[0] if text.splitlines() else ""
    delimiter = max((";", ",", "\t"), key=first.count)
    reader = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not reader:
        raise ValueError("Файл пустой.")
    header = reader[0]
    idx = {"brand": _header_index(header, "бренд", "brand"), "season": _header_index(header, "сезон", "season"),
           "size": _header_index(header, "диаметр", "радиус", "размер", "size"), "markdown": _header_index(header, "уценка", "наценка", "markdown"),
           "discount": _header_index(header, "скидка", "discount"), "bonus": _header_index(header, "бонус", "bonus"),
           "monthly": _header_index(header, "еж", "monthly")}
    if None in (idx["brand"], idx["season"], idx["size"], idx["markdown"]):
        raise ValueError("Нужны столбцы: Бренд, Сезон, Диаметр, Уценка (и по желанию Скидка, Бонус, ЕЖ).")
    if len(reader) < 2:
        raise ValueError("В файле нет строк с условиями.")
    prepared, errors = [], []

    def cell(r, key):
        i = idx[key]
        return r[i] if i is not None and i < len(r) else ""

    for number, r in enumerate(reader[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        brand = resolve_brand(cell(r, "brand"), brand_keys)
        season, size = _season(cell(r, "season")), _size(cell(r, "size"))
        if brand is None:
            errors.append("строка %d: бренд «%s» не найден или назван неоднозначно" % (number, cell(r, "brand")))
        elif season is None:
            errors.append("строка %d: сезон «%s» не понят" % (number, cell(r, "season")))
        elif size is None:
            errors.append("строка %d: диаметр «%s» не понят" % (number, cell(r, "size")))
        elif not _clean_number(cell(r, "markdown")):
            errors.append("строка %d: не заполнена уценка или наценка" % number)
        else:
            monthly = cell(r, "monthly").strip()
            try:
                prepared.append(_prepare({"brand_key": brand, "season": season, "size_class": size, "markdown_pct": cell(r, "markdown"),
                                          "discount_pct": cell(r, "discount"), "bonus_pct": cell(r, "bonus"),
                                          "monthly_pct": monthly if monthly else monthly_default, "valid_from": start}))
            except ValueError as err:
                errors.append("строка %d: %s" % (number, err))
    if prepared:
        with conn:
            conn.execute("DELETE FROM supplier_terms WHERE valid_from = ?", (start,))
            for item in prepared:
                _write(conn, item, now, author)
        store.write_audit(conn, now, author, "terms_imported", {"valid_from": start, "rows": len(prepared), "errors": len(errors)})
    return len(prepared), errors


def pricing_from_terms(rows: List[dict], day: str) -> Tuple[Dict[str, dict], List[str]]:
    """Brand percents for «Бренды и наценки» taken from the markdown column; a rule needs both sizes, otherwise it is listed."""
    pairs = sorted({(r["brand_key"], r["season"]) for r in rows})
    rules, missing = {}, []
    for brand, season in pairs:
        small = pick_terms(rows, brand, season, 15, day)
        large = pick_terms(rows, brand, season, 18, day)
        key = "%s|%s" % (brand, season)
        if small and large:
            rules[key] = {"pct_small": float(small["markdown_pct"]), "pct_large": float(large["markdown_pct"])}
        else:
            missing.append(key)
    return rules, missing


# --- commission and promotion of Avito (percents of the sale price) --------------------------------------------------------

PARAMS_KEY = "economics_params"


def get_params(conn) -> Dict[str, float]:
    import json
    raw = store.get_state(conn, PARAMS_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return {"fee_pct": float(data.get("fee_pct", 0.0)), "promo_pct": float(data.get("promo_pct", 0.0))}


def set_params(conn, fee, promo, now: str, author: str) -> None:
    import json
    values = {}
    for name, value, label in (("fee_pct", fee, "Комиссия Авито"), ("promo_pct", promo, "Реклама")):
        try:
            number = float(str(value).strip().replace(",", ".") or 0)
        except ValueError:
            raise ValueError("%s: нужно число, получено «%s»" % (label, value))
        if not math.isfinite(number) or not 0 <= number <= 50:
            raise ValueError("%s: допустимо от 0 до 50 %%" % label)
        values[name] = number
    store.set_state(conn, PARAMS_KEY, json.dumps(values))
    store.write_audit(conn, now, author, "economics_params", values)


# --- daily snapshots for the dynamics ----------------------------------------------------------------------------------------

def record_snapshot(conn, day: str, brands: List[dict]) -> None:
    """One row per brand per day (a second run on the same day replaces the first)."""
    with conn:
        conn.execute("DELETE FROM economics_snapshot WHERE day = ?", (day,))
        conn.executemany(
            "INSERT INTO economics_snapshot (day, brand_key, ads, stock, profit, sale_total, price_total, no_terms) VALUES (?,?,?,?,?,?,?,?)",
            [(day, b["brand"], b["ads"], b["stock"], b["profit"], b["sale_total"], b["price_total"], b["no_terms"]) for b in brands])


def snapshot_series(conn, brand: str = "", days: int = 30, today: Optional[str] = None) -> List[dict]:
    """Per day: ads, stock, potential profit, average margin (of the price) for all brands or for one."""
    query = "SELECT day, SUM(ads), SUM(stock), SUM(profit), SUM(sale_total), SUM(no_terms), SUM(price_total) FROM economics_snapshot"
    args: list = []
    where = []
    if brand:
        where.append("brand_key = ?")
        args.append(brand)
    if today:
        where.append("day <= ?")
        args.append(today)
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " GROUP BY day ORDER BY day DESC LIMIT ?"
    rows = conn.execute(query, args + [int(days)]).fetchall()
    out = []
    for day, ads, stock, profit, sale_total, no_terms, price_base in reversed(rows):
        out.append({"day": day, "ads": int(ads), "stock": int(stock), "profit": round(profit, 2), "sale_total": round(sale_total, 2),
                    "no_terms": int(no_terms),
                    "margin_pct": round(100.0 * profit / price_base, 2) if price_base else 0.0})
    return out
