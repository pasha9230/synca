"""«Чаты»: Avito chats of the accounts that work with messages, with the deal each one belongs to."""
from __future__ import annotations

import re
import secrets
from pathlib import Path

from flask import abort, flash, jsonify, redirect, render_template, request, session

from .. import catalog_archive, chat_deals as deals, chats, connections, order_engine as engine, order_manager as mgr, order_offers, order_wait, orders, store, trash
from . import service
from ..avito import AvitoClient
from ..transport import urllib_transport
from .common import author, can, chat_of_other_point, get_conn, login_required, own_point, point_account, safe_path
import logging
from datetime import datetime
from urllib.parse import urlencode

from .routes_main import context
from .routes_orders import local_iso, local_now, offer_send, send_offer

LIST_LIMIT = 200
FILTERS = {"": "Открытые", "wait": "Ждут ответа", "unread": "Не прочитанные", "nodeal": "Без заказа", "noreply": "Не требуют ответа", "closed": "Закрытые"}


def _int(value):
    text = str(value or "").strip()
    return int(text) if text.isascii() and text.isdigit() else None


def _to_own_point(conn, card_id) -> None:
    """An order the manager of a point makes from a chat is of the point (else the manager would not see it); another point's is never taken.
    The order of a chat of a point's account has the point already (chat_deals binds it by the account): this is for an account of several points.
    The rules of that binding (the owner 2026-10-22): an open order whose point no person took off."""
    point = own_point()
    if point is not None:                                                 # one UPDATE with the condition: a «Взять» of another point meanwhile wins
        stamp = local_iso()
        if deals.claim_point(conn, card_id, point, stamp, author(), "%s: %s" % (orders.LABELS["point_id"], point), auto=True):
            conn.commit()
            deals.fire_point_rules(conn, card_id, stamp)                 # the rules of the point, which the new order could not see
        else:
            conn.commit()


def _took(conn, chat_row) -> None:
    """The staff answered the client in the chat: a client a point handed over from it is «взят в работу» (never by the point itself).
    A failure here never spoils the answer that went."""
    if own_point() is not None or not can("orders", "edit"):
        return
    try:
        mgr.take_chat(conn, chat_row, "ответил в чате", author(), local_iso())
    except Exception:
        conn.rollback()
        logging.getLogger(__name__).exception("the handed-over client of chat %s could not be marked taken", chat_row)


def _chat_or_404(conn, chat_row):
    chat = chats.get_chat(conn, chat_row)
    if chat is None:
        abort(404)
    return chat


SEARCH_MAX = chats.SEARCH_MAX


def _search(conn, q: str) -> set:
    """The chats a search finds (chats.find: the name, the messages, a phone, the number of the order); for the manager of a point an order
    finds its chat only when it is of the own point, as in the one search. Returns the chat rows."""
    return chats.find(conn, q, point=own_point())


