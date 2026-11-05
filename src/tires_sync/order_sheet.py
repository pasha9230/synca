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

from . import connections, order_engine as engine, order_merge, order_report, orders, oth_sale, sales as sales_store, store

KIND = "gsheet"
SHEET = "Заказы"
COLUMNS = ("Дата счета", "№ заказа MIM", "Перемещение MIM", "Артикул", "Наименование", "Кол-во", "Цена MIM", "Сумма MIM",
           "Цена продажи", "Сумма продажи", "Имя", "Телефон", "ПВЗ", "Этап", "Реализация", "Списание", "Прибыль %", "Прибыль ₽",
           "Уведомление точке", "Уведомление клиенту", "Аккаунт", "Примечание")
OWNERS = ("Примечание",)                    # never filled (the owner 2026-10-30): «Примечание» is the people's; «Дата» is gone (2026-11-01)
OPTIONAL = ("Аккаунт",)                     # the owner 2026-11-02: sent apart («extra»), written only where the sheet has the column; a sheet
                                            # without it (or a script before 2026-11-02, which never reads «extra») takes the rest all the same
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
    if conn.execute("SELECT 1 FROM order_handoff WHERE order_id = ?", (card["id"],)).fetchone() or card["id"] in order_merge.handed(conn):
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
    phone, account = sheet_phone(card["client_phone"]), account_label(conn, card_id)
    return {mark: dict({c: row[c] for c in SENT}, **{"Телефон": phone, "Аккаунт": account})
            for mark, row in card_rows(conn, card, placed_at, _sent(conn, card_id)).items()}


def account_label(conn, card_id: int) -> str:
    """«Аккаунт» (the owner 2026-11-02): «Подпись в таблице заказов» of the order's Avito account — the account of its first chat, else of
    its first call. No account, or an account without the label: empty. Only the table has it: the list of «Заказы» and a point never do."""
    row = (conn.execute("SELECT account_id FROM avito_chat WHERE order_id = ? ORDER BY id LIMIT 1", (int(card_id),)).fetchone()
           or conn.execute("SELECT account_id FROM avito_call WHERE order_id = ? ORDER BY call_at, id LIMIT 1", (int(card_id),)).fetchone())
    return connections.get_sheet_label(conn, row[0]) if row and row[0] is not None else ""


