"""«Аналитика рекламы» (the owner 2026-11-11, stage 1: read only): the spending on Avito per ad of the feed accounts against its sales, the
ДРР and the prices of a view. The section «analytics» with the money right (the owner and the partner); the figures are those of ads_spend."""
from __future__ import annotations

from flask import render_template, request

from .. import ads_spend
from .common import get_conn, local_today, login_required

PERIOD_NAMES = {7: "7 дней", 30: "30 дней", 90: "90 дней"}
SORT_NAMES = {"spend": "По расходам", "drr": "По ДРР от выручки", "drr_margin": "По ДРР от маржи"}


def register(app) -> None:
    @app.route("/economics/ads")
    @login_required
    def ads_analytics_page():
        try:
            days = int(request.args.get("p", ""))
        except ValueError:
            days = 30
        days = days if days in ads_spend.PERIODS else 30
        sort = request.args.get("s", "")
        data = ads_spend.report(get_conn(), local_today(), days, sort if sort in ads_spend.SORTS else "spend")
        return render_template("ads_analytics.html", data=data, periods=PERIOD_NAMES, sorts=SORT_NAMES, rows_max=ads_spend.ROWS_MAX)
