"""«Моя точка»: the start page of a point's manager (the owner 2026-10-23: the points work in the panel, VK is the spare channel).

The orders of the point that lie there («На точке») and are on their way («Едут к нам», with what «Менеджеру: заказ оформлен» says), with «Приехал», «Выдал», «Клиент не пришёл»: the same functions as the VK
commands (order_manager.apply, no_show, tell_client_arrived) with the same checks of the stage, so the result is the same; the client hears
«можно забирать» on the arrival only, once per arrival. The point is taken in the query (the manager's own; the owner and the partner pick
one), never in the template. «Передать клиента» (a POST to the page itself) is order_manager.handoff, the function of the VK command
«клиент <телефон> <что нужно>»: an order on «Передано с точки» for the person on duty; nothing goes to the client; the point needs no VK for
it. «Для точки» shows what the bot did not write to a point in the panel (order_points.notes). /my-point/feed is what notify.js asks every
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

from .. import order_manager as mgr, order_placed, order_points as points, order_report, order_reserve as reserve, order_templates as templates, orders, users
from .common import author, current_user, get_conn, login_required, own_point
from .routes_orders import local_iso

STAGES = ("placed", "moving", "at_point")          # placed (the panel may not say «on the way» yet), on the way to the point, at the point
ACTIONS = {"arrived": mgr.ARRIVED, "issued": "выдал", "no_show": mgr.NO_SHOW, "returned": mgr.RETURN,
           "reserve_issued": "выдал", "reserve_here": mgr.HERE}         # the two answers to «Клиент забрал?» (the question of the reservation)
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


NEED_MAX = 500
HANDED_DAYS = 14                                   # «Переданные клиенты»: what became of the clients of the last two weeks


def _who(conn, login: str) -> str:
    """The name of a panel user for the manager of a point: the display name, else the login."""
    return (users.get_role(conn, login)["display_name"] if login else "") or login or "не назначен"


def _dup_view(conn, busy: dict, point_id: int) -> dict:
    """What the refusal says of the open order: its number and date only (nothing of another point's order). `href`: the order's own
    card for the owner and the partner; for a manager the anchor of the card on this page when it lies there, else no link."""
    card = orders.get_card(conn, busy["id"])
    if own_point() is None:
        href = "/orders/%d" % card["id"]
    elif busy["point_id"] == point_id and card["stage"]["code"] in STAGES:
        href = "#order-%d" % card["id"]
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
    busy = orders.open_card_by_phone(conn, phone)                                 # the owner 2026-10-27: a second open order on one phone is forbidden
    if busy is not None:
        return "", dict(form, dup=_dup_view(conn, busy, point["id"]))
    now = local_iso()
    try:
        from . import routes_vk
        _send, _deliver, notify = routes_vk.channel(current_app, conn, now)
    except Exception:                                                            # without the alert channel the handoff still goes; the order shows it
        current_app.logger.exception("«Моя точка»: the alert channel for a handoff failed")
        notify = None
    try:
        answer = mgr.handoff(conn, message_id, point["vk_peer_id"] or "панель: %s" % author(), dict(mgr.handoff_fields(form), phone=phone, comment=need),
                             "", now, notify, source="page", point=point)
    except Exception:                                                            # after the order was written (the alert): what counts is the order
        conn.rollback()
        current_app.logger.exception("«Моя точка»: the handoff of %s failed", message_id)
        answer = ""
    made = conn.execute("SELECT order_id, responsible FROM order_handoff WHERE message_id = ?", (message_id,)).fetchone()
    if made is not None:
        try:
            orders.add_event(conn, made[0], now, author(), "note", "Клиент передан через «Моя точка» (%s)." % author())
            conn.commit()
        except Exception:
            conn.rollback()
            current_app.logger.exception("«Моя точка»: the note of order %s failed", made[0])
        flash("Передано, ответственный — %s. Заказ №%d." % (_who(conn, made[1]), made[0]), "ok")
        return redirect(back)
    if answer == "Это сообщение уже обработано.":
        flash("Этот клиент уже передан.")
        return redirect(back)
    busy = orders.open_card_by_phone(conn, phone)                                 # another tab made the client's order between the check and now
    if busy is not None:
        return "", dict(form, dup=_dup_view(conn, busy, point["id"]))
    return (answer or "Не получилось принять клиента. Напишите владельцу."), form


def register(app) -> None:
    @app.route("/my-point", methods=["GET", "POST"])
    @login_required
    def mypoint_page():
        conn = get_conn()
        point_id = _point_id(conn)
        try:
            point = points.get_point(conn, point_id) if point_id is not None else None
        except points.PointError:                                               # the manager's point was deleted: nothing to show
            abort(403)
        error, form = "", {}
        if request.method == "POST":
            if point is None:
                abort(404)
            done = _hand_over(conn, point, "/my-point" + ("?p=%d" % point_id if own_point() is None else ""))
            if not isinstance(done, tuple):
                return done
            error, form = done
        now = local_iso()
        today = now[:10]
        cards = []
        for card in (_cards(conn, point_id) if point_id is not None else []):
            until = card["reserve_until"] or ""
            delivery = card["delivery_date"] or ""
            cards.append({"card": card, "label": mgr.label(card), "tel": tel(card["client_phone"]), "here": card["stage"]["code"] == "at_point",
                          "tokens": {action: secrets.token_hex(12) for action in ACTIONS},          # every button is its own press
                          "until": date.fromisoformat(until).strftime("%d.%m") if until else "", "late": bool(until) and until < today,
                          "ends": bool(until) and until <= today, "delivery": _short_date(delivery), "text": vk_text(conn, card, now),
                          "asked": reserve.question_open(conn, card, now)})
        labels = orders.origin_labels(conn, point_id, [row["card"]["id"] for row in cards]) if own_point() is not None else {}
        for row in cards:
            row["origin"] = labels.get(row["card"]["id"])                # not from the point's own Avito account: only receive and hand over
        since = (date.fromisoformat(today) - timedelta(days=HANDED_DAYS)).isoformat()
        handed = mgr.point_handoffs(conn, point_id, since) if point_id is not None else []
        bonus = order_report.rate_on(order_report.list_rates(conn), "handoff_fee", today)
        notes = points.notes(conn, point_id) if points.in_panel(point) else []         # back on «ВК»: the old notes are not shown
        if own_point() is not None and notes:                                   # a person of the point looked: the alerts shown are seen (the safety net)
            try:
                points.mark_seen(conn, point_id, max(n["at"] for n in notes))
            except Exception:                                                    # noqa: BLE001  (a busy base: the page still opens)
                conn.rollback()
        here = [r for r in cards if r["here"]]
        counts = {"coming": len(cards) - len(here), "here": len(here), "ending": sum(1 for r in here if r["ends"]),
                  "asked": sum(1 for r in cards if r["asked"])}
        return render_template("mypoint.html", point=point, cards=cards, counts=counts, questions=[r for r in cards if r["asked"]],
                               notes=notes, choose=own_point() is None, handed=handed, seasons=mgr.SEASONS, bonus=bonus,
                               points=points.list_points(conn) if own_point() is None else [], user=current_user(),
                               handoff_token=secrets.token_hex(12), handoff_error=error, handoff_form=form), 400 if error else 200

    @app.route("/my-point/<int:card_id>/<action>", methods=["POST"])
    @login_required
    def mypoint_action(card_id, action):
        if action not in ACTIONS:
            abort(404)
        conn = get_conn()
        point_id = _point_id(conn)
        card = _own_card(conn, card_id, point_id)
        back = "/my-point" + ("?p=%d" % point_id if own_point() is None else "")
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
            current_app.logger.exception("«Моя точка»: %s of order %s failed", action, card_id)
            outcome, answer, ok = "ошибка при выполнении", "Не получилось выполнить отметку. Напишите владельцу.", False
        conn.execute("UPDATE order_command_log SET outcome = ? WHERE message_id = ?", (outcome, "page-" + token))
        conn.commit()
        flash(answer, "info" if ok else "error")
        return redirect(back)

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
        items += [{"key": "note-%s" % n["id"], "title": n["kind_name"], "text": n["text"][:200], "href": "/my-point"}
                  for n in (points.notes(conn, point_id) if points.in_panel(point) else [])]
        if own_point() is not None:                                             # the bell of the manager of the point: only theirs (routes_alerts)
            items += [{"key": "alert-%s-%s" % (r["id"], r["more"]), "title": "Чат без заказа" if r.get("chat_row") else "Заказ №%d" % r["order_id"],
                       "text": (r["text"] or "")[:200], "href": href(r)} for r in with_orders(conn, grouped(conn, who))]
        return jsonify({"who": who, "items": items})
