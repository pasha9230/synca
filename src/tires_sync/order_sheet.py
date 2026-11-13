"""The owner's Google table of orders, sheet «Заказы» (`docs/DECISIONS.md`, 2026-10-23): the panel fills it by itself.

A row is a position of an order. An order joins the table when it is placed (the button «Оформить в WebMIM» or the move to «Заказ
оформлен» or later) while the table is switched on in «Подключения → Google-таблица»: the orders placed before never do. It stays only
while its stage is «Заказ оформлен» or later (`listed`, the owner 2026-11-02): moved back, its rows stay, filled grey; placed again, the fill goes. Its rows are
written once with every column, then only the columns that follow the order (UPDATED) are kept up to date; the owner's own columns and
rows are never touched. The table is written by a web app of Apps Script in the table itself (`docs/integrations/google-sheet-apps-script.md`):
it finds a row by the value «tires:<order>:<position>» in its hidden column «tires_id» (the script adds and hides it; a value moves with
its row when the sheet is sorted; the notes of «Дата счета» and «Дата» held it before 2026-11-01 — the script moves those marks over and
drops the notes: the owner wants no note of a mark), not by a cell the owner may edit, and writes into it only while its «Артикул» and «Телефон» are still what the panel wrote (CHECK; the name the owner may correct).

Nothing waits for Google in a request: `enroll` only queues the order (`sheet_order`) in the transaction of the move, `flush` sends what
changed since the last answer, after the request in a thread of its own (panel) and every hour (`orders-tick`). A failure is retried later
(RETRY_MINUTES) and said once in the order's journal; a second sender never takes an order that is being sent (`claim_until`), and the script
never makes a second row with the same mark."""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from . import connections, order_engine as engine, order_merge, order_report, orders, oth_sale, sales as sales_store, store, users

KIND = "gsheet"
SHEET = "Заказы"
COLUMNS = ("Дата счета", "№ заказа MIM", "Перемещение MIM", "Артикул", "Наименование", "Кол-во", "Цена MIM", "Сумма MIM",
           "Цена продажи", "Сумма продажи", "Имя", "Телефон", "ПВЗ", "Этап", "Реализация", "Списание", "Прибыль %", "Прибыль ₽",
           "Уведомление точке", "Уведомление клиенту", "Аккаунт", "Примечание")
OWNERS = ("Примечание",)                    # never filled (the owner 2026-10-30): «Примечание» is the people's; «Дата» is gone (2026-11-01)
NOTE_FLAG = "_oth_sold"                     # the owner 13.11: an order the point sold tyres of (part D): its «Примечание» is the panel's comment, sent
                                            # when it changes (over what was typed in the cell); never for another order
OPTIONAL = ("Аккаунт",)                     # the owner 2026-11-02: sent apart («extra»), written only where the sheet has the column; a sheet
                                            # without it (or a script before 2026-11-02, which never reads «extra») takes the rest all the same.
                                            # Not written once: when the value changes (changes) a written row follows it, over a correction in the cell
                                            # (the owner 2026-11-10: the responsible changed — the written rows are theirs; here the panel wins)
SENT = tuple(c for c in COLUMNS if c not in OWNERS + OPTIONAL)  # what a new row is written with (a sheet without «Примечание» still takes it)
ASKED = tuple(c for c in COLUMNS if c != "Примечание" and c not in OPTIONAL)   # what «Проверить» needs; never «tires_id» (the script's KEY_COLUMN:
                                                               # it adds the hidden column by itself, the panel never sends nor reads it)
UPDATED = ("№ заказа MIM", "Перемещение MIM", "Дата счета", "Этап", "Реализация", "Списание", "Прибыль %", "Прибыль ₽",
           "Уведомление точке", "Уведомление клиенту")          # the rest is written once: a correction of the owner in the cell stays
FOLLOWED = ("Кол-во", "Сумма MIM", "Сумма продажи")              # a position that gave tyres from «На ОТХ» (oth_sale): its row goes down
CHECK = ("Артикул", "Телефон")                              # written once: the script finds the row by its mark and checks it is still there;
                                                             # the phone too (the lead's review 2026-11-01): two rows of one code swapped
                                                             # by a sort that left their marks behind are «missing», never written over
RECENT_DAYS = 45                                               # the hourly look: the orders placed so recently (and every failing one)
URL = re.compile(r"^https://script\.google\.com/(?:macros|a/macros/[a-z0-9.-]{1,100})/s/[A-Za-z0-9_-]{10,200}/exec$")   # a/macros/<domain>: Workspace
REDIRECT = "https://script.googleusercontent.com/"            # Google answers a web app with a redirect there; anywhere else is refused
RETRY_MINUTES = (1, 5, 15, 60)                                 # then every hour
CLAIM_MINUTES = 3
BATCH = 20                                                     # orders sent by one flush after a request (the hourly one takes all)
AUTHOR = "Google-таблица"
REALIZED, REFUSED, FROM_STOCK = "Р", "О", "Б"                  # the legend of «Реализация» (docs/integrations/google-sheet-columns.md)

log = logging.getLogger("tires_sync.sheet")
Transport = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, Dict[str, str], bytes]]


class SheetError(Exception):
    """The table did not take the rows: the text goes to the journal and the check of «Подключения» (never a secret)."""


# ---- settings -----------------------------------------------------------------------------------------------------------------------

def settings(conn) -> dict:
    """url, enabled, since; secret_for: the address the secret was typed for (the secret never goes to another one); keep_from: «Вести
    заказы, оформленные с» (YYYY-MM-DD, the owner 2026-11-01: «старые не надо»; '' — every queued order, as before)."""
    return dict({"url": "", "enabled": False, "since": "", "secret_for": "", "keep_from": ""}, **connections.get_config(conn, KIND, 0))


DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def check_day(value) -> str:
    """«Вести заказы, оформленные с»: a day YYYY-MM-DD or empty (every queued order)."""
    value = str(value or "").strip()
    if value and (not DAY.match(value) or not _valid_day(value)):
        raise SheetError("«Вести заказы, оформленные с»: дата вида 01.11.2026.")
    return value


