"""The owner's Google table of orders, sheet «Заказы» (`docs/DECISIONS.md`, 2026-10-23): the panel fills it by itself.

A row is a position of an order. An order joins the table when it is placed (the button «Оформить в WebMIM» or the move to «Заказ
оформлен» or later) while the table is switched on in «Подключения → Google-таблица»: the orders placed before never do. Its rows are
written once with every column, then only the columns that follow the order (UPDATED) are kept up to date; the owner's own columns and
rows are never touched. The table is written by a web app of Apps Script in the table itself (`docs/integrations/google-sheet-apps-script.md`):
it finds a row by the note «tires:<order>:<position>» of its cell «Дата» (a note moves with its cell when the sheet is sorted), not by
a cell the owner may edit, and writes into it only while its «Артикул» is still what the panel wrote (CHECK; the name the owner may correct).

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
COLUMNS = ("Дата", "№ заказа MIM", "Перемещение MIM", "Дата счета", "Артикул", "Наименование", "Кол-во", "Цена MIM", "Сумма MIM",
           "Цена продажи", "Сумма продажи", "Имя", "Телефон", "ПВЗ", "Этап", "Реализация", "Списание", "Прибыль %", "Прибыль ₽",
           "Уведомление точке", "Уведомление клиенту", "Примечание")
UPDATED = ("№ заказа MIM", "Перемещение MIM", "Дата счета", "Этап", "Реализация", "Списание", "Прибыль %", "Прибыль ₽",
           "Уведомление точке", "Уведомление клиенту")          # the rest is written once: a correction of the owner in the cell stays
FOLLOWED = ("Кол-во", "Сумма MIM", "Сумма продажи")              # a position that gave tyres from «На ОТХ» (oth_sale): its row goes down
CHECK = ("Артикул",)                                         # written once: the script finds the row by its mark and checks it is still there
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
    """url, enabled, since; secret_for: the address the secret was typed for (the secret never goes to another one)."""
    return dict({"url": "", "enabled": False, "since": "", "secret_for": ""}, **connections.get_config(conn, KIND, 0))


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


def save_settings(conn, url: str, on: bool, now: str, author: str, secret_typed: bool = False) -> None:
    """The address of the web app and the switch; `since` is the moment it was switched on (the orders placed from then on).
    `secret_typed`: the secret was typed with this address (it is the address's secret from now on)."""
    url = check_url(url, on)
    before = settings(conn)
    since = before["since"] if before["enabled"] and on else (now if on else "")
    connections.set_config(conn, KIND, 0, {"url": url, "enabled": bool(on), "since": since,
                                           "secret_for": url if secret_typed else before["secret_for"]}, now, author)
    conn.commit()


def _secret(conn, box, s: dict) -> Optional[str]:
    """The secret, only for the address it was typed for."""
    if not s["url"] or s["url"] != s["secret_for"]:
        return None
    return connections.get_secret(conn, box, KIND, 0, "secret")


# ---- the queue ----------------------------------------------------------------------------------------------------------------------

def enroll(conn, card_id: int, now: str) -> None:
    """The order is placed: it joins the table if the table is on. In the caller's transaction; once (a second placing keeps the first)."""
    if enabled(conn):
        conn.execute("INSERT OR IGNORE INTO sheet_order (order_id, placed_at) VALUES (?, ?)", (int(card_id), now))


def _due(conn, now: str, full: bool) -> List[int]:
    """The orders to look at: those that failed and wait no longer, never sent, or with a record in the journal since the last look;
    `full` (the hourly tick) looks at every order placed in the last RECENT_DAYS (a WebMIM date read by the snapshot leaves no record) and
    at every failing one: an old closed order is not counted again every hour."""
    if full:
        changed, args = " AND (s.failing > 0 OR s.placed_at >= ?)", [(datetime.fromisoformat(now) - timedelta(days=RECENT_DAYS)).isoformat(timespec="seconds")]
    else:
        changed, args = (" AND (s.failing > 0 OR s.sent_at = '' OR EXISTS (SELECT 1 FROM order_event e WHERE e.order_id = s.order_id"
                         " AND e.id > s.seen_event AND e.author != ?))"), [AUTHOR]             # its own records are no change of the order
    rows = conn.execute("SELECT s.order_id FROM sheet_order s JOIN order_card c ON c.id = s.order_id WHERE (s.next_at = '' OR s.next_at <= ?)"
                        " AND (s.claim_until = '' OR s.claim_until < ?)" + changed + " ORDER BY s.order_id LIMIT ?", [now, now] + args + [-1 if full else BATCH])
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
    if code == "done":
        return FROM_STOCK if card["deal_kind"] == "stock" else REALIZED
    return REFUSED if code in ("cancel", "refused", "return", "oth", orders.RETURN_GIVEN) else ""      # «Возврат передан»: the tyres of «На ОТХ» went back


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


def rows_of(conn, card_id: int, placed_at: str) -> Dict[str, dict]:
    """The rows of the order now: mark → {column: value}. Numbers are numbers, days are «YYYY-MM-DD» (the script makes them dates)."""
    return card_rows(conn, orders.get_card(conn, card_id), placed_at, _sent(conn, card_id))


def card_rows(conn, card: dict, placed_at: str, sent: Optional[Dict[str, str]] = None) -> Dict[str, dict]:
    """rows_of of a card already read; the list of «Заказы» (orders_list) shows the same values. `sent` None: the columns of the
    notifications are empty (not looked up)."""
    card_id, profits = card["id"], _profits(conn, card)
    common = common_of(conn, card, placed_at, sent)
    from_oth = oth_sale.from_number(conn, card_id)                 # the positions taken from «На ОТХ»: «Б», the WebMIM order they came from
    merged, gave = order_merge.items_of(conn, card_id), order_merge.given_to(conn, card_id)     # a merge (the owner 2026-10-30)
    out = {}
    for item in card["items"]:
        qty, buy, sell = int(item["qty"]), round(float(item["supplier_price"]), 2), round(float(item["sale_unit"]), 2)
        profit = profits.get(item["id"])
        row = common
        if item["id"] in from_oth:
            source = conn.execute("SELECT from_order FROM order_item_oth WHERE item_id = ?", (item["id"],)).fetchone()[0]
            row = dict(common, **{"№ заказа MIM": from_oth[item["id"]], "Примечание": " ".join(
                ("с ОТХ, заказ №%d. %s" % (source, common["Примечание"] or "")).split()),
                "Реализация": FROM_STOCK if common["Реализация"] == REALIZED else common["Реализация"]})
        elif item["id"] in merged:                                 # a placed source: its WebMIM order and transfer; the client is in it too
            came = merged[item["id"]]
            own = {"№ заказа MIM": came["number"], "Перемещение MIM": came["transfer"],
                   "Дата счета": _invoice_day(conn, orders.get_card(conn, came["from_order"]))} if came["placed"] else {}   # else the new order's
            row = dict(common, **dict(own, **{"Примечание": " ".join(
                ("клиент уже есть в заказе №%s. %s" % (came["label"], common["Примечание"] or "")).split())}))
        elif item["id"] in gave:                                   # the old row stays, at 0 pieces: «объединён в заказ №…»
            row = dict(common, **{"Примечание": " ".join(("объединён в заказ №%d. %s" % (gave[item["id"]], common["Примечание"] or "")).split())})
        out[key(card_id, item["id"])] = dict(row, **{
            "Артикул": item["code"] or item["goods_id"], "Наименование": item["name"], "Кол-во": qty, "Цена MIM": buy,
            "Сумма MIM": round(buy * qty, 2), "Цена продажи": sell, "Сумма продажи": round(sell * qty, 2),
            "Прибыль ₽": profit[0] if profit else "", "Прибыль %": profit[1] if profit else ""})
    return out


def common_of(conn, card: dict, placed_at: str, sent: Optional[Dict[str, str]] = None) -> dict:
    """The columns of a row that are the order's, not the position's (card_rows)."""
    sent = sent or {}
    return {"Дата": _day(placed_at), "№ заказа MIM": card["webmim_number"] or card["webmim_id"], "Перемещение MIM": card["transfer_number"],
            "Дата счета": _invoice_day(conn, card), "Имя": card["client_name"], "Телефон": connections.format_phone(card["client_phone"]),
            "ПВЗ": card["point"]["name"] if card["point"] else "", "Этап": card["stage"]["name"], "Реализация": realization(card),
            "Списание": bool(card["codes_written_off_at"]), "Уведомление точке": _stamp(sent.get("manager", "")),
            "Уведомление клиенту": _stamp(sent.get("client", "")), "Примечание": card["comment"]}


def followed(conn, card_id: int) -> Tuple[set, set]:
    """The rows of the order whose quantity follows it (a position that gave tyres from «На ОТХ» or to a merge, or took them by a merge,
    order_merge) and those whose «Примечание» does too (a merge: «объединён в заказ №…», «клиент уже есть в заказе №…»)."""
    merged = {key(card_id, i) for i in order_merge.ever(conn, card_id)}
    return {key(card_id, i) for i in oth_sale.given(conn, card_id)} | merged, merged


def changes(now_rows: Dict[str, dict], written: Dict[str, dict], follow=(), notes=()) -> List[dict]:
    """What to send: a new position with every column (`create`), a written one with the columns of UPDATED that changed and the cells
    of CHECK as written (the script writes nothing into a row that no longer has them). A row the script did not find (the owner deleted or
    rewrote it) is not made again. `follow`: the rows whose quantity follows the order too (FOLLOWED: a position that gave tyres from «На ОТХ»
    to a new order, the owner 2026-10-29); `notes`: the rows whose «Примечание» follows too (a merge, the owner 2026-10-30)."""
    out = []
    for mark, fields in now_rows.items():
        before = written.get(mark)
        if before is None:
            out.append({"key": mark, "create": True, "fields": {c: fields[c] for c in COLUMNS}})
        elif not before.get("_missing"):
            diff = {c: fields[c] for c in UPDATED + (FOLLOWED if mark in follow else ()) + (("Примечание",) if mark in notes else ())
                    if before.get(c) != fields[c]}
            if diff:
                out.append({"key": mark, "create": False, "fields": diff, "check": {c: before.get(c, "") for c in CHECK}})
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
    answer = call(http, s["url"], {"secret": secret, "sheet": SHEET, "ping": True, "columns": list(COLUMNS)})
    missing = [str(c) for c in answer.get("missing") or []]
    if missing:
        raise SheetError("На листе «%s» нет колонок: %s." % (SHEET, ", ".join(missing)))
    return "Таблица отвечает: лист «%s», все колонки на месте." % SHEET


# ---- sending ------------------------------------------------------------------------------------------------------------------------

def _retry_at(now: str, failing: int) -> str:
    minutes = RETRY_MINUTES[min(failing, len(RETRY_MINUTES)) - 1]
    return (datetime.fromisoformat(now) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _last_event(conn, card_id: int) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM order_event WHERE order_id = ?", (card_id,)).fetchone()[0]


def _names(items: List[dict]) -> str:
    seen: List[str] = []
    for op in items:
        for c in op["fields"]:
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
    ops = changes(current, written, *followed(conn, card_id))
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
    why = next((str(r.get("error"))[:200] for r in replies if r.get("error")), "")
    made = [op for op in ops if op["create"] and results.get(op["key"]) in ("created", "updated")]     # updated: made by a send whose answer was lost
    kept = [op for op in ops if not op["create"] and results.get(op["key"]) == "updated"]
    missing = [op for op in ops if results.get(op["key"]) == "missing"]
    for op in made + kept:
        written[op["key"]] = dict(written.get(op["key"], {}), **op["fields"])
    for op in missing:
        written[op["key"]] = dict(written.get(op["key"], {}), _missing=True)
    lines = []
    if made:
        lines.append("Записано в Google-таблицу: %d стр." % len(made))
    if kept:
        lines.append("Google-таблица обновлена: %s." % _names(kept))
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
    for card_id, error in conn.execute("SELECT order_id, error FROM sheet_order WHERE failing > 0 ORDER BY order_id").fetchall():
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
