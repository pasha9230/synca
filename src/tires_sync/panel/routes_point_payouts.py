"""«Пункты выдачи» (ТЗ 91, 95): the office's ledger of what each point is owed — the fee of the issued orders, the bonuses, the KPI premiums
(point_payouts, one count with the report) — and «Рассчитан», by the order or for a whole point. Who sees the money (the section «analytics»
needs the right «money»); «Рассчитан» writes, so it is of the section «orders» with the right «money» (the owner and the partner)."""
from __future__ import annotations

from datetime import date

from flask import abort, flash, redirect, render_template, request

from .. import order_report, point_payouts
from .common import author, get_conn, login_required
from .routes_orders import local_iso

PAGE = "/points/payouts"


def _period(args, today: str) -> tuple:
    def day(name, default):
        try:
            return date.fromisoformat(str(args.get(name, "")).strip()[:10]).isoformat()
        except ValueError:
            return default
    start, end = day("from", point_payouts.LEDGER_FROM), day("to", today)
    return (end, start) if start > end else (start, end)


def _back(start: str, end: str, point: str, pay: str) -> str:
    from urllib.parse import urlencode
    return PAGE + "?" + urlencode({"from": start, "to": end, "p": point, "pay": pay})


def register(app) -> None:
    app.jinja_env.globals["source_point_for"] = lambda card_id: order_report.source_point(get_conn(), card_id)

    @app.route(PAGE)
    @login_required
    def points_payouts_page():
        conn = get_conn()
        today = local_iso()[:10]
        start, end = _period(request.args, today)
        names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM point ORDER BY name")}
        chosen = request.args.get("p", "").strip()
        point_id = int(chosen) if chosen.isdigit() and int(chosen) in names else None
        pay = request.args.get("pay", "") if request.args.get("pay", "") in ("active", "paid", "all") else "active"
        report = point_payouts.ledger_report(conn, start, end, today)       # one count for the page, the same as «Рассчитан»
        found = point_payouts.items(conn, start, end, point_id, report)
        summary = {}
        for e in found:
            got = summary.setdefault(e["point_id"], {"point_id": e["point_id"], "point": e["point"], "issue": 0.0, "bonus": 0.0, "kpi": 0.0,
                                                     "adj": 0.0, "total": 0.0, "paid": 0.0, "due": 0.0, "due_items": [], "paid_items": []})
            if e["paid"]:
                got["paid_items"].append("%d|%s" % (e["point_id"], e["item"]))
            for key, value in (("issue", e["issue"]), ("bonus", e["handoff"] + e["oth"]), ("kpi", e["kpi"]), ("adj", e["adj"]),
                               ("total", e["total"]), ("paid", e["paid_sum"]), ("due", e["due"])):
                got[key] = round(got[key] + value, 2)
            if e["due"]:
                got["due_items"].append("%d|%s" % (e["point_id"], e["item"]))
        if pay == "all":
            shown = found
        elif pay == "paid":
            shown = [e for e in found if e["paid"]]
        else:
            shown = [e for e in found if e["due"]]
        ids = sorted({e["order"] for e in shown if e["order"]})
        cards = {}
        for chunk in range(0, len(ids), 500):
            part = ids[chunk:chunk + 500]
            for cid, number, client in conn.execute("SELECT id, webmim_number, client_name FROM order_card WHERE id IN (%s)" % ",".join("?" * len(part)), part):
                cards[cid] = {"label": number or str(cid), "client": client}
        totals = {k: round(sum(s[k] for s in summary.values()), 2) for k in ("issue", "bonus", "kpi", "adj", "total", "paid", "due")}
        waiting = [w for w in order_report.waiting(conn, today, point_id) if w["point_id"] in names]   # «к начислению»: not in the sums
        more = sorted({w["order"] for w in waiting} - set(cards))
        for chunk in range(0, len(more), 500):
            part = more[chunk:chunk + 500]
            for cid, number, client in conn.execute("SELECT id, webmim_number, client_name FROM order_card WHERE id IN (%s)" % ",".join("?" * len(part)), part):
                cards[cid] = {"label": number or str(cid), "client": client}
        return render_template("points_payouts.html", start=start, end=end, today=today, names=names, point_id=point_id, pay=pay,
                               waiting=waiting, waiting_sum=round(sum(w["expected"] for w in waiting), 2),
                               summary=sorted(summary.values(), key=lambda s: s["point"]), totals=totals, rows=shown, cards=cards,
                               requests=point_payouts.open_requests(conn, today, report), ledger_from=point_payouts.LEDGER_FROM,
                               back=_back(start, end, str(point_id or ""), pay), fix_parts=order_report.FIX_PARTS)

    def _done():
        return redirect(_back(*_period(request.form, local_iso()[:10]), request.form.get("p", ""), request.form.get("pay", "active")))

    @app.route(PAGE + "/settle", methods=["POST"])
    @login_required
    def points_payouts_settle():
        """«Рассчитан» of the chosen items («<point>|<item>») of the period: the sums are counted again on the server; «Рассчитан (всё)»
        sends the sum the person confirmed (`expect`): another sum now is refused and shown again."""
        conn = get_conn()
        start, end = _period(request.form, local_iso()[:10])
        chosen = []
        for text in request.form.getlist("item"):
            point, _, item = text.partition("|")
            if not point.isdigit() or not item:
                abort(400)
            chosen.append((int(point), item))
        expect = request.form.get("expect", "").strip()
        try:
            done = point_payouts.settle(conn, chosen, start, end, local_iso(), author(), expect=float(expect) if expect else None,
                                        today=local_iso()[:10])
            conn.commit()
            flash("Рассчитано: %d поз. на %s." % (done["count"], point_payouts._money(done["amount"])))
        except ValueError as err:
            conn.rollback()
            flash(str(err) if isinstance(err, point_payouts.PayoutError) else "Сумма подтверждения неверна.")
        return _done()

    @app.route(PAGE + "/unsettle", methods=["POST"])
    @login_required
    def points_payouts_unsettle():
        """«Отменить расчёт» of one item (the owner 2026-11-25): it is owed again."""
        conn = get_conn()
        point, _, item = request.form.get("item", "").partition("|")
        if not point.isdigit() or not item:
            abort(400)
        try:
            amount = point_payouts.unsettle(conn, int(point), item, local_iso(), author())
            conn.commit()
            flash("Расчёт отменён: %s снова к выплате." % point_payouts._money(amount))
        except point_payouts.PayoutError as err:
            flash(str(err))
        return _done()

    @app.route(PAGE + "/bulk", methods=["POST"])
    @login_required
    def points_payouts_bulk():
        """The office's operations on the chosen items (a row, the rows checked, a whole point; the owner 2026-11-25), every article:
        «Вернуть в долг» (unsettle), «Отменить начисление» (0 with a reason), «Изменить сумму» (an article, the sum, a reason), «Вернуть по
        расчёту» (the sums set are taken back). «Рассчитан» is /settle."""
        conn = get_conn()
        chosen = []
        for text in request.form.getlist("item"):
            point, _, item = text.partition("|")
            if not point.isdigit() or not item:
                abort(400)
            chosen.append((int(point), item))
        action, now = request.form.get("action", ""), local_iso()
        period = _period(request.form, now[:10])
        from ..amounts import parse_amount
        try:
            if not chosen:
                raise point_payouts.PayoutError("Отметьте позиции.")
            if action == "unsettle":
                done = point_payouts.unsettle_many(conn, chosen, now, author())
                text = "Возвращено в долг: %d поз. на %s." % (done["count"], point_payouts._money(done["amount"]))
            elif action == "cancel":
                done = point_payouts.fix_items(conn, chosen, "all", 0, request.form.get("reason", ""), now, author(), *period, today=now[:10])
                text = "Начисление отменено: %d поз." % done
            elif action == "fix":
                try:
                    amount = parse_amount(request.form.get("amount", ""))
                except ValueError:
                    raise point_payouts.PayoutError("Сумма: нужно число.")
                done = point_payouts.fix_items(conn, chosen, request.form.get("part", ""), amount, request.form.get("reason", ""), now, author(),
                                               *period, today=now[:10])
                text = "Сумма изменена: %d поз." % done
            elif action == "unfix":
                done = point_payouts.unfix_items(conn, chosen, now, author())
                text = "Снова по расчёту: %d поз." % done
            else:
                abort(400)
            conn.commit()
            flash(text)
        except point_payouts.PayoutError as err:
            conn.rollback()
            flash(str(err))
        return _done()

    @app.route(PAGE + "/adjust", methods=["POST"])
    @login_required
    def points_payouts_adjust():
        """«Корректировка» (the owner 2026-11-25): ± to what a point is paid, with a reason; the counted sums stay."""
        conn = get_conn()
        today = local_iso()[:10]
        from ..amounts import parse_amount
        typed = request.form.get("amount", "").strip()
        counted = request.form.get("pieces", "").strip()                  # «по ручному подсчёту»: tyres × the rate
        try:
            amount = parse_amount(typed) if typed or not counted else None
            pieces = None
            if counted:
                if not (counted.isascii() and counted.isdigit()):              # «²» and the like are digits for str.isdigit
                    raise point_payouts.PayoutError("Количество по ручному подсчёту: целое число шин.")
                pieces = int(counted)
            day = _period({"from": request.form.get("day", ""), "to": today}, today)[0]
            point_payouts.adjust(conn, int(request.form.get("point_id", "0") or 0), amount, request.form.get("reason", ""), day,
                                 local_iso(), author(), pieces=pieces)
            conn.commit()
            flash("Корректировка записана.")
        except ValueError as err:
            flash(str(err) if isinstance(err, point_payouts.PayoutError) else "Корректировка: нужно число.")
        return _done()

    @app.route(PAGE + "/request/<int:request_id>/close", methods=["POST"])
    @login_required
    def points_payouts_request_close(request_id):
        """The office closes a request of a point (the cash checked)."""
        conn = get_conn()
        if point_payouts.close_request(conn, request_id, local_iso(), author()):
            conn.commit()
            flash("Запрос точки закрыт.")
        return _done()
