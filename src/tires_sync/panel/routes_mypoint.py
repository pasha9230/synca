"""«Рабочее место» (its old name is gone, the address /my-point stays): the start page of a point's manager (the owner 2026-10-23: the points work in the panel, VK is the spare
channel; 2026-10-29: in the panel's own frame with the menu, the stages of the point's orders, one search line, «Отменить передачу» of a
client not taken yet, the return of «На ОТХ» with the act МХ-3; every action goes to «История точки», point_log).

The orders of the point that lie there («На точке») and are on their way («Едут к нам», with what «Менеджеру: заказ оформлен» says), with «Приехал», «Выдал», «ОТКАЗ» (with its reason, to «На ОТХ»; the owner 2026-11-05): the same functions as the VK
commands (order_manager.apply, refuse, tell_client_arrived) with the same checks of the stage, so the result is the same; the client hears
«можно забирать» on the arrival only, once per arrival. The point is taken in the query (the manager's own; the owner and the partner pick
one), never in the template. «Передать клиента» (a POST to the page itself) is order_manager.handoff, the function of the VK command
«клиент <телефон> <что нужно>»: an order on «Передано с точки» for the person on duty; nothing goes to the client; the point needs no VK for
it. «Оповещения» (a line under the search, the list on ?notes=1) shows the alerts the bot did not write to a point in the panel
(order_points.notes); the bot's answers to the clients are not shown to the point (the owner 2026-10-30). /my-point/feed is what notify.js asks every
half a minute for the sound and the browser's notification: the orders of the point, its notes and the rows of its bell (one source with it).
The owner 2026-10-26: counters at the top; one list, newest first by the order's last event that matters to the point (_cards); «Подробнее» of a card
was the VK message «Менеджеру: заказ оформлен», since 2026-11-09 its positions line by line (point_lines: the client's prices, never the purchase); «Возврат» of an order at the
point has no button since 2026-11-02 (the VK «возврат отдал» stays); the question of the last day of the reservation (order_reserve, asked in VK) is answered
here too: «Выдан» is «выдал», «На точке» is «на точке» (order_reserve.here), one answer whichever comes first. The owner 2026-11-02: the
question of the seventh day on «На ОТХ» («Выдали» / «На точке», order_oth), and «Отменить» of a press that moved an order (point_log.undo)."""
from __future__ import annotations

import re
import secrets
from datetime import date, timedelta

from flask import abort, current_app, flash, g, jsonify, redirect, render_template, request

from .. import order_intercity, order_manager as mgr, order_merge, order_oth, oth_point_return, oth_point_sale, oth_return_act, oth_sale, point_log, search, order_points as points, order_report, order_reserve as reserve, order_templates as templates, orders, users
from .common import author, current_user, get_conn, login_required, own_point
from .routes_design import point_page
from .routes_orders import local_iso, local_now

STAGES = ("placed", "moving", "at_point")          # placed (the panel may not say «on the way» yet), on the way to the point, at the point
ACTIONS = {"arrived": mgr.ARRIVED, "issued": "выдал", "refuse": mgr.REFUSE,       # «Возврат» of an order at the point: no button since 2026-11-02;
           "reserve_issued": "выдал", "reserve_here": mgr.HERE,         # «ОТКАЗ» with its reason instead of «Клиент не пришёл» and «Да, на ОТХ»
                                                                        # (the owner 2026-11-05); the two answers to «Клиент забрал?»
           "act_signed": "акт МХ-3 подписан",                           # «Подписал акт и отдал»: the return of «На ОТХ» (order_oth.sign_act)
           "oth_issued": "выдали (ОТХ)", "oth_here": "на точке (ОТХ)",  # the two answers to the seventh day on «На ОТХ» (the owner 2026-11-02)
           "oth_sold": "продано (ОТХ)",                                 # «Продать»: the tyres on «На ОТХ» sold to a buyer from the street (the owner 2026-11-10)
           "oth_buyer": "есть покупатель (ОТХ)",                        # «Есть покупатель» of «Согласование возврата»: a task to the office (E2, 2026-11-12)
           "shipped": mgr.SHIP}                                         # «Передал курьеру ТК»: an order to another city (order_intercity, 2026-11-02)
PICKUP = ("issued", "reserve_issued", "reserve_here", "refuse")    # what a client's pickup means: never for an order to another city
REFUSE_CHOICES = {"year": mgr.REFUSE_REASONS[0], "size": mgr.REFUSE_REASONS[1], "other": ""}    # «Другой»: the text typed
UNDO = "undo"                                      # /my-point/<order>/undo: «Отменить» a press (point_log.undo, the owner 2026-11-02)
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
    mine = points.own_sales(conn, point_id) if ids else set()                  # its own sales are «Заказы»'s (the owner 2026-10-30)
    return [orders.get_card(conn, card_id) for card_id in ids if card_id not in mine]


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


def _not_here(card: dict, what: str):
    return ("не подходит: заказ на этапе «%s»" % card["stage"]["name"],
            "Заказ №%s сейчас на этапе «%s»: «%s» отмечают, когда заказ на точке." % (mgr.label(card), card["stage"]["name"], what), False)


