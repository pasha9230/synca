"""«Заказы»: the board, the list, a new order and moving cards."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import date, timedelta
from typing import Optional
from urllib.parse import urlencode

from flask import abort, flash, jsonify, redirect, render_template, request, session

from ..amounts import parse_amount
from ..avito import AvitoClient
from ..secretbox import SecretKeyError
from ..transport import urllib_transport
from .. import order_notify, order_oth, order_reserve as reserve_store, oth_sale, point_log
from .. import chat_deals as deals, chats as chats_store, connections, economics, order_alerts, order_offers, order_placed, order_report, order_search, order_sheet, order_wait, orders_export, users as panel_users, order_engine as engine, order_manager as manager, order_points as points, order_rules as rules, order_templates as templates, orders, orders_list, sales as sales_store, terms as terms_store, trash, vk_send, webmim_create, webmim_match
from . import economics_view, service
from .common import author, can, can_post, get_conn, has_right, login_required, now, now_iso, own_point, point_account
from .routes_main import context

log = logging.getLogger(__name__)


ORDER_PARTS = ("items", "wants", "money")        # the parts of the order page a form comes back to (old links name them as ?tab=)


def local_now():
    """Moscow time; the panel clock is UTC."""
    return now() + timedelta(hours=3)


def local_iso() -> str:
    return local_now().isoformat(timespec="seconds")


def from_panel() -> bool:
    """A page opened by GET that also does something («взяли в работу», «просмотрено», the journal of listening) does it only when it is
    opened inside the panel or directly (the address bar, a link from an app): Sec-Fetch-Site «same-origin» or «none», or no such header
    (an old browser). Sent from another site, the page is shown and nothing is done (хвост З20)."""
    return request.headers.get("Sec-Fetch-Site", "none").strip().lower() in ("same-origin", "none")


def error_kind(conn, fields: dict) -> str:
    """The flash category of a refused order form: «field:<name>» for the first field that its own check refuses (the message goes under
    that field, docs/design.md §4), else «error» (a banner at the top)."""
    for key, value in fields.items():
        if key not in orders.EDITABLE:
            continue
        try:
            orders._clean(key, value, conn)
        except orders.OrderError:
            return "field:" + key
        except Exception:
            continue
    return "error"


def _int(value):
    text = str(value or "").strip()
    return int(text) if text.isascii() and text.isdigit() else None


def _ajax() -> bool:
    return request.headers.get("X-Requested-With") == "fetch"


def _responsible_options(app, conn, current: str = "") -> list:
    """Who can answer for an order: the owner and the panel users (and a name already on the card, so that it is not lost)."""
    logins = [app.config["AUTHOR"]] + [u["login"] for u in panel_users.list_users(conn) if u["role"] != "client"]     # a client is not staff
    if current and current not in logins:
        logins.append(current)
    return [{"login": login, "label": login + (" (владелец)" if login == app.config["AUTHOR"] else "")} for login in dict.fromkeys(logins)]


def check_responsible(app, conn, login, current: str = "") -> None:
    """The person sent as responsible (or on duty) must be one of the list the form offers (the owner, the staff, the one already set):
    a forged POST never names a client (a buyer) or anybody else. Empty is «nobody»."""
    login = str(login or "").strip()
    if login and login not in {o["login"] for o in _responsible_options(app, conn, current)}:
        raise orders.OrderError("Ответственный: выберите из списка сотрудников.")


SEND_ALL_SECONDS = 15                              # «Отправить все»: a next message starts only this soon after the press, so a press is at
                                                   # most 15 s + one send (gunicorn and nginx cut a request at 120 s; one send alone as before)
MAX_NEW_ROWS = 20                                  # positions in the window of a new order; more are added in the card


def _known_tags(conn) -> list:
    """The tags already on orders, the most used first: the suggestions of the tag field of the order window."""
    count = {}
    for (text,) in conn.execute("SELECT tags FROM order_card WHERE tags != ''"):
        for tag in orders.tag_list(text):
            count[tag] = count.get(tag, 0) + 1
    return sorted(count, key=lambda t: (-count[t], t.lower()))[:50]


def _price_text(value) -> str:
    """A price for a field: 1234.5 → «1234.5», 5875.0 → «5875» (kopeks are kept, as in the position form of the card)."""
    return ("%.2f" % float(value)).rstrip("0").rstrip(".")


def _new_rows(form) -> list:
    """The positions of the window of a new order, one per row: the code chosen in the list, the text, pieces and the price for one
    (empty = the panel's). A row without a code and without a text is the empty field of the window: skipped."""
    names = ("item_goods_id", "item_query", "item_qty", "item_sale_unit", "item_sale_auto")
    lists = [form.getlist(name) for name in names]
    rows = []
    for i in range(max(len(values) for values in lists)):
        code, text, qty, price, auto = (values[i].strip() if i < len(values) else "" for values in lists)
        if code or text:
            rows.append({"goods_id": code, "query": text, "name": text, "qty": qty or "1", "sale_unit": price, "sale_auto": auto})
    return rows


def _amount_field(amount) -> str:
    """A sum for a field of the window: «16000», «1500.5»; empty when there is none."""
    return ("%.2f" % float(amount)).rstrip("0").rstrip(".") if amount and float(amount) > 0 else ""


def _return_amount(values: dict, money: dict, items: list, typed) -> dict:
    """«Сумма возврата» (the owner 2026-10-26): an empty sum is suggested by the kind, a return to the supplier with the purchase as
    written in the order (each position's price × qty, the «Закупка … за N шт» of the window, not `money["cost"]` with the supplier's
    discount: the owner 2026-10-27), a client's return with the sale; the manager keeps or types it and saves. `suggest`: the sum of each kind, for app.js to switch with the kind;
    the purchase is money: never in the page of who has no right «money», nor of a point, nor when a position has no purchase yet (it
    would be short). The window «Отмена или возврат» fills its empty field (its one button makes the return); the order window only
    shows the suggestion greyed (`hint`): every button of that window sends the whole form, so a value put there would be saved by
    «Отвязать» or a stage too. A saved or a typed sum stays as it is."""
    suggest = {"client": _amount_field(money["sale"])}
    if has_right("money") and own_point() is None and not money["no_purchase"]:
        suggest["supplier"] = _amount_field(round(sum(float(i["supplier_price"]) * int(i["qty"]) for i in items), 2))
    saved = values["return_amount"]
    empty = not (typed and "return_amount" in typed) and not float(saved or 0) > 0
    shown = "" if empty else _amount_field(saved) if isinstance(saved, (int, float)) else str(saved)
    return {"value": shown, "hint": suggest.get(values["return_kind"] or "", "") if empty else "",
            "suggest": {k: v for k, v in suggest.items() if v}}


def _row_money(row) -> None:
    """The price for one and what a row of the window comes to (pieces × the price typed); None while the price is the panel's or is not a number."""
    row["unit"] = row["sum"] = None
    try:
        row["unit"] = parse_amount(row["sale_unit"]) if row["sale_unit"] else None
        qty = int(row["qty"])
    except ValueError:
        return
    if row["unit"] is not None and 1 <= qty <= 100:
        row["sum"] = round(qty * row["unit"], 2)


def _touched(conn, changes: dict, was, stored: dict) -> dict:
    """Only the fields the manager changed on the page. The order window sends all of them, and a page opened before the system wrote a
    field (a tag of the wait check, the chat or the phone from the chats) must not wipe it. `was`: what the page showed (as stored);
    without it, or broken, everything sent is taken, as before. Tags are merged: the ones added or taken off on the page go over the
    tags stored now (`stored`), so a tag the system gave meanwhile stays."""
    try:
        shown = json.loads(was) if was else None
    except ValueError:
        shown = None
    if not isinstance(shown, dict):
        return changes
    out = {}
    for key, value in changes.items():
        try:
            clean = orders._clean(key, value, conn)
        except orders.OrderError:
            out[key] = value                                              # a wrong value goes on: update_card says what is wrong
            continue
        before = shown.get(key)
        same = key in shown and (clean == before or (isinstance(clean, float) and isinstance(before, (int, float)) and float(before) == clean))
        if same:
            continue
        if key == "tags" and key in shown:
            mine, before, now_tags = orders.tag_list(clean), orders.tag_list(before), orders.tag_list(stored.get("tags"))
            value = ", ".join([t for t in now_tags if t in mine or t not in before] + [t for t in mine if t not in before and t not in now_tags])
        out[key] = value
    return out


def _offer(conn, card_id, made: list) -> None:
    """Messages ready to send have just come: the next page of this order opens the window «Сообщения по заказу» once (the board and «Чаты»
    open «Отправить клиенту?» for the first message to the client, with a link to the window when other messages wait too)."""
    if not made:
        return
    to_client = [t for t in made if engine.audience(conn, t) == "client"]
    session["send_offer"] = {"card": card_id, "task": to_client[0]["id"] if to_client else None, "more": len(made) > len(to_client[:1])}


def offer_send(conn, card_id, moved_at: str) -> None:
    """A move has just made messages (to the client, to the point): the window opens once on the next page."""
    _offer(conn, card_id, engine.new_messages(conn, card_id, moved_at))


def offer_waiting(conn, card_id) -> None:
    """A chat was just bound, the order was placed in WebMIM, a lacking value was filled in: the waiting messages are offered once (they go
    only by a press)."""
    _offer(conn, card_id, [t for t in engine.list_tasks(conn, card_id, ("pending",)) if t["kind"] == "message"])


def messages_link(card_id) -> str:
    """The page of the order with the window «Сообщения по заказу» open."""
    return "/orders/%d?messages=1#messages" % card_id


def bind_chat(conn, card_id, chat_row, how: str, move: bool = False) -> None:
    """A person binds a chat to the order (the field «Чат Авито», the chat found by the phone, «Перепривязать сюда»); said by a flash."""
    result = deals.bind_chat(conn, card_id, chat_row, how, local_iso(), author(), move=move)
    if result["state"] == "linked":
        flash("Чат %s привязан к заказу: сообщения клиенту уйдут в него по кнопке «Отправить»." % deals.ref(chat_row), "ok")
        offer_waiting(conn, card_id)
    elif result["state"] == "other":
        flash("Чат %s уже в заказе №%s: он не перенесён. Перенести можно кнопкой «Перепривязать сюда»." % (deals.ref(chat_row), result["order"]))
    elif result["state"] == "busy":
        flash("Чат %s сейчас оформляется в заказ: попробуйте через минуту." % deals.ref(chat_row))
    elif result["state"] == "missing":
        flash("Чат %s не найден." % deals.ref(chat_row))


def _chat_label(conn, chat_row) -> dict:
    """Who is in the chat and about which ad: to check before «Привязать» (the client card may join people by mistake)."""
    chat = chats_store.get_chat(conn, chat_row)
    return {"chat": chat_row, "name": chat["peer_name"] or "без имени", "item": chat["item_title"]}


def chat_bind_view(conn, card: dict, linked: list):
    """What the order window says about binding a chat (None without the section «Чаты»): the chat of the field that is another order's,
    a field that names no chat of the panel, the free chat to bind (the field's, or the one found by the client's phone)."""
    if not can("chats", "edit"):
        return None
    field = str(card["avito_chat"] or "").strip()
    named = deals.chat_of_field(conn, field) if field else None
    names_chat = bool(orders.CHAT_REF.match(field) or deals.AVITO_CHAT_PAGE.match(field))      # not an ad link: that is no chat at all
    view = {"other": None, "missing": field if names_chat and named is None and not linked else "", "offer": None}
    if named is not None and named not in {ch["id"] for ch in linked}:
        owner = chats_store.get_chat(conn, named)["order_id"]
        if owner and owner > 0:
            view["other"] = {"chat": named, "order": owner}
        elif owner != -1:
            view["offer"] = dict(_chat_label(conn, named), why="из поля «Чат Авито»")
    if not linked and view["offer"] is None:
        found = deals.phone_chats(conn, card)
        if found:
            view["offer"] = dict(_chat_label(conn, found[0]), why="по телефону клиента")
    return view


def board_offer(conn, card_id, moved_at: str):
    """The window for the board (it has no page of the order to draw it in): what it shows, as data. A message that cannot go from the
    panel is said in the board's next message instead."""
    made = engine.new_messages(conn, card_id, moved_at)
    to_client = [t for t in made if engine.audience(conn, t) == "client"]
    form = send_form(conn, to_client[0]) if to_client else None
    if form and not form["chat"]:
        flash("Правило этапа подготовило сообщение клиенту «%s» (заказ №%d), но отправить его из панели нельзя: %s" % (form["task"]["title"], card_id, form["why"]))
    others = [t["title"] for t in made if not (form and form["chat"] and t["id"] == form["task"]["id"])]
    more = {"more": messages_link(card_id), "others": others} if others else {}       # the point's message never waits unseen (the lead 2026-10-23)
    if not (form and form["chat"]):
        return dict(more, card=card_id) if more else None
    return dict(more, url="/orders/%d/task/%d/send" % (card_id, form["task"]["id"]), task=form["task"]["id"], card=card_id, title=form["task"]["title"],
                client=form["client"], ref=form["ref"], chat=form["chat"]["id"], body=form["task"]["body"])


def take_offer(card_id):
    """The offer of a window for this order (taken once), or None. The live refresh of «Чаты» reads the page too: it must not take it away."""
    if _ajax():
        return None
    offer = session.pop("send_offer", None)
    return offer if offer and offer.get("card") == card_id else None


def send_offer(conn, card_id, prefer=None):
    """«Чаты»: the window to open on this page (taken once): the task, its text and the chat it goes to, and `more` (the link to «Сообщения
    по заказу») when other messages wait too; None when there is nothing to offer."""
    offer = take_offer(card_id)
    if not offer:
        return None
    form = None
    try:
        task = engine.get_task(conn, offer.get("task")) if offer.get("task") else None
    except orders.OrderError:
        task = None
    if task is not None and task["status"] == "pending":
        form = send_form(conn, task, prefer)
    more = messages_link(card_id) if offer.get("more") or (form is not None and not form["chat"]) else ""
    if form is None:
        return {"task": None, "chat": None, "card_id": card_id, "more": more} if more else None
    return dict(form, more=more)


def send_form(conn, task: dict, prefer=None):
    """What the «Отправить клиенту?» window of a waiting message to the client shows; `why` is set instead of `chat` when it cannot go."""
    if task["kind"] != "message" or engine.audience(conn, task) != "client":
        return None
    chat, why = engine.send_target(conn, task, prefer)
    card = orders.get_card(conn, task["order_id"])
    return {"task": task, "chat": chat, "ref": "Ч-%d" % chat["id"] if chat else "", "why": why, "client": card["client_name"], "card_id": card["id"]}


def _lacking(result: str) -> str:
    """What a message lacks, said as what to do in the order window."""
    names = [n.strip() for n in str(result or "").split(":", 1)[-1].rstrip(".").split(",") if n.strip()]
    if templates.VARIABLES["transfer_number"] in names:
        rest = [n for n in names if n != templates.VARIABLES["transfer_number"]]
        return "Впишите номер перемещения в окне заказа (поле «Номер перемещения»)%s." % ("; ещё не хватает: " + ", ".join(rest) if rest else "")
    return "Заполните в окне заказа: %s." % ", ".join(names) if names else str(result or "")


def messages_view(conn, card_id, vk_ready: bool) -> list:
    """The window «Сообщения по заказу»: every waiting message of the order (to the client, to the manager of the point), where it goes, what
    it lacks, and why it cannot go from the panel. `can`: its own button sends it. `current`: made since the order entered its stage (the
    owner 2026-10-25: «Отправить все» sends only these; the older ones keep their own buttons)."""
    out = []
    entered = orders._row(conn, card_id)["entered_at"] or ""
    for task in engine.list_tasks(conn, card_id, ("pending", "error", "running")):
        if task["kind"] != "message":
            continue
        whom = engine.audience(conn, task)
        view = {"task": task, "whom": whom, "where": "", "chat": None, "point": None, "why": "", "lacking": "", "can": False,
                "current": str(task["created_at"] or "") >= entered}
        if whom == "client":
            chat, view["why"] = engine.send_target(conn, task)
            if chat:
                view["chat"], view["where"] = chat["id"], "чат Ч-%d" % chat["id"]
        elif whom == "manager":
            point, view["why"] = engine.point_target(conn, task)
            if point:
                view["point"], view["where"] = point["id"], "точка «%s» в ВК" % point["name"]
                view["why"] = "" if vk_ready else engine.NO_VK_KEY
        else:
            view["why"] = "Шаблона этого сообщения больше нет: отправьте текст сами и отметьте «Отправлено вручную»."
        if task["status"] == "error":
            view["lacking"] = _lacking(task["result"])
        view["can"] = task["status"] == "pending" and bool(view["chat"] or view["point"]) and not view["why"]
        out.append(view)
    return out


def _with_stubs(events: list, chats_shown: list) -> list:
    """Avito's stub of a voice, a file… in «Клиент написал»: a label and the button «Открыть чат в Авито» (the chat shown on the card) in
    place of the raw text (хвост З19); the stored record is as it was."""
    chat_id = next((ch["chat_id"] for ch in chats_shown), "")
    out = []
    for event in events:
        shown = order_alerts.stub_text(event["text"])
        out.append(dict(event, text=shown, avito_link=chats_store.unsupported_link(event["text"][len(order_alerts.CLIENT_WROTE):], chat_id))
                   if shown else event)
    return out


OTH_OFFER = ("cancel", "refused")                 # a refusal of an order at the point: the tyres stay there (the owner 2026-10-29)
OTH_QUESTION = "Шины остаются на точке — перевести на «На ОТХ»?"


def no_account_tags(conn, tags):
    """The tags a manager of a point sees: never the name of an Avito account (chat_deals._account_tag puts it on the orders of the chats and
    the calls; the owner 2026-10-29: a point never sees an account's name). Everybody else: the tags as they are."""
    if own_point() is None:
        return tags
    names = orders.account_tags(conn)
    return [t for t in tags if t.lower() not in names]


def oth_offer(conn, card: dict, stage_id) -> Optional[dict]:
    """The stage of «Отмена» or «Отказ/невыкуп» chosen for an order on «Товар на точке» before the person answered the question: the move
    waits and the card asks it (the owner 2026-10-29); None: move as asked. The answer comes as the field oth_answer (yes / no)."""
    if card["stage"]["code"] != "at_point" or request.form.get("oth_answer") in ("yes", "no"):
        return None
    stage = next((s for s in orders.list_stages(conn) if s["id"] == stage_id), None)
    return stage if stage is not None and stage["code"] in OTH_OFFER else None


def ask_oth(card_id: int, stage: dict):
    """Keep the move asked for (and its reason) for the question of the card; the board is told where the question is."""
    session["oth_offer"] = {"card": card_id, "stage_id": stage["id"], "cancel_reason": str(request.form.get("cancel_reason") or "").strip()[:300]}
    where = "/orders/%d#oth-offer" % card_id
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=False, message=OTH_QUESTION, go=where)
    return redirect(where)


