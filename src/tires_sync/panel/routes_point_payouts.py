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
        found = point_payouts.items(conn, start, end, point_id)
        summary = {}
        for e in found:
            got = summary.setdefault(e["point_id"], {"point_id": e["point_id"], "point": e["point"], "issue": 0.0, "bonus": 0.0, "kpi": 0.0,
                                                     "total": 0.0, "paid": 0.0, "unpaid": 0.0, "unpaid_items": []})
            got["issue"] = round(got["issue"] + e["issue"], 2)
            got["bonus"] = round(got["bonus"] + e["handoff"] + e["oth"], 2)
            got["kpi"] = round(got["kpi"] + e["kpi"], 2)
            got["total"] = round(got["total"] + e["total"], 2)
            if e["paid"]:
                got["paid"] = round(got["paid"] + e["total"], 2)
            else:
                got["unpaid"] = round(got["unpaid"] + e["total"], 2)
                got["unpaid_items"].append("%d|%s" % (e["point_id"], e["item"]))
        shown = [e for e in found if pay == "all" or (pay == "paid") == (e["paid"] is not None)]
        ids = sorted({e["order"] for e in shown if e["order"]})
        cards = {}
        for chunk in range(0, len(ids), 500):
            part = ids[chunk:chunk + 500]
            for cid, number, client in conn.execute("SELECT id, webmim_number, client_name FROM order_card WHERE id IN (%s)" % ",".join("?" * len(part)), part):
                cards[cid] = {"label": number or str(cid), "client": client}
        totals = {k: round(sum(s[k] for s in summary.values()), 2) for k in ("issue", "bonus", "kpi", "total", "paid", "unpaid")}
        return render_template("points_payouts.html", start=start, end=end, today=today, names=names, point_id=point_id, pay=pay,
                               summary=sorted(summary.values(), key=lambda s: s["point"]), totals=totals, rows=shown, cards=cards,
                               requests=point_payouts.open_requests(conn), ledger_from=point_payouts.LEDGER_FROM,
                               back=_back(start, end, str(point_id or ""), pay))

    @app.route(PAGE + "/settle", methods=["POST"])
    @login_required
    def points_payouts_settle():
        """«Рассчитан» of the chosen items («<point>|<item>») of the period: the sums are counted again on the server."""
        conn = get_conn()
        today = local_iso()[:10]
        start, end = _period(request.form, today)
        chosen = []
        for text in request.form.getlist("item"):
            point, _, item = text.partition("|")
            if not point.isdigit() or not item:
                abort(400)
            chosen.append((int(point), item))
        try:
            done = point_payouts.settle(conn, chosen, start, end, local_iso(), author())
            conn.commit()
            flash("Рассчитано: %d поз. на %s." % (done["count"], point_payouts._money(done["amount"])))
        except point_payouts.PayoutError as err:
            flash(str(err))
        return redirect(_back(start, end, request.form.get("p", ""), request.form.get("pay", "active")))
