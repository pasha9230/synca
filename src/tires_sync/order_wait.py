"""What a waiting client wants (stage «Ожидает наличие») and the check of waiting cards against the stock.
A line is one wanted tire (size, season, studs, quantity, optional brand, model, code) with two ticks: other brands, other sizes.
A card is found exactly when every line is in stock under its own terms; it is found with analogs when every line is covered but some
only by an analog (another brand and/or a neighbour size). A partial find marks nothing: a part of the set is no sale (owner, 2026-10-15).
Nothing is sent to anybody here: the check only tags the card and leaves a task for the responsible manager."""
from __future__ import annotations

import logging
import re
from typing import Dict, Iterable, List, Optional

from . import orders
from .webmim import brand_key, studs_named

SEASONS = ("", "winter", "summer", "all")
SEASON_NAMES = {"winter": "зима", "summer": "лето", "all": "всесезон"}
STUDS = ("", "yes", "no")
STUD_NAMES = {"yes": "шипы", "no": "без шипов"}
NEIGHBOUR_WIDTH, NEIGHBOUR_PROFILE = 10, 5          # «другие размеры»: the same rim, the width within ±10 mm, the profile within ±5
TAG_EXACT, TAG_ANALOG = "Появилось", "Есть аналоги"
TASK_EXACT, TASK_ANALOG = 0, -1                      # the «rule» numbers of the two system tasks (a task is one per card per stay in the stage)
TITLE_EXACT, TITLE_ANALOG = "Позиция появилась: предложите клиенту", "Есть аналоги: можно предложить клиенту"
AUTHOR = "лист ожидания"
COLUMNS = "id, order_id, brand, model, width, profile, radius, season, studded, qty, code, any_brand, any_size"


def _row(row) -> dict:
    line = dict(zip(COLUMNS.split(", "), row))
    line["any_brand"], line["any_size"] = bool(line["any_brand"]), bool(line["any_size"])
    return line


def list_wants(conn, card_id) -> List[dict]:
    return [_row(r) for r in conn.execute("SELECT %s FROM order_want WHERE order_id = ? ORDER BY id" % COLUMNS, (card_id,))]


def _number(value, label: str, low: int, high: int) -> int:
    text = str(value if value is not None else "").strip()
    if not text or text == "0":
        return 0
    if not (text.isascii() and text.isdigit()) or len(text) > 6 or not low <= int(text) <= high:
        raise orders.OrderError("%s: число от %d до %d." % (label, low, high))
    return int(text)


def _flag(value) -> int:
    return 1 if str(value or "").strip().lower() in ("1", "true", "on", "yes", "да") else 0


def _clean(fields: dict) -> dict:
    out = {
        "brand": orders._single(fields.get("brand"), 60, "Бренд"), "model": orders._single(fields.get("model"), 60, "Модель"),
        "code": orders._single(fields.get("code"), 40, "Артикул"),
        "width": _number(fields.get("width"), "Ширина", 100, 400), "profile": _number(fields.get("profile"), "Профиль", 20, 100),
        "radius": _number(fields.get("radius"), "Радиус", 10, 30),
        "any_brand": _flag(fields.get("any_brand")), "any_size": _flag(fields.get("any_size")),
    }
    season = str(fields.get("season") or "").strip()
    if season not in SEASONS:
        raise orders.OrderError("Сезон: зима, лето, всесезон или не указан.")
    studded = str(fields.get("studded") or "").strip()
    if studded not in STUDS:
        raise orders.OrderError("Шипы: да, нет или не указано.")
    out["season"], out["studded"] = season, studded
    qty = str(fields.get("qty") if fields.get("qty") is not None else "1").strip()
    if not (qty.isascii() and qty.isdigit()) or len(qty) > 4 or not 1 <= int(qty) <= 100:
        raise orders.OrderError("Количество: число от 1 до 100.")
    out["qty"] = int(qty)
    if not out["code"] and not (out["width"] and out["profile"] and out["radius"]):
        raise orders.OrderError("Укажите артикул или размер (ширина, профиль, радиус).")
    return out


