"""«Рабочее место» (its old name is gone, the address /my-point stays): the start page of a point's manager (the owner 2026-10-23: the points work in the panel, VK is the spare
channel; 2026-10-29: in the panel's own frame with the menu, the stages of the point's orders, one search line, «Отменить передачу» of a
client not taken yet, the return of «На ОТХ» with the act МХ-3; every action goes to «История точки», point_log).

The orders of the point that lie there («На точке») and are on their way («Едут к нам», with what «Менеджеру: заказ оформлен» says), with «Приехал», «Выдал», «Клиент не пришёл»: the same functions as the VK
commands (order_manager.apply, no_show, tell_client_arrived) with the same checks of the stage, so the result is the same; the client hears
«можно забирать» on the arrival only, once per arrival. The point is taken in the query (the manager's own; the owner and the partner pick
one), never in the template. «Передать клиента» (a POST to the page itself) is order_manager.handoff, the function of the VK command
«клиент <телефон> <что нужно>»: an order on «Передано с точки» for the person on duty; nothing goes to the client; the point needs no VK for
it. «Оповещения» (a line under the search, the list on ?notes=1) shows the alerts the bot did not write to a point in the panel
(order_points.notes); the bot's answers to the clients are not shown to the point (the owner 2026-10-30). /my-point/feed is what notify.js asks every
half a minute for the sound and the browser's notification: the orders of the point, its notes and the rows of its bell (one source with it).
The owner 2026-10-26: counters at the top; one list, newest first by the order's last event that matters to the point (_cards); the card's text is
the VK message «Менеджеру: заказ оформлен» (its template, with the client's prices: the point takes the cash; never the purchase); «Возврат» on an order at the point is
the VK «возврат отдал» (order_manager.returned); the question of the last day of the reservation (order_reserve, asked in VK) is answered
here too: «Выдан» is «выдал», «На точке» is «на точке» (order_reserve.here), one answer whichever comes first."""
from __future__ import annotations

import re
import secrets
from datetime import date, timedelta

from flask import abort, current_app, flash, jsonify, redirect, render_template, request

from .. import order_manager as mgr, order_oth, oth_sale, order_placed, point_log, search, order_points as points, order_report, order_reserve as reserve, order_templates as templates, orders, users
from .common import author, current_user, get_conn, login_required, own_point
from .routes_orders import local_iso, local_now

STAGES = ("placed", "moving", "at_point")          # placed (the panel may not say «on the way» yet), on the way to the point, at the point
ACTIONS = {"arrived": mgr.ARRIVED, "issued": "выдал", "no_show": mgr.NO_SHOW, "returned": mgr.RETURN,
           "reserve_issued": "выдал", "reserve_here": mgr.HERE,         # the two answers to «Клиент забрал?» (the question of the reservation)
           "act_signed": "акт МХ-3 подписан",                           # «Подписал акт и отдал»: the return of «На ОТХ» (order_oth.sign_act)
           "to_oth": "на ОТХ"}                                          # after «Клиент не пришёл»: the tyres stay at the point (the owner 2026-10-29)
NO_SHOW_REASON = "Клиент не пришёл"
BUILTIN = {"arrived": (mgr.ARRIVE_FROM, "at_point"), "issued": (mgr.ISSUE_FROM, "done")}


def _point_id(conn):
    """The manager's own point; for the owner and the partner the one they picked (?p=), else the first one. None: there is no point."""
    mine = own_point()
    if mine is not None:
        return mine
    wanted = request.values.get("p", "")
    known = [p["id"] for p in points.list_points(conn)]
    if wanted.isascii() and wanted.isdigit() and int(wanted) in known:
        return int(wanted)
    return known[0] if known else None


RAISE = ("(e.kind = 'chat' AND e.text LIKE 'Клиент написал%') OR "      # the client's message in the order's chat (chats.py)
         "(e.kind = 'reserve' AND e.text LIKE 'Точке «%' AND e.text LIKE '%[резерв:точке:%')")   # the reserve question (order_reserve.ask_point)