def _valid_day(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def enabled(conn) -> bool:
    s = settings(conn)
    return bool(s["enabled"] and s["url"])


def check_url(url: str, on: bool) -> str:
    url = (url or "").strip()
    if url and not URL.match(url):
        raise SheetError("Адрес веб-приложения: вида https://script.google.com/macros/s/…/exec (скопируйте его из «Развертывания» Apps Script).")
    if on and not url:
        raise SheetError("Чтобы включить, впишите адрес веб-приложения.")
    return url


def save_settings(conn, url: str, on: bool, now: str, author: str, secret_typed: bool = False, keep_from: Optional[str] = None) -> None:
    """The address of the web app and the switch; `since` is the moment it was switched on (the orders placed from then on).
    `secret_typed`: the secret was typed with this address (it is the address's secret from now on). `keep_from` (None: as it was):
    the orders queued before that day are left alone — never sent nor marked (the owner 2026-11-01); «Включено с» is not touched."""
    url = check_url(url, on)
    before = settings(conn)
    keep_from = before["keep_from"] if keep_from is None else check_day(keep_from)
    since = before["since"] if before["enabled"] and on else (now if on else "")
    connections.set_config(conn, KIND, 0, {"url": url, "enabled": bool(on), "since": since,
                                           "secret_for": url if secret_typed else before["secret_for"], "keep_from": keep_from}, now, author)
    if keep_from != before["keep_from"]:
        store.write_audit(conn, now, author, "sheet_keep_from", {"from": before["keep_from"], "to": keep_from})
    conn.commit()


def _kept(conn) -> Tuple[str, list]:
    """The SQL of «Вести заказы, оформленные с» on sheet_order `s` (empty: every order)."""
    day = settings(conn)["keep_from"]
    return (" AND s.placed_at >= ?", [day]) if day else ("", [])


def _secret(conn, box, s: dict) -> Optional[str]:
    """The secret, only for the address it was typed for."""
    if not s["url"] or s["url"] != s["secret_for"]:
        return None
    return connections.get_secret(conn, box, KIND, 0, "secret")


# ---- the queue ----------------------------------------------------------------------------------------------------------------------

LISTED_CODES = orders.PLACED_CODES + ("cancel", "refused", "return", orders.RETURN_GIVEN)   # «Заказ оформлен» and the system stages after it
BEFORE_CODES = ("new", "talk", "wait", orders.FROM_POINT[0])                                  # before placing, wherever the owner puts them


def listed(conn, card_id: int) -> bool:
    """The order belongs in the table now (the owner 2026-11-02): its stage is «Заказ оформлен» or after it — a system stage by its code
    (moving a stage in the pipeline turns no row grey, the review of 2026-11-02), an own stage of the owner by its place after «Заказ
    оформлен» (orders._needs_placed decides so too). An order back on an earlier stage keeps its rows, filled grey (send_order), and loses
    the fill when it is placed again. A pipeline without «Заказ оформлен»: every own stage."""
    row = conn.execute("SELECT s.code, s.position, s.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE c.id = ?", (int(card_id),)).fetchone()
    if row is None:
        return False
    code, position, stage_id = row
    if code in LISTED_CODES or code in BEFORE_CODES:
        return code in LISTED_CODES
    placed = orders._placed_stage(conn)
    return placed is None or (position, stage_id) >= (placed["position"], placed["id"])          # the order of list_stages


def enroll(conn, card_id: int, now: str) -> None:
    """The order is placed: it joins the table if the table is on and the order stands on «Заказ оформлен» or later (listed: the button
    «Оформить в WebMIM» whose move of the stage failed puts nothing in). In the caller's transaction; once (a second placing keeps the first)."""
    if enabled(conn) and listed(conn, card_id):
        conn.execute("INSERT OR IGNORE INTO sheet_order (order_id, placed_at) VALUES (?, ?)", (int(card_id), now))


OPEN_STAGES = ("placed", "moving", "at_point", "oth", "return_given")      # «Заказ оформлен» … «На ОТХ», «На возврат»: not closed


def open_unlisted(conn) -> List[dict]:
    """The open orders (OPEN_STAGES) the table does not have: placed before it was switched on (the owner 2026-10-30). Oldest first."""
    rows = conn.execute("SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.code IN (%s) AND c.id NOT IN "
                        "(SELECT order_id FROM sheet_order) ORDER BY c.id" % ",".join("?" * len(OPEN_STAGES)), OPEN_STAGES).fetchall()
    return [orders.get_card(conn, r[0]) for r in rows]


def enroll_open(conn, now: str) -> List[int]:
    """`python -m tires_sync sheet-enroll --open`: the open orders placed before the table was on join its queue (the next send writes
    their rows; a row only ever added, the owner's cells never touched). Nothing when the table is off. Returns the orders put in."""
    if not enabled(conn):
        raise SheetError("Google-таблица выключена: включите её в «Подключения → Google-таблица».")
    found = [c["id"] for c in open_unlisted(conn)]
    for card_id in found:
        enroll(conn, card_id, now)
    conn.commit()
    return found


def _due(conn, now: str, full: bool) -> List[int]:
    """The orders to look at: those that failed and wait no longer, never sent, or with a record in the journal since the last look;
    `full` (the hourly tick) looks at every order placed in the last RECENT_DAYS (a WebMIM date read by the snapshot leaves no record) and
    at every failing one: an old closed order is not counted again every hour."""
    if full:
        changed, args = " AND (s.failing > 0 OR s.placed_at >= ?)", [(datetime.fromisoformat(now) - timedelta(days=RECENT_DAYS)).isoformat(timespec="seconds")]
    else:
        changed, args = (" AND (s.failing > 0 OR s.sent_at = '' OR EXISTS (SELECT 1 FROM order_event e WHERE e.order_id = s.order_id"
                         " AND e.id > s.seen_event AND e.author != ?))"), [AUTHOR]             # its own records are no change of the order
    kept, day = _kept(conn)                                                # the orders before «Вести заказы, оформленные с»: never looked at
    rows = conn.execute("SELECT s.order_id FROM sheet_order s JOIN order_card c ON c.id = s.order_id WHERE (s.next_at = '' OR s.next_at <= ?)"
                        " AND (s.claim_until = '' OR s.claim_until < ?)" + changed + kept + " ORDER BY s.order_id LIMIT ?",
                        [now, now] + args + day + [-1 if full else BATCH])
    return [r[0] for r in rows]


def _claim(conn, card_id: int, now: str) -> bool:
    until = (datetime.fromisoformat(now) + timedelta(minutes=CLAIM_MINUTES)).isoformat(timespec="seconds")
    cur = conn.execute("UPDATE sheet_order SET claim_until = ? WHERE order_id = ? AND (claim_until = '' OR claim_until < ?)", (until, card_id, now))
    conn.commit()
    return cur.rowcount == 1


# ---- the rows of an order -----------------------------------------------------------------------------------------------------------

def _day(text: str) -> str:
    return str(text or "")[:10]


def _stamp(text: str) -> str:
    """«2026-10-23T14:05:00» → «23.10.2026 14:05» (the owner 2026-10-23)."""
    try:
        return datetime.fromisoformat(str(text)).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return ""


def realization(card: dict) -> str:
    code = card["stage"].get("code")
    if code in ("done", "shipped"):                  # «Отправлен»: an order to another city handed to the courier (the owner 2026-11-02)
        return FROM_STOCK if card["deal_kind"] == "stock" else REALIZED
    return REFUSED if code == "oth" else ""          # the owner 2026-11-02: «О» is «На ОТХ» only; «Отмена», «Вернули поставщику», «На возврат»: empty


def _invoice_day(conn, card: dict) -> str:
    """«Дата счета»: the day the order was made in WebMIM (the owner 2026-10-23) — from the snapshot of WebMIM, else the moment the button made it."""
    made = conn.execute("SELECT server, webmim_id, updated_at FROM webmim_create WHERE order_id = ? AND state = 'done'", (card["id"],)).fetchone()
    server = made[0] if made else order_report.WEBMIM_SERVER
    ids = [v for v in (card["webmim_id"], made[1] if made else "") if v]
    found = None
    if ids:
        found = conn.execute("SELECT create_date FROM webmim_order WHERE server = ? AND id IN (%s) AND create_date != '' LIMIT 1"
                             % ",".join("?" * len(ids)), [server] + ids).fetchone()
    if found is None and card["webmim_number"]:
        found = conn.execute("SELECT create_date FROM webmim_order WHERE server = ? AND number = ? AND create_date != '' LIMIT 1",
                             (server, card["webmim_number"])).fetchone()
    if found:
        return _day(found[0])
    return _day(made[2]) if made else ""


def _sent(conn, card_id: int) -> Dict[str, str]:
    """The last message of the order sent to the point («manager») and to the client: when (the task done, by the panel or by hand)."""
    out: Dict[str, str] = {}
    for t in engine.list_tasks(conn, card_id, ("done",)):
        whom = engine.audience(conn, t)
        if whom and t["done_at"] and t["done_at"] > out.get(whom, ""):
            out[whom] = t["done_at"]
    return out


def _profits(conn, card: dict) -> Dict[int, Tuple[float, float]]:
    """Position → (profit ₽, profit %) as the analytics counts it (order_report): the margin of the position's sale (at WebMIM's price where
    there is one) less its share of the order's fixed sums (75 ₽ to the point, 50 ₽ for a client a point handed over), shared by the sum of
    the sale, so the rows add up to the net profit of the order. None before the sale is written and after it is reversed."""
    ids = [i for i in card["sale_ids"] if i > 0]
    if not ids or len(ids) != len(card["sale_ids"]):
        return {}
    names = [c.strip() for c in sales_store.COLUMNS.split(",")]
    rows = {r[0]: dict(zip(names, r)) for r in conn.execute("SELECT %s FROM sale WHERE id IN (%s) AND reversed_at IS NULL"
                                                             % (sales_store.COLUMNS, ",".join("?" * len(ids))), ids)}
    if len(rows) != len(ids):
        return {}
    counted = {r["id"]: r for r in order_report._counted(conn, [rows[i] for i in ids])}
    rates, day = order_report.list_rates(conn), min(r["day"] for r in counted.values())
    fee = order_report.rate_on(rates, "point_fee", day)
    from . import oth_swap
    if oth_swap.handed_fee(conn, card["id"]):                                     # never for a client who left (oth_swap)
        fee += order_report.rate_on(rates, "handoff_fee", day)
    total = sum(r["sale_total"] for r in counted.values())
    pairs = _pair(card["items"], [counted[i] for i in ids])
    out = {}
    for item_id, sale in pairs.items():
        share = fee * (sale["sale_total"] / total if total else 1.0 / len(ids))
        profit = round(sale["margin_total"] - share, 2)
        out[item_id] = (profit, round(profit * 100.0 / sale["sale_total"], 1) if sale["sale_total"] else 0.0)
    return out


def _pair(items: List[dict], sales: List[dict]) -> Dict[int, dict]:
    """The sale of each position: written one per position in the order of the positions (orders.record_sales); a position changed since
    is paired by its code and pieces."""
    if len(items) == len(sales) and all(str(i["goods_id"]) == str(s["goods_id"]) for i, s in zip(items, sales)):
        return {i["id"]: s for i, s in zip(items, sales)}
    left, out = list(sales), {}
    for i in items:
        found = next((s for s in left if str(s["goods_id"]) == str(i["goods_id"]) and int(s["pieces"]) == int(i["qty"])), None)
        if found is not None:
            left.remove(found)
            out[i["id"]] = found
    return out


def key(card_id: int, item_id: int) -> str:
    return "tires:%d:%d" % (int(card_id), int(item_id))


def extra_key(card_id: int, item_id: int) -> str:
    """The row of the pieces added to a position after the order had its own number (order_webmim «extra»): its second order of WebMIM."""
    return "tires:%d:%d:2" % (int(card_id), int(item_id))


def day_of_number(conn, number: str) -> str:
    """«Дата счета» of an order of WebMIM known only by its number (typed for a row): the snapshot of WebMIM, else empty."""
    if not number:
        return ""
    found = conn.execute("SELECT create_date FROM webmim_order WHERE server = ? AND number = ? AND create_date != '' LIMIT 1",
                         (order_report.WEBMIM_SERVER, number)).fetchone()
    return _day(found[0]) if found else ""


def sheet_phone(phone) -> str:
    """The phone as the table keeps it (the owner 2026-10-30): 8 and ten digits, no spaces nor +7 — 89111280606."""
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 11 and digits[0] in "78":
        return "8" + digits[1:]
    return "8" + digits if len(digits) == 10 else digits


def rows_of(conn, card_id: int, placed_at: str) -> Dict[str, dict]:
    """The rows of the order now, as they go to the table: mark → {column of SENT: value}. Numbers are numbers, days are «YYYY-MM-DD» (the
    script makes them dates); the phone is sheet_phone. «Список» of the panel shows card_rows (the comment, the panel's phone)."""
    card = orders.get_card(conn, card_id)
    phone, account = sheet_phone(card["client_phone"]), account_label(conn, card)
    return {mark: dict({c: row[c] for c in SENT}, **{"Телефон": phone, "Аккаунт": account},
                       **({NOTE_FLAG: True, "Примечание": row["Примечание"]} if row.get(NOTE_FLAG) else {}))    # the note of a sale from OTH (13.11)
            for mark, row in card_rows(conn, card, placed_at, _sent(conn, card_id)).items()}


def account_label(conn, card: dict) -> str:
    """«Аккаунт»: the label of the order's responsible, as the card shows it (the owner 2026-11-05: they differ, the responsible wins); else
    (the owner 2026-11-02) «Подпись в таблице заказов» of the order's Avito account — the account of its first chat, else of its first call.
    No account, or an account without the label: empty. Only the table has it: the list of «Заказы» and a point never do."""
    signed = users.get_sheet_label(conn, card["responsible"])
    if signed:
        return signed
    row = (conn.execute("SELECT account_id FROM avito_chat WHERE order_id = ? ORDER BY id LIMIT 1", (card["id"],)).fetchone()
           or conn.execute("SELECT account_id FROM avito_call WHERE order_id = ? ORDER BY call_at, id LIMIT 1", (card["id"],)).fetchone())
    return connections.get_sheet_label(conn, row[0]) if row and row[0] is not None else ""


def card_rows(conn, card: dict, placed_at: str, sent: Optional[Dict[str, str]] = None) -> Dict[str, dict]:
    """rows_of of a card already read; the list of «Заказы» (orders_list) shows the same values. `sent` None: the columns of the
    notifications are empty (not looked up)."""
    card_id, profits = card["id"], _profits(conn, card)
    common = common_of(conn, card, placed_at, sent)
    from_oth = oth_sale.from_number(conn, card_id)                 # the positions taken from «На ОТХ»: «Б», the WebMIM order they came from
    merged = order_merge.items_of(conn, card_id)                   # a merge (the owner 2026-10-30); its links are in the panel's history only
    from . import order_webmim
    added = order_webmim.extras(conn, card)                        # pieces added after the order had its own number: a row of their own (2026-11-07)
    out = {}
    for item in card["items"]:
        qty, buy, sell = int(item["qty"]), round(float(item["supplier_price"]), 2), round(float(item["sale_unit"]), 2)
        profit = profits.get(item["id"])
        row = common
        extra = added.get(item["id"])
        if extra:
            whole, qty = qty, qty - extra["qty"]                     # the first part keeps its pieces; the profit is shared by the pieces
            share = (lambda value, n: round(value * n / whole, 2)) if whole else (lambda value, n: 0.0)    # no pieces: no profit in either
            out[extra_key(card_id, item["id"])] = dict(common, **{
                "№ заказа MIM": extra["webmim_number"], "Перемещение MIM": extra["transfer_number"], "Дата счета": day_of_number(conn, extra["webmim_number"]),
                "Артикул": item["code"] or item["goods_id"], "Наименование": item["name"], "Кол-во": extra["qty"], "Цена MIM": buy,
                "Сумма MIM": round(buy * extra["qty"], 2), "Цена продажи": sell, "Сумма продажи": round(sell * extra["qty"], 2),
                "Прибыль ₽": share(profit[0], extra["qty"]) if profit else "", "Прибыль %": profit[1] if profit else "",
                "Примечание": " ".join(("добавлено к заказу. %s" % (common["Примечание"] or "")).split())})
            if profit:                                               # the first row: what the second does not have (the sum is exact)
                second = out[extra_key(card_id, item["id"])]["Прибыль ₽"]
                profit = (round(profit[0] - second, 2) if whole else 0.0, profit[1])
            if qty < 1 and not extra["in_base"]:                    # a new position: it never had a row of the first order
                continue
        if item["id"] in from_oth:
            row = dict(common, **_oth_values(conn, card, item["id"], from_oth[item["id"]]), **{"Примечание": " ".join(
                ("%s. %s" % (order_webmim.oth_note(conn, item["id"]), common["Примечание"] or "")).split()),
                "Реализация": FROM_STOCK if common["Реализация"] == REALIZED else common["Реализация"]})
        elif item["id"] in merged and merged[item["id"]]["placed"]:  # a placed source: its WebMIM order, transfer and invoice; else the new order's
            came = merged[item["id"]]
            row = dict(common, **{"№ заказа MIM": came["number"], "Перемещение MIM": came["transfer"],
                                  "Дата счета": _invoice_day(conn, orders.get_card(conn, came["from_order"]))})
        out[key(card_id, item["id"])] = dict(row, **{
            "Артикул": item["code"] or item["goods_id"], "Наименование": item["name"], "Кол-во": qty, "Цена MIM": buy,
            "Сумма MIM": round(buy * qty, 2), "Цена продажи": sell, "Сумма продажи": round(sell * qty, 2),
            "Прибыль ₽": profit[0] if profit else "", "Прибыль %": profit[1] if profit else ""})
    return out


def _oth_values(conn, card: dict, item_id: int, number: str, typed=None) -> dict:
    """«№ заказа MIM», «Перемещение MIM», «Дата счета» of a position «с ОТХ» (the owner 2026-11-05): those of the order its tyres were bought by
    (`number`: oth_sale.from_number), never of the order's own order of WebMIM; own stock has none. What a person typed for this very position
    stays (order_webmim.item_numbers; the owner 2026-11-07: the number typed on the order no longer stands for it, `typed` is not read), and a
    number typed brings its own day. A source gone from the base («Удалённые»): what the order has."""
    from . import order_webmim
    mine = order_webmim.item_numbers(conn, card["id"]).get(item_id, {})
    own, own_transfer = mine.get("webmim_number", ""), mine.get("transfer_number", "")
    first = oth_sale.purchase_order(conn, item_id)
    row = conn.execute("SELECT id, webmim_id, webmim_number, transfer_number FROM order_card WHERE id = ?", (first,)).fetchone() if first else None
    if first and row is None and not (own or own_transfer):
        return {"№ заказа MIM": card["webmim_number"] or card["webmim_id"], "Перемещение MIM": card["transfer_number"], "Дата счета": _invoice_day(conn, card)}
    source = dict(zip(("id", "webmim_id", "webmim_number", "transfer_number"), row)) if row else None
    own_day = ""
    if own:                                                          # typed equal to the source's: the source's day; the order's own: its day
        own_day = ((_invoice_day(conn, source) if source else "") if own == number else
                   _invoice_day(conn, card) if own == (card["webmim_number"] or card["webmim_id"]) else day_of_number(conn, own))
    return {"№ заказа MIM": own or number,
            "Перемещение MIM": own_transfer or (source["transfer_number"] if source else ""),
            "Дата счета": own_day if own else (_invoice_day(conn, source) if source else "")}


def mim_parts(conn, card: dict) -> dict:
    """The purchase of WebMIM of an order on «На ОТХ» (the plan of 2026-11-06), as its first row of the table counts them (a position «с ОТХ»: the
    order of purchase, _oth_values): {"number": «№ заказа MIM», "transfer": «Перемещение MIM», "invoice": «Дата счета» as DD.MM.YYYY}, '' when unknown."""
    numbers = oth_sale.from_number(conn, card["id"])
    held = [i for i in card["items"] if i["id"] in numbers and int(i["qty"]) >= 1]
    if held:
        values = _oth_values(conn, card, held[0]["id"], numbers[held[0]["id"]])
    else:
        values = {"№ заказа MIM": card["webmim_number"] or card["webmim_id"], "Перемещение MIM": card["transfer_number"], "Дата счета": _invoice_day(conn, card)}
    day = str(values["Дата счета"] or "")
    return {"number": str(values["№ заказа MIM"] or ""), "transfer": str(values["Перемещение MIM"] or ""),
            "invoice": "%s.%s.%s" % (day[8:10], day[5:7], day[:4]) if len(day) == 10 else ""}


def mim_title(conn, card: dict) -> str:
    """The name of an order on «На ОТХ» (the plan of 2026-11-06: a purchase of WebMIM, its client changes): «№ MIM <number> · пер. <transfer> ·
    счёт <day>» (mim_parts), «без № MIM» without a number; never the client's name."""
    m = mim_parts(conn, card)
    parts = ["№ MIM %s" % m["number"] if m["number"] else "без № MIM"]
    if m["transfer"]:
        parts.append("пер. %s" % m["transfer"])
    if m["invoice"]:
        parts.append("счёт %s" % m["invoice"])
    return " · ".join(parts)


def oth_invoice(conn, card: dict) -> List[dict]:
    """«Дата счета» of an order holding tyres «с ОТХ», for its card: [{order: the order of purchase (0: the order's own, a number typed here or the
    source gone), day}], one per order, as its rows say. Own stock without a typed number has no invoice: []. An order that took nothing: []."""
    from . import order_webmim
    numbers = oth_sale.from_number(conn, card["id"])
    typed = order_webmim.item_numbers(conn, card["id"]) if numbers else {}
    out: List[dict] = []
    for item in card["items"]:
        if item["id"] not in numbers or int(item["qty"]) < 1:
            continue
        first = oth_sale.purchase_order(conn, item["id"])
        known = first and conn.execute("SELECT 1 FROM order_card WHERE id = ?", (first,)).fetchone()
        own_number = bool(typed.get(item["id"], {}).get("webmim_number")) and typed[item["id"]]["webmim_number"] != numbers[item["id"]]
        if first or own_number:
            found = {"order": first if known and not own_number else 0,
                     "day": _oth_values(conn, card, item["id"], numbers[item["id"]])["Дата счета"]}
            if found not in out:
                out.append(found)
    return out


def common_of(conn, card: dict, placed_at: str, sent: Optional[Dict[str, str]] = None) -> dict:
    """The columns of a row that are the order's, not the position's (card_rows)."""
    sent = sent or {}
    from . import oth_point_sale
    sales = bool(oth_point_sale.sold_of(conn, card["id"]))     # the point sold tyres of its record (ТЗ part D, 2026-11-12)
    sold = sales and oth_point_sale.all_sold(conn, card["id"])  # the owner 13.11: «Выполнено» / «О» once every tyre is sold, not for a part
    note = {NOTE_FLAG: True} if sales else {}                   # the note of the sale goes to «Примечание» (the owner 13.11)
    return dict(note, **{"№ заказа MIM": card["webmim_number"] or card["webmim_id"], "Перемещение MIM": card["transfer_number"],
            "Дата счета": _invoice_day(conn, card), "Имя": card["client_name"], "Телефон": connections.format_phone(card["client_phone"]),
            "ПВЗ": card["point"]["name"] if card["point"] else "", "Этап": orders.stage_name(conn, "done") if sold else card["stage"]["name"],
            "Реализация": REFUSED if sold else realization(card),
            "Списание": bool(card["codes_written_off_at"]), "Уведомление точке": _stamp(sent.get("manager", "")),
            "Уведомление клиенту": _stamp(sent.get("client", "")), "Примечание": card["comment"]})


def followed(conn, card_id: int) -> set:
    """The rows of the order whose quantity follows it: a position that gave tyres from «На ОТХ» or to a merge, or took them by a merge
    (order_merge; the old row stays at 0 pieces, «Примечание» is the owner's, the owner 2026-10-30)."""
    from . import order_webmim
    split = {r[0] for r in conn.execute("SELECT item_id FROM order_mim_extra WHERE order_id = ?", (int(card_id),))}
    try:
        split |= set(order_webmim.extras(conn, orders.get_card(conn, int(card_id))))
    except orders.OrderError:
        pass
    seconds = {extra_key(card_id, i) for i in split} | {key(card_id, i) for i in split}    # a position split in two orders: both rows follow it
    return {key(card_id, i) for i in oth_sale.given(conn, card_id) | order_merge.ever(conn, card_id)} | seconds


def changes(now_rows: Dict[str, dict], written: Dict[str, dict], follow=(), grey: bool = False) -> List[dict]:
    """What to send: a new position with every column (`create`), a written one with the columns of UPDATED that changed and the cells
    of CHECK as written (the script writes nothing into a row that no longer has them). A row marked `_missing` (rewritten by hand, or
    said «missing» by a script before 2026-11-11) is not sent; one the script finds gone is made again by send_order. `follow`: the rows
    whose quantity follows the order too (FOLLOWED: a position that gave tyres from «На ОТХ» to a new order, the owner 2026-10-29). OPTIONAL
    columns that changed go in «extra» (never an empty one into a new row). `grey`: the order stands before «Заказ оформлен» (listed): its rows are filled grey, and the fill goes when it is placed again (`shade`, the owner
    2026-11-02: the rows are never taken out)."""
    out = []
    for mark, fields in now_rows.items():
        before = written.get(mark)
        extra = {c: fields[c] for c in OPTIONAL + (("Примечание",) if fields.get(NOTE_FLAG) else ()) if c in fields and fields[c] != (before or {}).get(c, "")}
        if before is None:
            op = {"key": mark, "create": True, "fields": {c: fields[c] for c in SENT}}
            if grey:
                op["shade"] = True
        elif not before.get("_missing") and not before.get("_removed"):  # a row removed before 2026-11-11 is never made again (the owner)
            diff = {c: fields[c] for c in UPDATED + (FOLLOWED if mark in follow else ()) if before.get(c) != fields[c]}
            shade = grey != bool(before.get("_grey"))
            if not diff and not extra and not shade:
                continue
            op = {"key": mark, "create": False, "fields": diff, "check": {c: before.get(c, "") for c in CHECK}}
            if shade:
                op["shade"] = grey
        else:
            continue
        out.append(dict(op, extra=extra) if extra else op)
    return out


# ---- the web app --------------------------------------------------------------------------------------------------------------------

def call(http: Transport, url: str, payload: dict) -> dict:
    """POST to the web app; Google answers by a redirect to the result. The answer is the script's JSON or SheetError."""
    if not URL.match(url or ""):
        raise SheetError("Адрес веб-приложения не задан или не похож на адрес Apps Script.")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        status, headers, data = http("POST", url, {"Content-Type": "application/json"}, body)
        if status in (301, 302, 303, 307, 308):
            where = headers.get("location", "")
            if not where.startswith(REDIRECT) or urllib.parse.urlsplit(where).netloc != urllib.parse.urlsplit(REDIRECT).netloc:
                raise SheetError("Google переадресовал не на результат скрипта: проверьте адрес веб-приложения.")
            status, headers, data = http("GET", where, {}, None)
    except OSError as err:
        raise SheetError("Google не ответил (%s): повторим позже." % type(err).__name__) from None
    if status != 200:
        raise SheetError("Google ответил %d: проверьте, что веб-приложение развернуто и доступ — «Все»." % status)
    try:
        answer = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SheetError("Ответ не от скрипта (страница Google вместо данных): разверните веб-приложение с доступом «Все».") from None
    if not isinstance(answer, dict):
        raise SheetError("Скрипт ответил не так, как ждёт панель: обновите текст скрипта.")
    if not answer.get("ok"):
        raise SheetError("Скрипт отказал: %s" % str(answer.get("error") or "без причины")[:200])
    return answer


def check(conn, box, http: Transport) -> str:
    """«Проверить»: the secret is right, the sheet «Заказы» is there with every column the panel writes."""
    s = settings(conn)
    secret = _secret(conn, box, s)
    if not secret:
        raise SheetError("Секрет не задан для этого адреса: впишите его." if connections.get_secret(conn, box, KIND, 0, "secret") else "Секрет не задан.")
    answer = call(http, s["url"], {"secret": secret, "sheet": SHEET, "ping": True, "columns": list(ASKED + OPTIONAL),
                                     "optional": list(OPTIONAL)})          # a missing optional one never stops the look at the marks
    missing = [str(c) for c in answer.get("missing") or []]
    needed = [c for c in missing if c not in OPTIONAL]
    if needed:
        raise SheetError("На листе «%s» нет колонок: %s." % (SHEET, ", ".join(needed)))
    said = "Таблица отвечает: лист «%s», все колонки на месте." % SHEET
    if missing:                                                            # the owner 2026-11-02: a warning, the rows go all the same
        said = ("Таблица отвечает: лист «%s», нужные колонки на месте. Нет колонки %s: добавьте её в первую строку листа, иначе она не "
                "заполняется (остальное пишется и без неё; строки, отправленные без неё, заполнятся, только когда значение колонки у заказа изменится)."
                % (SHEET, ", ".join("«%s»" % c for c in missing)))
    number = lambda name: answer.get(name) if isinstance(answer.get(name), int) and not isinstance(answer.get(name), bool) else None
    marked, moved, blocked = number("marked"), number("moved"), number("blocked")
    if marked is None or (missing and answer.get("optional") is not True):  # an older script: no word of the marks (or none looked at)
        return said
    said += (" Строк с меткой (скрытая колонка «tires_id»): %d, перенесено сейчас из примечаний: %d, не перенесено: %d."
             % (marked, moved or 0, blocked or 0))
    if answer.get("filter_outside") is True:                               # a sort from that filter would leave the marks behind
        said += (" Фильтр на листе не включает скрытую колонку «tires_id»: удалите фильтр и создайте заново (Данные → Создать фильтр), "
                 "иначе сортировка из фильтра оторвёт метки.")
    if blocked:                                                            # the owner 2026-11-01: no note of a mark, «Дата» goes last
        said += (" У %d строк метка в примечании расходится с колонкой «tires_id»: примечания оставлены, колонку «Дата» пока не удаляйте "
                 "и напишите разработчику." % blocked)
    return said


# ---- rows removed before 2026-11-11 ----------------------------------------------------------------------------------------------
# From 2026-11-07 the panel removed the rows of an order cancelled before its tyres reached the point (the op «remove»). The owner
# 2026-11-11: «Не удаляй строки вообще»: no row is ever removed now; such an order keeps its rows and gets its columns («Этап»: «Отмена»)
# as any other. A row removed back then is marked `_removed` in sheet_order.rows: it is never made again, on «Отмена» or off it (changes).


# ---- sending ------------------------------------------------------------------------------------------------------------------------

def _retry_at(now: str, failing: int) -> str:
    minutes = RETRY_MINUTES[min(failing, len(RETRY_MINUTES)) - 1]
    return (datetime.fromisoformat(now) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _last_event(conn, card_id: int) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM order_event WHERE order_id = ?", (card_id,)).fetchone()[0]


def _names(items: List[dict]) -> str:
    seen: List[str] = []
    for op in items:
        for c in list(op["fields"]) + list(op.get("extra") or {}):
            if c not in seen:
                seen.append(c)
    return ", ".join(c for c in COLUMNS if c in seen)


def _failed(conn, card_id: int, failing: int, why: str, now: str, seen: int) -> str:
    if not failing:                                                        # once per streak of failures, not at every retry
        orders.add_event(conn, card_id, now, AUTHOR, "sheet", "Ошибка Google-таблицы: %s" % why)
    conn.execute("UPDATE sheet_order SET failing = ?, error = ?, next_at = ?, seen_event = ?, claim_until = '' WHERE order_id = ?",
                 (failing + 1, why[:300], _retry_at(now, failing + 1), seen, card_id))
    conn.commit()
    return "ошибка: %s" % why


GONE, EDITED = "gone", "edited"        # the script from 2026-11-11: the mark is not in the sheet / the row has another code or phone
REMADE = "Строка Google-таблицы пропала — создана заново: %d стр."
EDITED_LINE = ("Строка Google-таблицы изменена вручную (артикул/телефон) — проверьте её; обновления этой строки не идут: %d стр.")


def _status(replies: List[dict], mark: str) -> str:
    return next((str(r.get("status")) for r in replies if str(r.get("key")) == mark), "")


def _recreate(http: Transport, url: str, secret: str, ops: List[dict], replies: List[dict], current: Dict[str, dict], lost: List[str],
              grey: bool) -> Tuple[List[dict], List[dict], str]:
    """The rows the script answered «gone» for (its mark is nowhere in the sheet: deleted by hand or by a failure, the owner 2026-11-11) are
    sent again at once with every column, as a new position (changes). Returns the ops and the answers with those rows put in their place,
    and why the second send failed ("": it went). A failed one leaves them «gone»: not taken, they go again at the next send."""
    if not lost:
        return ops, replies, ""
    again = changes({m: current[m] for m in lost}, {}, grey=grey)
    try:
        answer = call(http, url, {"secret": secret, "sheet": SHEET, "rows": again})
    except SheetError as err:
        return ops, replies, str(err)[:200]
    fresh = [r for r in answer.get("results") or [] if isinstance(r, dict)]
    ops = [op for op in ops if op["key"] not in lost] + again
    replies = [r for r in replies if str(r.get("key")) not in lost] + fresh
    return ops, replies, ""


def send_order(conn, card_id: int, url: str, secret: str, http: Transport, now: str) -> str:
    """Send what changed in one order (claimed by the caller); returns what happened, in words. No row is ever removed (the owner
    2026-11-11): an order on «Отмена» gets its columns as any other; a row removed before that (`_removed`) is never made again."""
    row = conn.execute("SELECT placed_at, rows, failing FROM sheet_order WHERE order_id = ?", (card_id,)).fetchone()
    placed_at, written, failing = row[0], json.loads(row[1] or "{}"), row[2]
    seen = _last_event(conn, card_id)                                      # before the rows: a change made while Google answers is looked at again
    current = rows_of(conn, card_id, placed_at)
    ops = changes(current, written, followed(conn, card_id), grey=not listed(conn, card_id))   # back before «Заказ оформлен»: grey
    gone = [m for m in written if m not in current and not written[m].get("_gone") and not written[m].get("_removed")]
    for m in gone:
        written[m]["_gone"] = True
        orders.add_event(conn, card_id, now, AUTHOR, "sheet", "Позиция убрана из заказа: её строку в Google-таблице поправьте вручную.")
    if not ops:
        conn.execute("UPDATE sheet_order SET rows = ?, seen_event = ?, claim_until = '' WHERE order_id = ?",
                     (json.dumps(written, ensure_ascii=False), seen, card_id))
        conn.commit()
        return "нечего отправлять"
    try:
        answer = call(http, url, {"secret": secret, "sheet": SHEET, "rows": ops})
    except SheetError as err:
        return _failed(conn, card_id, failing, str(err), now, seen)
    replies = [r for r in answer.get("results") or [] if isinstance(r, dict)]
    lost_rows = [op["key"] for op in ops if not op["create"] and _status(replies, op["key"]) == GONE]
    ops, replies, failed = _recreate(http, url, secret, ops, replies, current, lost_rows, grey=not listed(conn, card_id))
    results = {str(r.get("key")): str(r.get("status")) for r in replies}
    shaded = {str(r.get("key")): r.get("shade") for r in replies}          # the fill the script made (a script before 2026-11-02 says nothing)
    why = next((str(r.get("error"))[:200] for r in replies if r.get("error")), "") or failed
    unshaded = [op for op in ops if "shade" in op and results.get(op["key"]) in ("created", "updated") and shaded.get(op["key"]) is not op["shade"]]
    if unshaded and not why:
        why = "скрипт таблицы не умеет заливать строки: нужно новое развертывание Apps Script"
    made = [op for op in ops if op["create"] and results.get(op["key"]) in ("created", "updated") and op not in unshaded]   # updated: made
    kept = [op for op in ops if not op["create"] and results.get(op["key"]) == "updated" and op not in unshaded]            # by a lost answer
    missing = [op for op in ops if results.get(op["key"]) == "missing"]   # the script before 2026-11-11: gone or edited, it cannot tell
    edited = [op for op in ops if results.get(op["key"]) == EDITED]
    remade = [op for op in made if op["key"] in lost_rows]
    for op in made + kept:
        written[op["key"]] = dict(written.get(op["key"], {}), **op["fields"], **op.get("extra", {}))
        if "shade" in op:
            written[op["key"]]["_grey"] = op["shade"]
        elif op in remade:                                                 # made again white: no fill of the lost row is left
            written[op["key"]].pop("_grey", None)
    for op in missing + edited:
        written[op["key"]] = dict(written.get(op["key"], {}), _missing=True)
    lines = []
    if made:
        lines.append("Записано в Google-таблицу: %d стр." % len(made))
    if any(op["fields"] or op.get("extra") for op in kept):
        lines.append("Google-таблица обновлена: %s." % _names([op for op in kept if op["fields"] or op.get("extra")]))
    greyed = [op for op in made + kept if op.get("shade") is True]
    cleared = [op for op in kept if op.get("shade") is False]
    if greyed:
        lines.append("Заказ на этапе до «Заказ оформлен»: строки Google-таблицы залиты серым: %d стр." % len(greyed))
    if cleared:
        lines.append("Заказ снова оформлен: серая заливка строк Google-таблицы снята: %d стр." % len(cleared))
    if missing:
        lines.append("Строка Google-таблицы изменена или удалена вручную (другой артикул или строки нет): %d стр. "
                     "Заново не создаём, обновления этих строк не идут." % len(missing))
    if remade:
        lines.append(REMADE % len(remade))
    if edited:
        lines.append(EDITED_LINE % len(edited))
    lost = [op for op in ops if op not in made + kept + missing + edited]
    if lost and not failing:                                               # the script did not take them: they go again (said once)
        lines.append("Google-таблица не приняла %d стр.%s: отправим ещё раз." % (len(lost), " (%s)" % why if why else ""))
    for line in lines:
        orders.add_event(conn, card_id, now, AUTHOR, "sheet", line)
    conn.execute("UPDATE sheet_order SET rows = ?, sent_at = ?, failing = ?, error = ?, next_at = ?, seen_event = ?, claim_until = '' WHERE order_id = ?",
                 (json.dumps(written, ensure_ascii=False), now, failing + 1 if lost else 0, (why or "строки не приняты") if lost else "",
                  _retry_at(now, failing + 1) if lost else "", seen, card_id))
    conn.commit()
    return " ".join(lines)


# ---- the rows marked «missing» before 2026-11-11 -------------------------------------------------------------------------------------
# A script before 2026-11-11 said «missing» both for a row deleted and for one rewritten by hand: the panel marked them `_missing` and left
# them. `python -m tires_sync sheet-restore-missing` asks the new script about them once, with nothing to write and no creation: «gone»
# makes the row again (as send_order does), «edited» leaves it marked, «updated» (the row is there as written) takes the mark off.

def restore_candidates(conn) -> List[Tuple[int, str]]:
    """(order, mark) of the rows marked `_missing` of the open orders (OPEN_STAGES, «Вести заказы, оформленные с»), the position still in the
    order. Never a row the panel removed (`_removed`) nor a position taken out of the order (`_gone`)."""
    kept, day = _kept(conn)
    found = []
    for card_id, rows in conn.execute("SELECT s.order_id, s.rows FROM sheet_order s JOIN order_card c ON c.id = s.order_id JOIN order_stage st"
                                      " ON st.id = c.stage_id WHERE st.code IN (%s)" % ",".join("?" * len(OPEN_STAGES)) + kept +
                                      " ORDER BY s.order_id", list(OPEN_STAGES) + day).fetchall():
        for mark, row in json.loads(rows or "{}").items():
            if row.get("_missing") and not row.get("_removed") and not row.get("_gone"):
                found.append((card_id, mark))
    return found


def restore_missing(conn, box, http: Transport, now: str, dry_run: bool = False) -> List[dict]:
    """The rows of restore_candidates, order by order: {"order_id", "key", "result"}. `dry_run`: the list only, nothing is sent nor written.
    result: «создана заново», «изменена вручную, оставлена», «на месте, обновления снова идут», «старый скрипт: не различает, оставлена»,
    «ошибка: …», «занят другой отправкой», «проверка не отправлялась»."""
    found = restore_candidates(conn)
    if dry_run:
        return [{"order_id": o, "key": m, "result": "проверка не отправлялась"} for o, m in found]
    s = settings(conn)
    if not (s["enabled"] and s["url"]):
        raise SheetError("Google-таблица выключена или адрес не задан: включите её в «Подключения → Google-таблица».")
    secret = _secret(conn, box, s) if box is not None else None
    if not secret:
        raise SheetError("Секрет Google-таблицы не задан (или ключ не передан): ничего не отправлено.")
    out: List[dict] = []
    for card_id in sorted({o for o, _ in found}):
        marks = [m for o, m in found if o == card_id]
        if not _claim(conn, card_id, now):
            out += [{"order_id": card_id, "key": m, "result": "занят другой отправкой"} for m in marks]
            continue
        out += _restore_order(conn, card_id, marks, s["url"], secret, http, now)
    return out


def _restore_order(conn, card_id: int, marks: List[str], url: str, secret: str, http: Transport, now: str) -> List[dict]:
    row = conn.execute("SELECT placed_at, rows FROM sheet_order WHERE order_id = ?", (card_id,)).fetchone()
    placed_at, written = row[0], json.loads(row[1] or "{}")
    current = rows_of(conn, card_id, placed_at)
    marks = [m for m in marks if m in current]
    asked = [{"key": m, "create": False, "fields": {}, "check": {c: written[m].get(c, "") for c in CHECK}} for m in marks]
    release = lambda: (conn.execute("UPDATE sheet_order SET claim_until = '' WHERE order_id = ?", (card_id,)), conn.commit())
    if not asked:
        release()
        return []
    try:
        answer = call(http, url, {"secret": secret, "sheet": SHEET, "rows": asked})
    except SheetError as err:
        release()
        return [{"order_id": card_id, "key": m, "result": "ошибка: %s" % err} for m in marks]
    replies = [r for r in answer.get("results") or [] if isinstance(r, dict)]
    grey = not listed(conn, card_id)
    lost = [m for m in marks if _status(replies, m) == GONE]
    ops, replies, failed = _recreate(http, url, secret, asked, replies, current, lost, grey=grey)
    out, remade = [], 0
    for m in marks:
        status = _status(replies, m)
        if m in lost and status in ("created", "updated"):
            op = next(op for op in ops if op["key"] == m)
            written[m] = dict(op["fields"], **op.get("extra", {}))
            if "shade" in op:
                written[m]["_grey"] = op["shade"]
            remade += 1
            said = "создана заново"
        elif status == EDITED:
            said = "изменена вручную, оставлена"
        elif status == "updated":
            written[m].pop("_missing", None)
            said = "на месте, обновления снова идут"
        elif status == "missing":
            said = "старый скрипт: не различает, оставлена"
        else:
            reason = failed or next((str(r.get("error"))[:200] for r in replies if str(r.get("key")) == m and r.get("error")), "") or status or "нет ответа"
            said = "ошибка: %s" % reason
        out.append({"order_id": card_id, "key": m, "result": said})
    if remade:
        orders.add_event(conn, card_id, now, AUTHOR, "sheet", REMADE % remade)
    conn.execute("UPDATE sheet_order SET rows = ?, claim_until = '' WHERE order_id = ?", (json.dumps(written, ensure_ascii=False), card_id))
    conn.commit()
    return out


def flush(conn, box, http: Transport, now: str, full: bool = False) -> Dict[int, str]:
    """Send the queued orders whose rows changed (order → what happened). The table off, no address or no secret: nothing goes."""
    s = settings(conn)
    if not (s["enabled"] and s["url"]) or box is None:
        return {}
    secret = _secret(conn, box, s)
    if not secret:
        return {}
    done: Dict[int, str] = {}
    for card_id in _due(conn, now, full):
        if not _claim(conn, card_id, now):
            continue
        try:
            done[card_id] = send_order(conn, card_id, s["url"], secret, http, now)
        except Exception as err:                                           # one broken order never stops the others behind it
            conn.rollback()
            log.exception("the Google table: order %s", card_id)
            row = conn.execute("SELECT failing FROM sheet_order WHERE order_id = ?", (card_id,)).fetchone()
            done[card_id] = _failed(conn, card_id, row[0] if row else 0, "сбой панели (%s): сообщите разработчику." % type(err).__name__, now,
                                    _last_event(conn, card_id))
    return done


def status(conn) -> dict:
    """What «Подключения» shows: how many orders are in the table, how many fail now and the last failure."""
    total, failing = conn.execute("SELECT COUNT(*), COALESCE(SUM(failing > 0), 0) FROM sheet_order").fetchone()
    last = conn.execute("SELECT order_id, error FROM sheet_order WHERE failing > 0 ORDER BY order_id DESC LIMIT 1").fetchone()
    return {"orders": total, "failing": failing, "last_error": {"order_id": last[0], "error": last[1]} if last else None}


# ---- a lasting failure --------------------------------------------------------------------------------------------------------------

LASTING_HOURS = 3                  # the retries (1/5/15/60 minutes) and two hourly looks: a short hiccup of Google wakes nobody (the lead 2026-10-24)
ALERTED_KEY = "sheet_lasting_alerted"
STREAK_STARTS = ("Ошибка Google-таблицы:%", "Google-таблица не приняла%")       # said once, when a streak of failures begins (_failed, send_order)
LASTING_ALERT = "Tires Sync: Google-таблица не принимает заказы дольше %d ч: %s (последняя ошибка: %s). Проверьте «Подключения → Google-таблица»."
RECOVERED = "Tires Sync: Google-таблица снова принимает заказы."


def lasting(conn, now: str) -> List[dict]:
    """The orders whose failures go on for LASTING_HOURS or longer (the streak begins at its first record in the order's journal)."""
    edge = (datetime.fromisoformat(now[:19]) - timedelta(hours=LASTING_HOURS)).isoformat(timespec="seconds")
    found = []
    kept, day = _kept(conn)                                                # an order left alone never keeps the alarm on
    for card_id, error in conn.execute("SELECT s.order_id, s.error FROM sheet_order s WHERE s.failing > 0" + kept + " ORDER BY s.order_id",
                                       day).fetchall():
        row = conn.execute("SELECT MAX(at) FROM order_event WHERE order_id = ? AND author = ? AND kind = 'sheet' AND (text LIKE ? OR text LIKE ?)",
                           (card_id, AUTHOR) + STREAK_STARTS).fetchone()
        if row and row[0] and row[0][:19] <= edge:
            found.append({"order_id": card_id, "since": row[0], "error": error})
    return found


def lasting_alarm(conn, now: str) -> Optional[Tuple[str, bool]]:
    """(text, notice) for the owner, once per episode: the alert when an order has failed for LASTING_HOURS, the note when none has any more.
    The episode turns only when the caller says it was told (alarm_told). None: nothing new to say. The table switched off: the episode is
    over without a word."""
    alerted = store.get_state(conn, ALERTED_KEY)
    if not enabled(conn):
        if alerted:
            store.set_state(conn, ALERTED_KEY, "")
        return None
    found = lasting(conn, now)
    if found and not alerted:
        names = ", ".join("№%d" % f["order_id"] for f in found[:10]) + (" и ещё %d" % (len(found) - 10) if len(found) > 10 else "")
        return LASTING_ALERT % (LASTING_HOURS, names, (found[-1]["error"] or "без текста")[:200]), False
    if alerted and not found:
        return RECOVERED, True
    return None


def alarm_told(conn, now: str, notice: bool) -> None:
    """The owner was told what lasting_alarm gave: the episode begins (the alert) or ends (the note). cli._alert said False (Telegram refused
    it): the next hour says it again."""
    store.set_state(conn, ALERTED_KEY, "" if notice else now[:19])