def card_or_404(conn, card_id):
    try:
        return orders.get_card(conn, card_id)
    except orders.OrderError:
        abort(404)


def register(app) -> None:
    def offer_item(conn, goods_id, account_id=None):
        """The keyword arguments of orders.add_item for a position of the supplier's unload, with its money of today: the price of its ad in
        the Avito account `account_id` (order_offers.account_for; the main one by default)."""
        _conn, settings, parsed, ref = context(app)
        return order_offers.item_args(conn, settings, parsed, ref, goods_id, local_now().date().isoformat(), account_id)

    def typed_goods(conn, code, text, stock=False):
        """(code, name when found by the text): the position the manager chose in the list, or the one the text names exactly. Raises OrderError when the text is not exact
        enough: then the manager chooses in the list. `stock`: a deal of the point's stock, whose tyre the unload may not have (the list offers it)."""
        if code:
            return code, ""
        _conn, _settings, parsed, ref = context(app)
        offer, count = order_search.typed(conn, parsed, ref, text)
        if offer is not None:
            return offer.goods_id, offer.name_orig or offer.name
        hint = " Шину, которой нет в выгрузке, выберите в списке строкой «шина с точки»." if stock else ""
        if count:
            raise orders.OrderError("По «%s» нашлось позиций: %d. Выберите нужную в списке под полем.%s" % (text[:80], count, hint))
        raise orders.OrderError("По «%s» не нашлось ни кода позиции, ни модели с остатком.%s" % (text[:80], hint))

    def row_item(conn, code, text, sale_unit, stock=False, account_id=None, sale_auto=""):
        """(keyword arguments of orders.add_item without qty, name found by the text) for a row of «Позиции»: a position of the unload, or a tyre of the
        point (code POINT_GOODS, chosen in the list: its name is the text). The price typed wins over the panel's; the price the list or the catalog
        put in the field and nobody changed (`sale_auto`, the same text) is the panel's: priced now by the account of the window."""
        if code == orders.POINT_GOODS:
            return order_offers.point_item_args(conn, text, sale_unit), ""
        goods_id, by_text = typed_goods(conn, code, text, stock)
        args = offer_item(conn, goods_id, account_id)
        if str(sale_unit or "").strip() and str(sale_unit).strip() != str(sale_auto or "").strip():
            args["sale_unit"] = sale_unit
        return args, by_text

    def window_account(conn, values, card_id=None):
        """The Avito account that prices the positions of a window (order_offers.account_for): the order's (`card_id`, or `order` of the
        search), the chat's (`chat`, the search of «Чаты»), else the chat named in «Чат Авито» or the point of a window not saved yet."""
        card_id = card_id if card_id is not None else _int(values.get("order"))
        chat_row = _int(values.get("chat"))
        if card_id is None and chat_row is None and values.get("avito_chat"):
            chat_row = deals.chat_of_field(conn, values.get("avito_chat"))
        return order_offers.account_for(conn, chat_row=chat_row, card_id=card_id, point_id=None if card_id is not None else _int(values.get("point_id")))

    @app.route("/orders/items/find")
    @login_required
    def order_item_find():
        conn = get_conn()
        _conn, settings, parsed, ref = context(app)
        query = request.args.get("q", "").strip()
        account = window_account(conn, request.args) if len(query) >= 2 else None
        items = order_search.suggestions(conn, settings, parsed, ref, query, local_now().date().isoformat(), account_id=account) if len(query) >= 2 else []
        if items and own_point() is None:                                      # the tyres lying on «На ОТХ» (oth_sale): «на ОТХ: Бета 2 шт»
            marks = oth_sale.find_hint(conn, [it["goods_id"] for it in items])
            items = [dict(it, oth=marks[str(it["goods_id"])]) if str(it["goods_id"]) in marks else it for it in items]
        if len(query) >= 2 and request.args.get("deal") == "stock":           # a deal of the point's stock: its tyre may be one the unload has not
            items.append({"goods_id": orders.POINT_GOODS, "name": " ".join(query.split())[:200], "point": True})
        return jsonify({"items": items})

    def _dots(conn):
        """{order: colour of its most important open alert} for the dots by the numbers. Same rows as the «Уведомления» page
        (one's own records are skipped, except red). A failure here never breaks the page of orders."""
        try:
            from .routes_alerts import grouped
            order = order_alerts.ALERT_LEVELS
            found = {}
            for row in grouped(conn, session["user"]):
                order_id = row["order_id"] if row["order_id"] is not None else row.get("of_order")   # «Срочно ответить» of the order's chat
                have = found.get(order_id)
                if have is None or order.index(row["level"]) < order.index(have):
                    found[order_id] = row["level"]
            return found
        except Exception:
            log.exception("order dots failed")
            return {}

    @app.route("/orders")
    @login_required
    def orders_page():
        conn = get_conn()
        orders.ensure_pipeline(conn, local_iso())
        view = "list" if request.args.get("v") == "list" else "board"
        point, tag, query = own_point() or _int(request.args.get("p")), request.args.get("t", "").strip()[:30], request.args.get("q", "").strip()[:80]
        data = orders.board(conn, local_now(), point_id=point, tag=tag, query=query,          # a manager of a point: only the point (common.own_point),
                            with_sums=own_point() is None)                                       # and no sums (the owner 2026-10-26)
        only = _int(request.args.get("s"))                     # one stage (the stages of «Рабочее место» lead here, the owner 2026-10-29)
        if only is not None and any(col["stage"]["id"] == only for col in data["columns"]):
            data = dict(data, columns=[col for col in data["columns"] if col["stage"]["id"] == only])
        else:
            only = None
        if own_point():                                                   # the tags with an account's name are not the point's to see
            for col in data["columns"]:
                for c in col["cards"]:
                    c["tags"] = no_account_tags(conn, c["tags"])
        rows = sorted(({"card": c, "stage": col["stage"]} for col in data["columns"] for c in col["cards"]), key=lambda r: -r["card"]["id"])
        labels = orders.origin_labels(conn, own_point(), [r["card"]["id"] for r in rows]) if own_point() else {}   # not from the point's account
        table = list_table(conn, rows, only) if view == "list" else None
        return render_template("orders.html", data=data, view=view, point=point, tag=tag, query=query, labels=labels, table=table,
                               points=[p for p in points.list_points(conn) if not own_point() or p["id"] == own_point()],
                               dots=_dots(conn), rows=rows, total_cards=sum(col["count"] for col in data["columns"]), only_stage=only,
                               waiting={} if own_point() else engine.waiting_counts(conn), inbox_new=0 if own_point() else manager.new_count(conn))

    def list_money() -> bool:
        """The purchase and the profit in the list: the right «money», never a manager of a point (the owner 2026-10-29)."""
        return has_right("money") and own_point() is None

    def list_table(conn, rows: list, only) -> dict:
        """«Список» as the Google table (orders_list): the rows of one page, the totals of every page of the filter."""
        money, ids = list_money(), [r["card"]["id"] for r in rows]
        pages = max(1, -(-len(ids) // orders_list.PAGE))
        page = min(max(_int(request.args.get("page")) or 1, 1), pages)
        shown = set(ids[(page - 1) * orders_list.PAGE:page * orders_list.PAGE])
        if money:                                                         # the totals count every order of the filter
            found = orders_list.rows(conn, ids, money)
            lines = [r for r in found if r["order"] in shown]
        else:
            found = lines = orders_list.rows(conn, [i for i in ids if i in shown], money, point=own_point() is not None)
        stage = {r["card"]["id"]: r["stage"] for r in rows}
        for line in lines:
            line["stage"] = stage[line["order"]]
        columns = orders_list.choice(conn, session["user"], money, point=own_point() is not None)
        keep = {k: v for k, v in (("v", "list"), ("p", request.args.get("p", "")), ("t", request.args.get("t", "")),
                                  ("q", request.args.get("q", "")), ("s", only or "")) if v}
        return {"rows": lines, "columns": columns, "shown": [c["name"] for c in columns if c["shown"]], "totals": orders_list.totals(found, money),
                "numbers": orders_list.NUMBERS, "page": page, "pages": pages, "orders": len(ids), "keep": keep,
                "link": lambda n: "/orders?" + urlencode(dict(keep, page=n))}

    @app.route("/orders/list/columns", methods=["POST"])
    @login_required
    def orders_list_columns():
        """«Колонки» of the list: the person's own choice and order of the columns (user_pref), or back to the default."""
        conn = get_conn()
        if request.form.get("reset"):
            orders_list.reset(conn, session["user"])
        elif not orders_list.save(conn, session["user"], list_money(), request.form.getlist("order"), request.form.getlist("shown"), now_iso(),
                                   point=own_point() is not None):
            flash("Оставьте хотя бы одну колонку.")
        keep = {k: request.form.get(k, "").strip()[:80] for k in ("p", "t", "q", "s")}
        return redirect("/orders?" + urlencode(dict({"v": "list"}, **{k: v for k, v in keep.items() if v})))

    @app.route("/orders/export.xlsx")
    @login_required
    def orders_export_file():
        """The orders of the page (the same filters) as an Excel file. Read only."""
        conn = get_conn()
        orders.ensure_pipeline(conn, local_iso())
        stamp = local_now()
        try:
            data = orders_export.build(conn, stamp, point_id=own_point() or _int(request.args.get("p")), tag=request.args.get("t", "").strip()[:30],
                                       query=request.args.get("q", "").strip()[:80], money=own_point() is None)   # a point: no sums
        except ImportError:                                           # the library of Excel files is not installed on this machine
            flash("Выгрузка в Excel недоступна: на сервере не установлена библиотека openpyxl.")
            return redirect("/orders")
        resp = app.response_class(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp.headers["Content-Disposition"] = 'attachment; filename="orders-%s.xlsx"' % stamp.date().isoformat()
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _report_args():
        today = local_now().date()
        start, end = request.args.get("from", "").strip()[:10], request.args.get("to", "").strip()[:10]
        try:
            start = date.fromisoformat(start).isoformat()
            end = date.fromisoformat(end).isoformat()
        except ValueError:
            start, end = today.replace(day=1).isoformat(), today.isoformat()
        return start, end, request.args.get("pt", "").strip()[:80]

    @app.route("/orders/report")
    @login_required
    def orders_report():
        """What the orders sold, per point, with the point fee and the split of the profit. Read only."""
        conn = get_conn()
        start, end, point = _report_args()
        names = [r[0] for r in conn.execute("SELECT DISTINCT %s FROM sale s LEFT JOIN point p ON p.id = s.point_id WHERE s.source = 'order'"
                                            " AND %s != ''" % (sales_store.POINT_NAME, sales_store.POINT_NAME))]        # the point's name now
        report = order_report.build(conn, start, end, point)
        every = order_report.build(conn, start, end) if point else report               # a point paid only for the clients it handed over
        names = sorted(set(names) | {r["point"] for r in every["handoff"]["lines"] if r["point"] != order_report.NO_POINT})   # is chosen too (З8)
        rates, today = order_report.list_rates(conn), local_now().date().isoformat()
        return render_template("orders_report.html", report=report, start=start, end=end, point=point,
                               point_names=names, rates=rates, today=today, fee=order_report.rate_on(rates, "point_fee", today),
                               owner_share=order_report.rate_on(rates, "owner_share", today), defaults=order_report.DEFAULTS,
                               handoff_fee=order_report.rate_on(rates, "handoff_fee", today), labels=order_report.share_labels(conn),
                               saved_labels=order_report.saved_share_labels(conn), label_defaults=order_report.SHARE_LABELS,
                               label_max=order_report.SHARE_LABEL_MAX)

    @app.route("/orders/report/rates", methods=["POST"])
    @login_required
    def orders_report_rates():
        """The point fee and the owner's share from a date (today or later); an order is counted with the rates of its day.
        The form «Подписи долей» comes here too (`what=labels`): who the two shares are shown to, with no date (a label, not money)."""
        conn = get_conn()
        if request.form.get("what") == "labels":
            if not has_right("money"):
                abort(403)
            changed = order_report.set_share_labels(conn, request.form.get("owner_label", ""), request.form.get("partner_label", ""), now_iso(), author())   # UTC: the journal reads it so
            flash("Подписи долей сохранены." if changed else "Подписи не менялись.")
            return redirect("/orders/report")
        def typed(name):                     # a field left as the form filled it is not sent: it would undo a rate already saved for a later date
            text = request.form.get(name, "").strip()
            return None if text == request.form.get(name + "_was", "").strip() else parse_amount(text)
        try:
            fee, pct, handoff = typed("point_fee"), typed("owner_pct"), typed("handoff_fee")
            share = None if pct is None else pct / 100
            changed = order_report.set_rates(conn, request.form.get("valid_from", "").strip()[:10], local_now().date().isoformat(), local_iso(), author(),
                                             point_fee=fee, owner_share=share, handoff_fee=handoff)
        except ValueError as err:
            flash(str(err) if str(err) != "not a number" else "Ставка: нужно число.")
            return redirect("/orders/report")
        flash("Ставки сохранены." if changed else "Ничего не записано: поля не менялись или такие ставки уже действуют с этой даты. "
              "Чтобы записать прежнее значение поверх будущей ставки, впишите его заново (например, 75,00).")
        return redirect("/orders/report")

    @app.route("/orders/report/export.csv")
    @login_required
    def orders_report_export():
        start, end, point = _report_args()
        conn = get_conn()
        resp = app.response_class(order_report.export_csv(order_report.build(conn, start, end, point), order_report.share_labels(conn)),
                                  mimetype="text/csv")
        resp.headers["Content-Disposition"] = 'attachment; filename="orders-report-%s_%s.csv"' % (start, end)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def new_page(conn, values: dict, rows: list, status: int = 200, busy=None):
        """The window of a new order: what was typed (or the defaults) and the positions chosen so far. `busy`: the client's open order
        that refused this one, linked when the user can open it (a point's manager: only an order of the point)."""
        for row in rows:
            _row_money(row)
        if busy is not None and own_point() is not None and conn.execute("SELECT point_id FROM order_card WHERE id = ?", (busy,)).fetchone() != (own_point(),):
            busy = None
        return render_template("order_new.html", v=values, rows=rows, busy=busy, points=points.list_points(conn), sources=orders.SOURCES, deal_kinds=orders.DEAL_KINDS,
                               responsible_options=_responsible_options(app, conn, values.get("responsible", "")), known_tags=_known_tags(conn),
                               tags=orders.tag_list(values.get("tags", ""))), status

    @app.route("/orders/new")
    @login_required
    def order_new_page():
        conn = get_conn()
        rows, goods_id = [], request.args.get("goods_id", "").strip()
        if goods_id:                                                      # «Оформить заказ» from the catalog: the position is already in the window
            try:
                args = offer_item(conn, goods_id)
                shown = _price_text(args["sale_unit"])                     # the main account's: priced again by the window's account if kept
                rows.append({"goods_id": args["goods_id"], "query": args["name"], "name": args["name"], "qty": "1", "sale_unit": shown, "sale_auto": shown})
            except orders.OrderError as err:
                flash(str(err), "error")
        return new_page(conn, {"source": "avito", "deal_kind": "webmim", "responsible": author()}, rows)

    @app.route("/orders/new", methods=["POST"])
    @login_required
    def order_new():
        conn = get_conn()
        orders.ensure_pipeline(conn, local_iso())
        fields = {k: v for k, v in request.form.items() if k in orders.EDITABLE}
        typed = fields.get("client_name", "")
        if len(re.sub(r"\D", "", typed)) >= 10 and not re.search(r"[^\W\d_]", typed) and not fields.get("client_phone"):
            fields["client_phone"], fields["client_name"] = typed, ""            # quick add: a phone typed into the name field
        rows = _new_rows(request.form)
        goods_id = request.form.get("goods_id", "").strip()
        if goods_id:                                                      # the older form of the catalog: one position, one piece, the panel's price
            rows.insert(0, {"goods_id": goods_id, "query": "", "name": "", "qty": "1", "sale_unit": "", "sale_auto": ""})
        found = []
        try:
            if len(rows) > MAX_NEW_ROWS:
                raise orders.OrderError("В новом заказе не больше %d позиций: остальные добавьте в карточке." % MAX_NEW_ROWS)
            check_responsible(app, conn, fields.get("responsible"))
            items, account = [], window_account(conn, fields) if rows else None
            for row in rows:                                              # every position is checked before anything is written
                args, by_text = row_item(conn, row["goods_id"], row["query"], row["sale_unit"], fields.get("deal_kind") == "stock", account,
                                         row["sale_auto"])
                items.append(dict(args, qty=row["qty"]))
                if by_text:
                    found.append(by_text)
            stamp = local_iso()
            card_id = orders.create_card_with_items(conn, fields, items, stamp, author(), require_contact=not items, scope_point=own_point())
        except orders.OpenOrderExists as err:                            # the owner 2026-10-27: one open order per phone
            flash(str(err), "error")
            return new_page(conn, dict(request.form.items()), rows, busy=err.order_id)
        except orders.OrderError as err:
            flash(str(err), error_kind(conn, fields))                    # the window comes back with what was typed: nothing is lost, no card is made
            return new_page(conn, dict(request.form.items()), rows)
        if found:
            flash("Добавлено по тексту: %s." % "; ".join(found))
        named = deals.chat_of_field(conn, fields.get("avito_chat")) if can("chats", "edit") and fields.get("avito_chat") else None
        if named is not None:
            bind_chat(conn, card_id, named, "field")                     # the chat of the client is bound before the window offers the message
        offer_send(conn, card_id, stamp)                                  # the rules of the first stage made a message: «Отправить клиенту?» at once
        return redirect("/orders/%d" % card_id)

    @app.route("/orders/<int:card_id>/move", methods=["POST"])
    @login_required
    def order_move(card_id):
        conn = get_conn()
        stage_id = _int(request.form.get("stage_id"))
        changes = {} if own_point() else {k: v for k, v in request.form.items() if k in orders.EDITABLE}     # a point manager only moves
        card = card_or_404(conn, card_id)
        asked = oth_offer(conn, card, stage_id) if stage_id is not None else None
        if asked is not None:
            return ask_oth(card_id, asked)
        if request.form.get("oth_answer") in ("yes", "no"):                # the answer to the question: its reason goes with the move, for a point too
            session.pop("oth_offer", None)
            if request.form.get("cancel_reason"):
                changes["cancel_reason"] = str(request.form.get("cancel_reason"))[:300]
        send = None
        try:
            if stage_id is None:
                raise orders.OrderError("Этап выбран неверно.")
            check_responsible(app, conn, changes.get("responsible"), card["responsible"])
            stamp = local_iso()
            moved = orders.move_card(conn, card_id, stage_id, stamp, author(), changes)
            message, ok = ("Заказ перенесён." if moved else "Заказ уже на этом этапе."), True
            if moved and own_point() is not None:                         # «История точки»: the point moved its order on the board
                point_log.record(conn, stamp, author(), own_point(), "move", card_id, "этап «%s»" % orders.get_stage(conn, stage_id)["name"])
            if moved and _ajax():                                         # the board draws the same window from the answer
                try:
                    send = board_offer(conn, card_id, stamp) if own_point() is None else None      # a point sees no messages
                except Exception:                                         # the card did move: no window, the task waits in the card
                    conn.rollback()
                    log.exception("the send window of card %s could not be prepared", card_id)
            elif moved:
                offer_send(conn, card_id, stamp)
        except orders.OrderError as err:
            message, ok = str(err), False
        if _ajax():
            return jsonify(ok=ok, message=message, send=send)
        flash(message, "message" if ok else "error")
        return redirect("/orders/%d" % card_id)

    def _oth_ask(conn, card: dict):
        """The question «Шины остаются на точке» kept by ask_oth for this order, while it still stands on «Товар на точке»."""
        asked = session.get("oth_offer") or {}
        if asked.get("card") != card["id"] or card["stage"]["code"] != "at_point":
            return None
        stages = {s["id"]: s for s in orders.list_stages(conn)}
        target, oth = stages.get(asked.get("stage_id")), next((s for s in stages.values() if s["code"] == "oth"), None)
        if target is None or oth is None:
            return None
        return {"target": target, "oth": oth, "cancel_reason": asked.get("cancel_reason", ""), "question": OTH_QUESTION}

    @app.route("/orders/<int:card_id>/oth-agreed", methods=["POST"])
    @login_required
    def order_oth_agreed(card_id):
        """«Согласовано с поставщиком» for an order on «На ОТХ» (the owner and the partner): the date of the return, the point sees it."""
        conn = get_conn()
        card = card_or_404(conn, card_id)
        try:
            day = order_oth.agree(conn, card, (local_now()).date(), local_iso(), author())
            flash("Возврат согласован: точка передаст шины поставщику %s.%s и подпишет акт МХ-3." % (day[8:10], day[5:7]), "ok")
        except order_oth.OthError as err:
            flash(str(err), "error")
        return redirect("/orders/%d" % card_id)

    @app.route("/orders/<int:card_id>/oth-unagree", methods=["POST"])
    @login_required
    def order_oth_unagree(card_id):
        """«Отменить согласование» of the return of «На ОТХ» (the owner and the partner), until the point signs the act МХ-3."""
        conn = get_conn()
        card = card_or_404(conn, card_id)
        if order_oth.unagree(conn, card, local_iso(), author()):
            flash("Согласование отменено: шины снова на ОТХ, точка возврат не видит.", "ok")
        else:
            flash("Отменять нечего: возврат не согласован или акт МХ-3 уже подписан.", "error")
        return redirect("/orders/%d" % card_id)

    @app.route("/orders/<int:card_id>")
    @login_required
    def order_card_page(card_id):
        conn = get_conn()
        return card_page(conn, card_or_404(conn, card_id))

    def card_page(conn, card: dict, typed=None, status: int = 200):
        """The page of an order. `typed`: what the window sent and could not be saved; the window shows it again, with the error."""
        card_id = card["id"]
        values = dict(card, **typed) if typed else card
        if own_point() is not None:                                      # the comment of an order made from a call named the account (2026-10-29)
            values = dict(values, comment=orders.without_account_names(conn, values.get("comment")))
        stored = orders._row(conn, card_id)
        shown = json.dumps({k: stored[k] for k in orders.EDITABLE}, ensure_ascii=False)     # what the window shows, to save only what is changed on it
        engine.refresh_tasks(conn, card_id, local_iso())
        part = request.args.get("tab") if request.args.get("tab") in ORDER_PARTS else ""
        events = orders.list_events(conn, card_id)
        acting = from_panel()                                            # sent from another site: the page only
        if events and acting:                                            # the user has seen the journal up to the newest record shown
            order_alerts.mark_seen(conn, session["user"], card_id, local_iso(), events[0]["id"])
            conn.commit()
        if acting and own_point() is None and can("orders", "edit") and manager.take(conn, card_id, "открыл заказ", session["user"], local_iso()):     # «взяли в работу»
            events = orders.list_events(conn, card_id)
        tasks = [t for t in engine.list_tasks(conn, card_id, ("pending", "error", "running")) if t["kind"] != "message"]   # messages: their window
        offer = take_offer(card_id)
        messages = messages_view(conn, card_id, vk_sender(conn, quiet=True) is not None) if own_point() is None else []      # points: none
        open_messages = (bool(messages) and bool(offer or request.args.get("messages")) and any(m["task"]["status"] == "pending" for m in messages)
                         and (can_post("order_messages_send") or bool(request.args.get("messages"))))     # by itself: only for who may send
        linked = chats_store.chats_of_order(conn, card_id)
        links, bind, chat_ref, origin = linked, chat_bind_view(conn, card, linked), orders.chat_link(card["avito_chat"]), None
        money = orders.totals(card["items"])
        if own_point() is not None:                                      # the manager of a point (the owner 2026-10-22): an order not from the
            origin = orders.origin_labels(conn, own_point(), [card_id]).get(card_id)        # point's account is labelled; the chats of other
            account = point_account()                                    # accounts and the client's words in them are not shown
            links = [ch for ch in linked if ch["account_id"] == account]
            if len(links) < len(linked):
                events = [e for e in events if not orders.client_words(e)]
            if origin or len(links) < len(linked):                       # not the point's own order: what was written to the client is named only
                events = [engine.for_point(e) for e in events]
            events = [dict(e, text=orders.without_account_names(conn, e["text"])) for e in events]   # never an account's name (2026-10-29)
            if chat_ref and not chat_ref[1] and chat_ref[0] not in {"/chats/%d" % ch["id"] for ch in links}:
                chat_ref = None                                          # «Открыть чат» of the field: a chat of another account
            if bind:
                bind = dict(bind, **{k: None for k in ("other", "offer") if bind[k] and chats_store.get_chat(conn, bind[k]["chat"])["account_id"] != account})
        return render_template(
            "order_card.html", messages=messages, open_messages=open_messages,
            c=card, entity=orders.card_view(card), v=values, oth_plan=order_oth.return_plan(conn, card["id"]),
            oth_taken={i: dict(t, back=oth_sale.can_untake(conn, card_id, i)) for i, t in oth_sale.taken(conn, card_id).items()},
            oth_at_point=oth_sale.all_from_oth(conn, card) and own_point() is None and can_post("order_oth_at_point"),
            oth_back=oth_sale.why_not(conn, card) is None and own_point() is None and can_post("order_oth_untake"), oth_offers=oth_sale.offers(conn, card) if own_point() is None and can_post("order_oth_take") else {}, oth_ask=_oth_ask(conn, card), was=shown, part=part, tags=no_account_tags(conn, orders.tag_list(typed["tags"]) if typed and "tags" in typed else card["tags"]), known_tags=no_account_tags(conn, _known_tags(conn)), days=int(orders._seconds_on_stage(card["entered_at"], local_now()) // 86400), stages=orders.list_stages(conn), points=points.list_points(conn),
            stage_ids={s["code"]: s["id"] for s in orders.list_stages(conn) if s["code"]},
            events=_with_stubs(events, links), totals=money, return_amount=_return_amount(values, money, card["items"], typed), return_kinds=orders.RETURN_KINDS, sources=orders.SOURCES, deal_kinds=orders.DEAL_KINDS, marks=orders.MARKS,
            tasks=tasks, chat_links=links, chat_bind=bind, origin=origin, chat_sends=chats_store.pending_sends(conn, [ch["id"] for ch in links], local_iso()),
            chat_ref=chat_ref, responsible_options=_responsible_options(app, conn, card["responsible"]),
            want_seasons=order_wait.SEASON_NAMES, want_studs=order_wait.STUD_NAMES, want_text=order_wait.describe,
            client_text=order_wait.client_text(card) if card["wants"] and any(
                t["title"] == order_wait.TITLE_EXACT for t in engine.list_tasks(conn, card_id, ("pending",))) else "",
            send_chats=[ch for ch in links if ch["account_id"] in {                                 # (a point: its own account's only)
                a["id"] for a in connections.list_accounts(conn) if a["chats_enabled"] and a["chats_send"]}],
            exact_title=order_wait.TITLE_EXACT, webmim=webmim_match.card_block(conn, card, with_hints=own_point() is None),
            webmim_cost=order_report.order_webmim(conn, card_id) if has_right("money") else None,                # the purchase is money
            webmim_create=webmim_create.button(conn, card, webmim_match.current_server(conn)) if can_post("order_webmim_create") else None), status, {
            "Cache-Control": "no-store"}            # «Назад» reads the order again: the positions a press added are shown (хвост З6)

    def back(card_id, part=""):
        return redirect("/orders/%d%s" % (card_id, "#" + part if part else ""))

    @app.route("/orders/<int:card_id>/save", methods=["POST"])
    @login_required
    def order_save(card_id, part=None):
        conn = get_conn()
        card = card_or_404(conn, card_id)
        changes = _touched(conn, {k: v for k, v in request.form.items() if k in orders.EDITABLE}, request.form.get("was"), orders._row(conn, card_id))
        ticked = request.form.get("codes_written_off_was")                # «Коды маркировки списаны» of the window: the date of the mark «Списание»
        if ticked in ("0", "1") and ticked != ("1" if request.form.get("codes_written_off") == "1" else "0"):    # only a tick changed on the page
            changes["codes_written_off_at"] = (card["codes_written_off_at"] or local_now().date().isoformat()) if ticked == "0" else ""
        number_before = card["webmim_number"]
        lacking_before = {t["id"] for t in engine.list_tasks(conn, card_id, ("error",)) if t["kind"] == "message"}
        if changes.get("responsible") == card["responsible"]:
            changes.pop("responsible")                                       # the form shows who answers now: sending that back names nobody new
        # the row of «Позиции» chosen or typed and not added by «+»: «Сохранить» adds it with the rest (the owner 2026-10-23, order №428).
        # A choice in the list fills the text too: a code without the text is what a browser restored after «Назад», not a choice
        typed_row = {"row_" + k: request.form.get(k, "").strip() for k in ("goods_id", "query", "qty", "sale_unit", "sale_auto")}
        typed_row = typed_row if typed_row["row_query"] else {}
        try:
            check_responsible(app, conn, changes.get("responsible"), card["responsible"])
            changed = orders.update_card(conn, card_id, changes, local_iso(), author())
        except orders.OrderError as err:
            flash(str(err), error_kind(conn, changes))
            if typed_row:
                flash("Позиция не добавлена: окно не сохранено.", "error")
            if "stage_id" in request.form or request.form.get("tab") != "money":
                return card_page(conn, card, dict(changes, **typed_row))  # the window comes back with what was typed: nothing saved, nothing moved
            return back(card_id, "money")
        if can("chats", "edit"):                                          # the chat of the order: unlink, move here, bind the offered or the typed one;
            # before the row: the row is priced by the account of the chats the order has after this press (order_offers.account_for)
            unlink, moved, offered = (_int(request.form.get(k)) for k in ("chat_unlink", "chat_move", "chat_bind"))
            if unlink is not None:
                if deals.unbind_chat(conn, card_id, unlink, local_iso(), author()):
                    flash("Чат %s отвязан от заказа." % deals.ref(unlink), "ok")
            elif moved is not None:
                bind_chat(conn, card_id, moved, "field", move=True)
            elif offered is not None:
                bind_chat(conn, card_id, offered, "field" if offered == deals.chat_of_field(conn, orders._row(conn, card_id)["avito_chat"]) else "phone")
            elif "avito_chat" in changed:
                named = deals.chat_of_field(conn, orders._row(conn, card_id)["avito_chat"])
                if named is not None:
                    bind_chat(conn, card_id, named, "field")
        refused = None
        if typed_row:
            try:
                by_text = add_typed_item(conn, card_id, request.form)
                flash("Сохранено, позиция добавлена.", "ok")
                if by_text:
                    flash("Добавлено по тексту: %s." % by_text)
            except orders.OrderError as err:
                refused = err                                              # the fields stay saved; the window comes back with the row typed
                flash("Поля заказа сохранены. Позиция не добавлена: %s" % err, "error")
        elif part == "items":                                              # «+» with no row chosen: the fields are saved, the row is not there
            flash("Поля заказа сохранены. Позиция не выбрана: начните вводить модель и выберите шину из списка.", "error")
        else:
            flash("Сохранено.", "ok")
        row = orders._row(conn, card_id)
        if row["webmim_number"] and (("webmim_number" in changed and not number_before) or "webmim_id" in changed):
            # placed in WebMIM (the number appeared, or the id for the API came): the purchase from there; a corrected number keeps the purchase
            _conn, _settings, parsed, _ref = context(app)
            prices, source = order_offers.webmim_purchase(conn, orders.get_card(conn, card_id), parsed, app.config)
            orders.refresh_purchase(conn, card_id, prices, source, local_iso(), author())
        if lacking_before:                                                # a lacking value typed (the transfer number): the window opens
            engine.refresh_tasks(conn, card_id, local_iso())
            if any(t["id"] in lacking_before for t in engine.list_tasks(conn, card_id, ("pending",))):
                offer_waiting(conn, card_id)                              # a move of this press below offers its own messages instead
        if refused is not None:                                           # the press meant the position and the move: no move without it
            if "stage_id" in request.form:
                flash("Заказ не перенесён: позиция не добавлена.", "error")
            return card_page(conn, card_or_404(conn, card_id), typed_row)
        if "stage_id" in request.form:                                    # a stage of the window: the fields are saved first, then the move
            asked = oth_offer(conn, card_or_404(conn, card_id), _int(request.form.get("stage_id")))
            if asked is not None:                                         # the fields are saved (the reason too); the move asks first
                return ask_oth(card_id, asked)
            try:
                stage_id = _int(request.form.get("stage_id"))
                if stage_id is None:
                    raise orders.OrderError("Этап выбран неверно.")
                stamp = local_iso()
                if orders.move_card(conn, card_id, stage_id, stamp, author()):
                    flash("Заказ перенесён.")
                    offer_send(conn, card_id, stamp)
            except orders.OrderError as err:
                flash(str(err), "error")                                  # the fields stay saved; the refusal is a red record of the journal
        part = part or request.form.get("tab", "")
        return back(card_id, part if part in ORDER_PARTS else "")

    @app.route("/orders/webmim")
    @login_required
    def orders_webmim_page():
        """«Сверка с WebMIM»: the panel's orders without a pair, the WebMIM orders nobody is paired with, the suggestions. Reads the snapshot only."""
        conn = get_conn()
        return render_template("orders_webmim.html", r=webmim_match.reconcile(conn, local_now().date()))

    @app.route("/orders/<int:card_id>/webmim/link", methods=["POST"])
    @login_required
    def order_webmim_link(card_id):
        """«Это он»: a person confirms the WebMIM order of a suggestion; the number and the id go to the order (its journal says so)."""
        conn = get_conn()
        card_or_404(conn, card_id)
        try:
            order = webmim_match.link(conn, card_id, request.form.get("order", ""), local_iso(), author())
            flash("Заказ сопоставлен с заказом WebMIM №%s." % (order["number"] or order["id"]), "ok")
        except orders.OrderError as err:
            flash(str(err), "error")
        except sqlite3.OperationalError:                                  # the base is busy longer than its wait: nothing was written
            flash("База занята, попробуйте ещё раз.", "error")
        if request.form.get("back") == "webmim":
            return redirect("/orders/webmim")
        return back(card_id, "webmim")

    def avito_deliver(conn, stamp: str):
        """deliver(chat row, text, token) for order_placed: chats.send_reply from the Avito account of the chat; None without the master key."""
        box = app.config.get("SECRET_BOX")
        if box is None:
            return None

        def deliver(chat_row, text, token):
            chat = chats_store.get_chat(conn, chat_row)
            client_id = connections.get_secret(conn, box, "avito", chat["account_id"], "client_id")
            client_secret = connections.get_secret(conn, box, "avito", chat["account_id"], "client_secret")
            if not client_id or not client_secret:
                raise chats_store.ChatError("У аккаунта не заданы ключи Авито.")
            client = AvitoClient(client_id, client_secret, http=app.config.get("AVITO_HTTP") or urllib_transport, allow_write=True)
            chats_store.send_reply(conn, client, chat_row, text, token, stamp, author())
        return deliver

    def vk_sender(conn, quiet=False):
        """send(peer_id, text, random_id=) through the VK community; None without the master key or the key of the community.
        `quiet`: the page only asks whether VK can be written to (no record of a key that cannot be read on every view)."""
        try:
            return vk_send.sender_for(conn, app.config.get("SECRET_BOX"), http=app.config.get("VK_HTTP") or urllib_transport)
        except Exception:                                                 # a key that cannot be read: the message waits, the press goes on
            if not quiet:
                log.exception("the key of the VK community could not be read")
            return None

    @app.route("/orders/<int:card_id>/webmim/create", methods=["POST"])
    @login_required
    def order_webmim_create(card_id):
        """«Оформить в WebMIM» / «Досоздать в WebMIM»: a person's press makes the order in WebMIM (webmim_create; the test server only, the owner
        and the partner). The result is shown and is a record of the order's journal; the stage stays. After the first full success: order_placed.on_placed
        (the client's message)."""
        conn = get_conn()
        card_or_404(conn, card_id)
        server = webmim_match.current_server(conn)
        try:
            client = None
            if server in webmim_create.CREATE_SERVERS and app.config.get("SECRET_BOX") is not None:
                client = webmim_match.client_for(conn, app.config["SECRET_BOX"], app.config.get("WEBMIM_HTTP") or urllib_transport, server)
            try:
                result = webmim_create.place(conn, card_id, client, server, local_iso(), local_now().date(), author())
            finally:
                if client is not None:
                    client.close()
            flash(result["text"], "ok" if result["ok"] else "error")
            if result.get("stage_error"):                                 # the order is made in WebMIM, its stage is not: a red line of its own
                flash(result["stage_error"], "error")
            if result["ok"]:                                              # the first full success only: a done order refuses a press
                stamp = local_iso()
                state = webmim_create.get_state(conn, card_id) or {"webmim_id": ""}
                order_sheet.enroll(conn, card_id, stamp)                  # placed in WebMIM: its rows go to the Google table (if it is on)
                conn.commit()
                for line in order_placed.on_placed(conn, card_id, state["webmim_id"], stamp, author(), avito_deliver(conn, stamp), vk_sender(conn)):
                    flash(line, "ok" if " отправлено «" in line and "не отправлено" not in line else "info" if order_placed.IN_PANEL in line else "error")
                offer_waiting(conn, card_id)                              # what is left to send opens «Сообщения по заказу»
        except orders.OrderError as err:
            flash(str(err), "error")
        except SecretKeyError:
            flash("Секреты не читаются: проверьте мастер-ключ.", "error")
        except sqlite3.OperationalError:                                  # the base is busy longer than its wait (before or after WebMIM)
            flash("База занята: итог нажатия мог не сохраниться. Нажмите ещё раз через минуту: панель сначала поищет заказ в WebMIM "
                  "по номеру заказа и не создаст второй.", "error")
        return back(card_id, "webmim")

    @app.route("/orders/<int:card_id>/note", methods=["POST"])
    @login_required
    def order_note(card_id):
        conn = get_conn()
        card_or_404(conn, card_id)
        try:
            orders.add_note(conn, card_id, request.form.get("text", ""), local_iso(), author())
        except orders.OrderError as err:
            flash(str(err))
        return back(card_id)

    def add_typed_item(conn, card_id, form) -> str:
        """The row of «Позиции» to the order, by «+» and by «Сохранить» alike: the position chosen in the list or the one the text names exactly,
        its pieces, the price typed (empty = the panel's). Returns the name found by the text, or ""; OrderError when it is not added."""
        code, text = form.get("goods_id", "").strip(), form.get("query", "").strip()
        if not text:                                     # a choice in the list fills the text too: a code alone is what a browser restored after «Назад»
            raise orders.OrderError("Позиция не выбрана: начните вводить модель и выберите шину из списка.")
        args, by_text = row_item(conn, code, text, form.get("sale_unit", ""), orders._row(conn, card_id)["deal_kind"] == "stock",
                                 window_account(conn, {}, card_id), form.get("sale_auto", ""))
        orders.add_item(conn, card_id, qty=form.get("qty", "1"), now=local_iso(), author=author(), **args)
        return by_text

    @app.route("/orders/<int:card_id>/items/add", methods=["POST"])
    @login_required
    def order_item_add(card_id):
        conn = get_conn()
        card_or_404(conn, card_id)
        if "was" in request.form:                                          # «+» of the card's window sends the whole window (the owner 2026-10-26:
            return order_save(card_id, part="items")                       # «Точка» was lost): its fields are saved as by «Сохранить», with the row
        try:
            by_text = add_typed_item(conn, card_id, request.form)
            if by_text:
                flash("Добавлено по тексту: %s." % by_text)
        except orders.OrderError as err:
            flash(str(err))
        return back(card_id, "items")

    @app.route("/orders/<int:card_id>/items/<int:item_id>/oth-take", methods=["POST"])
    @login_required
    def order_oth_take(card_id, item_id):
        """«Взять с ОТХ»: the supplier's position gives its pieces to the tyres of an order on «На ОТХ» at the order's point (oth_sale)."""
        conn = get_conn()
        card = card_or_404(conn, card_id)
        if item_id not in {i["id"] for i in card["items"]}:
            abort(404)
        try:
            pieces = oth_sale.take(conn, card_id, item_id, _int(request.form.get("from_item")) or 0, local_iso(), author())
            flash("Взято с ОТХ: %d шт. В WebMIM оформится только недостающее." % pieces)
        except orders.OrderError as err:
            flash(str(err))
        return back(card_id, "items")

    @app.route("/orders/<int:card_id>/items/<int:item_id>/oth-untake", methods=["POST"])
    @login_required
    def order_oth_untake(card_id, item_id):
        """«Вернуть на ОТХ»: the position «с ОТХ» goes back to its old order."""
        conn = get_conn()
        card = card_or_404(conn, card_id)
        if item_id not in {i["id"] for i in card["items"]}:
            abort(404)
        try:
            pieces = oth_sale.untake(conn, card_id, item_id, local_iso(), author())
            flash("Возвращено на ОТХ: %d шт. Если шины нужны у поставщика, добавьте позицию заново." % pieces)
        except orders.OrderError as err:
            flash(str(err))
        return back(card_id, "items")

    @app.route("/orders/<int:card_id>/oth-at-point", methods=["POST"])
    @login_required
    def order_oth_at_point(card_id):
        """«Шины уже на точке — перевести на «Товар на точке»» (the owner 2026-10-30): every tyre is taken from «На ОТХ», so the order goes
        past «Заказ оформлен» and «Перемещение» (no WebMIM) by the ordinary move with the rules of the stage (the reservation of three days);
        the client hears the message of the arrival as after the point's «приехал», the point its own text (order_notify.tell_point)."""
        conn = get_conn()
        card = card_or_404(conn, card_id)
        if not oth_sale.all_from_oth(conn, card):
            flash("Перевести сразу на «Товар на точке» можно, когда все шины заказа взяты с ОТХ и заказ ещё не оформлен.", "error")
            return back(card_id)
        stage = next((s for s in orders.list_stages(conn) if s["code"] == "at_point"), None)
        now = local_iso()
        try:
            changes = {} if card["delivery_date"] else {"delivery_date": now[:10]}    # the tyres are at the point: today, nobody is asked (the lead 2026-10-30)
            if stage is None or not orders.move_card(conn, card_id, stage["id"], now, author(), changes=changes):
                flash("Заказ уже перевели: обновите страницу.", "error")
                return back(card_id)
        except orders.OrderError as err:
            flash(str(err), "error")
            return back(card_id)
        card = orders.get_card(conn, card_id)
        body = None if card["webmim_number"] else reserve_store.NUMBER.sub("", manager.template_body(conn, templates.ARRIVED_SAMPLE))
        sent, note = manager.tell_client_arrived(conn, card_id, avito_deliver(conn, now), now, author(), body=body)
        told = order_notify.tell_point(conn, app.config.get("SECRET_BOX"), card, oth_sale.point_text(conn, card, now), now,
                                       vk_http=app.config.get("VK_HTTP") or urllib_transport)
        flash("Заказ на «%s». %s Точке: %s." % (stage["name"], note, told), "ok" if sent else "error")
        return back(card_id)

    @app.route("/orders/<int:card_id>/items/<int:item_id>/update", methods=["POST"])
    @login_required
    def order_item_update(card_id, item_id):
        conn = get_conn()
        card = card_or_404(conn, card_id)
        if item_id not in {i["id"] for i in card["items"]}:
            abort(404)
        try:
            purchase = request.form.get("supplier_price", "").strip() if has_right("money") else ""     # the purchase is money: only with the right
            orders.update_item(conn, item_id, request.form.get("qty"), request.form.get("sale_unit"), local_iso(), author(), supplier_price=purchase or None)
        except orders.OrderError as err:
            flash(str(err))
        return back(card_id, "items")

    @app.route("/orders/<int:card_id>/items/<int:item_id>/delete", methods=["POST"])
    @login_required
    def order_item_delete(card_id, item_id):
        conn = get_conn()
        card = card_or_404(conn, card_id)
        if item_id not in {i["id"] for i in card["items"]}:
            abort(404)
        try:
            orders.remove_item(conn, item_id, local_iso(), author())
        except orders.OrderError as err:
            flash(str(err))
        return back(card_id, "items")

    def _want_fields(form) -> dict:
        data = {key: form.get(key, "") for key in ("brand", "model", "width", "profile", "radius", "season", "studded", "qty", "code")}
        data["any_brand"] = "1" if form.get("any_brand") else "0"            # a ticked box is sent, an unticked one is not: say it explicitly
        data["any_size"] = "1" if form.get("any_size") else "0"
        return data

    @app.route("/orders/<int:card_id>/wants/add", methods=["POST"])
    @login_required
    def order_want_add(card_id):
        conn = get_conn()
        card_or_404(conn, card_id)
        try:
            order_wait.add_want(conn, card_id, _want_fields(request.form), local_iso(), author())
        except orders.OrderError as err:
            flash(str(err))
        return back(card_id, "wants")

    @app.route("/orders/<int:card_id>/wants/<int:want_id>/update", methods=["POST"])
    @login_required
    def order_want_update(card_id, want_id):
        conn = get_conn()
        card = card_or_404(conn, card_id)
        if want_id not in {w["id"] for w in card["wants"]}:
            abort(404)
        try:
            order_wait.update_want(conn, want_id, _want_fields(request.form), local_iso(), author())
        except orders.OrderError as err:
            flash(str(err))
        return back(card_id, "wants")

    @app.route("/orders/<int:card_id>/wants/<int:want_id>/delete", methods=["POST"])
    @login_required
    def order_want_delete(card_id, want_id):
        conn = get_conn()
        card = card_or_404(conn, card_id)
        if want_id not in {w["id"] for w in card["wants"]}:
            abort(404)
        try:
            order_wait.remove_want(conn, want_id, local_iso(), author())
        except orders.OrderError as err:
            flash(str(err))
        return back(card_id, "wants")

    @app.route("/orders/<int:card_id>/sale", methods=["POST"])
    @login_required
    def order_sale(card_id):
        conn = get_conn()
        card_or_404(conn, card_id)
        day = local_now().date().isoformat()
        try:
            orders.record_sales(conn, card_id, day, local_iso(), author(), day)
            flash("Продажа записана в «Продажи».", "ok")
        except orders.OrderError as err:
            flash(str(err))
        return back(card_id)

    @app.route("/orders/<int:card_id>/sale/reverse", methods=["POST"])
    @login_required
    def order_sale_reverse(card_id):
        conn = get_conn()
        card_or_404(conn, card_id)
        count = orders.reverse_sales(conn, card_id, local_iso(), author())
        flash("Продажа сторнирована." if count else "Записанной продажи нет.")
        return back(card_id)

    @app.route("/orders/<int:card_id>/delete", methods=["POST"])
    @login_required
    def order_delete(card_id):
        conn = get_conn()
        card = card_or_404(conn, card_id)
        try:
            trash.delete_order(conn, card_id, local_iso(), author(),          # the tyres taken from «На ОТХ» are not lost with the order
                               only_if=lambda c, _card: oth_sale.why_not_delete(c, card_id))
        except orders.OrderError as err:
            flash(str(err))
            return back(card_id)
        flash("%s удалён. Вернуть его можно в «Удалённых»." % trash.order_title(card))
        return redirect("/orders")

    @app.route("/deleted")
    @login_required
    def deleted_page():
        kind = request.args.get("k", "") if request.args.get("k", "") in trash.KINDS else ""
        return render_template("deleted.html", rows=trash.list_deleted(get_conn(), kind or None), kind=kind)

    @app.route("/deleted/<int:record_id>/restore", methods=["POST"])
    @login_required
    def deleted_restore(record_id):
        conn = get_conn()
        kind = request.form.get("k", "") if request.form.get("k", "") in trash.KINDS else ""
        record = next((r for r in trash.list_deleted(conn) if r["id"] == record_id), None)
        if record is None:
            flash("Такой удалённой записи нет или она уже восстановлена.")
            return redirect("/deleted" + ("?k=" + kind if kind else ""))
        try:
            if record["kind"] == "order":
                card_id = trash.restore_order(conn, record_id, local_iso(), author())
                flash("Заказ №%d восстановлен." % card_id)
                return redirect("/orders/%d" % card_id)
            chat_row = trash.restore_chat(conn, record_id, local_iso(), author())
            flash("Чат восстановлен. Новые сообщения подтянутся при следующем чтении Авито.")
            return redirect("/chats/%d" % chat_row)
        except orders.OrderError as err:
            flash(str(err))
        return redirect("/deleted" + ("?k=" + kind if kind else ""))

    @app.route("/orders/settings")
    @login_required
    def orders_settings_page():
        conn = get_conn()
        orders.ensure_pipeline(conn, local_iso())
        counts = {r[0]: r[1] for r in conn.execute("SELECT stage_id, COUNT(*) FROM order_card GROUP BY stage_id")}
        stages = orders.list_stages(conn)
        return render_template(
            "orders_settings.html", stages=stages, counts=counts, removable=orders.REMOVABLE_SYSTEM, kinds=orders.KIND_NAMES, triggers=rules.TRIGGERS, actions=rules.ACTIONS,
            modes=rules.MODES, templates=templates.list_templates(conn), points=points.list_points(conn),
            rules_by_stage={s["id"]: rules.list_rules(conn, s["id"]) for s in stages},
            commands_by_stage={s["id"]: rules.list_commands(conn, s["id"]) for s in stages}, stage_names={s["id"]: s["name"] for s in stages},
            template_names={t["id"]: t["name"] for t in templates.list_templates(conn)}, point_names={p["id"]: p["name"] for p in points.list_points(conn)},
            default_responsible=orders.default_responsible(conn), responsible_options=_responsible_options(app, conn, orders.default_responsible(conn)),
            duty=manager.duty_settings(conn), weekdays=manager.WEEKDAYS, today=local_now().date().isoformat())

    def _settings_back():
        return redirect("/orders/settings")

    @app.route("/orders/settings/responsible", methods=["POST"])
    @login_required
    def orders_default_responsible():
        conn = get_conn()
        try:
            check_responsible(app, conn, request.form.get("responsible", ""), orders.default_responsible(conn))
            orders.set_default_responsible(conn, request.form.get("responsible", ""))
            flash("Ответственный по умолчанию сохранён.", "ok")
        except orders.OrderError as err:
            flash(str(err))
        return _settings_back()

    @app.route("/orders/settings/duty", methods=["POST"])
    @login_required
    def orders_duty():
        """Who gets the clients the points hand over: a person per day of the week, a change for today, the working hours."""
        f, conn = request.form, get_conn()
        try:
            was = manager.duty_settings(conn)
            for d in range(7):
                check_responsible(app, conn, f.get("day%d" % d, ""), was["week"].get(d, ""))
            check_responsible(app, conn, f.get("today", ""), was["today"].get("login", ""))
            manager.set_duty(conn, {d: f.get("day%d" % d, "") for d in range(7)}, (_int(f.get("start")) or 0, _int(f.get("end")) or 0),
                             f.get("today", ""), local_now().date().isoformat())
            flash("Дежурные сохранены.")
        except (orders.OrderError, ValueError) as err:
            flash(str(err))
        return _settings_back()

    @app.route("/orders/settings/stage/add", methods=["POST"])
    @login_required
    def orders_stage_add():
        f = request.form
        try:
            orders.add_stage(get_conn(), f.get("name"), f.get("color"), f.get("kind"), f.get("hint"), f.get("limit_hours"))
            flash("Этап добавлен.")
        except orders.OrderError as err:
            flash(str(err))
        return _settings_back()

    @app.route("/orders/settings/stage/<int:stage_id>/update", methods=["POST"])
    @login_required
    def orders_stage_update(stage_id):
        f = request.form
        try:
            orders.update_stage(get_conn(), stage_id, f.get("name"), f.get("color"), f.get("kind"), f.get("hint"), f.get("limit_hours"))
            flash("Этап сохранён.")
        except orders.OrderError as err:
            flash(str(err))
        return _settings_back()

    @app.route("/orders/settings/stage/<int:stage_id>/shift", methods=["POST"])
    @login_required
    def orders_stage_shift(stage_id):
        try:
            orders.shift_stage(get_conn(), stage_id, -1 if request.form.get("dir") == "up" else 1)
        except orders.OrderError as err:
            flash(str(err))
        return _settings_back()

    @app.route("/orders/settings/stage/<int:stage_id>/delete", methods=["POST"])
    @login_required
    def orders_stage_delete(stage_id):
        try:
            orders.delete_stage(get_conn(), stage_id)
            flash("Этап удалён.")
        except orders.OrderError as err:
            flash(str(err))
        return _settings_back()

    def _task_or_404(conn, card_id, task_id):
        card_or_404(conn, card_id)
        try:
            task = engine.get_task(conn, task_id)
        except orders.OrderError:
            abort(404)
        if task["order_id"] != card_id:
            abort(404)
        return task

    @app.route("/orders/<int:card_id>/task/<int:task_id>/run", methods=["POST"])
    @login_required
    def order_task_run(card_id, task_id):
        conn = get_conn()
        _task_or_404(conn, card_id, task_id)
        try:
            status = engine.run_task(conn, task_id, local_iso(), author())
            flash("Готово." if status == "done" else "Не выполнено: %s" % engine.get_task(conn, task_id)["result"], "ok" if status == "done" else "error")
        except orders.OrderError as err:
            flash(str(err))
        return back(card_id)

    @app.route("/orders/<int:card_id>/task/<int:task_id>/skip", methods=["POST"])
    @login_required
    def order_task_skip(card_id, task_id):
        conn = get_conn()
        _task_or_404(conn, card_id, task_id)
        try:
            engine.skip_task(conn, task_id, local_iso(), author())
        except orders.OrderError as err:
            flash(str(err))
        return back(card_id)

    def send_message(conn, card_id, task_id, now: str) -> dict:
        """One message of the window «Сообщения по заказу»: to the client in Avito or to the point in VK, by its audience. Never raises."""
        try:
            task = engine.get_task(conn, task_id)
        except orders.OrderError:
            task = None
        if task is None or task["order_id"] != card_id or task["kind"] != "message":
            return {"task": task_id, "ok": False, "message": "Такого сообщения в заказе нет: обновите страницу."}
        if task["status"] == "done":                                      # «Отправить оба» after one went, a second window: never twice
            return {"task": task_id, "ok": True, "sent": False, "message": "«%s» уже отправлено: второй раз не уходит." % task["title"]}
        whom = engine.audience(conn, task)
        if str(request.form.get("whom_%d" % task_id) or "") != whom:      # the template's audience changed under the window: never to another
            return {"task": task_id, "ok": False, "message": "«%s» не отправлено: адресат сообщения изменился (шаблон поправили). Обновите "
                                                             "страницу." % task["title"]}
        text = chats_store.CONTROL.sub("", str(request.form.get("text_%d" % task_id) or "").replace("\r", "")).strip()
        limit = chats_store.MAX_REPLY if whom == "client" else vk_send.VK_LIMIT
        try:
            if not text or len(text) > limit:
                raise orders.OrderError("«%s» не отправлено: текст сообщения от 1 до %d знаков." % (task["title"], limit))
            if whom == "client":
                deliver = avito_deliver(conn, now)
                if deliver is None:
                    raise orders.OrderError("Ключи Авито не читаются (мастер-ключ): сообщение ждёт.")
                sent_to = engine.send_task(conn, task_id, text, deliver, now, author(), _int(request.form.get("chat_%d" % task_id)))
                message = "Клиенту отправлено «%s» в чат %s." % (task["title"], deals.ref(sent_to))
            elif whom == "manager":
                name = engine.send_point_task(conn, task_id, text, vk_sender(conn), now, author(), _int(request.form.get("point_%d" % task_id)))
                message = "Точке «%s» отправлено «%s» в ВК." % (name, task["title"])
            else:
                raise orders.OrderError("Шаблона этого сообщения больше нет: отправьте текст сами и отметьте «Отправлено вручную».")
            return {"task": task_id, "ok": True, "sent": True, "message": message}
        except (orders.OrderError, chats_store.ChatError, vk_send.VkError) as err:      # ChatSendUnknown too: its text says not to send again
            return {"task": task_id, "ok": False, "message": "«%s»: %s" % (task["title"], err)}
        except SecretKeyError:
            return {"task": task_id, "ok": False, "message": "«%s»: секреты не читаются, проверьте мастер-ключ. Сообщение ждёт." % task["title"]}
        except Exception:
            conn.rollback()
            log.exception("the message task %s could not be sent", task_id)
            return {"task": task_id, "ok": False, "message": "«%s»: сбой панели при отправке. Проверьте чат клиента или ВК точки, прежде чем "
                                                             "отправлять ещё раз." % task["title"]}

    def _not_yet(conn, card_id, task_id) -> dict:
        """A message «Отправить все» did not start: nothing of it was sent or marked; the next press sends it."""
        try:
            task = engine.get_task(conn, task_id)
        except orders.OrderError:
            task = None
        title = task["title"] if task is not None and task["order_id"] == card_id else "Сообщение"
        return {"task": task_id, "ok": False, "sent": False, "message": "«%s» ещё не отправлено: прошлые отправки шли долго, и панель остановилась, "
                "чтобы нажатие не оборвалось. Нажмите «Отправить все» ещё раз." % title}

    @app.route("/orders/<int:card_id>/messages/send", methods=["POST"])
    @login_required
    def order_messages_send(card_id):
        """The buttons of the window «Сообщения по заказу»: one message («Отправить клиенту в Авито», «Отправить точке в ВК») or every ready one
        («Отправить оба»). Each goes by itself and once (order_engine takes the task first); the text and the result are in the journal."""
        conn = get_conn()
        card_or_404(conn, card_id)
        pressed = str(request.form.get("task") or "")
        chosen = request.form.getlist("ready") if pressed == "ready" else [pressed]
        ids = list(dict.fromkeys(i for i in (_int(x) for x in chosen) if i is not None))
        budget, started, results = float(app.config.get("SEND_ALL_SECONDS", SEND_ALL_SECONDS)), time.monotonic(), []
        for task_id in ids:
            if results and time.monotonic() - started > budget:          # a slow Avito: the rest wait untouched for the next press
                results.append(_not_yet(conn, card_id, task_id))
                continue
            results.append(send_message(conn, card_id, task_id, local_iso()))
        if not results:
            results = [{"task": None, "ok": False, "message": "Нечего отправлять: обновите страницу."}]
        if _ajax():
            return jsonify(ok=all(r["ok"] for r in results), results=results)
        for r in results:
            flash(r["message"], "ok" if r["ok"] else "error")
        return redirect(messages_link(card_id) if not all(r["ok"] for r in results) else "/orders/%d" % card_id)

    @app.route("/orders/templates")
    @login_required
    def orders_templates_page():
        return render_template("orders_templates.html", templates=templates.list_templates(get_conn()), audiences=templates.AUDIENCES,
                               variables=templates.VARIABLES)

    @app.route("/orders/templates/save", methods=["POST"])
    @login_required
    def orders_template_save():
        conn, f = get_conn(), request.form
        try:
            template_id = _int(f.get("id"))
            if template_id is None:
                templates.add_template(conn, f.get("name"), f.get("audience"), f.get("body"))
            else:
                templates.update_template(conn, template_id, f.get("name"), f.get("audience"), f.get("body"))
            flash("Шаблон сохранён.")
        except orders.OrderError as err:
            flash(str(err))
        return redirect("/orders/templates")

    @app.route("/orders/templates/samples", methods=["POST"])
    @login_required
    def orders_template_samples():
        flash("Добавлено шаблонов: %d." % templates.add_samples(get_conn()), "ok")
        return redirect("/orders/templates")

    @app.route("/orders/templates/<int:template_id>/delete", methods=["POST"])
    @login_required
    def orders_template_delete(template_id):
        try:
            templates.delete_template(get_conn(), template_id)
            flash("Шаблон удалён.")
        except orders.OrderError as err:
            flash(str(err))
        return redirect("/orders/templates")

    @app.route("/orders/settings/rule/add", methods=["POST"])
    @login_required
    def orders_rule_add():
        f = request.form
        try:
            rules.add_rule(get_conn(), _int(f.get("stage_id")), f.get("trigger"), f.get("hours"), f.get("action"), _int(f.get("template_id")),
                           f.get("arg_stage") if f.get("action") == "move" else f.get("arg"), f.get("mode"), _int(f.get("point_id")))
            flash("Правило добавлено.")
        except orders.OrderError as err:
            flash(str(err))
        return _settings_back()

    @app.route("/orders/settings/rule/<int:rule_id>/toggle", methods=["POST"])
    @login_required
    def orders_rule_toggle(rule_id):
        conn = get_conn()
        try:
            rules.set_enabled(conn, rule_id, not rules.get_rule(conn, rule_id)["enabled"])
        except orders.OrderError as err:
            flash(str(err))
        return _settings_back()

    @app.route("/orders/settings/rule/<int:rule_id>/delete", methods=["POST"])
    @login_required
    def orders_rule_delete(rule_id):
        try:
            rules.delete_rule(get_conn(), rule_id)
        except orders.OrderError as err:
            flash(str(err))
        return _settings_back()

    @app.route("/orders/settings/command/add", methods=["POST"])
    @login_required
    def orders_command_add():
        f = request.form
        try:
            rules.add_command(get_conn(), _int(f.get("stage_id")), f.get("word"), _int(f.get("to_stage_id")))
            flash("Команда добавлена.")
        except orders.OrderError as err:
            flash(str(err))
        return _settings_back()

    @app.route("/orders/settings/command/<int:command_id>/delete", methods=["POST"])
    @login_required
    def orders_command_delete(command_id):
        rules.delete_command(get_conn(), command_id)
        return _settings_back()

    @app.route("/orders/inbox")
    @login_required
    def orders_inbox_page():
        conn = get_conn()
        from .. import vk_callback
        vk_callback.safe_sweep(conn, now())                           # what a restart of the panel cut off shows here
        return render_template("orders_inbox.html", messages=manager.list_inbox(conn), answers=manager.list_bot_answers(conn))

    from .routes_vk import channel as inbox_channel                  # the bot's channel: VK to the points, Avito to the clients

    def _to_point(conn, item, text, bot: bool = False) -> str:
        """The answer goes to the manager in VK; what happened, for the flash. `bot`: the text the panel made (a confirmation), signed «(бот) »
        (the owner 2026-10-24); the owner's own answer is not."""
        if not (str(item["sender"]).isascii() and str(item["sender"]).isdigit()):          # a line of the panel itself: nobody to write to
            return ""
        point = points.panel_point_of(conn, item["sender"])
        if point is not None:                                                             # the owner 2026-10-23: never in VK, on «Рабочее место»
            if text:
                points.add_note(conn, point["id"], "answer", text, local_iso(), asked=item["text"])
            return "Точка «%s» работает в панели: ответ показан на её «Рабочем месте», в ВК не отправлен." % item["point"]
        send = inbox_channel(app, conn, local_iso())[0]
        if send is None:
            return "В ВК не ушло: ключ сообщества не сохранён (Подключения → ВК). Отправьте менеджеру вручную."
        try:
            send(item["sender"], manager.signed(text) if bot else text)
        except Exception as err:
            return "В ВК не ушло: %s Отправьте менеджеру вручную." % err
        return "Отправлено в ВК точке «%s»." % item["point"]

    @app.route("/orders/inbox/<int:message_id>/answer", methods=["POST"])
    @login_required
    def orders_inbox_answer(message_id):
        conn = get_conn()
        try:
            item = manager.get_message(conn, message_id)
            manager.answer(conn, message_id, request.form.get("text"), local_iso())
            flash(("Ответ записан. " + _to_point(conn, item, str(request.form.get("text")).strip())).strip())
        except orders.OrderError as err:
            flash(str(err))
        return redirect("/orders/inbox")

    @app.route("/orders/inbox/<int:message_id>/confirm", methods=["POST"])
    @login_required
    def orders_inbox_confirm(message_id):
        conn = get_conn()
        try:
            item = manager.get_message(conn, message_id)
            send, deliver, notify = inbox_channel(app, conn, local_iso())
            reply = manager.confirm(conn, message_id, request.form.get("order_no"), local_iso(), author(), deliver, notify)
            flash(("Подтверждено. %s %s" % (reply, _to_point(conn, item, reply, bot=True))).strip())
        except orders.OrderError as err:
            flash(str(err))
        return redirect("/orders/inbox")

    @app.route("/orders/inbox/answers/add", methods=["POST"])
    @login_required
    def orders_bot_answer_add():
        try:
            manager.add_bot_answer(get_conn(), request.form.get("keywords"), request.form.get("answer"))
            flash("Автоответ добавлен (выключен: включите его, когда проверите текст).")
        except orders.OrderError as err:
            flash(str(err))
        return redirect("/orders/inbox")

    @app.route("/orders/inbox/answers/<int:answer_id>/toggle", methods=["POST"])
    @login_required
    def orders_bot_answer_toggle(answer_id):
        conn = get_conn()
        current = next((a for a in manager.list_bot_answers(conn) if a["id"] == answer_id), None)
        if current is None:
            abort(404)
        manager.set_bot_answer_enabled(conn, answer_id, not current["enabled"])
        return redirect("/orders/inbox")

    @app.route("/orders/inbox/answers/<int:answer_id>/delete", methods=["POST"])
    @login_required
    def orders_bot_answer_delete(answer_id):
        manager.delete_bot_answer(get_conn(), answer_id)
        return redirect("/orders/inbox")