def _cards(conn, point_id) -> list:
    """The orders of the point on «placed», «moving» or «at_point», newest first by the last event that matters to the point (the owner
    2026-10-26): the arrival at the point (the move to «at_point» of this stay), the client's message in the order's chat, the reserve
    question to the point; the other records of the journal leave an order where it is (else it stands by its creation). The filter is here,
    in the query."""
    ids = [r[0] for r in conn.execute(
        "SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE c.point_id = ? AND s.code IN (%s) "
        "ORDER BY MAX(c.created_at, CASE WHEN s.code = 'at_point' THEN COALESCE(c.entered_at, '') ELSE '' END, "
        "COALESCE((SELECT MAX(e.at) FROM order_event e WHERE e.order_id = c.id AND (%s)), '')) DESC, c.id DESC"
        % (",".join("?" * len(STAGES)), RAISE), (point_id,) + STAGES)]
    return [orders.get_card(conn, card_id) for card_id in ids]


def _own_card(conn, card_id, point_id) -> dict:
    """The order when it is of this point; otherwise 404 (a card of another point is not even named)."""
    if point_id is None or not conn.execute("SELECT 1 FROM order_card WHERE id = ? AND point_id = ?", (card_id, point_id)).fetchone():
        abort(404)
    return orders.get_card(conn, card_id)


def _short_date(value: str) -> str:
    try:
        return date.fromisoformat(value).strftime("%d.%m") if value else ""
    except ValueError:
        return value


def tel(phone) -> str:
    """The phone as a tel: address (digits and a leading plus only)."""
    digits = re.sub(r"\D", "", str(phone or ""))
    return ("+" if str(phone or "").strip().startswith("+") else "") + digits if digits else ""


def vk_text(conn, card: dict, now: str) -> str:
    """What the VK message «Менеджеру: заказ оформлен» says of the order: the owner's template of that name (as order_placed sends it), else
    the sample; a value the order lacks is left out, as in the message."""
    row = conn.execute("SELECT body FROM order_template WHERE name = ? AND audience = 'manager' ORDER BY id LIMIT 1",
                       (order_placed.POINT_TEMPLATE,)).fetchone()
    body = row[0] if row else next(b for name, _a, b in templates.SAMPLES if name == order_placed.POINT_TEMPLATE)
    return templates.render(conn, card, body, now)[0].strip()


def _not_here(card: dict, what: str):
    return ("не подходит: заказ на этапе «%s»" % card["stage"]["name"],
            "Заказ №%s сейчас на этапе «%s»: «%s» отмечают, когда заказ на точке." % (mgr.label(card), card["stage"]["name"], what), False)


