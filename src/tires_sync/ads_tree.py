"""«Аналитика рекламы» как в Roistat (the owner 2026-11-12: «не вижу сортировки, возьми пример с Roistat», «хочу видеть в разрезе: аккаунт →
объявления, по каждому объявлению, бренду, ширине, профилю, диаметру, модели бренда»). Read only: the rows of ads_spend.report (the spending
of the period per ad and its sales from the orders) put into a tree of groups.

- The levels: LEVELS, any of them in any order (the address `g`, kept for the person in user_pref LEVELS_PREF); «ad» is the leaf. An ad gets
  its brand, model, width, profile and diameter from the position of the catalog behind our Id of the ad (`avito_ad.ad_id`): the Id an
  account adopted for a code (mirror_feed.adopted_maps), the Id of a retired ad, the alias of a code of an account with the alias scheme,
  else the supplier's code in the Id (`<code>`, `<code>_aN`, `<code>-x4`, `<code>_r<stamp>`). No our Id, or no such position: «Без данных».
  The positions are read in one query for the page (both periods together).
- The totals of a group are the sums of its ads; the formulas (metrics) are taken from the sums, a dash (None) for a division by zero.
- The sort: any numeric column, inside every group (the groups by their own totals), the empty values below in both directions.
- The previous period of the same length: the change, %, of the spending, the pieces and the ДРР of «Итого» and of the groups of the top level."""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from . import ads_spend, user_prefs

LEVELS = {"account": "Аккаунт", "brand": "Бренд", "model": "Модель", "width": "Ширина", "profile": "Профиль", "diameter": "Диаметр",
          "ad": "Объявление"}
DEFAULT_LEVELS = ("account", "brand", "ad")
NO_DATA = "Без данных"
LEVELS_PREF = "ads.levels"
COLUMNS_PREF = "ads.cols"
HIDDEN_DEFAULT: Tuple[str, ...] = ()
SUMS = ("spend", "views", "contacts", "pieces", "orders", "revenue", "margin", "no_purchase", "promo")
# (key, the header, how it is counted): the order of the funnel (the owner 2026-11-12)
COLUMNS = (
    ("spend", "Расходы", "Всё, что Авито взяло за объявления за период: размещение и продвижение."),
    ("views", "Просмотры", "Просмотры объявлений за период по статистике Авито."),
    ("cpm", "CPM", "Цена тысячи просмотров: расходы ÷ просмотры × 1000. Прочерк — просмотров нет."),
    ("contacts", "Контакты", "Контакты за период по статистике Авито: показ телефона и сообщения."),
    ("contact_conv", "Конверсия в контакт", "Контакты ÷ просмотры × 100 %. Прочерк — просмотров нет."),
    ("cpl", "CPL", "Цена контакта: расходы ÷ контакты. Прочерк — контактов нет."),
    ("pieces", "Продано", "Шины, проданные по объявлению: заказы на этапе «Выполнено» или «Отправлен в ТК», "
                               "пришедшие через чат объявления, и записанные по ним продажи."),
    ("sale_conv", "Конверсия в продажу", "Заказы с продажей ÷ контакты × 100 %. Считаются заказы, а не штуки. Прочерк — контактов нет."),
    ("cpa", "CPA", "Цена продажи: расходы ÷ проданные штуки. Прочерк — продаж нет."),
    ("revenue", "Выручка", "Сумма продаж объявления за период."),
    ("margin", "Маржа", "Как в «Продажах»: выручка минус закупка с условиями поставщика, комиссия и продвижение позиции."),
    ("profit", "Прибыль", "Маржа − расходы на рекламу. Минус — реклама не окупилась."),
    ("drr", "ДРР", "Доля рекламных расходов: расходы ÷ выручка × 100 %. Прочерк — выручки нет."),
    ("romi", "ROMI", "Окупаемость рекламы: (маржа − расходы на рекламу) ÷ расходы на рекламу × 100 %. Прочерк — расходов нет."),
    ("bid", "Цена просмотра на Авито", "Текущая цена просмотра объявления в кабинете Авито и дневной лимит. Только в строке объявления."),
)
SORTABLE = tuple(c[0] for c in COLUMNS if c[0] != "bid")
DELTAS = ("spend", "pieces", "drr")


