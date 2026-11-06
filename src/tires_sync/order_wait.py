"""What a waiting client wants (stage «Ожидает наличие») and the check of waiting cards against the stock.
A line is one wanted tire (size, season, studs, quantity, optional brand, model, code) with two ticks: other brands, other sizes.
A card is found exactly when every line is in stock under its own terms; it is found with analogs when every line is covered but some
only by an analog (another brand and/or a neighbour size). A partial find marks nothing: a part of the set is no sale (owner, 2026-10-15).
Found exactly, the client is written to by itself in the order's Avito chat, in the working hours, once per stay in the stage (the owner
2026-11-04, `_tell_client`); when it cannot go the responsible manager gets the task instead. Found with analogs: only the task."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import message_texts, order_templates as templates, orders, store
from .suppliers import supplier_of_id
from .webmim import brand_key, studs_named

SEASONS = ("", "winter", "summer", "all")
SEASON_NAMES = {"winter": "зима", "summer": "лето", "all": "всесезон"}
STUDS = ("", "yes", "no")
STUD_NAMES = {"yes": "шипы", "no": "без шипов"}
NEIGHBOUR_WIDTH, NEIGHBOUR_PROFILE = 10, 5          # «другие размеры»: the same rim, the width within ±10 mm, the profile within ±5
TAG_EXACT, TAG_ANALOG = "Появилось", "Есть аналоги"
TASK_EXACT, TASK_ANALOG = 0, -1                      # the «rule» numbers of the two system tasks (a task is one per card per stay in the stage)
TASK_REPLY = -5                                      # «Клиент ответил…» (order_engine: −2 placed, −3 arrived; order_intercity: −4 shipped)
TITLE_REPLY = "Клиент ответил на сообщение о наличии"
TASK_SILENT = -6                                     # «Клиент молчит…»: no answer to «появилось в наличии» (the owner 2026-11-05)
TITLE_SILENT = "Клиент молчит 3 дня: снять с него шины?"
SILENT_DAYS = 3                                      # calendar days from the send
MAX_SENDS = 20                                       # messages per cycle: the rest go at the next one, an hour later
NAME_WORDS = re.compile(r"\d{3}[/ ]?\d{2}([/ ]?Z?R?\d{2}C?)?|Z?R\d{2}C?|\d{2,3}(/\d{2,3})?[H-Z]|XL|RF|RFT|RUNFLAT|-|"
                        r"ШИН[АЫ]?|РЕЗИНА|НОВ\w*|ЗИМН\w*|ЗИМА|ЛЕТН\w*|ЛЕТО|ВСЕСЕЗОН\w*|ШИП\w*|НЕШИП\w*|БЕЗ|ЛИПУЧКА")
BEFORE_MODEL = re.compile(r"\d{2,3}|[A-Z]")                 # in WebMIM's own names the indexes stand before the model: «IKON 205/55/16 T 94 Test Model»
HELD = "wait_held:%s"                                # app_state, per send mark: the positions the client was told of, [[position, pieces]]: they are held
SENT_TEXT = "wait_text:%s"                           # app_state, per send mark: the words of a send until it is known to have gone
TITLE_EXACT, TITLE_ANALOG = "Позиция появилась: предложите клиенту", "Есть аналоги: можно предложить клиенту"
TITLE_CHECK = "Позиция появилась: проверьте чат клиента"    # the message may have gone: the same task, without the card's form «отправить клиенту»
AUTHOR = "лист ожидания"
TOLD = "Сообщение о наличии отправлено: "             # the record of the journal after the send: the mark of the stay
QUEUED_TOLD = "Сообщение о наличии не отправлено: первым написали заказу №%d"
QUEUED_TASK = "Сообщение о наличии не отправлено: шины первым предлагает менеджер заказу №%d"
NOT_SENT = "Клиенту само не отправлено: %s"
CUT_OFF = "Сообщение клиенту «появилось в наличии»: неизвестно, ушло ли. Проверьте чат Авито заказа: если сообщения там нет, напишите клиенту, повторно не отправляйте."
PriceOf = Callable[..., object]                      # (offer, chat row) -> the sale price of one tyre as the client would pay it today
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
        lines.append("%s, %d шт" % (", ".join(parts), line["qty"]))                # the dot is the sentence's: «4 шт. Подскажите»
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


def _tag(conn, card_id, card: dict, kind: str, now: str) -> None:
    """The tag of the find, once per stay: a manager who takes it off is not given it again; the exact find replaces the analog tag."""
    tag = TAG_EXACT if kind == "exact" else TAG_ANALOG
    tags = [t for t in card["tags"] if t != TAG_ANALOG or kind != "exact"]
    if tag not in tags:
        tags.append(tag)
    if tags != card["tags"]:
        try:
            orders.update_card(conn, card_id, {"tags": ",".join(tags)}, now, AUTHOR)
        except orders.OrderError:                                  # ten tags already: the task or the message is there, the person is still told
            conn.rollback()
            logging.getLogger("tires_sync.order_wait").warning("card %s could not take the tag", card_id)


def _mark(conn, card_id, kind: str, found: List, now: str, notify=None, note: str = "", title: str = "") -> None:
    card = orders.get_card(conn, card_id)
    rule, title = (TASK_EXACT, title or TITLE_EXACT) if kind == "exact" else (TASK_ANALOG, TITLE_ANALOG)
    wanted = "; ".join(describe(w) for w in card["wants"])
    shown = "; ".join("%s (%d шт.)" % (o.name, o.stock) for o in found[:3]) or "см. чат заказа"
    body = "Заказ №%d, клиент %s. Ответственный: %s. Нужно: %s. В наличии: %s." % (
        card_id, card["client_name"] or card["client_phone"] or "—", card["responsible"], wanted.rstrip("."), shown)     # «4 шт.» ends the line
    if note:
        body += " " + note
    made = conn.execute(
        "INSERT OR IGNORE INTO order_task (order_id, rule_id, entered_at, kind, arg, stage_id, status, title, body, result, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (card_id, rule, card["entered_at"], "remind", "", card["stage_id"], "pending", title, body, "", now)).rowcount
    if made:
        orders.add_event(conn, card_id, now, AUTHOR, "rule", "Задача создана: %s" % title)
    conn.commit()
    if made:                                                       # the tag comes with the task, once per stay
        _tag(conn, card_id, card, kind, now)
    if made and notify is not None:                                # one alert per task: the next cycle finds the task and says nothing
        try:
            notify(card_id, title, body)
        except Exception:                                          # an alert that fails never stops the check
            conn.rollback()
            logging.getLogger("tires_sync.order_wait").warning("alert for card %s failed", card_id, exc_info=True)


def _same_code(line: dict, offer) -> bool:
    return bool(line["code"]) and _norm(offer.code) == _norm(line["code"])


def _matches(line: dict, stock: List) -> List:
    """The offers of a line: of its code when one has it (_exact also lets a line with a code through by its size, for the manager's task),
    else all the exact ones."""
    found = [o for o in stock if _exact(line, o)]
    return [o for o in found if _same_code(line, o)] or found


def _names_only_this_model(name: str, brand: str, model: str) -> bool:
    """The name has the model and nothing that could make it another one: besides the model only the brand and the common words. «Ice Zero» is
    not named by «Ice Zero 2», «Ice Zero FR» or «Super Ice Zero»: what is not known here leaves the choice to a person."""
    text = " ".join(re.sub(r"\d+ *ШТ\.?", " ", str(name or "").upper().replace(",", " ")).split())
    found = re.search(r"(?<![0-9A-ZА-ЯЁ])%s(?![0-9A-ZА-ЯЁ])" % re.escape(model), text)
    if not found:
        return False
    return (all(NAME_WORDS.fullmatch(w) or w in brand.split() or BEFORE_MODEL.fullmatch(w) for w in text[:found.start()].split())
            and all(NAME_WORDS.fullmatch(w) for w in text[found.end():].split()))


def _names_in_order(conn, card: dict, offer) -> List[str]:
    """The names the order has that name the model of the offer: of its positions, or of the ad the client wrote about."""
    names = [i["name"] for i in card.get("items") or []]
    names += [r[0] for r in conn.execute("SELECT item_title FROM avito_chat WHERE order_id = ?", (card["id"],))]
    model = " ".join(str(offer.model_raw or "").upper().split())
    return [str(n) for n in names if model and _names_only_this_model(n, _brand(offer), model)]


def _one_tire(conn, card: dict, line: dict, stock: List) -> str:
    """Why the line does not name one tire to offer without a person, "" when it does: its code in stock, its brand, season and model, or its
    brand and season when the one model in stock is the one named in the order, its studs not against that name (the owner 2026-11-05). One
    tire: the model, the season, the studs, the size, the indexes, runflat and XL all the same. An empty field of a line fits anything (_exact):
    «205/55 R16» of an ad's title would offer the first tire of that size of any season."""
    if line["code"] and not any(_same_code(line, o) for o in stock if _exact(line, o)):
        return "артикула %s нет в наличии, подходит только другая шина того же размера." % line["code"]
    if not line["code"] and not (line["brand"] and line["season"]):
        return "строка заказа не называет одну шину (нужен артикул, либо бренд и сезон)."
    found = _matches(line, stock)
    if len({(_brand(o), _norm(o.model_raw), o.season.value, bool(o.studded), o.width, o.height, o.diameter, _norm(o.speed), _norm(o.load), bool(o.runflat), bool(o.xl))
            for o in found}) != 1:
        return "строка заказа не называет одну шину: подходит несколько моделей или индексов."
    if line["code"]:
        return ""
    if not _norm(found[0].model_raw):
        return "у позиции %s не указана модель." % (found[0].code or found[0].goods_id)
    if not line["model"]:
        names = _names_in_order(conn, card, found[0])
        if not names:
            return "модель в наличии (%s) не совпадает с названием в заказе." % found[0].model_raw
        if any(studs_named(n) is not None and studs_named(n) != bool(found[0].studded) for n in names):
            return "шипы шины в наличии (%s) не те, что в названии в заказе." % found[0].model_raw
    return ""


def _is_mim(offer) -> bool:
    """A position of the first supplier, WebMIM: the others' ids begin with «s<number>-» (suppliers.load_all_parsed)."""
    return supplier_of_id(str(offer.goods_id)) == 1


def _line_price(conn, line: dict, pick, stock: List, chat, price_of: PriceOf, left: Dict[str, int]) -> Tuple[object, float, str]:
    """(the offer to name, its price, "") or (None, 0, why): the price of WebMIM (the owner 2026-11-05: «приоритет у МИМ»); the others' only when
    WebMIM has no price or its stock went to an earlier order, and only when they agree and have what earlier orders left. Two prices in the
    group that decides: a person chooses."""
    found = [o for o in _matches(line, stock) if o is pick or left.get(o.goods_id, o.stock) >= line["qty"]]
    groups = ([[o for o in found if _is_mim(o)]] if _is_mim(pick) else []) + [[o for o in found if not _is_mim(o)]]
    for group in groups:
        priced: Dict[float, object] = {}
        for each in group:
            try:
                price = float(price_of(each, chat) or 0)
            except Exception:                                      # no price is no message: the manager gets the task, the others go on
                conn.rollback()
                logging.getLogger("tires_sync.order_wait").warning("position %s of a waiting order has no price", each.code or each.goods_id, exc_info=True)
                price = 0
            if price > 0:
                priced.setdefault(price, each)
        if len(priced) > 1 and _is_mim(group[0]):                  # two WebMIM positions of one tire: the higher price (the owner 2026-11-05)
            price = max(priced)
            return priced[price], price, ""
        if len(priced) > 1:
            return None, 0, "у позиции %s разные цены у поставщиков: выберите сами." % pick.name
        if priced:
            price, offer = next(iter(priced.items()))
            return offer, price, ""
    return None, 0, "у позиции %s нет цены продажи." % (pick.code or pick.goods_id)


def _ad_price(conn, line: dict, stock: List, chat) -> Optional[float]:
    """The price of one tyre of the line in the ad of the account of the chat (order_offers.ad_prices, what the feed published; the files of the
    feed are only read): the highest of the positions of this tire in stock, whichever of them the client saw and whatever other orders hold
    of them. None when no ad of the account has it."""
    from . import order_offers
    found = _matches(line, stock)
    prices = order_offers.ad_prices(conn, order_offers.account_for(conn, chat_row=chat), [o.goods_id for o in found]) if found else {}
    return float(max(prices.values())) if prices else None


def _found_text(conn, lines: List[dict], picks: List, stock: List, chat, price_of: PriceOf, left: Dict[str, int],
                notes: Optional[List[str]] = None) -> Tuple[str, str, list]:
    """The words for the client, "" and the positions they name [[position, pieces]]; or "", why they cannot go and []: nothing goes with an
    empty name or price or a placeholder left, nor with a price nobody can tell (_line_price). The price of a piece is never below the price of
    the ad of the account of the chat (the owner 2026-11-05: «можем продавать дороже», never cheaper than the ad): below it, the ad's; the ad
    not known, the price as it is and a line in `notes` for the order's journal."""
    body = message_texts.body(conn, message_texts.WAIT_FOUND)
    if set(templates.PLACEHOLDER.findall(body)) != {"name", "price"}:
        return "", "в шаблоне «%s» нужны {name} и {price}, других подстановок быть не может." % message_texts.entry(message_texts.WAIT_FOUND)["name"], []
    items: List[tuple] = []
    held: Dict[str, int] = {}
    for line, pick in zip(lines, picks):
        offer, price, why = _line_price(conn, line, pick, stock, chat, price_of, left)
        if why:
            return "", why, []
        floor = _ad_price(conn, line, stock, chat)
        if floor is None and notes is not None:
            notes.append("Цена объявления позиции %s не определена: в сообщении о наличии расчётная цена %s ₽." % (
                offer.code or offer.goods_id, templates.money(price)))
        elif floor is not None and price < floor:
            price = floor
        name = " ".join(str(offer.name_orig or offer.name or "").split())
        if not name:
            return "", "у позиции %s нет наименования." % (offer.code or offer.goods_id), []
        held[offer.goods_id] = held.get(offer.goods_id, 0) + line["qty"]
        own = sum(l["qty"] for l, p in zip(lines, picks) if p is offer)    # what this order took of it itself; the rest is what earlier orders left
        if held[offer.goods_id] > left.get(offer.goods_id, offer.stock) + own:     # two lines of one tire, named from the other supplier
            return "", "остатка позиции %s не хватает на все строки заказа." % (offer.code or offer.goods_id), []
        if (name, price) not in items:                             # two lines of one position: it is named once
            items.append((name, price))
    shown = [templates.money(p) + " ₽" for _n, p in items]
    if len(set(shown)) == 1:
        price_text = shown[0]
    else:                                                          # in the order of the names: a price is not left out because another has it
        price_text = "%s и %s" % (", ".join(shown[:-1]), shown[-1])
    values = {"name": ", ".join(n for n, _p in items), "price": price_text}
    text = templates.PLACEHOLDER.sub(lambda m: values[m.group(1)], body)
    if "{" in text or "}" in text or templates.unfilled(text):
        return "", "в тексте осталась незаполненная подстановка.", []
    return text, "", [[g, q] for g, q in held.items()]


def form_text(conn, card: dict, offers: Iterable, price_of: Optional[PriceOf], chat_row=None) -> str:
    """The words of the form of the task «Позиция появилась: предложите клиенту» (the owner 2026-11-05): those the message by itself would have,
    the name and the price of a piece (_found_text), priced for the chat `chat_row` the form sends to; the words of the lines (client_text) when
    the tire or its price cannot be told now. A person may change them; nothing goes without the press."""
    fallback = client_text(card)
    if price_of is None or not card["wants"]:
        return fallback
    try:
        stock = [o for o in offers if o.stock >= 1]
        lines = card["wants"]
        if any(_one_tire(conn, card, line, stock) for line in lines):
            return fallback
        picks, _holder = _take(lines, stock, {}, {})
        if picks is None:
            return fallback
        text, _why, _held = _found_text(conn, lines, picks, stock, chat_row, price_of, {})
        return text or fallback
    except Exception:                                              # a form is never broken by its words
        conn.rollback()
        logging.getLogger("tires_sync.order_wait").warning("the words of the form of card %s", card.get("id"), exc_info=True)
        return fallback


def _token(card_id, stay) -> str:
    return "wait-%d-%s" % (card_id, re.sub(r"\D", "", stay or ""))


def _state(conn, card_id) -> str:
    """Where this stay in the stage stands: "told" (the message went), "task" (the manager has it to offer, or closed it having offered),
    "closed" (the task was skipped), "" (nothing yet). "told" and "task" hold the tires against younger orders."""
    stay = conn.execute("SELECT entered_at FROM order_card WHERE id = ?", (card_id,)).fetchone()[0] or ""
    if conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND kind = 'chat' AND text LIKE ? AND at >= ? LIMIT 1", (card_id, TOLD + "%", stay)).fetchone():
        return "told"
    task = conn.execute("SELECT status FROM order_task WHERE order_id = ? AND rule_id = ? AND entered_at = ?", (card_id, TASK_EXACT, stay)).fetchone()
    return "" if task is None else "closed" if task[0] == "skipped" else "task"


def _take(lines: List[dict], stock: List, left: Dict[str, int], first: Dict[str, tuple]) -> Tuple[Optional[List], Optional[tuple]]:
    """The offers of an order out of what earlier orders left in this cycle (`left`, per position; WebMIM first). (the offers, None); or (None, the
    order that holds the tires), (None, None) when its own lines alone are more than there is: then nothing is taken."""
    taken: Dict[str, int] = {}
    picks = []
    for line in lines:
        found = sorted(_matches(line, stock), key=lambda o: not _is_mim(o))
        pick = next((o for o in found if left.get(o.goods_id, o.stock) - taken.get(o.goods_id, 0) >= line["qty"]), None)
        if pick is None:
            return None, next((first[o.goods_id] for o in found if o.goods_id in first), None)
        picks.append(pick)
        taken[pick.goods_id] = taken.get(pick.goods_id, 0) + line["qty"]
    for goods_id, qty in taken.items():
        offer = next(o for o in picks if o.goods_id == goods_id)
        left[goods_id] = left.get(goods_id, offer.stock) - qty
    return picks, None


def _queued(conn, card_id, holder: tuple, now: str) -> None:
    """The mark of an order behind another for the same tires (the owner 2026-11-05), once per stay and per the order ahead of it."""
    text = (QUEUED_TOLD if holder[1] == "told" else QUEUED_TASK) % holder[0]
    stay = conn.execute("SELECT entered_at FROM order_card WHERE id = ?", (card_id,)).fetchone()[0] or ""
    if not conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND kind = 'chat' AND text = ? AND at >= ? LIMIT 1", (card_id, text, stay)).fetchone():
        orders.add_event(conn, card_id, now, AUTHOR, "chat", text, level="yellow")
        conn.commit()


def _tell_client(conn, card_id, lines: List[dict], picks: List, stock: List, now: str, deliver, price_of: Optional[PriceOf], may_send: bool = True,
                 left: Optional[Dict[str, int]] = None) -> Optional[Tuple[str, str]]:
    """Everything the client waits for is here: «появилось в наличии» in the order's chat, by chats.send_reply, the send mark being the order
    and the moment it entered the stage. None: no task is due (it went and the journal has it; it went earlier in this stay; outside the
    working hours, or past the limit of a cycle (not `may_send`), the next cycle looks again). Else the task is due: (the words for its body, its title): why the client was not written to
    (the line does not name one tire, no chat, no price, a failed send), or, when it may have gone, TITLE_CHECK, which has no form of sending
    and says not to send a second one; the sync that delivers it closes it (`delivered`). A task of this stay already: nothing goes."""
    from . import chats, order_manager
    card = orders.get_card(conn, card_id)
    stay = card["entered_at"] or ""
    if card["stage"]["code"] != "wait":                            # moved while the check was sending to the others
        return None
    if conn.execute("SELECT 1 FROM order_task WHERE order_id = ? AND rule_id = ? AND entered_at = ?", (card_id, TASK_EXACT, stay)).fetchone():
        return "", ""
    if conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND kind = 'chat' AND text LIKE ? AND at >= ? LIMIT 1", (card_id, TOLD + "%", stay)).fetchone():
        return None
    for line in lines:
        why = _one_tire(conn, card, line, stock)
        if why:
            return NOT_SENT % why, ""
    chat, why = order_manager._client_chat(conn, card_id)
    if chat is None:
        return NOT_SENT % (why + "."), ""
    if deliver is None or price_of is None:
        return NOT_SENT % "ключи Авито недоступны.", ""
    hours = order_manager.duty_settings(conn)["hours"]
    if not may_send or not hours[0] <= int(now[11:13]) < hours[1]:    # the cycle runs all night: the client is not woken, nor the manager for a price
        return None
    if chats._unread(conn, chat):                                  # our message would count as the answer and wipe «Клиент написал»
        return NOT_SENT % "клиент написал в чат и ждёт ответа.", ""
    notes: List[str] = []
    text, why, held = _found_text(conn, lines, picks, stock, chat, price_of, left or {}, notes)
    if why:
        return NOT_SENT % why, ""
    for note in dict.fromkeys(notes):                              # the ad's price not known: the price as it is, and the journal says so
        if not conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND kind = 'chat' AND text = ? AND at >= ? LIMIT 1", (card_id, note, stay)).fetchone():
            orders.add_event(conn, card_id, now, AUTHOR, "chat", note, level="yellow")
    conn.commit()
    token = _token(card_id, stay)
    key = SENT_TEXT % token                                        # the words that go: the history has these, the price may change before it is written
    store.set_state(conn, key, text)
    store.set_state(conn, HELD % token, json.dumps(held))
    status, why, _copy = order_manager.send_to_client(conn, card, text, token, deliver, now)
    if status == "sent":
        orders.add_event(conn, card_id, now, AUTHOR, "chat", TOLD + text, level="green")
        conn.execute("DELETE FROM app_state WHERE key = ?", (key,))
        conn.commit()
        _tag(conn, card_id, card, "exact", now)
        return None
    if status == "failed":
        conn.execute("DELETE FROM app_state WHERE key IN (?, ?)", (key, HELD % token))
        orders.add_event(conn, card_id, now, AUTHOR, "chat", "Сообщение клиенту «появилось в наличии» не отправлено: %s" % why, level="red")
        conn.commit()
        return NOT_SENT % why, ""
    mark = conn.execute("SELECT status FROM chat_send_log WHERE token = ?", (token,)).fetchone()
    mark = mark[0] if mark else ""
    said = why if mark in ("retry", "lost") else "%s. Проверьте чат Авито заказа: если сообщения там нет, напишите клиенту, повторно не отправляйте." % why
    orders.add_event(conn, card_id, now, AUTHOR, "chat", "Сообщение клиенту «появилось в наличии»: %s" % said, level="yellow" if mark == "retry" else "red")
    conn.commit()
    return "Сообщение клиенту «появилось в наличии»: %s" % said, TITLE_CHECK


def delivered(conn, token: str, text: str, now: str, how: str) -> None:
    """The minute's sync delivered «появилось в наличии» after an unknown outcome or a 402, or found it in the chat, or a press sent it again:
    the task of that stay made meanwhile is done; the order's history has the words that went, once per stay (chats._close_task)."""
    found = re.fullmatch(r"wait-(\d+)-(\d*)", str(token or ""))
    if not found:
        return
    card_id = int(found.group(1))
    for task_id, stay in conn.execute("SELECT id, entered_at FROM order_task WHERE order_id = ? AND rule_id = ? AND status = 'pending'", (card_id, TASK_EXACT)).fetchall():
        if re.sub(r"\D", "", stay or "") == found.group(2):
            conn.execute("UPDATE order_task SET status = 'done', result = ?, done_at = ? WHERE id = ?", (how, now, task_id))
    row = conn.execute("SELECT entered_at FROM order_card WHERE id = ?", (card_id,)).fetchone()
    if row and re.sub(r"\D", "", row[0] or "") == found.group(2) and _state(conn, card_id) != "told":     # another stay: its own marks
        orders.add_event(conn, card_id, now, AUTHOR, "chat", TOLD + (text or store.get_state(conn, SENT_TEXT % token) or how), level="green")
    conn.execute("DELETE FROM app_state WHERE key = ?", (SENT_TEXT % token,))
    conn.commit()


def _answered(conn, log) -> bool:
    """A message of the client in the chat after ours (`log`: chat_row, message_id, at of the send)."""
    ours = conn.execute("SELECT ts FROM avito_message WHERE chat_row = ? AND message_id = ?", (log[0], log[1])).fetchone() if log[1] else None
    since = max(ours[0] if ours else 0, int(datetime.fromisoformat(log[2]).replace(tzinfo=timezone(timedelta(hours=3))).timestamp()))
    return conn.execute("SELECT 1 FROM avito_message WHERE chat_row = ? AND direction = 'in' AND kind NOT IN ('system', 'deleted') AND ts > ? LIMIT 1",
                        (log[0], since)).fetchone() is not None


def replies(conn, now: str, notify=None) -> int:
    """Any message of the client in the chat after «появилось в наличии», while the order is still in that stay in the stage: one task per message
    for the responsible (the owner 2026-11-05; the words are not read as consent), run by the minute's sync. `notify(card_id, title, body)` once
    per task. Returns how many were made."""
    made = 0
    rows = conn.execute("SELECT c.id, c.entered_at FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.code = 'wait'").fetchall()
    for card_id, stay in rows:
        try:
            log = conn.execute("SELECT chat_row, message_id, at FROM chat_send_log WHERE token = ? AND status = 'sent'", (_token(card_id, stay),)).fetchone()
            if log is None or conn.execute("SELECT 1 FROM order_task WHERE order_id = ? AND rule_id = ? AND entered_at = ?", (card_id, TASK_REPLY, stay)).fetchone():
                continue
            if not _answered(conn, log):
                continue
            card = orders.get_card(conn, card_id)
            from . import order_arrived
            body = "Заказ №%d, клиент %s. Ответственный: %s. Ответ клиента в чате заказа: %s" % (
                card_id, card["client_name"] or card["client_phone"] or "—", card["responsible"], order_arrived._link(conn, card_id))
            if conn.execute("INSERT OR IGNORE INTO order_task (order_id, rule_id, entered_at, kind, arg, stage_id, status, title, body, result, created_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (card_id, TASK_REPLY, stay, "remind", "", card["stage_id"], "pending", TITLE_REPLY, body, "", now)).rowcount:
                orders.add_event(conn, card_id, now, AUTHOR, "rule", "Задача создана: %s" % TITLE_REPLY)
                conn.commit()
                made += 1
                if notify is not None:
                    notify(card_id, TITLE_REPLY, body)
            conn.commit()
        except Exception:                                          # one card, or an alert that fails, never stops the others nor the sync
            conn.rollback()
            logging.getLogger("tires_sync.order_wait").warning("the reply of the client of card %s could not be looked at", card_id, exc_info=True)
    return made


def silent(conn, now: str, notify=None) -> int:
    """«Клиент молчит 3 дня» (the owner 2026-11-05): «появилось в наличии» went, the tires are held for the order, and the client has not
    written in the chat since, SILENT_DAYS calendar days after the send: one task for the responsible per stay in the stage, made and told only
    in the working hours. The tires are not let go and the client is sent nothing. Run by the minute's sync. Returns how many were made."""
    from . import order_arrived, order_manager
    hours = order_manager.duty_settings(conn)["hours"]
    if not hours[0] <= int(now[11:13]) < hours[1]:
        return 0
    made = 0
    rows = conn.execute("SELECT c.id, c.entered_at FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.code = 'wait'").fetchall()
    for card_id, stay in rows:
        try:
            token = _token(card_id, stay)
            log = conn.execute("SELECT chat_row, message_id, at FROM chat_send_log WHERE token = ? AND status = 'sent'", (token,)).fetchone()
            if log is None or _state(conn, card_id) != "told" or not store.get_state(conn, HELD % token):
                continue
            if (datetime.fromisoformat(now[:10]) - datetime.fromisoformat(log[2][:10])).days < SILENT_DAYS:
                continue
            if conn.execute("SELECT 1 FROM order_task WHERE order_id = ? AND rule_id IN (?, ?) AND entered_at = ?", (card_id, TASK_SILENT, TASK_REPLY, stay)).fetchone():
                continue
            if _answered(conn, log):
                continue
            card = orders.get_card(conn, card_id)
            body = ("Заказ №%d, клиент %s. Ответственный: %s. Сообщение о наличии ушло %s.%s.%s, клиент не ответил. Шины придержаны за ним и сами не "
                    "снимаются: пока заказ на этапе «%s», они его. Чат заказа: %s") % (
                card_id, card["client_name"] or card["client_phone"] or "—", card["responsible"], log[2][8:10], log[2][5:7], log[2][:4],
                card["stage"]["name"], order_arrived._link(conn, card_id))
            if conn.execute("INSERT OR IGNORE INTO order_task (order_id, rule_id, entered_at, kind, arg, stage_id, status, title, body, result, created_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (card_id, TASK_SILENT, stay, "remind", "", card["stage_id"], "pending", TITLE_SILENT, body, "", now)).rowcount:
                orders.add_event(conn, card_id, now, AUTHOR, "rule", "Задача создана: %s" % TITLE_SILENT)
                conn.commit()
                made += 1
                if notify is not None:
                    notify(card_id, TITLE_SILENT, body)
            conn.commit()
        except Exception:                                          # one card, or an alert that fails, never stops the others nor the sync
            conn.rollback()
            logging.getLogger("tires_sync.order_wait").warning("the silence of the client of card %s could not be looked at", card_id, exc_info=True)
    return made


def check(conn, offers: Iterable, now: str, notify=None, deliver=None, price_of: Optional[PriceOf] = None, max_sends: int = MAX_SENDS) -> Dict[str, List[int]]:
    """Look at every card in «Ожидает наличие» against the offers in stock. Returns {"exact": [card ids], "analogs": [card ids]}.
    Found exactly: the client is written to by `deliver` (chats.send_reply of the chat's account) with the price from `price_of` (_tell_client);
    the task only when that cannot go. Marking is once per stay in the stage (the task), the tag only once. `notify(card_id, title, body)` is
    called once for each new task.
    Stock enough for fewer orders than wait for it (the owner 2026-11-05): the order that waits longest goes first and holds the tires, counted
    across the orders of this cycle: while it stays in this stay in the stage after its message or its task, unless the task was skipped.
    Left the stage or the stock no longer enough for it, the next order gets its message at the next cycle. Until then that one has the mark
    QUEUED_TOLD or QUEUED_TASK and no task. An order whose line does not name one tire (_one_tire) is the manager's and holds nothing. Stock
    for all: all go. At most `max_sends` messages a cycle, the longest waiting first; the rest
    hold their tires and go at the next one."""
    stock = [o for o in offers if o.stock >= 1]
    out: Dict[str, List[int]] = {"exact": [], "analogs": []}
    cards = []
    stays: Dict[int, str] = {}
    for card_id, stay in conn.execute("SELECT c.id, c.entered_at FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.code = 'wait' ORDER BY c.entered_at, c.id").fetchall():
        try:                                                       # a cycle cut off in the send: settled whatever the stock is now
            log = conn.execute("SELECT status FROM chat_send_log WHERE token = ?", (_token(card_id, stay),)).fetchone()
            if log and log[0] == "sent" and _state(conn, card_id) != "told":
                delivered(conn, _token(card_id, stay), "", now, "отправлено")
            elif log and log[0] in ("sending", "unknown", "retry", "lost") and not _state(conn, card_id):      # it may have gone: never the form
                _mark(conn, card_id, "exact", [], now, notify, CUT_OFF, TITLE_CHECK)
        except Exception:
            conn.rollback()
            logging.getLogger("tires_sync.order_wait").warning("the send of card %s could not be settled", card_id, exc_info=True)
        lines = list_wants(conn, card_id)
        results = [_line_result(line, stock) for line in lines]
        if not lines or any(r is None for r in results):
            continue                                               # a part of the set is no sale: nothing is marked
        kind = "exact" if all(r[0] == "exact" for r in results) else "analogs"
        out[kind].append(card_id)
        cards.append((card_id, kind, lines, [r[1] for r in results]))
        stays[card_id] = stay
    log = logging.getLogger("tires_sync.order_wait")
    left: Dict[str, int] = {}
    first: Dict[str, tuple] = {}
    states: Dict[int, str] = {}
    sends = [0]

    def counted(row, text, token):
        sends[0] += 1
        return deliver(row, text, token)

    def hold(picks, card_id, state):
        """Taken means held at once: a failure later in the same card does not give its tires to the next one."""
        for pick in picks:
            if first.setdefault(pick.goods_id, (card_id, state))[0] == card_id:
                first[pick.goods_id] = (card_id, state)

    def take_held(said, card_id, state):
        for goods_id, qty in json.loads(said):
            offer = next((o for o in stock if o.goods_id == goods_id), None)
            if offer is not None and left.get(goods_id, offer.stock) >= qty:
                left[goods_id] = left.get(goods_id, offer.stock) - qty
                first.setdefault(goods_id, (card_id, state))

    def named(card_id, lines):
        card = orders.get_card(conn, card_id)
        return not any(_one_tire(conn, card, line, stock) for line in lines)
    for card_id, kind, lines, _found in cards:                     # those already told or being offered hold their tires, however long the others wait
        if kind == "exact":
            try:
                states[card_id] = _state(conn, card_id)
                said = store.get_state(conn, HELD % _token(card_id, stays[card_id]))
                if states[card_id] in ("told", "task") and said:     # the client was told of these, whatever else fits the line by now
                    take_held(said, card_id, states[card_id])
                elif states[card_id] == "task" and named(card_id, lines):
                    picks, _holder = _take(lines, stock, left, first)
                    hold(picks or [], card_id, states[card_id])
            except Exception:
                conn.rollback()
                log.warning("card %s could not be looked at", card_id, exc_info=True)
                states[card_id] = "closed"
    for card_id, kind, lines, found in cards:
        try:
            if kind == "analogs":
                _mark(conn, card_id, "analog", found, now, notify)
                continue
            if states[card_id]:
                continue
            clear = named(card_id, lines)
            picks, holder = _take(lines, stock, left, first) if clear else (found, None)
            if picks is None:                                      # no holder: its own lines are more than there is, short stock: nothing
                if holder is not None and holder[1]:               # the order ahead waits for the working hours: nothing to say yet
                    _queued(conn, card_id, holder, now)
                continue
            if clear:
                hold(picks or [], card_id, "")
            told = _tell_client(conn, card_id, lines, picks, stock, now, None if deliver is None else counted, price_of, sends[0] < max_sends, left)
            if told is not None:
                _mark(conn, card_id, "exact", picks or found, now, notify, told[0], told[1])
            if clear:
                state = _state(conn, card_id)
                hold(picks or [], card_id, state)
                said = store.get_state(conn, HELD % _token(card_id, stays[card_id]))
                if said:                                           # named another position than the one taken (WebMIM had no price): that one is what is held
                    for pick, line in zip(picks, lines):
                        left[pick.goods_id] += line["qty"]
                    take_held(said, card_id, state)
        except Exception:                                          # a card that cannot be marked (ten tags already, a broken one) must not stop the others
            conn.rollback()
            log.warning("card %s was found but could not be marked", card_id, exc_info=True)
    out["exact"].sort()
    out["analogs"].sort()
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
    line["code"] = str(offer.code or "").strip()                       # the position itself: its indexes, runflat and XL are not in the line
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