def act(conn, card: dict, action: str, who: str, now: str, deliver, notify):
    """What the VK command of the same word does for an order of the point (order_manager._command for the built-in words): returns
    (outcome for the log, answer for the manager, done). A word that does not fit the stage changes nothing."""
    code = card["stage"]["code"]
    if action == "act_signed":                                                  # the return of «На ОТХ» agreed with the supplier
        plan = order_oth.return_plan(conn, card["id"])
        if plan is not None and plan["signed_at"]:
            return "уже отмечено", "Заказ №%s уже отмечен: акт МХ-3 подписан, шины отданы." % mgr.label(card), True
        if code != "oth" or plan is None:
            return ("не подходит: возврат не согласован или заказ на этапе «%s»" % card["stage"]["name"],
                    "Заказ №%s: возврат с ОТХ не согласован с поставщиком, отмечать нечего." % mgr.label(card), False)
        try:
            done = order_oth.sign_act(conn, card, (card.get("point") or {}).get("name") or "", now, who)
        except orders.OrderError as err:
            return "не выполнено: %s" % err, "Не получилось: %s" % err, False
        if not done:
            return "не выполнено: заказ уже изменили", "Заказ №%s уже изменили: обновите страницу." % mgr.label(card), False
        return ("заказ перенесён на «Возврат передан»", "Заказ №%s: акт МХ-3 подписан, шины отданы. Менеджер заполнит сумму возврата." % mgr.label(card),
                True)
    if action == "to_oth":                                                      # «Шины остаются на точке — на ОТХ?» after «Клиент не пришёл»
        if code != "at_point" or not no_show_now(conn, card):
            return _not_here(card, "на ОТХ") if code != "at_point" else (
                "не подходит: клиент не отмечен «не пришёл»", "Заказ №%s: сначала отметьте «Клиент не пришёл»." % mgr.label(card), False)
        oth = next(s for s in orders.list_stages(conn) if s["code"] == "oth")
        try:
            moved = orders.move_card(conn, card["id"], oth["id"], now, who, changes={} if card["cancel_reason"] else {"cancel_reason": NO_SHOW_REASON})
        except orders.OrderError as err:
            return "не выполнено: %s" % err, "Не получилось: %s" % err, False
        if not moved:
            return "не перенесён: заказ уже на другом этапе", "Заказ №%s уже переместили, проверьте его в панели." % mgr.label(card), False
        return "заказ перенесён на «%s»" % oth["name"], "Заказ №%s на «%s»: шины остаются на точке нашим товаром." % (mgr.label(card), oth["name"]), True
    if action == "reserve_issued":                                              # «Выдан» to the question is «выдал»
        action = "issued"
    if action == "returned":                                                    # the VK «возврат отдал»; the button is on the orders at the point only
        if code == "return":
            return "уже отмечено", "Заказ №%s уже отмечен: %s." % (mgr.label(card), mgr.RETURNED), True
        if code != "at_point":
            return _not_here(card, "возврат")
        outcome, answer = mgr.returned(conn, card, who, now)
        return outcome, answer, not outcome.startswith("не ")
    if action == "reserve_here":                                                # the VK «на точке»: the client hears the reminder only when asked
        if code != "at_point":
            return _not_here(card, "на точке")
        outcome, answer = reserve.here(conn, card, who, now, deliver)
        return outcome, answer, not outcome.startswith("«")
    if action == "no_show":
        if code != "at_point":
            return ("не подходит: заказ на этапе «%s»" % card["stage"]["name"],
                    "Заказ №%s сейчас на этапе «%s»: «клиент не пришёл» отмечают, когда заказ на точке." % (mgr.label(card), card["stage"]["name"]), False)
        outcome, answer = mgr.no_show(conn, card, who, now, notify)
        return outcome, answer, True
    sources, target = BUILTIN[action]
    if code == target:                                                          # marked already: the client still hears «приехал» once
        sent, note = mgr.tell_client_arrived(conn, card["id"], deliver, now, who) if target == "at_point" else (False, "")
        return ("уже отмечено" + ("; клиенту %s" % ("отправлено" if sent else "не отправлено") if note else ""),
                ("Заказ №%s уже отмечен: %s. %s" % (mgr.label(card), mgr.ARRIVED if target == "at_point" else mgr.ISSUED, note)).strip(), True)
    if code not in sources:
        return ("не подходит: заказ на этапе «%s»" % card["stage"]["name"],
                "Заказ №%s на этапе «%s»: эта отметка ему не подходит." % (mgr.label(card), card["stage"]["name"]), False)
    stage_id = next(s["id"] for s in orders.list_stages(conn) if s["code"] == target)
    outcome, answer = mgr.apply(conn, card, stage_id, who, now, deliver)
    return outcome, answer, not outcome.startswith("не ")


def no_show_now(conn, card: dict) -> bool:
    """«Клиент не пришёл» was marked in this stay at the point (mgr.no_show's red record after the arrival)."""
    return conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND kind = 'point' AND text LIKE 'Клиент не пришёл за заказом%' AND at >= ? LIMIT 1",
                        (card["id"], card["entered_at"] or "")).fetchone() is not None


NEED_MAX = 500
HANDED_DAYS = 14                                   # «Переданные клиенты»: what became of the clients of the last two weeks


def _who(conn, login: str) -> str:
    """The name of a panel user for the manager of a point: the display name, else the login."""
    return (users.get_role(conn, login)["display_name"] if login else "") or login or "не назначен"


def _dup_view(conn, busy: dict, point_id: int) -> dict:
    """What the refusal says of the open order: its number and date only (nothing of another point's order). `href`: the order's own
    card for the owner and the partner; for a manager the card on «Рабочее место», on its tab (the form is on «Переданные клиенты»,
    a page of its own: 2026-10-30), when it lies there, else no link."""
    card = orders.get_card(conn, busy["id"])
    if own_point() is None:
        href = "/orders/%d" % card["id"]
    elif busy["point_id"] == point_id and card["stage"]["code"] in STAGES:
        href = "/my-point?tab=%s#order-%d" % ("here" if card["stage"]["code"] == "at_point" else "moving", card["id"])
    else:
        href = ""
    return {"label": mgr.label(card), "date": _short_date(str(busy["created_at"] or "")[:10]), "href": href}