# ---- the levels and the columns of the person --------------------------------------------------------------------------------------

def parse_levels(text: str) -> Optional[Tuple[str, ...]]:
    """The levels of the address («account,brand,ad»): known ones, each once, in their order; None when none is left."""
    out: List[str] = []
    for part in str(text or "").split(","):
        part = part.strip()
        if part in LEVELS and part not in out:
            out.append(part)
    return tuple(out) or None


def saved_levels(conn, login: str) -> Tuple[str, ...]:
    saved = user_prefs.get(conn, login, LEVELS_PREF) if login else None
    return (parse_levels(",".join(map(str, saved))) if isinstance(saved, list) else None) or DEFAULT_LEVELS


def save_levels(conn, login: str, levels: Iterable[str], now: str) -> None:
    levels = list(levels)
    if login and levels != list(saved_levels(conn, login)):
        user_prefs.put(conn, login, LEVELS_PREF, levels, now)


def hidden_columns(conn, login: str) -> List[str]:
    saved = user_prefs.get(conn, login, COLUMNS_PREF) if login else None
    if not isinstance(saved, list):
        return list(HIDDEN_DEFAULT)
    return [c[0] for c in COLUMNS if c[0] in saved]


def save_columns(conn, login: str, shown: Iterable[str], now: str) -> List[str]:
    """«Колонки»: every column not in `shown` is hidden, kept for the login."""
    shown = set(shown)
    hidden = [c[0] for c in COLUMNS if c[0] not in shown]
    user_prefs.put(conn, login, COLUMNS_PREF, hidden, now)
    return hidden


# ---- the formulas --------------------------------------------------------------------------------------------------------------------

def _div(a: float, b: float, times: float = 1.0, digits: int = 2) -> Optional[float]:
    return round(a / b * times, digits) if b else None


def metrics(sums: dict) -> dict:
    """The sums of a row (an ad or a group) and the formulas of the funnel; None (a dash) for a division by zero."""
    m = {k: sums.get(k, 0) for k in SUMS}
    for k in ("spend", "revenue", "margin", "promo"):
        m[k] = round(float(m[k]), 2)
    spend = m["spend"]
    m.update(cpm=_div(spend, m["views"], 1000), contact_conv=_div(m["contacts"], m["views"], 100, 1), cpl=_div(spend, m["contacts"]),
             sale_conv=_div(m["orders"], m["contacts"], 100, 1), cpa=_div(spend, m["pieces"]), profit=round(m["margin"] - spend, 2),
             drr=ads_spend.drr(spend, m["revenue"]), romi=_div(m["margin"] - spend, spend, 100, 1))
    return m


def change(current, previous) -> Optional[float]:
    """The change against the previous period, %; None when there is nothing to compare with."""
    if current is None or previous is None or not previous:
        return None
    return round((current - previous) / abs(previous) * 100, 1)


# ---- our Id → the position of the catalog --------------------------------------------------------------------------------------------

def _id_codes(conn, account_id: int, today: date) -> Dict[str, str]:
    """{Id: code} the account knows without the code in the Id: adopted Ids, retired ads, aliases. Per account, not per ad."""
    from . import mirror_feed
    out: Dict[str, str] = {}
    for ad_id, code in conn.execute("SELECT ad_id, goods_id FROM retired_ad WHERE account_id = ? AND ad_id <> ''", (account_id,)):
        out.setdefault(str(ad_id), str(code))
    if mirror_feed.alias_scheme(conn, account_id):
        for (code,) in conn.execute("SELECT goods_id FROM published_state WHERE account_id = ?", (account_id,)):
            out.setdefault(mirror_feed.alias_id(account_id, code), code)
    for code, bound in mirror_feed.adopted_maps(conn, account_id, today)[0].items():
        out[str(bound)] = str(code)
    return out