def act(conn, card: dict, action: str, who: str, now: str, deliver, notify, tell=None, reason: str = ""):
    """What the VK command of the same word does for an order of the point (order_manager._command for the built-in words): returns
    (outcome for the log, answer for the manager, done). A word that does not fit the stage changes nothing. `reason`: of «ОТКАЗ»."""
    code = card["stage"]["code"]
    if action == "shipped":                                                     # the courier of the transport company took it
        outcome, answer = order_intercity.ship(conn, card, who, now, deliver, tell)
        return outcome, answer, not outcome.startswith("не ")
    if action in PICKUP and order_intercity.on(card):
        return ("не подходит: заказ в другой город", "Заказ №%s — в другой город, клиент за ним не придёт: отдайте шины с этикетками курьеру ТК "
                "и нажмите «Передал курьеру ТК»." % mgr.label(card), False)
    if action == "act_signed":                                                  # the return of «На ОТХ» agreed with the supplier
        plan = order_oth.return_plan(conn, card["id"])
        if plan is not None and plan["signed_at"]:
            return "уже отмечено", "Заказ №%s уже отмечен: акт МХ-3 подписан, шины отданы." % mgr.label(card), True
        if code not in order_oth.RETURNING or plan is None:
            return ("не подходит: возврат не согласован или заказ на этапе «%s»" % card["stage"]["name"],
                    "Заказ №%s: возврат с ОТХ не согласован с поставщиком, отмечать нечего." % mgr.label(card), False)
        try:
            done = order_oth.sign_act(conn, card, (card.get("point") or {}).get("name") or "", now, who)
        except orders.OrderError as err:
            return "не выполнено: %s" % err, "Не получилось: %s" % err, False
        if not done:
            return "не выполнено: заказ уже изменили", "Заказ №%s уже изменили: обновите страницу." % mgr.label(card), False
        if done == "signed":                                                    # no purchase to count the refund: the manager fills it
            return ("акт подписан, заказ на «%s»: ждёт суммы возврата" % orders.stage_name(conn, orders.RETURN_GIVEN),
                    "Заказ №%s: акт МХ-3 подписан, шины отданы. Менеджер заполнит сумму возврата." % mgr.label(card), True)
        return ("заказ перенесён на «%s»" % orders.stage_name(conn, "return"),
                "Заказ №%s: акт МХ-3 подписан, шины отданы, возврат закрыт." % mgr.label(card), True)
    if action == "oth_issued":                                                  # «Выдали» to «Заказ выдали или он на точке?»
        return order_oth.answer_issued(conn, card, who, now)
    if action == "oth_sold":                                                    # «Продать»: no question is needed, no client comes back
        return order_oth.sell(conn, card, who, now)
    if action == "oth_here":
        return order_oth.answer_here(conn, card, who, now)
    if action == "oth_buyer":                                                   # «Есть покупатель»: the office sells, from the card (E2)
        return oth_point_return.tell_buyer(conn, card, (card.get("point") or {}).get("name") or "", who, now)
    if action == "reserve_issued":                                              # «Выдан» to the question is «выдал»
        action = "issued"
    if action == "reserve_here":                                                # the VK «на точке»: marks the answer (the reminder: at 12:30, 2026-11-02)
        if code != "at_point":
            return _not_here(card, "на точке")
        outcome, answer = reserve.here(conn, card, who, now, deliver)
        return outcome, answer, not outcome.startswith("«")
    if action == "refuse":                                                      # «ОТКАЗ» with its reason: to «На ОТХ» (the owner 2026-11-05)
        return mgr.refuse(conn, card, reason, who, now, notify)
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


def _refuse_reason() -> str:
    """The reason of «ОТКАЗ» from its form: one of the two given, or the text of «Другой»; '' for none (order_manager.refuse_reason)."""
    choice = str(request.form.get("reason") or "")
    if choice not in REFUSE_CHOICES:
        return ""
    return mgr.refuse_reason(REFUSE_CHOICES[choice] or str(request.form.get("other") or "")[:mgr.REFUSE_MAX * 2])


def _tell(conn, now: str):
    """The owner's and the partner's Telegram for a press of the point (order_notify.tell_people with the panel's keys); never raises. Never
    to the person who pressed it working as the point (common.own_logins)."""
    from .. import order_notify
    from .common import own_logins
    skip = own_logins(conn)

    def tell(logins, text):
        return order_notify.tell_people(conn, current_app.config.get("SECRET_BOX"), logins, text,
                                        tg_http=current_app.config.get("TG_HTTP") or order_notify.urllib_transport, now=now, skip_logins=skip)
    return tell


NEED_MAX = 500
HANDED_DAYS = 14                                   # «Переданные клиенты»: what became of the clients of the last two weeks


def _who(conn, login: str) -> str:
    """The name of a panel user for the manager of a point: the display name (the owner's main login: «Имя основного входа»), else the login."""
    from flask import current_app
    return users.person_name(conn, login, current_app.config["AUTHOR"]) or "не назначен"


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
TABS = {"moving": "Едут", "here": "На точке", "oth": "На ОТХ", "agree": "Согласование возврата", "return": "На возврат",
        "issued": "Выдано"}                        # the owner 2026-11-09: what the point gave out in the last ISSUED_DAYS (_issued)