def wait_text(minutes) -> str:
    """«12 мин», «1 ч 05 мин», «2 дн»."""
    if minutes is None:
        return ""
    if minutes < 60:
        return "%d мин" % minutes
    if minutes < 24 * 60:
        return "%d ч %02d мин" % (minutes // 60, minutes % 60)
    return "%d дн" % (minutes // (24 * 60))


ANSWERING_ROLES = ("owner", "partner", "chats")                                  # who may answer a chat: the roles with «Чаты» to change


def _answerers(conn) -> list:
    """[(login, name)] of the panel users who may be given a chat; the owner's shared login first."""
    from flask import current_app
    from .. import users
    owner = current_app.config["AUTHOR"]
    found = [(owner, "Владелец")]
    for u in users.list_users(conn):
        if u["active"] and u["role"] in ANSWERING_ROLES and u["login"] != owner:
            found.append((u["login"], u["display_name"] or u["login"]))
    return found


def _keep_query():
    args = [(k, v) for k, v in request.form.items() if v and ((k == "f" and v in FILTERS) or (k == "acc" and _int(v) is not None) or k == "q")]
    args = [(k, " ".join(v.split())[:SEARCH_MAX] if k == "q" else v) for k, v in args]
    return ("?" + urlencode(args)) if args else ""

log = logging.getLogger(__name__)

_SIZE = re.compile(r"\b(?:(\d{3})\s*/\s*(\d{2})\s*(?:[zZ]?[rRрР]|/)\s*(\d{2}(?:[.,]5)?)|\d{3}\s*[rRрР]\s*\d{2})\s*[cCсС]?(?!\w|[.,]\d)")   # 225/45 R17, 245/40 ZR18, 225/45/17, 195 R14C
_INDEX_AFTER_SIZE = re.compile(r"^[\s.,;]*(?:\d{2,3}(?:/\d{2,3})?\s*[A-Za-zА-Яа-я]{1,2}\b|XL\b|RF\b|Run\s*Flat\b)(?:\s*(?:XL|RF)\b)?", re.I)
_INDEX_AT_END = re.compile(r"\s+\d{2,3}(?:/\d{2,3})?\s?[HTVWYSRQPUZНТВ]{1,2}\s*$")     # the index written after the model, in capitals
_NOISE = re.compile(r"\b(?:шины|шина|резина|покрышки?|колёса|колеса|комплект|зимние|летние|всесезонные|шипованные|липучка|б/у|новые|XL|RF|run[\s-]*flat)\b|(?:\d+\s*)?шт\.?(?!\w)", re.I)


def _title_search(title: str) -> str:
    """The catalog search for an ad that is not ours: its title (model and size) without the index of load and speed that follows the size, the
    common filler words and the «C» of the size (the catalog search wants every word of the query found in one field of the position)."""
    text = title or ""
    m = _SIZE.search(text)
    if m:
        tail = _INDEX_AFTER_SIZE.sub("", _NOISE.sub(" ", text[m.end():]), count=1)
        size = "%s/%sR%s" % m.group(1, 2, 3) if m.group(1) else ""      # width and height are unknown for «195 R14C»: only the words remain
        text = text[:m.start()] + " " + size + " " + _INDEX_AT_END.sub("", tail)
    return " ".join(w for w in _NOISE.sub(" ", text).split() if re.search(r"\w", w))


def register(app) -> None:
    def _product(conn, chat):
        """The position behind the ad of a chat: the link to it in our catalog (not Avito's page: a click there counts as a view and may cost
        advertising money) and what the supplier had at the last unload. None when the ad is not ours."""
        traced = deals.ad_goods(conn, chat["account_id"], chat["item_id"])
        if traced is None:
            return None
        goods_id = traced[0]
        offer = None
        try:
            _conn, _settings, parsed, _ref = context(app)
            offer = parsed.offers.get(goods_id)
        except Exception:
            log.exception("the unload is not readable for the product of a chat")
        article = (offer.code if offer else "") or goods_id                         # the maker's article is what the catalog search finds best
        return {"code": article, "catalog_url": "/catalog?" + urlencode({"q": article}), "stock": offer.stock if offer else None}

    def _live_offers(conn):
        try:
            _conn, _settings, parsed, _ref = context(app)
            return parsed.offers
        except Exception:
            log.exception("the unload is not readable for the waiting list of a chat")
            return {}

    def _catalog_offers(conn):
        """Every tire the catalog knows: today's unload and the positions the supplier took away (the permanent base). Only for the press of the button."""
        live = _live_offers(conn)
        return list(live.values()) + [o for o, _ in catalog_archive.gone_offers(conn) if o.goods_id not in live]

    def _wait_prefill(conn, chat):
        """What the form «Клиент ждёт эту шину» starts with: the tire of the ad when the catalog knows it (one lookup, not a scan), else what the title says."""
        live = _live_offers(conn)
        traced = deals.ad_goods(conn, chat["account_id"], chat["item_id"])
        offer = (live.get(traced[0]) or catalog_archive.gone_offer(conn, traced[0])) if traced else None
        if offer is not None:
            return {"line": order_wait.line_from_offer(offer), "source": "каталога"}
        line = order_wait.line_from_title(chat["item_title"], {order_wait._offer_brand(o) for o in live.values()})
        return {"line": line, "source": "названия объявления" if len(line) > 1 else ""}

    def _position_provider(conn, chat_row):
        """Prices the position of the ad of a chat as the feed of the chat's account prices it today (order_offers.account_for); no unload or
        no price is a lesser trouble than no deal."""
        try:
            _conn, settings, parsed, ref = context(app)
            account = order_offers.account_for(conn, chat_row=chat_row)
        except Exception:
            log.exception("the position of a chat ad was not priced: the unload is not readable")

            def unreadable(goods_id, pack):
                raise orders.OrderError("выгрузка поставщика не прочитана")
            return unreadable
        day = local_now().date().isoformat()

        def provider(goods_id, pack):
            try:
                return order_offers.item_args(conn, settings, parsed, ref, goods_id, day, account)
            except orders.OrderError:
                raise
            except Exception as err:
                log.exception("the position of a chat ad was not priced")
                raise orders.OrderError("цена не посчитана (%s)" % type(err).__name__)
        return provider

    @app.route("/chats")
    @app.route("/chats/<int:chat_row>")
    @login_required
    def chats_page(chat_row=None):
        conn = get_conn()
        flt = request.args.get("f", "") if request.args.get("f", "") in FILTERS else ""
        acc = _int(request.args.get("acc"))
        mine = own_point() is not None                                    # the manager of a point: the chats of the point's account only (in the query)
        if mine:
            acc = point_account()
        q = " ".join(request.args.get("q", "").split())[:SEARCH_MAX]
        moment = local_now().replace(tzinfo=None)
        answerers = _answerers(conn)
        baselines = {}

        def waits(chat):
            """The clock of the waiting client; none for the chats that were there before their account was first read (the past)."""
            if chat["account_id"] not in baselines:
                baselines[chat["account_id"]] = store.get_state(conn, "chats_baseline_%d" % chat["account_id"]) or ""
            return chats.waiting_minutes(chat, moment) if (chat.get("last_at") or "") >= baselines[chat["account_id"]] else None
        found = _search(conn, q) if q else None
        accounts = [a for a in connections.list_accounts(conn) if (a["chats_enabled"] or a["id"] == acc) and (not mine or a["id"] == acc)]
        enabled = {a["id"] for a in connections.list_accounts(conn) if a["chats_enabled"]}
        rows, place, opened = [], {}, None
        for chat in (chats.list_chats(conn, acc) if not mine or acc is not None else []):
            if chat["account_id"] not in enabled and acc is None:
                continue
            if found is not None and chat["id"] not in found:
                continue
            place[chat["id"]] = len(place)
            if chat["id"] == chat_row:                                    # the open chat (owner's word 2026-10-24 (6)): see below
                opened = chat
            if flt == "unread" and not chat["unread"]:
                continue
            if flt == "noreply" and chat["status"] != "no_reply":
                continue
            if flt == "nodeal" and chat["order_id"]:
                continue
            if flt == "closed" and chat["status"] != "closed":
                continue
            if flt == "wait" and chat["status"] != "open":
                continue
            if flt in ("", "unread", "nodeal") and chat["status"] == "closed" and found is None:     # a search finds closed chats too
                continue
            rows.append(chat)
        pending, asked = {}, set()
        if flt == "wait":                                                 # «Ждут ответа»: the same clock as the badge «ждёт N мин»
            asked = {c["id"] for c in rows}
            pending = chats.wait_starts(conn, sorted(asked))
            for chat in rows:
                if chat["id"] in pending:
                    chat["wait_since"] = pending[chat["id"]]
            rows = [c for c in rows if waits(c) is not None]
        total = len(rows)
        fits = chat_row is not None and any(c["id"] == chat_row for c in rows)   # in the state, maybe beyond the first LIST_LIMIT
        rows = rows[:LIST_LIMIT]
        kept = False
        if opened is not None and all(c["id"] != chat_row for c in rows):   # answered, read, closed: it stays in its place, not in «Всего»
            rows = sorted(rows + [opened], key=lambda c: place[c["id"]])
            kept = opened["kept"] = not fits                              # «вне фильтра» on its row only when it is out of the state
        names = {} if mine else {a["id"]: a["name"] for a in connections.list_accounts(conn)}   # a point never sees an account's name (2026-10-29)
        wanted = [c["id"] for c in rows] + ([chat_row] if chat_row is not None else [])
        starts = dict(pending)                                            # «Ждут ответа» read them already: only the others are read
        starts.update(chats.wait_starts(conn, [r for r in wanted if r not in asked]))        # the auto-answer is no answer
        for chat in rows:
            chat["deal"] = None
            chat["last_avito"] = chats.unsupported_link(chat["last_text"], chat["chat_id"])     # Avito's stub instead of the text: a short label
            if chat["order_id"]:
                try:
                    card = orders.get_card(conn, chat["order_id"])
                    if not (mine and card["point_id"] not in (None, own_point())):      # an order of another point is not named
                        chat["deal"] = {"id": card["id"], "name": card["client_name"], "stage": card["stage"]["name"]}
                except orders.OrderError:
                    pass
            chat["account_name"] = names.get(chat["account_id"], "")
            chat["ref"] = deals.ref(chat["id"])
            if chat["id"] in starts:
                chat["wait_since"] = starts[chat["id"]]
            chat["waits"] = waits(chat)
            chat["wait_text"] = wait_text(chat["waits"])
        current, messages, deal = None, [], None
        if chat_row is not None:
            current = _chat_or_404(conn, chat_row)
            current["account_name"] = names.get(current["account_id"], "")
            current["ref"] = deals.ref(current["id"])
            current["product"] = _product(conn, current)
            current["catalog_url"] = current["product"]["catalog_url"] if current["product"] else (
                "/catalog?" + urlencode({"q": _title_search(current.get("item_title"))}) if current.get("item_title") else "")
            current["wait"] = _wait_prefill(conn, current)
            if current["id"] in starts:
                current["wait_since"] = starts[current["id"]]
            current["waits"] = waits(current)
            current["wait_text"] = wait_text(current["waits"])
            messages = chats.list_messages(conn, chat_row)
            for m in messages:                                            # Avito's stub «Сообщение не поддерживается»: a plain label and its link
                m["avito_link"] = chats.unsupported_link(m["text"], current["chat_id"]) if m["kind"] not in ("image", "call") else None
            if current["order_id"]:
                try:
                    deal = orders.get_card(conn, current["order_id"])
                    if mine and deal["point_id"] not in (None, own_point()):
                        raise orders.OrderError("another point")
                    deal["totals"] = orders.totals(deal["items"])
                    deal["days"] = int(orders._seconds_on_stage(deal["entered_at"], local_now()) // 86400)
                    deal["earlier"] = [i for i in deals.earlier_deals(conn, current) if i != deal["id"]]
                    if mine:                                                     # the numbers of other points' orders are not the point's
                        ours = {r[0] for r in conn.execute("SELECT id FROM order_card WHERE point_id = ?", (own_point(),))}
                        deal["earlier"] = [i for i in deal["earlier"] if i in ours]
                    deal["gaps"] = orders.gaps_to_place(conn, deal)
                    if mine:                                                     # never an account's name (the owner 2026-10-29)
                        deal["comment"] = orders.without_account_names(conn, deal["comment"])
                        deal["tags"] = [t for t in deal["tags"] if t.lower() not in orders.account_tags(conn)]
                except orders.OrderError:
                    deal = None
        def link(f, a):
            args = urlencode([kv for kv in (("f", f), ("acc", a), ("q", q)) if kv[1]])
            return "/chats" + ("?" + args if args else "")
        # two buttons (owner's word 2026-10-24): «Состояние» and «Аккаунт»; each keeps the other one and the search
        filter_menus = [("Состояние: " + FILTERS[flt], [(label, link(k, acc), k == flt) for k, label in FILTERS.items()])]
        if not mine and (len(accounts) > 1 or acc is not None):                     # an account in the address can always be dropped; a point's is fixed
            filter_menus.append(("Аккаунт: " + ((names.get(acc) or "аккаунт №%d" % acc) if acc is not None else "Все аккаунты"),
                                 [("Все аккаунты", link(flt, None), acc is None)] + [(a["name"], link(flt, a["id"]), a["id"] == acc) for a in accounts]))
        handoff = None
        if current and mine and current["account_id"] == point_account() and not chat_of_other_point(current["id"]):   # what the POST allows
            typed = session.pop("handoff_%d" % current["id"], None)
            handed = mgr.open_handoff_of_chat(conn, current["id"])
            handoff = {"action": "/chats/%d/handoff" % current["id"], "token": secrets.token_hex(12), "error": bool(typed),
                       "form": typed or {"phone": mgr.chat_phone(conn, current["id"])}, "seasons": mgr.SEASONS,
                       "done": handed}
        can_send = bool(current) and any(a["id"] == current["account_id"] and a["chats_send"] and a["chats_enabled"] for a in connections.list_accounts(conn))
        offer = send_offer(conn, deal["id"], chat_row) if deal else None
        return render_template("chats.html", offer=offer, handoff=handoff, can_send=can_send, live_sig=chats.signature(conn, chat_row), reply_token=secrets.token_hex(12), image_token=secrets.token_hex(12), sends=chats.pending_sends(conn, [current["id"]], local_iso()) if current else [], draft=session.get("draft_%d" % chat_row, "") if chat_row else "", rows=rows, total=total, kept=kept, current=current, messages=messages, deal=deal, flt=flt, filters=FILTERS, acc=acc, filter_menus=filter_menus,
                               accounts=accounts, stages=orders.list_stages(conn), statuses=chats.STATUSES, q=q, answerers=answerers, answer_names=dict(answerers),
                               unsearch="/chats" + ("?" + urlencode([kv for kv in (("f", flt), ("acc", acc)) if kv[1]]) if (flt or acc) else ""),
                               query=("?" + urlencode([kv for kv in (("f", flt), ("acc", acc), ("q", q)) if kv[1]])) if (flt or acc or q) else "")

    def back(chat_row):
        return redirect("/chats/%d%s" % (chat_row, _keep_query()))

    @app.route("/chats/<int:chat_row>/status", methods=["POST"])
    @login_required
    def chat_status(chat_row):
        conn = get_conn()
        _chat_or_404(conn, chat_row)
        try:
            chats.set_status(conn, chat_row, request.form.get("status", ""), local_iso())
        except ValueError as err:
            flash(str(err))
        if request.form.get("back") == "alerts":                       # «Не требует ответа» / «Закрыть» on «Срочно ответить»
            return redirect("/alerts")
        return back(chat_row)

    @app.route("/chats/<int:chat_row>/assign", methods=["POST"])
    @login_required
    def chat_assign(chat_row):
        """Who answers the chat: one of the panel users who may answer chats, or nobody."""
        conn = get_conn()
        _chat_or_404(conn, chat_row)
        login = request.form.get("responsible", "").strip()
        names = dict(_answerers(conn))
        if login and login not in names:
            flash("Такого пользователя нет среди тех, кто отвечает в чатах.")
            return back(chat_row)
        chats.set_responsible(conn, chat_row, login, local_iso(), author())
        flash("Ответственный за чат: %s." % names[login] if login else "Ответственный за чат снят.", "ok")
        return back(chat_row)

    @app.route("/chats/<int:chat_row>/items/add", methods=["POST"])
    @login_required
    def chat_items_add(chat_row):
        """«Добавить в заказ» from the chat: the position picked in the list (the same search as the order window) goes to the chat's order
        with the money of today, as the order window adds it (order_offers.item_args, orders.add_item); back to the chat."""
        conn = get_conn()
        chat = _chat_or_404(conn, chat_row)
        if not chat["order_id"] or chat["order_id"] <= 0:
            flash("У чата нет заказа: сначала создайте или привяжите заказ.")
            return back(chat_row)
        if _int(request.form.get("order_id")) not in (None, chat["order_id"]):
            flash("Чат уже привязан к другому заказу (№%d): проверьте и добавьте позицию ещё раз." % chat["order_id"])
            return back(chat_row)
        goods_id = request.form.get("goods_id", "").strip()
        if not goods_id:
            flash("Выберите позицию в списке подсказок: начните вводить модель, бренд или размер.")
            return back(chat_row)
        try:
            _conn, settings, parsed, ref = context(app)
            args = order_offers.item_args(conn, settings, parsed, ref, goods_id, local_now().date().isoformat(), order_offers.account_for(conn, chat_row=chat_row))
            if request.form.get("sale_unit", "").strip():
                args["sale_unit"] = request.form["sale_unit"]
            orders.add_item(conn, chat["order_id"], qty=request.form.get("qty", "1"), now=local_iso(), author=author(), **args)
            flash("В заказ №%d добавлено: %s." % (chat["order_id"], args.get("name") or goods_id), "ok")
        except orders.OrderError as err:
            flash(str(err))
        return back(chat_row)

    REFRESH_EVERY = 15                                                                # seconds between two reads of Avito that open pages may start

    @app.route("/chats/refresh", methods=["POST"])
    @login_required
    def chats_refresh():
        """An open «Чаты» page asks for fresh messages every few seconds: read the newest chats of Avito (at most once in REFRESH_EVERY seconds for all
        pages) and answer with the signature of what the page shows; the page reloads itself when it differs from the one it was drawn with."""
        conn = get_conn()
        box = app.config.get("SECRET_BOX")
        chat_row = _int(request.form.get("chat"))
        if own_point() is not None and chat_row is not None and (chats.get_chat(conn, chat_row) or {}).get("account_id") != point_account():
            chat_row = None                                               # the manager of a point: the signature of the own chats only
        stamp = local_now()
        last = store.get_state(conn, "chats_refresh_at")
        due = box is not None and (not last or (stamp - datetime.fromisoformat(last)).total_seconds() >= REFRESH_EVERY)
        if due:
            store.set_state(conn, "chats_refresh_at", stamp.isoformat(timespec="seconds"))
            lock_path = Path(app.config["DB_PATH"]).resolve().parent / ".chats.lock"
            with chats.sync_lock(lock_path) as held:
                if held:
                    for account in connections.list_chat_accounts(conn):
                        try:
                            client_id = connections.get_secret(conn, box, "avito", account["id"], "client_id")
                            client_secret = connections.get_secret(conn, box, "avito", account["id"], "client_secret")
                            if client_id and client_secret:
                                client = AvitoClient(client_id, client_secret, http=app.config.get("AVITO_HTTP") or urllib_transport)
                                chats.sync_account(conn, client, account["id"], local_iso(), max_pages=1)
                        except Exception:                                          # a miss of Avito must not break the page: the timer reads again in a minute
                            conn.rollback()
        return jsonify(sig=chats.signature(conn, chat_row))

    def _writer(conn, chat):
        """An Avito client of the account of the chat that may write: the replies and the photos to the client."""
        box = app.config.get("SECRET_BOX")
        if box is None:
            raise chats.ChatError("Мастер-ключ недоступен: ключи Авито не расшифровать.")
        client_id = connections.get_secret(conn, box, "avito", chat["account_id"], "client_id")
        client_secret = connections.get_secret(conn, box, "avito", chat["account_id"], "client_secret")
        if not client_id or not client_secret:
            raise chats.ChatError("У аккаунта не заданы ключи Авито.")
        return AvitoClient(client_id, client_secret, http=app.config.get("AVITO_HTTP") or urllib_transport, allow_write=True)

    def send_again(conn, chat, token):
        """«Отправить снова» of a send the sync found not delivered (chats.pending_sends): its kept text once more under its own mark, by this
        press only; the reply form's right. Back to the chat, or to `back` (the order's card)."""
        row = conn.execute("SELECT status, text, photo FROM chat_send_log WHERE token = ? AND chat_row = ?", (token[:64], chat["id"])).fetchone()
        if row is not None and row[0] == "sent":
            flash("Это сообщение уже отправлено клиенту.")
        elif row is None or row[0] != "lost" or row[2] or not row[1]:
            flash("Это сообщение не ждёт повтора: обновите страницу.")
        else:
            try:
                found = chats.send_reply(conn, _writer(conn, chat), chat["id"], row[1], token[:64], local_iso(), author(), again=True)
                flash("Сообщение уже есть в чате Авито: повторно не отправлено." if found == "found" else "Сообщение отправлено клиенту снова.")
            except chats.ChatError as err:                                 # ChatSendUnknown too: the sync checks it again
                flash(str(err))
        target = safe_path(str(request.form.get("back") or ""))
        return redirect(target) if target else back(chat["id"])

    @app.route("/chats/<int:chat_row>/send", methods=["POST"])
    @login_required
    def chat_send(chat_row):
        """The one «Отправить» of a chat: the text, the photo or both. The photo goes first; a text never follows a photo that did not surely go."""
        conn = get_conn()
        chat = _chat_or_404(conn, chat_row)
        if request.form.get("again"):
            return send_again(conn, chat, str(request.form.get("again")))
        text = str(request.form.get("text") or "")
        upload = request.files.get("photo")
        data = upload.read(chats.MAX_IMAGE_BYTES + 1) if upload else b""
        plain = chats.CONTROL.sub("", text.replace("\r", "")).strip()           # what send_reply would send: a blank text is no message

        def keep_text():
            session["draft_%d" % chat_row] = text[:2000]                       # the typed text is not lost when it was not sent
        if upload and upload.filename and not data:                           # a chosen photo that came empty: the text must not go without it
            flash("Файл фото пустой: ничего не отправлено. Выберите фото ещё раз.")
            if plain:
                keep_text()
            return back(chat_row)
        if not data and not plain:
            flash("Ничего не отправлено: напишите ответ или выберите фото.")
            return back(chat_row)
        if len(plain) > chats.MAX_REPLY:
            flash("Ничего не отправлено: текст ответа длиннее %d знаков." % chats.MAX_REPLY)
            keep_text()
            return back(chat_row)
        try:
            writer = _writer(conn, chat)
            if data:
                chats.send_image(conn, writer, chat_row, data, request.form.get("image_token"), local_iso(), author())
        except chats.ChatError as err:                                     # ChatSendUnknown too: its text says not to send the photo again
            if plain:
                lead = "" if isinstance(err, chats.ChatSendUnknown) else "Фото не отправлено: "    # an unsure photo may have gone: never «не отправлено»
                flash("%s%s Текст не отправлен, он остался в поле.%s" % (lead, err, (                 # L2: the text would stop the photo's retry
                    " Если отправить текст сейчас, повтор фото отменится — тогда фото отправьте заново." if isinstance(err, chats.ChatSendRetry)
                    else "")) if data else str(err))
                keep_text()
            else:
                flash(str(err))
            return back(chat_row)
        if not plain:
            flash("Фото отправлено клиенту.")
            session.pop("draft_%d" % chat_row, None)                       # the field was sent empty: an old draft must not come back
            _took(conn, chat_row)
            return back(chat_row)
        try:
            chats.send_reply(conn, writer, chat_row, text, request.form.get("reply_token"), local_iso(), author())
            flash("Фото и ответ отправлены клиенту." if data else "Ответ отправлен клиенту.")
            session.pop("draft_%d" % chat_row, None)
            _took(conn, chat_row)
        except chats.ChatSendUnknown as err:
            flash(("Фото отправлено клиенту. %s" % err) if data else str(err))
            session.pop("draft_%d" % chat_row, None)                       # it may have gone: the text must not come back for a second press
        except chats.ChatError as err:
            flash(("Фото отправлено клиенту, а текст не ушёл: %s Текст остался в поле." % err) if data else str(err))
            keep_text()
        return back(chat_row)

    @app.route("/orders/<int:card_id>/task/<int:task_id>/send", methods=["POST"])
    @login_required
    def order_task_send(card_id, task_id):
        """«Отправить» of the «Отправить клиенту?» window: the message task goes to the client's Avito chat of the order, once per task."""
        conn = get_conn()
        try:
            task = engine.get_task(conn, task_id)
        except orders.OrderError:
            abort(404)
        if task["order_id"] != card_id:
            abort(404)
        text = chats.CONTROL.sub("", str(request.form.get("text") or "").replace("\r", "")).strip()
        ok, sent_to = False, None

        def deliver(chat_row, body, token):
            chats.send_reply(conn, _writer(conn, chats.get_chat(conn, chat_row)), chat_row, body, token, local_iso(), author())
        try:
            if not text or len(text) > chats.MAX_REPLY:
                raise chats.ChatError("Ничего не отправлено: текст сообщения от 1 до %d знаков." % chats.MAX_REPLY)
            sent_to = engine.send_task(conn, task_id, text, deliver, local_iso(), author(), _int(request.form.get("chat")))
            message, ok = "Сообщение отправлено клиенту в чат %s." % deals.ref(sent_to), True
        except (orders.OrderError, chats.ChatError) as err:
            message = str(err)
        except Exception:
            conn.rollback()
            log.exception("the message task %s could not be sent", task_id)
            message = "Сбой панели при отправке. Проверьте чат клиента, прежде чем отправлять ещё раз."
        if ok:
            flash(message)
        back = safe_path(str(request.form.get("back") or ""))
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify(ok=ok, message=message)
        if not ok:
            flash(message, "error")
        return redirect(back or "/orders/%d" % card_id)

    @app.route("/chats/<int:chat_row>/send-image", methods=["POST"])
    @login_required
    def chat_send_image(chat_row):
        conn = get_conn()
        chat = _chat_or_404(conn, chat_row)
        upload = request.files.get("photo")
        data = upload.read(chats.MAX_IMAGE_BYTES + 1) if upload else b""
        try:
            if not data:
                raise chats.ChatError("Выберите фото.")
            chats.send_image(conn, _writer(conn, chat), chat_row, data, request.form.get("image_token"), local_iso(), author())
            flash("Фото отправлено клиенту.")
        except chats.ChatError as err:                                     # ChatSendUnknown too: its text says not to send it again
            flash(str(err))
        return back(chat_row)

    @app.route("/chats/<int:chat_row>/delete", methods=["POST"])
    @login_required
    def chat_delete(chat_row):
        conn = get_conn()
        chat = _chat_or_404(conn, chat_row)
        try:
            trash.delete_chat(conn, chat_row, local_iso(), author())
        except orders.OrderError as err:
            flash(str(err))
            return back(chat_row)
        flash("Чат %s удалён. Из Авито он больше не загрузится; вернуть его можно в «Удалённых»." % deals.ref(chat["id"]))
        return redirect("/chats%s" % _keep_query())

    @app.route("/chats/<int:chat_row>/link", methods=["POST"])
    @login_required
    def chat_link(chat_row):
        conn = get_conn()
        _chat_or_404(conn, chat_row)
        order_id = _int(request.form.get("order_id"))
        try:
            if order_id is None:
                raise orders.OrderError("Укажите номер заказа.")
            if own_point() is not None and orders.get_card(conn, order_id)["point_id"] != own_point():
                raise orders.OrderError("Заказ №%d не вашей точки: привязать к нему чат нельзя." % order_id)
            deals.link_chat(conn, chat_row, order_id, local_iso(), author())
            flash("Чат привязан к заказу №%d." % order_id, "ok")
        except orders.OrderError as err:
            flash(str(err))
        return back(chat_row)

    @app.route("/chats/<int:chat_row>/unlink", methods=["POST"])
    @login_required
    def chat_unlink(chat_row):
        conn = get_conn()
        _chat_or_404(conn, chat_row)
        try:
            deals.unlink_chat(conn, chat_row, local_iso(), author())
            flash("Чат отвязан. Автоматически он больше не привязывается.")
        except orders.OrderError as err:
            flash(str(err))
        return back(chat_row)

    @app.route("/chats/<int:chat_row>/create-deal", methods=["POST"])
    @login_required
    def chat_create_deal(chat_row):
        conn = get_conn()
        _chat_or_404(conn, chat_row)
        try:
            card_id = deals.create_deal(conn, chat_row, local_iso(), author(), position_provider=_position_provider(conn, chat_row), own_point=own_point())
            _to_own_point(conn, card_id)
        except orders.OrderError as err:
            flash(str(err))
        return back(chat_row)

    @app.route("/chats/<int:chat_row>/handoff", methods=["POST"])
    @login_required
    def chat_handoff(chat_row):
        """«Передать под заказ»: the manager of a point hands the client of a chat of the point's account to the person on duty
        (order_manager.hand_over). Only a point's manager; the window comes back open with what was typed when something is wrong."""
        conn = get_conn()
        chat = _chat_or_404(conn, chat_row)
        point = own_point()
        if point is None or chat["account_id"] != point_account():
            abort(403)
        form = {k: str(request.form.get(k) or "")[:600] for k in ("phone", "size", "season", "brand", "comment")}
        try:
            from .routes_vk import channel
            _send, _deliver, notify = channel(app, conn, local_iso())
        except Exception:                                                  # without the alert channel the client still goes; the order shows it
            logging.getLogger(__name__).exception("the alert channel for a handoff failed")
            notify = None
        try:
            _card_id, answer = mgr.hand_over(conn, point, {"kind": "chat", "chat_row": chat_row}, form["phone"], mgr.handoff_fields(form),
                                             request.form.get("token"), author(), local_iso(), notify)
            from .. import point_log
            point_log.record(conn, local_iso(), author(), point, "handoff", _card_id, "из чата")              # «История точки»
            flash(answer, "ok")
        except orders.OrderError as err:
            flash(str(err))
            session["handoff_%d" % chat_row] = form                        # the window opens again with what was typed
        return back(chat_row)

    @app.route("/chats/<int:chat_row>/wait", methods=["POST"])
    @login_required
    def chat_wait(chat_row):
        conn = get_conn()
        _chat_or_404(conn, chat_row)
        fields = {k: request.form.get(k, "") for k in ("brand", "model", "width", "profile", "radius", "season", "studded", "qty", "any_brand", "any_size")}
        try:
            card_id, added, moved = deals.wait_from_chat(conn, chat_row, fields, _catalog_offers(conn), local_iso(), author(), own_point=own_point())
            _to_own_point(conn, card_id)
            flash("%s Заказ №%d%s." % ("Шина добавлена в лист ожидания." if added else "Такая строка уже есть в заказе, второй не добавлено.", card_id,
                                       ", этап «Ожидает наличие»" if moved else " уже в этапе «Ожидает наличие»"))
        except orders.OrderError as err:
            flash(str(err))
        return back(chat_row)

    @app.route("/chats/<int:chat_row>/stage", methods=["POST"])
    @login_required
    def chat_stage(chat_row):
        conn = get_conn()
        chat = _chat_or_404(conn, chat_row)
        stage_id = _int(request.form.get("stage_id"))
        try:
            if not chat["order_id"] or stage_id is None:
                raise orders.OrderError("У чата нет заказа или этап выбран неверно.")
            if own_point() is not None and orders.get_card(conn, chat["order_id"])["point_id"] != own_point():
                raise orders.OrderError("Этап заказа №%d меняет владелец: заказ не вашей точки." % chat["order_id"])
            from .routes_orders import ask_oth, oth_offer
            asked = oth_offer(conn, orders.get_card(conn, chat["order_id"]), stage_id)
            if asked is not None:                                          # «Шины остаются на точке — на ОТХ?»: the order's card asks (2026-10-29)
                return ask_oth(chat["order_id"], asked)
            stamp = local_iso()
            moved = orders.move_card(conn, chat["order_id"], stage_id, stamp, author())
            if moved:
                offer_send(conn, chat["order_id"], stamp)
                if own_point() is not None:                                # «История точки»
                    from .. import point_log
                    point_log.record(conn, stamp, author(), own_point(), "move", chat["order_id"], "этап «%s» (из чата)" % orders.get_stage(conn, stage_id)["name"])
            flash("Заказ №%d перенесён в «%s»." % (chat["order_id"], orders.get_stage(conn, stage_id)["name"]) if moved
                  else "Заказ №%d уже на этом этапе." % chat["order_id"])
        except orders.OrderError as err:
            hint = " Заполните в заказе №%d (кнопка «Открыть» справа) и переведите снова." % chat["order_id"] if chat["order_id"] and "заполните" in str(err) else ""
            flash(str(err) + hint, "error")
        return back(chat_row)

    @app.route("/chats/merge", methods=["POST"])
    @login_required
    def chats_merge():
        conn = get_conn()
        source, target = _int(request.form.get("from_order")), _int(request.form.get("into_order"))
        try:
            if source is None or target is None:
                raise orders.OrderError("Укажите оба заказа.")
            moved = deals.merge_chats(conn, source, target, local_iso(), author())
            flash("Перенесено чатов: %d. Заказ №%d закройте сами." % (moved, source), "ok")
        except orders.OrderError as err:
            flash(str(err))
        return redirect("/orders/%d" % (target or source or 0)) if (target or source) else redirect("/orders")
