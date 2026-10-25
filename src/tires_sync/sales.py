"""Sales (the fact): what was actually sold, for how much and what was left after the supplier's terms and Avito's costs.

Every sale keeps the money of its moment (supplier price, terms, Avito percents), so later changes of prices or terms
do not rewrite history. A mistake is reversed, not deleted: the journal keeps who did it."""
from __future__ import annotations

import csv
import io
import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Dict, List, Optional

from . import economics, store

AD_KINDS = ("single", "set4")
SEASONS = ("winter", "summer", "all")
COLUMNS = ("id, day, goods_id, brand_key, season, diameter, name, ad_kind, pieces, price_total, sale_total, cost_total, bonus_total, "
           "monthly_total, fee_total, promo_total, margin_total, account_id, point, note, source, created, author, reversed_at, reversed_by")
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _check_day(value: str, today: Optional[str]) -> str:
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError:
        raise ValueError("Дата продажи: нужен формат ГГГГ-ММ-ДД.")
    if today and str(value) > today:
        raise ValueError("Дата продажи не может быть в будущем.")
    if parsed < date(2020, 1, 1):
        raise ValueError("Дата продажи слишком давняя: не раньше 2020 года.")
    return parsed.isoformat()


def add_sale(conn, *, day: str, goods_id: str, brand_key: str, season: str, diameter: int, name: str, ad_kind: str, pieces: int,
             sale_total: float, supplier_price: float, terms: Optional[dict], fee_pct: float, promo_pct: float,
             account_id: Optional[int], point: str, note: str, now: str, author: str, today: Optional[str] = None,
             source: str = "manual") -> int:
    day = _check_day(day, today)
    if not str(goods_id).strip() or not str(brand_key).strip():
        raise ValueError("Не указана позиция.")
    if season not in SEASONS:
        raise ValueError("Сезон позиции не распознан.")
    if ad_kind not in AD_KINDS:
        raise ValueError("Вид объявления: одиночное или комплект «за 4 шт.».")
    if isinstance(pieces, bool) or not isinstance(pieces, int) or not 1 <= pieces <= 100:
        raise ValueError("Количество шин: целое число от 1 до 100.")
    if ad_kind == "set4" and pieces % 4:
        raise ValueError("Комплект «за 4 шт.» продаётся кратно четырём шинам.")
    if not isinstance(sale_total, (int, float)) or not math.isfinite(sale_total) or not 0 < sale_total <= 10_000_000:
        raise ValueError("Выручка: число больше нуля и не больше 10 000 000 ₽.")
    if not isinstance(supplier_price, (int, float)) or not math.isfinite(supplier_price) or supplier_price <= 0:
        raise ValueError("Цена поставщика для этой позиции неизвестна.")
    point = str(point or "").strip()
    note = str(note or "").strip()
    if len(point) > 60 or len(note) > 200:
        raise ValueError("Точка не длиннее 60 знаков, заметка не длиннее 200.")
    money = economics.unit(float(supplier_price), float(sale_total) / pieces, terms, fee_pct, promo_pct, pieces)
    cur = conn.execute(
        "INSERT INTO sale (day, goods_id, brand_key, season, diameter, name, ad_kind, pieces, price_total, sale_total, cost_total, bonus_total,"
        " monthly_total, fee_total, promo_total, margin_total, account_id, point, note, source, created, author)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (day, str(goods_id), brand_key, season, int(diameter), name, ad_kind, pieces, money["price"], round(float(sale_total), 2), money["cost"],
         money["bonus"], money["monthly"], money["fee"], money["promo"], money["margin"], account_id, point, note, source, now, author))
    conn.commit()
    store.write_audit(conn, now, author, "sale_added", {"id": cur.lastrowid, "goods_id": str(goods_id), "pieces": pieces, "day": day})
    return int(cur.lastrowid)


def reverse_sale(conn, sale_id: int, now: str, author: str, from_order: bool = False) -> None:
    """Reverse one sale. A sale written from an order is the order's own record: reversing one line of it alone would leave the order counting
    the full profit, so it is reversed only through the order (`from_order`)."""
    row = conn.execute("SELECT source, note FROM sale WHERE id = ?", (int(sale_id),)).fetchone()
    if row is not None and row[0] == "order" and not from_order:
        raise ValueError("Продажа записана из заказа (%s): сторнируйте её через заказ, вернув его с этапа выполнения." % (row[1] or "заказ"))
    with conn:
        changed = conn.execute("UPDATE sale SET reversed_at = ?, reversed_by = ? WHERE id = ? AND reversed_at IS NULL",
                               (now, author, int(sale_id))).rowcount
    if not changed:
        exists = conn.execute("SELECT 1 FROM sale WHERE id = ?", (int(sale_id),)).fetchone()
        raise ValueError("Продажа уже сторнирована." if exists else "Такой продажи нет.")
    store.write_audit(conn, now, author, "sale_reversed", {"id": int(sale_id)})