# the owner 2026-11-12 (the partner's answers): «Согласование возврата» is a tab of its own between «На ОТХ» and «На возврат», and the chosen
# tab says in small letters what lies in it (the texts are the owner's, word for word; a tuple: a line each)
TAB_NOTES = {"moving": ("Ждём доставки, отметьте «Приехал», как прибудут на точку.",),         # the owner's task of 2026-11-12: the new texts
             "here": ("Эти позиции у нас, ожидаем выкупа заказчиком.",),
             "oth": ("Невыкуп, хранение 7 дней. Если продаёте, впишите данные покупателя.", "Можно продать, +50 рублей за штуку."),
             "agree": ("Отправили запрос на оформление возврата этих позиций. Для продажи нужна проверка.",),
             "return": ("Эти товары согласованы на возврат. Возвраты возможны в Пн, Ср, Пт. Распечатайте Акт МХ-3.",),
             "issued": ("Заказы, отмеченные как реализованные заказчику.",)}
DEFAULT_TAB = "moving"                             # the owner's task of 2026-11-12: «Рабочее место» opens on «Едут» (was «На точке»)
ACT_COLOURS = 4                                    # the colours of «1 Акт на несколько заказов» (.mp-act-mark--1…): by the dates, then round
ISSUED_DAYS = 30
CONFIRM = ("issued", "reserve_issued", "oth_issued", "oth_sold")   # the owner 2026-11-09: a hand-out is confirmed with the order's number, tyres and client
ALERTS_READ = 500                                  # the notes read to find the point's newest alerts among the bot's answers (two weeks kept)


def _in_tab(row: dict, tab: str) -> bool:
    """«Едут»: «Заказ оформлен» and «Перемещение на точку» (the owner 2026-10-29); «На точке»: «Товар на точке», waiting for the client; «На ОТХ»:
    our tyres at the point (the owner 2026-10-30). The owner 2026-11-12: «Согласование возврата»: the orders of the stage «На возврат» whose
    return is not agreed yet, and those of «На ОТХ» with a position marked for the return (the whole order: a record is sold or returned whole);
    «На возврат»: agreed (the date is set, the act МХ-3 not signed yet; one of «На ОТХ» agreed before the stage came too) and the act signed."""
    code = row["card"]["stage"]["code"]
    agreed = bool(row.get("returning"))
    agreeing = not agreed and bool(row.get("agreeing"))
    return {"moving": code in ("placed", "moving"), "here": row["here"], "oth": code == "oth" and not agreed and not agreeing, "agree": agreeing,
            "return": agreed or (code == orders.RETURN_GIVEN and not agreeing),
            "issued": False}[tab]                                       # «Выдано» is not of these cards: _issued


def issue_lines(card: dict) -> list:
    """What the point checks before a hand-out (the owner 2026-11-09): the order's number (WebMIM's, «№ MIM» of 08.11), the tyres line by line with the quantity, the
    client. Nothing of money."""
    lines = ["№ заказа: %s" % (card["webmim_number"] or "нет (заказ в панели №%d)" % card["id"])]     # no word «MIM» at a point (30.10)
    lines += ["%s × %s" % (i["name"], i["qty"]) for i in card["items"] if i["qty"] >= 1] or ["Шины не указаны"]
    return lines + ["Клиент: %s" % (card["client_name"] or "без имени")]


def sell_lines(card: dict) -> list:
    """What the point checks before «Продать» (the owner 2026-11-10): the order's number, the tyres with the quantity, the price of one and the
    sum — point_lines' own (the price written in the order), and what the press does. No client: the buyer is not the order's."""
    lines = ["№ заказа: %s" % (card["webmim_number"] or "нет (заказ в панели №%d)" % card["id"])]
    rows = point_lines(card)
    for i in rows:
        lines.append("%s × %d шт." % (i["name"], i["qty"]) + (": %s ₽ за шт., сумма %s ₽" % (_rub(i["unit"]), _rub(i["sum"])) if i["unit"] else ": цена не указана"))
    if len(rows) > 1 and all(i["unit"] for i in rows):
        lines.append("Сумма продажи: %s ₽" % _rub(sum(i["sum"] for i in rows)))     # not «Итого»: the card «На ОТХ» has no client's total (2026-10-30)
    return lines + ([] if rows else ["Шины не указаны"]) + ["Шины проданы с ОТХ; заказ закроется как выполненный."]


def _sale_url(card_id, point_id, back: str) -> str:
    """The window of «Продать» of a record (oth_point_sale): the point chosen (the owner, the partner) and the tab to come back to."""
    from urllib.parse import parse_qs, urlsplit
    tab = (parse_qs(urlsplit(back).query).get("tab") or [""])[0]
    args = (["p=%d" % point_id] if own_point() is None else []) + (["tab=%s" % tab] if tab else [])
    return "/my-point/%d/oth-sale" % card_id + ("?" + "&".join(args) if args else "")


def _rub(value: float) -> str:
    return ("%.2f" % value).rstrip("0").rstrip(".").replace(".", ",") if value != int(value) else "{:,}".format(int(value)).replace(",", " ")