def _hand_over(conn, point: dict, back: str):
    """«Передать клиента»: the VK command's own function with the point's VK as its sender and a press token of its own as the message.
    Whatever is wrong makes nothing: the page comes back with the error and the text kept. Returns a response or (error, form) to show."""
    form = {"phone": str(request.form.get("phone") or "")[:40], "need": str(request.form.get("need") or "")[:2000],
            "size": str(request.form.get("size") or "")[:40], "season": str(request.form.get("season") or "")[:20], "brand": str(request.form.get("brand") or "")[:60]}
    token = str(request.form.get("token") or "").strip()[:64]
    if not re.fullmatch(r"[0-9a-f]{24}", token):
        return "Форма устарела: обновите страницу.", form
    if own_point() is None and str(request.form.get("p") or "") != str(point["id"]):      # the owner's point must be the one on the form
        return "Точка не выбрана или её уже нет: обновите страницу и выберите точку.", form
    parsed = mgr.parse_handoff("клиент " + " ".join(form["phone"].split()))       # the VK command's own rule for a whole phone
    phone = parsed["phone"] if parsed and not parsed["need"] else ""
    if not phone:
        return "Телефон клиента: 10 цифр, например +7 900 111-22-33.", form
    need = re.sub(r"\s+", " ", form["need"]).strip(" ,.:;-")                      # cleaned as parse_handoff cleans it
    if len(need) > NEED_MAX:
        return "Что нужно: не больше %d знаков, сократите текст." % NEED_MAX, form
    message_id = "page-" + token
    earlier = conn.execute("SELECT outcome, order_id FROM order_command_log WHERE message_id = ?", (message_id,)).fetchone()
    if earlier is not None:                                                       # the same press twice (a double tap, «Назад» and again)
        if earlier[0] == "передан клиент":
            flash("Этот клиент уже передан, заказ №%s." % earlier[1])
            return redirect(back)
        return "Прошлая попытка не удалась: обновите страницу и передайте клиента снова.", form
    busy = orders.open_order_conflict(conn, phone, point["id"])                                 # the owner 2026-10-27: a second open order on one phone is forbidden
    if busy is not None:
        return "", dict(form, dup=_dup_view(conn, busy, point["id"]))
    now = local_iso()
    try:
        from . import routes_vk
        _send, _deliver, notify = routes_vk.channel(current_app, conn, now)
    except Exception:                                                            # without the alert channel the handoff still goes; the order shows it
        current_app.logger.exception("«Рабочее место»: the alert channel for a handoff failed")
        notify = None
    try:
        answer = mgr.handoff(conn, message_id, point["vk_peer_id"] or "панель: %s" % author(), dict(mgr.handoff_fields(form), phone=phone, comment=need),
                             "", now, notify, source="page", point=point)
    except Exception:                                                            # after the order was written (the alert): what counts is the order
        conn.rollback()
        current_app.logger.exception("«Рабочее место»: the handoff of %s failed", message_id)
        answer = ""
    made = conn.execute("SELECT order_id, responsible FROM order_handoff WHERE message_id = ?", (message_id,)).fetchone()
    if made is not None:
        try:
            orders.add_event(conn, made[0], now, author(), "note", "Клиент передан через «Рабочее место» (%s)." % author())
            conn.commit()
        except Exception:
            conn.rollback()
            current_app.logger.exception("«Рабочее место»: the note of order %s failed", made[0])
        point_log.record(conn, now, author(), point["id"], "handoff", made[0], "со страницы")
        flash("Передано, ответственный — %s. Заказ №%d." % (_who(conn, made[1]), made[0]), "ok")
        return redirect(back)
    if answer == "Это сообщение уже обработано.":
        flash("Этот клиент уже передан.")
        return redirect(back)
    busy = orders.open_order_conflict(conn, phone, point["id"])                                 # another tab made the client's order between the check and now
    if busy is not None:
        return "", dict(form, dup=_dup_view(conn, busy, point["id"]))
    return (answer or "Не получилось принять клиента. Напишите владельцу."), form


# «Рабочее место» (the owner 2026-10-29): the tabs of the point's orders, each with its count; an order may be in two of them.
# the tabs of «Рабочее место» (the owner 2026-10-30, the new design: a list of the point's work, no «Все», «Резерв истекает» nor «Ждут ответа»;
# the question «Клиент забрал?» is a line of its card on «На точке», first); an old address (all, ending, asked, …) opens DEFAULT_TAB
TABS = {"moving": "Едут", "here": "На точке", "oth": "На ОТХ", "return": "На возврат"}     # «Переданные клиенты»: a section of its own
DEFAULT_TAB = "here"
ALERTS_READ = 500                                  # the notes read to find the point's newest alerts among the bot's answers (two weeks kept)