def describe(line: dict) -> str:
    size = "%s/%s R%s" % (line["width"], line["profile"], line["radius"]) if line["width"] else ""
    parts = [" ".join(p for p in (line["brand"], line["model"], size) if p) or ("артикул %s" % line["code"])]
    if line["season"]:
        parts.append(SEASON_NAMES[line["season"]])
    if line["studded"]:
        parts.append(STUD_NAMES[line["studded"]])
    parts.append("%d шт." % line["qty"])
    if line["any_brand"]:
        parts.append("можно другие бренды")
    if line["any_size"]:
        parts.append("можно другие размеры")
    return ", ".join(parts)


def client_text(card: dict) -> str:
    """The default words for the client when the tires he waits for are found: what he asked for, without the ticks of the manager."""
    lines = []
    for line in card["wants"]:
        size = "%s/%s R%s" % (line["width"], line["profile"], line["radius"]) if line["width"] else ""
        parts = [" ".join(p for p in (line["brand"], line["model"], size) if p) or ("артикул %s" % line["code"])]
        if line["season"]:
            parts.append(SEASON_NAMES[line["season"]])
        if line["studded"]:
            parts.append(STUD_NAMES[line["studded"]])
        lines.append("%s, %d шт." % (", ".join(parts), line["qty"]))
    return "Здравствуйте! Шины, которые вы ждали, появились в наличии: %s. Подскажите, оформляем заказ?" % "; ".join(lines)


def add_want(conn, card_id, fields: dict, now: str, author: str, commit: bool = True) -> int:
    """A line «Что нужно клиенту». `commit=False`: written with the caller's change (a new card and its line, all or nothing)."""
    orders._row(conn, card_id)                                     # raises when there is no such card
    clean = _clean(fields)
    keys = sorted(clean)
    cur = conn.execute("INSERT INTO order_want (order_id, %s, created_at) VALUES (?, %s, ?)" % (", ".join(keys), ", ".join("?" for _ in keys)),
                       [card_id] + [clean[k] for k in keys] + [now])
    orders.add_event(conn, card_id, now, author, "want", "Что нужно клиенту: %s" % describe(dict(clean, id=cur.lastrowid)))
    if commit:
        conn.commit()
    return cur.lastrowid


def _get(conn, want_id) -> dict:
    row = conn.execute("SELECT %s FROM order_want WHERE id = ?" % COLUMNS, (want_id,)).fetchone()
    if row is None:
        raise orders.OrderError("Такой строки нет.")
    return _row(row)