def list_sales(conn, start: str = "", end: str = "", brand: str = "", account_id: Optional[int] = None, point: str = "",
               include_reversed: bool = False) -> List[dict]:
    where, args = [], []
    if not include_reversed:
        where.append("reversed_at IS NULL")
    for clause, value in (("day >= ?", start), ("day <= ?", end), ("brand_key = ?", brand), ("point = ?", point)):
        if value:
            where.append(clause)
            args.append(value)
    if account_id is not None:
        where.append("account_id = ?")
        args.append(account_id)
    query = "SELECT %s FROM sale %s ORDER BY day DESC, id DESC" % (COLUMNS, ("WHERE " + " AND ".join(where)) if where else "")
    names = [c.strip() for c in COLUMNS.split(",")]
    return [dict(zip(names, r)) for r in conn.execute(query, args)]


def summarize(rows: List[dict]) -> dict:
    live = [r for r in rows if not r.get("reversed_at")]
    price = sum(r["price_total"] for r in live)
    sale = sum(r["sale_total"] for r in live)
    margin = sum(r["margin_total"] for r in live)
    return {"sales": len(live), "pieces": sum(r["pieces"] for r in live), "price": round(price, 2), "sale": round(sale, 2),
            "margin": round(margin, 2), "fee": round(sum(r["fee_total"] + r["promo_total"] for r in live), 2),
            "margin_pct_price": round(100.0 * margin / price, 2) if price else 0.0,
            "margin_pct_sale": round(100.0 * margin / sale, 2) if sale else 0.0}


def breakdown(rows: List[dict], by: str) -> List[dict]:
    """Totals per season / radius / brand / account / point: the same numbers as the summary, one line per key."""
    def key(r):
        return {"season": r["season"], "radius": "R%d" % r["diameter"], "brand": r["brand_key"],
                "account": str(r["account_id"]) if r["account_id"] is not None else "", "point": r["point"]}[by]
    grouped: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        grouped[key(r)].append(r)
    out = [dict(key=k, **summarize(items)) for k, items in grouped.items()]
    if by == "radius":
        return sorted(out, key=lambda r: int(r["key"][1:]))
    return sorted(out, key=lambda r: (-r["margin"], r["key"]))


def _bucket(day: str, granularity: str) -> str:
    if granularity == "month":
        return day[:7]
    if granularity == "week":
        d = date.fromisoformat(day)
        return (d - timedelta(days=d.weekday())).isoformat()
    return day


def series(rows: List[dict], granularity: str = "day") -> List[dict]:
    grouped: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        grouped[_bucket(r["day"], granularity)].append(r)
    return [dict(key=k, **summarize(grouped[k])) for k in sorted(grouped)]


def _safe(value) -> str:
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


SEASON_NAMES = {"winter": "зима", "summer": "лето", "all": "всесезонные"}
KIND_NAMES = {"single": "одиночное", "set4": "комплект ×4"}
SOURCE_NAMES = {"manual": "вручную", "avito": "Авито"}


def _money(value) -> str:
    return ("%.2f" % value).replace(".", ",")        # Russian Excel reads «9150,50» as a number and «9150.50» as a date


def export_csv(rows: List[dict], accounts: Optional[Dict[int, str]] = None) -> str:
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\n")
    writer.writerow(["Дата", "Позиция", "Бренд", "Сезон", "Радиус", "Вид", "Шин", "Цена поставщика", "Выручка", "Закупка", "Бонус", "ЕЖ",
                     "Комиссия", "Реклама", "Прибыль", "Аккаунт", "Точка", "Заметка", "Источник"])
    accounts = accounts or {}
    for r in rows:
        account = accounts.get(r["account_id"], r["account_id"]) if r["account_id"] is not None else ""
        writer.writerow([r["day"], _safe(r["name"]), _safe(r["brand_key"]), SEASON_NAMES.get(r["season"], r["season"]), "R%d" % r["diameter"],
                         KIND_NAMES.get(r["ad_kind"], r["ad_kind"]), r["pieces"], _money(r["price_total"]), _money(r["sale_total"]),
                         _money(r["cost_total"]), _money(r["bonus_total"]), _money(r["monthly_total"]), _money(r["fee_total"]),
                         _money(r["promo_total"]), _money(r["margin_total"]), _safe(account), _safe(r["point"]), _safe(r["note"]),
                         SOURCE_NAMES.get(r["source"], r["source"])])
    return "\ufeff" + out.getvalue()