def _in_tab(row: dict, tab: str) -> bool:
    """«Едут»: «Заказ оформлен» and «Перемещение на точку» (the owner 2026-10-29); «На точке»: «Товар на точке», waiting for the client; «На ОТХ»:
    our tyres at the point (the owner 2026-10-30); «На возврат»: an «На ОТХ» whose return is agreed and the act МХ-3 not signed yet."""
    code = row["card"]["stage"]["code"]
    return {"moving": code in ("placed", "moving"), "here": row["here"], "oth": row["oth"] and not row.get("returning"),
            "return": bool(row.get("returning"))}[tab]


def _tab_of(row: dict) -> str:
    return next((t for t in TABS if _in_tab(row, t)), "")


def _order_day(conn, card: dict) -> str:
    """«от ДД.ММ.ГГГГ» of the card: the day the order last entered «Заказ оформлен», else the day it was made (never «today» by default)."""
    placed = next((s["name"] for s in orders.list_stages(conn) if s["code"] == "placed"), None)
    row = conn.execute("SELECT at FROM order_event WHERE order_id = ? AND kind = 'stage' AND text LIKE ? ORDER BY id DESC LIMIT 1",
                       (card["id"], "%→ «" + (placed or "") + "»")).fetchone() if placed else None
    day = str(row[0] if row else card["created_at"] or "")[:10]
    try:
        return date.fromisoformat(day).strftime("%d.%m.%Y")
    except ValueError:
        return ""


def _back(point_id) -> str:
    """Back to «Рабочее место» after a press: the point chosen (the owner, the partner) and the tab the press came from (its ?tab=)."""
    from urllib.parse import parse_qs, urlsplit
    came = urlsplit(request.referrer or "")
    tab = (parse_qs(came.query).get("tab") or [""])[0] if came.path == "/my-point" and (not came.netloc or came.netloc == request.host) else ""
    args = (["p=%d" % point_id] if own_point() is None else []) + (["tab=%s" % tab] if tab in TABS and tab != DEFAULT_TAB else [])
    return "/my-point" + ("?" + "&".join(args) if args else "")


def _oth_cards(conn, point_id) -> list:
    """The point's orders on «На ОТХ»: the tyres the client refused lie at the point (tab «На возврат»)."""
    return [orders.get_card(conn, r[0]) for r in conn.execute(
        "SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE c.point_id = ? AND s.code = 'oth' ORDER BY c.entered_at, c.id",
        (point_id,))]


def _stages(conn, point_id) -> list:
    """«Этапы заказов»: every stage of the owner's pipeline, in its order and colour, with the number of this point's orders on it."""
    data = orders.board(conn, local_now(), point_id=point_id, with_sums=False)
    return [{"stage": col["stage"], "count": col["count"]} for col in data["columns"]]


FOUND_MAX = 20


def _found(conn, query: str, point_id: int) -> list:
    """The search line: the orders of this point only, by the number of the order, of the transfer, the phone or the client's name (in the
    order, in its chat of the point's account) — the top bar's own search, search._orders, narrowed to the point."""
    row = conn.execute("SELECT avito_account_id FROM point WHERE id = ?", (point_id,)).fetchone()
    scope = search.Scope(frozenset({"orders"}), point=point_id, account=row[0] if row else None)
    groups = search.run(conn, query, scope, local_now(), limit=FOUND_MAX)["groups"]
    return groups[0]["items"] if groups else []


def _oth_ask(conn, cards: list) -> dict:
    """?oth=<order>: the order just marked «Клиент не пришёл», still at the point: its card asks «Шины остаются на точке — на ОТХ?»."""
    wanted = str(request.args.get("oth") or "")
    row = next((r for r in cards if wanted.isdigit() and r["card"]["id"] == int(wanted) and r["here"]), None)
    return {row["card"]["id"]: True} if row is not None and no_show_now(conn, row["card"]) else {}