def update_want(conn, want_id, fields: dict, now: str, author: str) -> None:
    line = _get(conn, want_id)
    clean = _clean({**line, **fields, "any_brand": fields.get("any_brand", line["any_brand"]), "any_size": fields.get("any_size", line["any_size"])})
    conn.execute("UPDATE order_want SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in sorted(clean)), [clean[k] for k in sorted(clean)] + [want_id])
    orders.add_event(conn, line["order_id"], now, author, "want", "Строка изменена: %s" % describe(dict(clean, id=want_id)))
    conn.commit()


def remove_want(conn, want_id, now: str, author: str) -> None:
    line = _get(conn, want_id)
    card = orders.get_card(conn, line["order_id"])
    if card["stage"]["code"] == "wait" and len(card["wants"]) <= 1:
        raise orders.OrderError("Это последняя строка: заказ на этапе «%s» без неё нельзя. Перенесите заказ на другой этап." % card["stage"]["name"])
    conn.execute("DELETE FROM order_want WHERE id = ?", (want_id,))
    orders.add_event(conn, line["order_id"], now, author, "want", "Строка убрана: %s" % describe(line))
    conn.commit()


def _norm(text) -> str:
    return re.sub(r"\s+", "", str(text or "")).upper()


def _brand(offer) -> str:
    """The brand key of an offer without the prefix of a second supplier («S2:KUMHO» is KUMHO)."""
    return re.sub(r"^S\d+:", "", offer.brand_key)


def _same_size(line: dict, offer) -> bool:
    return (line["width"], line["profile"], line["radius"]) == (offer.width, offer.height, offer.diameter)


def _near_size(line: dict, offer) -> bool:
    return (offer.diameter == line["radius"] and abs(offer.width - line["width"]) <= NEIGHBOUR_WIDTH
            and abs(offer.height - line["profile"]) <= NEIGHBOUR_PROFILE)


def _terms_ok(line: dict, offer) -> bool:
    """Season and studs: the same for an exact tire and for an analog."""
    if line["season"] and offer.season.value != line["season"]:
        return False
    if line["studded"] and bool(offer.studded) != (line["studded"] == "yes"):
        return False
    return True


def _exact(line: dict, offer) -> bool:
    if offer.stock < line["qty"]:
        return False
    if line["code"] and _norm(offer.code) == _norm(line["code"]):
        return True
    if not (line["width"] and _same_size(line, offer) and _terms_ok(line, offer)):
        return False
    if line["brand"] and brand_key(line["brand"]) != _brand(offer):
        return False
    return not line["model"] or _norm(line["model"]) == _norm(offer.model_raw)


def _analog(line: dict, offer) -> bool:
    if offer.stock < line["qty"] or not line["width"] or not (line["any_brand"] or line["any_size"]) or not _terms_ok(line, offer):
        return False
    if not (_near_size(line, offer) if line["any_size"] else _same_size(line, offer)):
        return False
    return line["any_brand"] or not line["brand"] or brand_key(line["brand"]) == _brand(offer)


def _line_result(line: dict, offers: List) -> Optional[tuple]:
    for offer in offers:
        if _exact(line, offer):
            return "exact", offer
    for offer in offers:
        if _analog(line, offer):
            return "analog", offer
    return None


def _mark(conn, card_id, kind: str, found: List, now: str, notify=None) -> None:
    card = orders.get_card(conn, card_id)
    tag, rule, title = (TAG_EXACT, TASK_EXACT, TITLE_EXACT) if kind == "exact" else (TAG_ANALOG, TASK_ANALOG, TITLE_ANALOG)
    wanted = "; ".join(describe(w) for w in card["wants"])
    shown = "; ".join("%s (%d шт.)" % (o.name, o.stock) for o in found[:3])
    body = "Заказ №%d, клиент %s. Ответственный: %s. Нужно: %s. В наличии: %s." % (
        card_id, card["client_name"] or card["client_phone"] or "—", card["responsible"], wanted, shown)
    made = conn.execute(
        "INSERT OR IGNORE INTO order_task (order_id, rule_id, entered_at, kind, arg, stage_id, status, title, body, result, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (card_id, rule, card["entered_at"], "remind", "", card["stage_id"], "pending", title, body, "", now)).rowcount
    if made:
        orders.add_event(conn, card_id, now, AUTHOR, "rule", "Задача создана: %s" % title)
    conn.commit()
    if made:                                                       # the tag comes with the task, once per stay: a manager who takes it off is not given it again
        tags = [t for t in card["tags"] if t != TAG_ANALOG or kind != "exact"]     # the exact find replaces the analog tag
        if tag not in tags:
            tags.append(tag)
        if tags != card["tags"]:
            try:
                orders.update_card(conn, card_id, {"tags": ",".join(tags)}, now, AUTHOR)
            except orders.OrderError:                              # ten tags already: the task is there, the person is still told below
                conn.rollback()
                logging.getLogger("tires_sync.order_wait").warning("card %s could not take the tag", card_id)
    if made and notify is not None:                                # one alert per task: the next cycle finds the task and says nothing
        try:
            notify(card_id, title, body)
        except Exception:                                          # an alert that fails never stops the check
            conn.rollback()
            logging.getLogger("tires_sync.order_wait").warning("alert for card %s failed", card_id, exc_info=True)


def check(conn, offers: Iterable, now: str, notify=None) -> Dict[str, List[int]]:
    """Look at every card in «Ожидает наличие» against the offers in stock. Returns {"exact": [card ids], "analogs": [card ids]}.
    Marking is once per stay in the stage (the task), the tag only once. `notify(card_id, title, body)` is called once for each new task."""
    stock = [o for o in offers if o.stock >= 1]
    out: Dict[str, List[int]] = {"exact": [], "analogs": []}
    ids = [r[0] for r in conn.execute("SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.code = 'wait' ORDER BY c.id")]
    for card_id in ids:
        lines = list_wants(conn, card_id)
        results = [_line_result(line, stock) for line in lines]
        if not lines or any(r is None for r in results):
            continue                                               # a part of the set is no sale: nothing is marked
        kind = "exact" if all(r[0] == "exact" for r in results) else "analogs"
        out[kind].append(card_id)
        try:
            _mark(conn, card_id, "exact" if kind == "exact" else "analog", [r[1] for r in results], now, notify)
        except orders.OrderError:                                  # a card that cannot take the tag (ten tags already) must not stop the others
            conn.rollback()
            logging.getLogger("tires_sync.order_wait").warning("card %s was found but could not be marked", card_id, exc_info=True)
    return out


# ---- from a chat: «Клиент ждёт эту шину» ---------------------------------------------------------------------------------------------------

DEFAULT_QTY = 4
SIZE_IN_TITLE = re.compile(r"(?<!\d)(\d{3})\s*[/ ]\s*(\d{2})\s*(?:[/ ]\s*|Z?[RР]\s*|-\s*)(\d{2})(?!\d)", re.IGNORECASE)      # the Latin R and the Cyrillic Р


def _offer_brand(offer) -> str:
    return re.sub(r"^S\d+:", "", offer.brand_key)


def line_from_offer(offer) -> dict:
    """What a client who asked about this tire waits for: its brand, model, size, season and studs (the quantity is the default four)."""
    season = offer.season.value if offer.season.value in SEASON_NAMES else ""                      # a season the supplier did not name is left to the manager
    return {"brand": _offer_brand(offer), "model": " ".join(str(offer.model_raw or "").split()), "width": offer.width, "profile": offer.height,
            "radius": offer.diameter, "season": season, "studded": "yes" if offer.studded else "no", "qty": DEFAULT_QTY}


def line_from_title(title: str, brands: Iterable[str]) -> dict:
    """What can be read from the title of an ad that is not in our catalog: the size always, the brand when it is a known one, season and studs
    when the title says so. Nothing is guessed: what is not there stays empty."""
    text = str(title or "")
    out = {"qty": DEFAULT_QTY}
    found = SIZE_IN_TITLE.search(text)
    if found:
        out.update(width=int(found.group(1)), profile=int(found.group(2)), radius=int(found.group(3)))
    upper = " ".join(text.upper().split())
    for brand in sorted({b for b in brands if b}, key=len, reverse=True):
        if re.search(r"(?<![0-9A-ZА-ЯЁ])%s(?![0-9A-ZА-ЯЁ])" % re.escape(brand.upper()), upper):
            out["brand"] = brand
            break
    low = text.lower()
    if "всесезон" in low:
        out["season"] = "all"
    elif "зимн" in low or "зима" in low:
        out["season"] = "winter"
    elif "летн" in low or "лето" in low:
        out["season"] = "summer"
    said = studs_named(text)                                                   # the one set of stud rules (webmim)
    if said is not None:
        out["studded"] = "yes" if said else "no"
    return out


def resolve_line(offers: Iterable, fields: dict) -> dict:
    """Season and studs of a manual line: a tire the catalog knows gets them from the catalog; one it does not know must be given by the manager."""
    fields = dict(fields)
    size = (_number(fields.get("width"), "Ширина", 100, 400), _number(fields.get("profile"), "Профиль", 20, 100), _number(fields.get("radius"), "Радиус", 10, 30))
    offers = list(offers)
    brand = brand_key(str(fields.get("brand") or "")) if str(fields.get("brand") or "").strip() else ""
    keys = {_offer_brand(o) for o in offers}
    if brand and brand not in keys:                                    # «Nokian» for «NOKIAN TYRES»: the typed name is the first word(s) of exactly one known brand
        starts = [k for k in keys if k.startswith(brand + " ")]
        if len(starts) == 1:
            brand = starts[0]
            fields["brand"] = brand
    model = _norm(fields.get("model"))
    known = [o for o in offers if o.season.value in SEASON_NAMES and all(size) and (o.width, o.height, o.diameter) == size and (not brand or _offer_brand(o) == brand)
             and (not model or _norm(o.model_raw) == model)]
    seasons = {o.season.value for o in known}
    season = str(fields.get("season") or "").strip()
    if not season:
        if len(seasons) == 1:
            season = next(iter(seasons))
        elif len(seasons) > 1:
            raise orders.OrderError("В каталоге есть такой размер в нескольких сезонах (%s): выберите сезон." % ", ".join(SEASON_NAMES[s] for s in sorted(seasons)))
        else:
            raise orders.OrderError("Выберите сезон: такой шины нет в каталоге.")
    if not str(fields.get("studded") or "").strip():
        studs = {("yes" if o.studded else "no") for o in known if o.season.value == season}
        fields["studded"] = next(iter(studs)) if len(studs) == 1 else ""
    fields["season"] = season
    return fields


# ---- from the catalog: «В лист ожидания» (the owner 2026-10-24) ----------------------------------------------------------------------------

SAME_LINE = ("brand", "model", "code", "width", "profile", "radius", "season", "studded", "qty")


def _phone_key(phone) -> str:
    """The same number however it is typed: «+7 900 …», «8 900 …», «900 …» are the last ten digits."""
    return re.sub(r"\D", "", str(phone or ""))[-10:]


def catalog_line(offer, fields: dict) -> dict:
    """The line of a position of the catalog: brand, model, size, season and studs of the position; the quantity and the two ticks from the
    window; the season from the window only when the position has none (then it must be chosen)."""
    line = line_from_offer(offer)
    line["qty"] = fields.get("qty") if str(fields.get("qty") or "").strip() else DEFAULT_QTY
    line["any_brand"], line["any_size"] = _flag(fields.get("any_brand")), _flag(fields.get("any_size"))
    if not line["season"]:
        line["season"] = str(fields.get("season") or "").strip()
        if not line["season"]:
            raise orders.OrderError("Выберите сезон: у этой позиции он не указан.")
    if not (line["width"] and line["profile"] and line["radius"]):
        raise orders.OrderError("У позиции не указан размер: добавьте клиента в лист ожидания из карточки заказа.")
    return line


def waiting_card(conn, phone: str, clean: dict) -> Optional[int]:
    """An open card in «Ожидает наличие» of the same phone with the same line: the client already waits for this tire."""
    key = _phone_key(phone)
    if not key:
        return None
    for card_id, card_phone in conn.execute("SELECT c.id, c.client_phone FROM order_card c JOIN order_stage s ON s.id = c.stage_id "
                                            "WHERE s.code = 'wait' ORDER BY c.id"):
        if _phone_key(card_phone) != key:
            continue
        for w in list_wants(conn, card_id):
            if all(w[k] == clean[k] for k in SAME_LINE) and w["any_brand"] == bool(clean["any_brand"]) and w["any_size"] == bool(clean["any_size"]):
                return card_id
    return None


def wait_from_catalog(conn, offer, fields: dict, now: str, author: str) -> tuple:
    """«В лист ожидания» from the catalog: a new order right in «Ожидает наличие» with the client's name and phone (both needed), the point
    and the comment of the window and one line of the position. All or nothing; the rules of the stage run after it is saved, as for any
    new order. Returns (id of the order, True), or (id of the order that already waits for this tire with this phone, False): no second one."""
    name = orders._clean("client_name", fields.get("client_name"), conn)
    phone = orders._clean("client_phone", fields.get("client_phone"), conn)
    if not name:
        raise orders.OrderError("Укажите имя клиента.")
    if not phone:
        raise orders.OrderError("Укажите телефон клиента.")
    line = catalog_line(offer, fields)
    clean = _clean(line)                                               # raises for a bad window before anything is made
    card = {"client_name": name, "client_phone": phone, "point_id": fields.get("point_id"), "comment": fields.get("comment"),
            "responsible": orders.default_responsible(conn)}
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")                                    # two tabs pressing at once: the second sees the first one's order
    try:
        found = waiting_card(conn, phone, clean)
        if found is not None:
            conn.rollback()
            return found, False
        stage = next((s for s in orders.list_stages(conn) if s["code"] == "wait"), None)
        if stage is None:
            raise orders.OrderError("В воронке нет этапа «Ожидает наличие».")
        card_id = orders.create_card(conn, card, now, author, commit=False, stage=stage)
        add_want(conn, card_id, line, now, author, commit=False)
        orders.add_event(conn, card_id, now, author, "want", "Из каталога: %s (код %s)" % (offer.name, offer.code or offer.goods_id))
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    orders._fire_rules(conn, card_id, now, ())
    return card_id, True