def point_lines(card: dict) -> list:
    """The positions of «Подробнее» and of a card «На ОТХ» (the owner 2026-11-09): the supplier's name, its code (a tyre of the point has
    none), the pieces, the price of one and the sum — the price written in the order (sale_unit; «бери цену, которую мы указывали в заказе
    изначально»), never Avito's nor the purchase. A position given on to another order (0 pieces) is not here; no price given: no price."""
    lines = []
    for i in card["items"]:
        if int(i["qty"] or 0) < 1:
            continue
        unit = float(i["sale_unit"] or 0)
        lines.append({"name": i["name"], "code": "" if i["goods_id"] == orders.POINT_GOODS else str(i.get("code") or i["goods_id"] or ""),
                      "qty": int(i["qty"]), "unit": unit if unit > 0 else None, "sum": unit * int(i["qty"]) if unit > 0 else None})
    return lines


def _issued(conn, point_id, today: str) -> list:
    """«Выдано» (the owner 2026-11-09): the orders of the point closed by a hand-out in the last ISSUED_DAYS, newest first — on
    «Выполнено», come there from «На точке» (or from the way: «Выдал» skips «Товар на точке», order_manager.ISSUE_FROM) by the point's
    press, its VK command or the office; not an order to another city (the courier took it), not its own sale («Заказы»). With the moment,
    who marked it and the press still to undo (point_log.undoable). Nothing of money."""
    since = (date.fromisoformat(today) - timedelta(days=ISSUED_DAYS)).isoformat()
    done = orders.stage_name(conn, "done")
    sources = {s["name"] for s in orders.list_stages(conn) if s["code"] in mgr.ISSUE_FROM + ("oth", orders.RETURN_GIVEN)}     # and «Выдали»/«Продать» from «На ОТХ» and «На возврат» (2026-11-09, 2026-11-10)
    rows = conn.execute("SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE c.point_id = ? AND s.code = 'done' "
                        "AND c.entered_at >= ? ORDER BY c.entered_at DESC, c.id DESC", (point_id, since)).fetchall()
    mine = points.own_sales(conn, point_id) if rows else set()
    found = []
    for (card_id,) in rows:
        if card_id in mine:
            continue
        card = orders.get_card(conn, card_id)
        if order_intercity.on(card):
            continue
        move = conn.execute("SELECT author, text FROM order_event WHERE order_id = ? AND kind = 'stage' ORDER BY id DESC LIMIT 1",
                            (card_id,)).fetchone()                     # the last move of an order on «Выполнено» is the one there
        came = re.fullmatch(r"Этап: «(.*)» → «(.*)»", move[1]) if move else None
        if came is not None and came.group(2) != done:
            came = None
        if came is None or came.group(1) not in sources:
            continue
        found.append({"card": card, "label": mgr.label(card), "at": card["entered_at"], "who": users.person_name(conn, move[0], current_app.config["AUTHOR"]),
                      "undo": point_log.undoable(conn, card, point_id)})
    return found


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
    tab = str(request.form.get("tab") or request.args.get("tab") or "") or tab     # «Подтвердите выдачу», the window of the sale keep the tab
    args = (["p=%d" % point_id] if own_point() is None else []) + (["tab=%s" % tab] if tab in TABS and tab != DEFAULT_TAB else [])
    return "/my-point" + ("?" + "&".join(args) if args else "")


def _oth_cards(conn, point_id) -> list:
    """The point's orders on «На ОТХ» and «На возврат»: the tyres the client refused lie at the point (tabs «На ОТХ», «На возврат»). Not its
    own sales («Заказы»)."""
    ids = [r[0] for r in conn.execute(
        "SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE c.point_id = ? AND s.code IN (?, ?) ORDER BY c.entered_at, c.id",
        (point_id,) + order_oth.RETURNING)]
    mine = points.own_sales(conn, point_id) if ids else set()
    return [orders.get_card(conn, card_id) for card_id in ids if card_id not in mine]


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


def _full_date(value: str) -> str:
    try:
        return date.fromisoformat(value).strftime("%d.%m.%Y") if value else ""
    except ValueError:
        return ""


def mark_shared_acts(rows: list) -> None:
    """«1 Акт на несколько заказов» (the owner's task of 2026-11-12): an act (a point and a date of the return) of several orders of
    «На возврат» gives each of them the same act_mark, the number of its colour: 1…ACT_COLOURS by the dates of the return, then round. The
    orders of an act of its own and those without an act get none."""
    shared = {}
    for row in rows:
        if row.get("act_file"):
            shared.setdefault(row["act_file"]["id"], []).append(row)
    groups = sorted((g for g in shared.values() if len(g) > 1), key=lambda g: (g[0]["act_file"]["return_date"], g[0]["act_file"]["id"]))
    for n, group in enumerate(groups):
        for row in group:
            row["act_mark"] = n % ACT_COLOURS + 1


def _returns(conn, point_id) -> list:
    """«Возврат поставщику»: the orders of the point on «На ОТХ» agreed for the return, with a press token each."""
    today = local_iso()[:10]
    return [{"card": r["card"], "label": mgr.label(r["card"]), "date": _short_date(r["plan"]["return_date"]),
             "return_by": _full_date(r["plan"]["return_date"]),                 # «Вернуть дд.мм.гггг» by the number (the task of 2026-11-12)
             "late": r["plan"]["return_date"] < today, "token": secrets.token_hex(12), "act_order": order_oth.act_orders(conn, r["card"]["id"]),
             "act_file": oth_return_act.find(conn, point_id, r["plan"]["return_date"])}      # «Скачать акт» (the owner 2026-11-12)
            for r in order_oth.to_return(conn, point_id)]