def attributes(conn, ads: Iterable[Tuple[int, str]], today: date) -> Dict[Tuple[int, str], Optional[dict]]:
    """{(account, our Id): {brand, model, width, profile, diameter} or None} of the ads: the positions are read in one query."""
    from . import mirror_feed
    ads = {(a, str(i)) for a, i in ads if i}
    known = {a: _id_codes(conn, a, today) for a in {a for a, _ in ads}}
    code_of = {(a, i): known[a].get(i) or mirror_feed.supplier_code(i) for a, i in ads}
    found: Dict[str, dict] = {}
    for goods_id, text in conn.execute("SELECT goods_id, data FROM catalog_item WHERE goods_id IN (SELECT value FROM json_each(?))",
                                       (json.dumps(sorted(set(code_of.values()))),)):
        try:
            data = json.loads(text)
        except ValueError:
            continue
        brand = str(data.get("brand_raw") or "").strip()
        found[goods_id] = {"brand": brand, "model": str(data.get("model_raw") or "").strip(),
                           "width": int(data.get("width") or 0), "profile": int(data.get("height") or 0), "diameter": int(data.get("diameter") or 0)}
    return {k: found.get(code) for k, code in code_of.items()}


def _group(level: str, leaf: dict) -> Tuple[object, str]:
    """(the key, the label) of the group of `level` the ad belongs to; the key None is «Без данных»."""
    if level == "account":
        return leaf["account_id"], leaf["account"]
    attrs = leaf["attrs"]
    if level == "brand":
        return (attrs["brand"].casefold(), attrs["brand"]) if attrs and attrs["brand"] else (None, NO_DATA)
    if level == "model":
        if not attrs or not attrs["model"]:
            return None, NO_DATA
        label = ("%s %s" % (attrs["brand"], attrs["model"])).strip()
        return label.casefold(), label
    value = attrs.get(level) if attrs else 0
    if not value:
        return None, NO_DATA
    return value, ("R%d" % value if level == "diameter" else str(value))


# ---- the tree --------------------------------------------------------------------------------------------------------------------------

def _leaves(accounts: List[dict], only: str) -> List[dict]:
    out = []
    for acc in accounts:
        for r in acc["every"]:
            if (only == "sales" and not r["pieces"]) or (only == "spend" and not r["spend"]):
                continue
            out.append({"account_id": acc["id"], "account": acc["name"], "row": r, "attrs": None,
                        "m": metrics(dict(r, orders=r.get("orders", 0)))})
    return out


def _sort_key(sort: str, desc: bool):
    def key(node):
        value = node["m"].get(sort)
        empty = value is None
        return (empty, 0 if empty else (-value if desc else value), -node["m"]["spend"], node["tie"])
    return key


def _sums(leaves: List[dict]) -> dict:
    return {k: sum(leaf["m"][k] for leaf in leaves) for k in SUMS}