def card_rows(conn, card: dict, placed_at: str, sent: Optional[Dict[str, str]] = None) -> Dict[str, dict]:
    """rows_of of a card already read; the list of «Заказы» (orders_list) shows the same values. `sent` None: the columns of the
    notifications are empty (not looked up)."""
    card_id, profits = card["id"], _profits(conn, card)
    common = common_of(conn, card, placed_at, sent)
    from_oth = oth_sale.from_number(conn, card_id)                 # the positions taken from «На ОТХ»: «Б», the WebMIM order they came from
    typed = oth_sale.typed_numbers(conn, card) if from_oth else set()
    merged = order_merge.items_of(conn, card_id)                   # a merge (the owner 2026-10-30); its links are in the panel's history only
    out = {}
    for item in card["items"]:
        qty, buy, sell = int(item["qty"]), round(float(item["supplier_price"]), 2), round(float(item["sale_unit"]), 2)
        profit = profits.get(item["id"])
        row = common
        if item["id"] in from_oth:
            source = conn.execute("SELECT from_order FROM order_item_oth WHERE item_id = ?", (item["id"],)).fetchone()[0]
            where = "заказ №%d" % source if source else "свой остаток"     # «Свой остаток (ОТХ)» (oth_manual): no old order
            row = dict(common, **_oth_values(conn, card, item["id"], from_oth[item["id"]], typed), **{"Примечание": " ".join(
                ("с ОТХ, %s. %s" % (where, common["Примечание"] or "")).split()),
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


def _oth_values(conn, card: dict, item_id: int, number: str, typed: set) -> dict:
    """«№ заказа MIM», «Перемещение MIM», «Дата счета» of a position «с ОТХ» (the owner 2026-11-05): those of the order its tyres were bought by
    (`number`: oth_sale.from_number), never of the order's own order of WebMIM; own stock has none. What a person wrote stays (`typed`:
    oth_sale.typed_numbers), and a number typed brings its own day."""
    first = oth_sale.first_order(conn, item_id)
    row = conn.execute("SELECT id, webmim_id, webmim_number, transfer_number FROM order_card WHERE id = ?", (first,)).fetchone() if first else None
    source = dict(zip(("id", "webmim_id", "webmim_number", "transfer_number"), row)) if row else None
    return {"№ заказа MIM": card["webmim_number"] if "webmim_number" in typed else number,
            "Перемещение MIM": card["transfer_number"] if "transfer_number" in typed else (source["transfer_number"] if source else ""),
            "Дата счета": _invoice_day(conn, card) if "webmim_number" in typed else (_invoice_day(conn, source) if source else "")}


def oth_invoice(conn, card: dict) -> List[dict]:
    """«Дата счета» of an order holding tyres «с ОТХ», for its card: [{order: the order of purchase (0: a number typed here), day}], one per order, as
    its rows say. Own stock has no invoice: []. An order that took nothing: []."""
    numbers = oth_sale.from_number(conn, card["id"])
    typed = oth_sale.typed_numbers(conn, card) if numbers else set()
    out: List[dict] = []
    for item in card["items"]:
        first = oth_sale.first_order(conn, item["id"]) if item["id"] in numbers and int(item["qty"]) >= 1 else None
        if first:
            found = {"order": 0 if "webmim_number" in typed else first,
                     "day": _oth_values(conn, card, item["id"], numbers[item["id"]], typed)["Дата счета"]}
            if found not in out:
                out.append(found)
    return out


def common_of(conn, card: dict, placed_at: str, sent: Optional[Dict[str, str]] = None) -> dict:
    """The columns of a row that are the order's, not the position's (card_rows)."""
    sent = sent or {}
    return {"№ заказа MIM": card["webmim_number"] or card["webmim_id"], "Перемещение MIM": card["transfer_number"],
            "Дата счета": _invoice_day(conn, card), "Имя": card["client_name"], "Телефон": connections.format_phone(card["client_phone"]),
            "ПВЗ": card["point"]["name"] if card["point"] else "", "Этап": card["stage"]["name"], "Реализация": realization(card),
            "Списание": bool(card["codes_written_off_at"]), "Уведомление точке": _stamp(sent.get("manager", "")),
            "Уведомление клиенту": _stamp(sent.get("client", "")), "Примечание": card["comment"]}


def followed(conn, card_id: int) -> set:
    """The rows of the order whose quantity follows it: a position that gave tyres from «На ОТХ» or to a merge, or took them by a merge
    (order_merge; the old row stays at 0 pieces, «Примечание» is the owner's, the owner 2026-10-30)."""
    return {key(card_id, i) for i in oth_sale.given(conn, card_id) | order_merge.ever(conn, card_id)}


def changes(now_rows: Dict[str, dict], written: Dict[str, dict], follow=(), grey: bool = False) -> List[dict]:
    """What to send: a new position with every column (`create`), a written one with the columns of UPDATED that changed and the cells
    of CHECK as written (the script writes nothing into a row that no longer has them). A row the script did not find (the owner deleted or
    rewrote it) is not made again. `follow`: the rows whose quantity follows the order too (FOLLOWED: a position that gave tyres from «На ОТХ»
    to a new order, the owner 2026-10-29). OPTIONAL columns that changed go in «extra» (never an empty one into a new row). `grey`: the order
    stands before «Заказ оформлен» (listed): its rows are filled grey, and the fill goes when it is placed again (`shade`, the owner
    2026-11-02: the rows are never taken out)."""
    out = []
    for mark, fields in now_rows.items():
        before = written.get(mark)
        extra = {c: fields[c] for c in OPTIONAL if c in fields and fields[c] != (before or {}).get(c, "")}
        if before is None:
            op = {"key": mark, "create": True, "fields": {c: fields[c] for c in SENT}}
            if grey:
                op["shade"] = True
        elif not before.get("_missing"):
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
                "заполняется (остальное пишется и без неё; строки, отправленные без неё, заполнятся, только когда подпись аккаунта изменится)."
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


def send_order(conn, card_id: int, url: str, secret: str, http: Transport, now: str) -> str:
    """Send what changed in one order (claimed by the caller); returns what happened, in words."""
    row = conn.execute("SELECT placed_at, rows, failing FROM sheet_order WHERE order_id = ?", (card_id,)).fetchone()
    placed_at, written, failing = row[0], json.loads(row[1] or "{}"), row[2]
    seen = _last_event(conn, card_id)                                      # before the rows: a change made while Google answers is looked at again
    current = rows_of(conn, card_id, placed_at)
    ops = changes(current, written, followed(conn, card_id), grey=not listed(conn, card_id))   # back before «Заказ оформлен»: grey
    gone = [m for m in written if m not in current and not written[m].get("_gone")]
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
    results = {str(r.get("key")): str(r.get("status")) for r in replies}
    shaded = {str(r.get("key")): r.get("shade") for r in replies}          # the fill the script made (a script before 2026-11-02 says nothing)
    why = next((str(r.get("error"))[:200] for r in replies if r.get("error")), "")
    unshaded = [op for op in ops if "shade" in op and results.get(op["key"]) in ("created", "updated") and shaded.get(op["key"]) is not op["shade"]]
    if unshaded and not why:
        why = "скрипт таблицы не умеет заливать строки: нужно новое развертывание Apps Script"
    made = [op for op in ops if op["create"] and results.get(op["key"]) in ("created", "updated") and op not in unshaded]   # updated: made
    kept = [op for op in ops if not op["create"] and results.get(op["key"]) == "updated" and op not in unshaded]            # by a lost answer
    missing = [op for op in ops if results.get(op["key"]) == "missing"]
    for op in made + kept:
        written[op["key"]] = dict(written.get(op["key"], {}), **op["fields"], **op.get("extra", {}))
        if "shade" in op:
            written[op["key"]]["_grey"] = op["shade"]
    for op in missing:
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
    lost = [op for op in ops if op not in made + kept + missing]
    if lost and not failing:                                               # the script did not take them: they go again (said once)
        lines.append("Google-таблица не приняла %d стр.%s: отправим ещё раз." % (len(lost), " (%s)" % why if why else ""))
    for line in lines:
        orders.add_event(conn, card_id, now, AUTHOR, "sheet", line)
    conn.execute("UPDATE sheet_order SET rows = ?, sent_at = ?, failing = ?, error = ?, next_at = ?, seen_event = ?, claim_until = '' WHERE order_id = ?",
                 (json.dumps(written, ensure_ascii=False), now, failing + 1 if lost else 0, (why or "строки не приняты") if lost else "",
                  _retry_at(now, failing + 1) if lost else "", seen, card_id))
    conn.commit()
    return " ".join(lines)


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
