"""«Аналитика»: the sales and the profit of a period (only with the money right), the funnel «chat → order → issued» whole and by a slice,
the time on the stages and the orders stuck now. Read only: the figures are those of analytics.py."""
from __future__ import annotations

from datetime import date, timedelta

from flask import render_template, request

from .. import analytics, order_report
from .common import get_conn, has_right, now

SLICE_NAMES = {"": "Целиком", "source": "По источнику", "account": "По аккаунту Авито", "responsible": "По ответственному", "point": "По точке"}


def register(app) -> None:
    @app.route("/analytics")
    def analytics_page():
        conn = get_conn()
        local = (now() + timedelta(hours=3)).replace(tzinfo=None)
        today = local.date()
        start, end = request.args.get("from", "").strip()[:10], request.args.get("to", "").strip()[:10]
        try:
            start, end = date.fromisoformat(start).isoformat(), date.fromisoformat(end).isoformat()
            if start > end:
                start, end = end, start
        except ValueError:
            start, end = (today - timedelta(days=29)).isoformat(), today.isoformat()
        by = request.args.get("by", "")
        by = by if by in analytics.SLICES else ""
        return render_template("analytics.html", start=start, end=end, by=by, slices=SLICE_NAMES,
                               money=analytics.money(conn, start, end) if has_right("money") else None,
                               labels=order_report.share_labels(conn),
                               funnel=analytics.funnel(conn, start, end, by or None),
                               times=analytics.stage_times(conn, start, end, local.isoformat(timespec="seconds")))