def _tree(leaves: List[dict], levels: Tuple[str, ...], sort: str, desc: bool, parent: str, depth: int, out: List[dict]) -> None:
    key = _sort_key(sort, desc)
    if not levels:
        return
    level, rest = levels[0], levels[1:]
    if level == "ad":
        nodes = [{"m": leaf["m"], "tie": (leaf["account_id"], leaf["row"]["avito_id"]), "leaf": leaf} for leaf in leaves]
        nodes.sort(key=key)
        for n in nodes[:ads_spend.ROWS_MAX]:
            r = n["leaf"]["row"]
            out.append({"kind": "ad", "id": "%s/%d.%d" % (parent, n["leaf"]["account_id"], r["avito_id"]), "parent": parent, "depth": depth,
                        "label": r["title"] or "№%s" % r["avito_id"], "level": level, "m": n["m"], "ad": r, "count": 1, "delta": None,
                        "account": n["leaf"]["account"]})
        if len(nodes) > ads_spend.ROWS_MAX:
            out.append({"kind": "more", "id": parent + "/more", "parent": parent, "depth": depth, "label": "", "level": level,
                        "m": {}, "count": len(nodes) - ads_spend.ROWS_MAX, "delta": None, "shown": ads_spend.ROWS_MAX, "of": len(nodes)})
        return
    buckets: Dict[object, dict] = {}
    for leaf in leaves:
        k, label = _group(level, leaf)
        buckets.setdefault(k, {"key": k, "label": label, "leaves": []})["leaves"].append(leaf)
    nodes = []
    for b in buckets.values():
        b["m"] = metrics(_sums(b["leaves"]))
        b["tie"] = (b["key"] is None, b["label"])
        nodes.append(b)
    nodes.sort(key=key)
    for n in nodes:
        node_id = "%s/%s:%s" % (parent, level, "-" if n["key"] is None else n["key"])
        out.append({"kind": "group", "id": node_id, "parent": parent, "depth": depth, "label": n["label"], "level": level, "key": n["key"],
                    "m": n["m"], "count": len(n["leaves"]), "delta": None, "leaf_level": not rest})
        _tree(n["leaves"], rest, sort, desc, node_id, depth + 1, out)


def _filled(leaves: List[dict]) -> set:
    """The columns some ad has a value in: a dash is none, nor a zero of a sum."""
    out = {c for c in SORTABLE if any(leaf["m"][c] is not None and (leaf["m"][c] != 0 or c in ("drr", "romi", "cpm")) for leaf in leaves)}
    if any(leaf["row"]["bid"] for leaf in leaves):                       # as before: the column comes with the first price read
        out.add("bid")
    return out


def build(conn, today: date, days: int, levels: Iterable[str], sort: str = "spend", desc: bool = True, only: str = "") -> dict:
    """The page: the tree of the period, «Итого» and the change against the previous period of the same length. Read only."""
    if days not in ads_spend.PERIODS:
        raise ValueError("period must be one of %s" % (ads_spend.PERIODS,))
    levels = parse_levels(",".join(levels)) or DEFAULT_LEVELS
    sort = sort if sort in SORTABLE else "spend"
    only = only if only in ads_spend.FILTERS else ""
    now = ads_spend.report(conn, today, days)
    before = ads_spend.report(conn, today - timedelta(days=days), days)
    leaves, old = _leaves(now["accounts"], only), _leaves(before["accounts"], only)
    attrs = attributes(conn, [(leaf["account_id"], leaf["row"]["ad_id"]) for leaf in leaves + old], today)
    for leaf in leaves + old:
        leaf["attrs"] = attrs.get((leaf["account_id"], str(leaf["row"]["ad_id"]))) if leaf["row"]["ad_id"] else None
    rows: List[dict] = []
    _tree(leaves, levels, sort, desc, "", 0, rows)
    total = {"m": metrics(_sums(leaves)), "count": len(leaves)}
    prev_total = metrics(_sums(old))
    total["delta"] = {k: change(total["m"][k], prev_total[k]) for k in DELTAS}
    if levels[0] != "ad":
        prev_top: Dict[object, List[dict]] = {}
        for leaf in old:
            prev_top.setdefault(_group(levels[0], leaf)[0], []).append(leaf)
        for row in rows:
            if row["depth"] == 0 and row["kind"] == "group":
                was = metrics(_sums(prev_top.get(row["key"], [])))
                row["delta"] = {k: change(row["m"][k], was[k]) for k in DELTAS}
    return {"days": days, "since": now["since"], "until": now["until"], "prev_since": before["since"], "prev_until": before["until"],
            "levels": levels, "sort": sort, "desc": desc, "only": only, "rows": rows, "total": total, "filled": _filled(leaves),
            "accounts": now["accounts"], "none": now["none"]}