def nav_counts(conn, point_id) -> dict:
    """The counters of the sections of a point's phone (the owner 2026-10-30, the prototype's PM_SECS): «Рабочее место» (what lies at the
    point and comes, the returns), «Переданные» (the handoffs still to be taken), «Заказы» (its own sales in the column «Новый заказ»)."""
    if point_id is None:
        return {"mypoint": 0, "handed": 0, "orders": 0}
    since = (date.fromisoformat(local_iso()[:10]) - timedelta(days=HANDED_DAYS)).isoformat()
    mine = points.own_sales(conn, point_id)
    stages = {st["id"]: st for st in orders.list_stages(conn)}
    new = [cid for cid, sid in conn.execute("SELECT id, stage_id FROM order_card WHERE point_id = ?", (point_id,))
           if cid in mine and points.point_column(stages[sid]) == "new"]               # as the chip «Новый заказ» counts
    return {"mypoint": len(_cards(conn, point_id)) + len(order_oth.to_return(conn, point_id)),
            "handed": sum(1 for h in mgr.point_handoffs(conn, point_id, since) if h["cancellable"]),
            "orders": len(new)}


def register(app) -> None:
    def point_nav():
        """The counters for the sections of the point's phone (the manager of a point only; None for the others), once per request."""
        if own_point() is None:
            return None
        if "point_nav" not in g:
            g.point_nav = nav_counts(get_conn(), own_point())
        return g.point_nav

    app.jinja_env.globals["point_nav"] = point_nav

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
        return render_template(point_page("mypoint_handed.html"), point=point, choose=own_point() is None, user=current_user(),
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
            cards.append({"card": card, "label": mgr.label(card), "tel": tel(card["client_phone"]), "here": code == "at_point", "oth": code in order_oth.RETURNING,
                          "tokens": {action: secrets.token_hex(12) for action in ACTIONS},          # every button is its own press
                          "until": date.fromisoformat(until).strftime("%d.%m") if until else "", "late": bool(until) and until < today,
                          "hold": date.fromisoformat(until).strftime("%d.%m.%Y") if until else "",     # «Резерв: Хранение шин до …» (2026-11-09)
                          "since": card["entered_at"], "lines": point_lines(card),     # «на точке с 07.11», «на ОТХ с 31.10»: the stage came then
                          "ends": code == "at_point" and bool(until) and until <= tomorrow, "delivery": _short_date(delivery) if delivery else (oth_sale.IN_STOCK if card["in_stock"] else ""),
                          "sell_lines": sell_lines(card) if code in order_oth.RETURNING else [],     # «Продать» (2026-11-10): until the act МХ-3 is signed
                          # «Продать» of the point (the owner 2026-11-12, over 2026-11-10): on «На ОТХ» only; on the agreement of the
                          # return and agreed there is no button (order_oth.sell refuses too): the office cancels the agreement first
                          "can_sell": (code == "oth" and order_oth.return_plan(conn, card["id"]) is None
                                       and not order_oth.agreeing(conn, card) and not order_oth.given_back(conn, card["id"])),
                          "sale_url": _sale_url(card["id"], point_id, "/my-point?tab=oth") if card.get("oth_record") and code == "oth" else "",     # the window (ТЗ part D)
                          "took": reserve.took_open(conn, card),      # «Клиент пишет, что забрал заказ…» (the owner 2026-11-12)
                          "asked": reserve.question_open(conn, card, now),     # «Подробнее» is no longer the VK text: point_lines (2026-11-09)
                          "oth_asked": order_oth.question_open(conn, card), "to_agree": order_oth.to_agree(conn, card),
                          "agreeing": order_oth.agreeing(conn, card),          # «Согласование возврата» (the owner 2026-11-12)
                          "can_buyer": oth_point_return.can_tell_buyer(conn, card, now),     # «Есть покупатель», 2 days of the agreement (E2)
                          "ret_label": order_oth.point_label_of(order_oth.return_plan(conn, card["id"])) if code == orders.RETURN_GIVEN else "",
                          "ret_state": order_oth.state_of(order_oth.return_plan(conn, card["id"])) if code == orders.RETURN_GIVEN else "",
                          "oth_question": order_oth.question(conn, card, now) if code in order_oth.RETURNING else "",
                          "order_date": _order_day(conn, card), "from_oth": templates._oth_orders(conn, card["id"]),
                          "merged_from": ", ".join("№%s" % s["label"] for s in order_merge.sources(conn, card["id"])),     # «Шины с ОТХ, заказ №…»
                          "intercity": order_intercity.on(card) and code not in order_oth.RETURNING,     # «В другой город» (2026-11-02)
                          "labels": order_intercity.labels(conn, card["id"]) if order_intercity.on(card) else [],
                          # «Итого» (the owner 2026-11-07): the order window's own sum of the positions already read; «На ОТХ»: no client, no sum
                          "sale": orders.totals(card["items"])["sale"] if card["items"] and code not in order_oth.RETURNING else None,
                          "issue_lines": issue_lines(card), "undo": point_log.undoable(conn, card, point_id)})     # «Отменить» in the card (2026-11-09)
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
        for row in cards:                                                       # given, waits for the amount: the act of its date by its card
            plan = order_oth.return_plan(conn, row["card"]["id"]) if row["ret_state"] == "signed" else None
            row["act_file"] = oth_return_act.find(conn, point_id, plan["return_date"]) if plan else None
        today_ret = order_oth.return_day(conn, point_id, now) if point_id is not None else None
        if today_ret:
            today_ret["act_file"] = oth_return_act.find(conn, point_id, today_ret["date"])
            today_ret["token"] = secrets.token_hex(12)
        mark_shared_acts(returns + [row for row in cards if _in_tab(row, "return") and not row["returning"]])
        for ret in returns:                                                     # agreed, the question still open: on the plaque, the only place of the order
            ret["asked"] = next((row for row in cards if row["card"]["id"] == ret["card"]["id"] and row["oth_asked"]), None)
        tab = request.args.get("tab", "")
        tab = tab if tab in TABS else DEFAULT_TAB
        shown = sorted((r for r in cards if _in_tab(r, tab)), key=lambda r: not (r["asked"] or r["oth_asked"]))     # the questions first
        counts = {key: sum(1 for r in cards if _in_tab(r, key)) for key in TABS}
        issued = _issued(conn, point_id, today) if point_id is not None else []
        counts["issued"] = len(issued)
        hot = {"here": any(r["asked"] for r in cards), "oth": any(r["oth_asked"] and _in_tab(r, "oth") for r in cards),
               "agree": any(r["oth_asked"] and _in_tab(r, "agree") for r in cards),
               "return": any(r.get("returning") or (r["oth_asked"] and _in_tab(r, "return")) for r in cards)}     # the tabs that wait for the point
        query = str(request.args.get("q") or "").strip()[:search.QUERY_MAX]
        found = _found(conn, query, point_id) if query and point_id is not None else None
        if found:                                                                # the search looks at every order of the point: say its tab
            tab_of = {r["card"]["id"]: "issued" for r in issued}
            tab_of.update({r["card"]["id"]: _tab_of(r) for r in cards})
            for f in found:
                oid = int(f["url"].rsplit("/", 1)[-1]) if f.get("url", "").rsplit("/", 1)[-1].isdigit() else None
                f["tab"] = tab_of.get(oid, "")
                f["id"] = oid
                if f["tab"] in ("oth", "agree", "return"):                               # our tyres at the point: the client is gone (the owner 2026-10-30)
                    f["title"] = "Заказ №%d · шины на ОТХ" % oid
        if found is not None:                                                    # the owner 13.11: the search shows the positions found only, as
            ids = {f.get("id") for f in found}                                   # in their tab (with their buttons), never a link to the order
            shown = sorted((r for r in cards if r["card"]["id"] in ids and not r.get("returning")),
                           key=lambda r: not (r["asked"] or r["oth_asked"]))
            issued = [r for r in issued if r["card"]["id"] in ids]
            returns = [r for r in returns if r["card"]["id"] in ids]
        return render_template(point_page("mypoint.html"), point=point, cards=shown, counts=counts, tab=tab, tabs=TABS, tab_notes=TAB_NOTES, default_tab=DEFAULT_TAB, hot=hot, issued=issued,
                               alerts=alerts if alerts_open else [], alerts_new=alerts_new, alerts_open=alerts_open, choose=own_point() is None,
                               points=points.list_points(conn) if own_point() is None else [], user=current_user(),
                               stages=_stages(conn, point_id) if point_id is not None else [], query=query,
                               found=found,
                               returns=returns,
                               return_day=today_ret, return_day_mark=order_oth.RETURN_DAY_MARK, not_taken_next=oth_point_return.NOT_TAKEN_NEXT,
                               buyer_ask=oth_point_return.BUYER_ASK, comment_max=oth_point_return.COMMENT_MAX,
                               undo_hint=point_log.UNDO_HINT, intercity_hint=order_intercity.HINT, took_label=reserve.TOOK_LABEL)

    def _undo(card_id):
        """«Отменить» a press that moved the order (point_log.undo; the address /my-point/<order>/undo, the endpoint of the presses): the
        owner and the partner are told; what went to the client stays."""
        conn = get_conn()
        point_id = _point_id(conn)
        card = _own_card(conn, card_id, point_id)
        back = _back(point_id)
        try:
            point = points.get_point(conn, point_id)
        except points.PointError:
            abort(403)
        now = local_iso()
        try:
            answer = point_log.undo(conn, card, point, author(), now, _tell(conn, now))
        except orders.OrderError as err:
            flash(str(err), "error")
            return redirect(back)
        flash(answer, "info")
        return redirect(back)

    def _confirm_issue(card, action, token, point_id, back):
        """«Подтвердите выдачу» without the script (the owner 2026-11-09; with it, the same lines in the panel's window, app.js): the
        order's number, the tyres, the client; «Да, выдал» sends the same press (its token) once more with confirm=yes, «Отмена» goes back."""
        from urllib.parse import parse_qs, urlsplit
        return render_template("mypoint_confirm.html", card=card, action=action, token=token, label=mgr.label(card), sell=action == "oth_sold",
                               lines=sell_lines(card) if action == "oth_sold" else issue_lines(card),
                               p=point_id if own_point() is None else None, back=back,
                               tab=(parse_qs(urlsplit(back).query).get("tab") or [""])[0])

    @app.route("/my-point/<int:card_id>/<action>", methods=["POST"])
    @login_required
    def mypoint_action(card_id, action):
        if action == UNDO:
            return _undo(card_id)
        if action not in ACTIONS:
            abort(404)
        conn = get_conn()
        point_id = _point_id(conn)
        card = _own_card(conn, card_id, point_id)
        back = _back(point_id)
        if card_id in points.own_sales(conn, point_id):                         # its own sale is moved in «Заказы», on its four stages
            flash("Заказ №%d — продажа точки: его этап меняется в «Заказах»." % card_id)
            return redirect(back)
        token = str(request.form.get("token") or "").strip()[:64]
        if not re.fullmatch(r"[0-9a-f]{24}", token):
            flash("Отметка не выполнена: обновите страницу.")
            return redirect(back)
        reason = _refuse_reason() if action == "refuse" else ""
        if action == "refuse" and not reason:                                   # checked before the press is taken: the same form may go again
            flash("Отказ не отмечен: укажите причину отказа («Другой» — впишите её).", "error")
            return redirect(back)
        if action == "act_signed" and request.form.get("handover") != "yes":    # the owner 2026-11-04: the second step of the button, checked here
            flash("Передача не отмечена: подтвердите передачу поставщику (водителю) кнопкой «Да, передал».", "error")
            return redirect(back)
        if action == "oth_sold" and card.get("oth_record"):                    # a record of storage is sold by the window (ТЗ part D)
            return redirect(_sale_url(card_id, point_id, back))
        if action in CONFIRM and request.form.get("confirm") != "yes" and not (order_intercity.on(card) and action not in ("oth_issued", "oth_sold")):
            return _confirm_issue(card, action, token, point_id, back)        # nothing is taken nor changed until «Да, выдал»
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
            outcome, answer, ok = act(conn, card, action, author(), now, deliver, notify, _tell(conn, now), reason)
        except Exception:                                                        # the press must not stay «обрабатывается»
            conn.rollback()
            current_app.logger.exception("«Рабочее место»: %s of order %s failed", action, card_id)
            outcome, answer, ok = "ошибка при выполнении", "Не получилось выполнить отметку. Напишите владельцу.", False
        conn.execute("UPDATE order_command_log SET outcome = ? WHERE message_id = ?", (outcome, "page-" + token))
        conn.commit()
        if ok and not outcome.startswith("уже"):                               # «История точки»: what was done, not a repeat
            point_log.record(conn, now, author(), point_id, action, card["id"], outcome)
            point_log.remember(conn, card, action, point_id, author(), now)     # «Отменить» of a press that moved the order (2026-11-02)
        flash(answer, "info" if ok else "error")
        return redirect(back)

    @app.route("/my-point/<int:card_id>/oth-sale", methods=["GET", "POST"])
    @login_required
    def mypoint_oth_sale(card_id):
        """The window of «Продать» of a record of storage on «На ОТХ» (oth_point_sale, ТЗ part D): the buyer, the numbers, the pieces of
        each position; the sum counts itself. A refusal shows the window again with what was typed and why."""
        conn = get_conn()
        point_id = _point_id(conn)
        card = _own_card(conn, card_id, point_id)
        back = _back(point_id)
        why = oth_point_sale.why_not(conn, card)
        if why:
            flash(why, "error")
            return redirect(back)
        if request.method == "GET":
            return _sale_window(card, point_id, back, {}, "")
        form = request.form
        token = str(form.get("token") or "").strip()[:64]
        if not re.fullmatch(r"[0-9a-f]{24}", token):
            flash("Продажа не выполнена: обновите страницу.")
            return redirect(back)
        pieces = {}
        for line in oth_point_sale.lines(conn, card):
            raw = str(form.get("qty_%d" % line["item_id"]) or "0").strip()
            pieces[line["item_id"]] = int(raw) if raw.isascii() and raw.isdigit() else -1
        now = local_iso()
        claimed = conn.execute("INSERT OR IGNORE INTO order_command_log (message_id, at, sender, word, order_id, outcome) VALUES (?,?,?,?,?,'обрабатывается')",
                               ("page-" + token, now, "панель: %s" % author(), ACTIONS["oth_sold"], card["id"]))
        conn.commit()
        if claimed.rowcount != 1:                                               # the same press twice (a double tap, a resent form)
            flash("Эта продажа уже выполнена.")
            return redirect(back)
        try:
            sold = oth_point_sale.sell(conn, card["id"], pieces, form.get("name"), form.get("phone"), form.get("number"), form.get("transfer"),
                                       form.get("anonymous") == "1", now, author())
            error = ""
        except orders.OrderError as err:
            error = str(err)
        except Exception:                                                        # the press must not stay «обрабатывается»
            conn.rollback()
            current_app.logger.exception("«Рабочее место»: the sale from «На ОТХ» of order %s failed", card_id)
            error = "Не получилось выполнить продажу. Напишите владельцу."
        if error:                                                               # the same window again: a new press may go
            conn.execute("DELETE FROM order_command_log WHERE message_id = ?", ("page-" + token,))
            conn.commit()
            return _sale_window(orders.get_card(conn, card_id), point_id, back, form, error), 400
        outcome = "продано с ОТХ: заказ №%d" % sold
        conn.execute("UPDATE order_command_log SET outcome = ? WHERE message_id = ?", (outcome, "page-" + token))
        conn.commit()
        point_log.record(conn, now, author(), point_id, "oth_sold", card["id"], outcome)     # «История точки»
        flash("Заказ №%s — продано с ОТХ: заказ №%d на этапе «%s»." % (mgr.label(card), sold, orders.stage_name(conn, "done")), "info")
        return redirect(back)

    def _sale_window(card, point_id, back, form, error):
        from urllib.parse import parse_qs, urlsplit
        conn = get_conn()
        rows = oth_point_sale.lines(conn, card)
        for row in rows:
            typed = str(form.get("qty_%d" % row["item_id"]) or "") if form else ""
            row["qty"] = int(typed) if typed.isdigit() and int(typed) <= row["max"] else row["max"]
            row["unit_text"], row["sum_text"] = _rub(row["unit"]), _rub(row["unit"] * row["qty"])
        moment = local_iso()
        return render_template("mypoint_oth_sale.html", card=card, label=mgr.label(card), rows=rows, error=error, form=form or {},
                               number=form.get("number", card["webmim_number"]) if form else card["webmim_number"],
                               transfer=form.get("transfer", card["transfer_number"]) if form else card["transfer_number"],
                               total=_rub(sum(r["unit"] * r["qty"] for r in rows)), when="%s.%s.%s %s" % (moment[8:10], moment[5:7], moment[:4], moment[11:16]),
                               token=secrets.token_hex(12), single=len(rows) == 1, bonus=oth_point_sale.BONUS, no_contact=oth_point_sale.NO_CONTACT,
                               p=point_id if own_point() is None else None, back=back,
                               tab=(parse_qs(urlsplit(back).query).get("tab") or [""])[0])

    @app.route("/my-point/return-day/not-taken", methods=["POST"])
    @login_required
    def mypoint_not_taken():
        """«Водитель не забрал» of «Сегодня возврат!» (G, the owner 2026-11-12): the point's own date of today only; the comment is optional.
        The office gets one task and a row of its bell (oth_point_return.driver_not_taken); the window says what to do next."""
        conn = get_conn()
        point_id = _point_id(conn)
        try:
            point = points.get_point(conn, point_id)
        except points.PointError:
            abort(403)
        now = local_iso()
        back = _back(point_id)
        answer, ok, new = oth_point_return.driver_not_taken(conn, point_id, point["name"], str(request.form.get("comment") or "")[:oth_point_return.COMMENT_MAX * 2],
                                                       author(), now)
        if new:                                                                 # «История точки»: what was done, not a repeat
            point_log.record(conn, now, author(), point_id, "not_taken", None, answer)
        flash(answer, "info" if ok else "error")
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

    @app.route("/my-point/return-act/<int:act_id>")
    @login_required
    def mypoint_return_act(act_id):
        """«Скачать акт МХ-3» of a return (oth_return_act): the file of the person's own point only (the office: of the point it looks at,
        ?p=); another point's id is a 404, as one that is not there. «Распечатать» (?print=1, the task of 2026-11-12): the same file with
        the same rights, shown in the browser to print instead of saved."""
        from .routes_catalog_oth import act_response
        conn = get_conn()
        act = oth_return_act.file(conn, act_id)
        if act is None or act["point_id"] != _point_id(conn):
            abort(404)
        return act_response(act, inline=request.args.get("print") == "1")

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
        from .. import order_call
        calls = order_call.sounds(conn, who, local_iso())                      # one's own tasks «Позвонить…» (working as a point too)
        if point_id is None:
            return jsonify({"who": who, "items": calls})
        from .routes_alerts import HERE, grouped, href, with_orders
        cards, now = _cards(conn, point_id), local_iso()
        items = [{"key": "question-%d-%s" % (card["id"], re.sub(r"\D", "", card["entered_at"] or "")), "title": "Клиент забрал заказ № %s?" % mgr.label(card), "text": card["client_name"] or "",
                  "href": HERE} for card in cards if reserve.question_open(conn, card, now)]      # of the stay: a new stay rings again
        items += [{"key": "oth-question-%d-%s" % (card["id"], re.sub(r"\D", "", card["entered_at"] or "")), "title": "Заказ № %s: выдали или на точке?"
                   % mgr.label(card), "text": "7-й день на ОТХ", "href": "/my-point?tab=%s" % ("oth" if card["stage"]["code"] == "oth" else "return")} for card in _oth_cards(conn, point_id)
                  if order_oth.question_open(conn, card)]
        today = order_oth.return_day(conn, point_id, now)                      # «Сегодня возврат!»: a new key at 09:00, 12:00 and 15:00
        if today and not today["not_taken"]:                                    # «Водитель не забрал» (G): the sound of the date stops
            items.append({"key": "return-day-%s-%s" % (today["date"].replace("-", ""), today["sound"]), "title": order_oth.RETURN_DAY_TITLE,
                          "text": "%s Заказы: %s." % (order_oth.RETURN_DAY_BODY, ", ".join("№ %s" % n for n in today["orders"])),
                          "href": "/my-point?tab=return"})
        items += [{"key": "order-%d" % card["id"], "title": "Заказ № %s едет к вам" % mgr.label(card) if card["stage"]["code"] != "at_point"
                   else "Заказ № %s на точке" % mgr.label(card), "text": card["client_name"] or "",
                   "href": HERE if card["stage"]["code"] == "at_point" else "/my-point"} for card in cards]     # «Едут» is the page itself
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
        return jsonify({"who": who, "items": items + calls})