def _returns(conn, point_id) -> list:
    """«Возврат поставщику»: the orders of the point on «На ОТХ» agreed for the return, with a press token each."""
    today = local_iso()[:10]
    return [{"card": r["card"], "label": mgr.label(r["card"]), "date": _short_date(r["plan"]["return_date"]),
             "late": r["plan"]["return_date"] < today, "token": secrets.token_hex(12), "act_order": order_oth.act_orders(conn, r["card"]["id"])}
            for r in order_oth.to_return(conn, point_id)]


def register(app) -> None:
    def _the_point(conn):
        point_id = _point_id(conn)
        try:
            return point_id, (points.get_point(conn, point_id) if point_id is not None else None)
        except points.PointError:                                               # the manager's point was deleted: nothing to show
            abort(403)

    @app.route("/my-point/handed", methods=["GET", "POST"])
    @login_required
    def mypoint_handed():
        """«Переданные клиенты» (the owner 2026-10-30: a section of the point's menu, not a tab): the clients handed over, «Отменить передачу»,
        then the form «Передать клиента» (the VK command's own function, _hand_over); a refused form comes back here with what was typed."""
        conn = get_conn()
        point_id, point = _the_point(conn)
        error, form = "", {}
        if request.method == "POST":
            if point is None:
                abort(404)
            done = _hand_over(conn, point, "/my-point/handed" + ("?p=%d" % point_id if own_point() is None else ""))
            if not isinstance(done, tuple):
                return done
            error, form = done
        since = (date.fromisoformat(local_iso()[:10]) - timedelta(days=HANDED_DAYS)).isoformat()
        return render_template("mypoint_handed.html", point=point, choose=own_point() is None, user=current_user(),
                               points=points.list_points(conn) if own_point() is None else [],
                               handed=mgr.point_handoffs(conn, point_id, since) if point_id is not None else [], seasons=mgr.SEASONS,
                               bonus=order_report.rate_on(order_report.list_rates(conn), "handoff_fee", local_iso()[:10]),
                               handoff_token=secrets.token_hex(12), handoff_error=error, handoff_form=form), 400 if error else 200

    @app.route("/my-point", methods=["GET", "POST"])
    @login_required
    def mypoint_page():
        if request.method == "POST":                                            # the form of an old page: «Переданные клиенты» takes it
            return mypoint_handed()
        conn = get_conn()
        point_id, point = _the_point(conn)
        if request.args.get("tab") == "handed":                                 # an old address of the tab: its section
            return redirect("/my-point/handed" + ("?p=%d" % point_id if own_point() is None and point_id is not None else ""))
        now = local_iso()
        today = now[:10]
        cards = []
        tomorrow = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
        for card in ((_cards(conn, point_id) + _oth_cards(conn, point_id)) if point_id is not None else []):
            until = (card["reserve_until"] or "") if card["stage"]["code"] == "at_point" else ""     # «На ОТХ» has no term (2026-10-30)
            delivery = card["delivery_date"] or ""
            code = card["stage"]["code"]
            cards.append({"card": card, "label": mgr.label(card), "tel": tel(card["client_phone"]), "here": code == "at_point", "oth": code == "oth",
                          "tokens": {action: secrets.token_hex(12) for action in ACTIONS},          # every button is its own press
                          "until": date.fromisoformat(until).strftime("%d.%m") if until else "", "late": bool(until) and until < today,
                          "ends": code == "at_point" and bool(until) and until <= tomorrow, "delivery": _short_date(delivery) if delivery else (oth_sale.IN_STOCK if card["in_stock"] else ""),
                          "text": orders.without_account_names(conn, vk_text(conn, card, now)), "asked": reserve.question_open(conn, card, now),
                          "order_date": _order_day(conn, card), "from_oth": templates._oth_orders(conn, card["id"])})     # «Шины с ОТХ, заказ №…»
        labels = orders.origin_labels(conn, point_id, [row["card"]["id"] for row in cards]) if own_point() is not None else {}
        for row in cards:
            row["origin"] = labels.get(row["card"]["id"])                # not from the point's own Avito account: only receive and hand over
        # «Оповещения: N новых» (the owner 2026-10-30): the alerts of the point only (the bot's answers to the clients are not the point's to read),
        # folded into a line under the search; ?notes=1 opens the list, and only that opening marks them seen (the safety net of order_notify:
        # an alert nobody of the point opened goes to the owner) — never the mere look at the page. Back on «ВК»: none shown.
        alerts = [n for n in points.notes(conn, point_id, limit=ALERTS_READ) if n["kind"] == "alert"][:points.NOTES_SHOWN] if points.in_panel(point) else []
        seen = points.seen_at(conn, point_id) if point_id is not None else ""
        alerts_new = sum(1 for n in alerts if str(n["at"]) > seen)
        alerts_open = request.args.get("notes") == "1" and bool(alerts)
        if alerts_open and own_point() is not None:                             # a person of the point opened the list: the alerts shown are seen
            try:
                points.mark_seen(conn, point_id, max(n["at"] for n in alerts))
            except Exception:                                                    # noqa: BLE001  (a busy base: the page still opens)
                conn.rollback()
        returns = _returns(conn, point_id) if point_id is not None else []
        returning = {r["card"]["id"] for r in returns}
        for row in cards:
            row["returning"] = row["card"]["id"] in returning
        tab = request.args.get("tab", "")
        tab = tab if tab in TABS else DEFAULT_TAB
        shown = sorted((r for r in cards if _in_tab(r, tab)), key=lambda r: not r["asked"])     # «Клиент забрал?» first (sorted keeps the order)
        counts = {key: sum(1 for r in cards if _in_tab(r, key)) for key in TABS}
        hot = {"here": any(r["asked"] for r in cards), "return": bool(counts["return"])}           # the tabs that wait for the point
        query = str(request.args.get("q") or "").strip()[:search.QUERY_MAX]
        found = _found(conn, query, point_id) if query and point_id is not None else None
        if found:                                                                # the search looks at every order of the point: say its tab
            tab_of = {r["card"]["id"]: _tab_of(r) for r in cards}
            for f in found:
                oid = int(f["url"].rsplit("/", 1)[-1]) if f.get("url", "").rsplit("/", 1)[-1].isdigit() else None
                f["tab"] = tab_of.get(oid, "")
        return render_template("mypoint.html", point=point, cards=shown, counts=counts, tab=tab, tabs=TABS, hot=hot,
                               alerts=alerts if alerts_open else [], alerts_new=alerts_new, alerts_open=alerts_open, choose=own_point() is None,
                               points=points.list_points(conn) if own_point() is None else [], user=current_user(),
                               stages=_stages(conn, point_id) if point_id is not None else [], query=query,
                               found=found,
                               returns=returns,
                               oth_ask=_oth_ask(conn, cards))

    @app.route("/my-point/<int:card_id>/<action>", methods=["POST"])
    @login_required
    def mypoint_action(card_id, action):
        if action not in ACTIONS:
            abort(404)
        conn = get_conn()
        point_id = _point_id(conn)
        card = _own_card(conn, card_id, point_id)
        back = _back(point_id)
        token = str(request.form.get("token") or "").strip()[:64]
        if not re.fullmatch(r"[0-9a-f]{24}", token):
            flash("Отметка не выполнена: обновите страницу.")
            return redirect(back)
        now = local_iso()
        claimed = conn.execute("INSERT OR IGNORE INTO order_command_log (message_id, at, sender, word, order_id, outcome) VALUES (?,?,?,?,?,'обрабатывается')",
                               ("page-" + token, now, "панель: %s" % author(), ACTIONS[action], card["id"]))
        conn.commit()
        if claimed.rowcount != 1:                                               # the same press twice (a double tap, a resent form)
            flash("Эта отметка уже выполнена.")
            return redirect(back)
        try:
            from . import routes_vk
            _send, deliver, notify = routes_vk.channel(current_app, conn, now)
            outcome, answer, ok = act(conn, card, action, author(), now, deliver, notify)
        except Exception:                                                        # the press must not stay «обрабатывается»
            conn.rollback()
            current_app.logger.exception("«Рабочее место»: %s of order %s failed", action, card_id)
            outcome, answer, ok = "ошибка при выполнении", "Не получилось выполнить отметку. Напишите владельцу.", False
        conn.execute("UPDATE order_command_log SET outcome = ? WHERE message_id = ?", (outcome, "page-" + token))
        conn.commit()
        if ok and not outcome.startswith("уже"):                               # «История точки»: what was done, not a repeat
            point_log.record(conn, now, author(), point_id, action, card["id"], outcome)
        flash(answer, "info" if ok else "error")
        if action == "no_show" and ok:                                         # the question «Шины остаются на точке — на ОТХ?» (2026-10-29)
            return redirect(back + ("&" if "?" in back else "?") + "oth=%d#oth-%d" % (card["id"], card["id"]))
        return redirect(back)

    @app.route("/my-point/handoff/<int:card_id>/cancel", methods=["POST"])
    @login_required
    def mypoint_handoff_cancel(card_id):
        """«Отменить передачу»: the point's own handoff not taken yet (order_manager.cancel_handoff); the person who would take it is told."""
        conn = get_conn()
        point_id = _point_id(conn)
        _own_card(conn, card_id, point_id)
        back = "/my-point/handed" + ("?p=%d" % point_id if own_point() is None else "")     # back to «Переданные клиенты»
        try:
            point = points.get_point(conn, point_id)
        except points.PointError:
            abort(403)
        now = local_iso()
        try:
            from . import routes_vk
            _send, _deliver, notify = routes_vk.channel(current_app, conn, now)
        except Exception:                                                        # without the alert channel the cancel still goes; the order shows it
            current_app.logger.exception("«Рабочее место»: the alert channel for a cancelled handoff failed")
            notify = None
        try:
            answer = mgr.cancel_handoff(conn, card_id, point, author(), now, notify)
        except orders.OrderError as err:
            flash(str(err), "error")
            return redirect(back)
        point_log.record(conn, now, author(), point_id, "handoff_cancel", card_id)
        flash(answer, "ok")
        return redirect(back)

    @app.route("/points/history")
    @login_required
    def points_history():
        """The old address of «История точек»: for good to the tab «Точки» of «История», with the point chosen."""
        wanted = str(request.args.get("p") or "")
        return redirect("/history/points" + ("?p=" + wanted if wanted.isascii() and wanted.isdigit() else ""), code=301)

    @app.route("/history/points")
    @login_required
    def history_points():
        """«История → Точки»: what the points did, with the time and the author (the owner and the partner: section «users»)."""
        conn = get_conn()
        known = points.list_points(conn)
        wanted = str(request.args.get("p") or "")
        point_id = int(wanted) if wanted.isascii() and wanted.isdigit() and int(wanted) in {p["id"] for p in known} else None
        return render_template("points_history.html", rows=point_log.history(conn, point_id), points=known, point_id=point_id)

    @app.route("/my-point/feed")
    @login_required
    def mypoint_feed():
        """For notify.js (read-only): the orders of the point (on the way and there), its notes and the rows of its bell. Each item has a key
        that is new when something new came: the page rings for new keys only."""
        conn = get_conn()
        point_id = _point_id(conn)
        who = current_user()["login"]                                           # notify.js keeps what it has seen per person (a shared PC)
        if point_id is None:
            return jsonify({"who": who, "items": []})
        from .routes_alerts import grouped, href, with_orders
        cards, now = _cards(conn, point_id), local_iso()
        items = [{"key": "question-%d-%s" % (card["id"], re.sub(r"\D", "", card["entered_at"] or "")), "title": "Клиент забрал заказ № %s?" % mgr.label(card), "text": card["client_name"] or "",
                  "href": "/my-point"} for card in cards if reserve.question_open(conn, card, now)]      # of the stay: a new stay rings again
        items += [{"key": "order-%d" % card["id"], "title": "Заказ № %s едет к вам" % mgr.label(card) if card["stage"]["code"] != "at_point"
                   else "Заказ № %s на точке" % mgr.label(card), "text": card["client_name"] or "", "href": "/my-point"} for card in cards]
        try:
            point = points.get_point(conn, point_id)
        except points.PointError:                                               # the manager's point was deleted: nothing to show
            abort(403)
        items += [{"key": "note-%s" % n["id"], "title": n["kind_name"], "text": n["text"][:200], "href": "/my-point?notes=1"}
                  for n in ([n for n in points.notes(conn, point_id, limit=ALERTS_READ) if n["kind"] == "alert"][:points.NOTES_SHOWN]
                            if points.in_panel(point) else [])]     # the bot's answers: not the point's (2026-10-30)
        if own_point() is not None:                                             # the bell of the manager of the point: only theirs (routes_alerts)
            items += [{"key": "alert-%s-%s" % (r["id"], r["more"]), "title": "Чат без заказа" if r.get("chat_row") else "Заказ №%d" % r["order_id"],
                       "text": (r["text"] or "")[:200], "href": href(r)} for r in with_orders(conn, grouped(conn, who))]
        return jsonify({"who": who, "items": items})
