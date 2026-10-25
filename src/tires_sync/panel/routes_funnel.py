"""«Воронка» в юнит-экономике: просмотры → контакты → продажи → маржа по позиции и первое место, где она проседает."""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

from flask import render_template, request

from .. import funnel
from .common import get_conn, local_today, login_required

PERIODS = ((7, "7 дней"), (14, "14 дней"), (30, "30 дней"))
PAGE_SIZE = 100


def register(app) -> None:
    @app.route("/economics/funnel")
    @login_required
    def economics_funnel():
        conn = get_conn()
        today = local_today()
        try:
            days = int(request.args.get("p", ""))
        except ValueError:
            days = 14
        days = days if days in dict(PERIODS) else 14
        since = (today - timedelta(days=days - 1)).isoformat()
        items = funnel.rows(conn, since, today.isoformat(), today)
        total = funnel.summarize(items)
        pick = request.args.get("c", "")
        shown = [r for r in items if r["code"] == pick] if pick in funnel.CODES else items
        pages = max(1, -(-len(shown) // PAGE_SIZE))
        try:
            page = min(max(int(request.args.get("pg", "1")), 1), pages)
        except ValueError:
            page = 1
        base = {k: v for k, v in (("p", days), ("c", pick)) if v}
        return render_template(
            "economics_funnel.html", rows=shown[(page - 1) * PAGE_SIZE: page * PAGE_SIZE], total=total, days=days, periods=PERIODS,
            pick=pick, labels=funnel.LABELS, codes=funnel.CODES, collected=funnel.last_collected(conn), page=page, pages=pages,
            pager={n: "/economics/funnel?" + urlencode(dict(base, pg=n)) for n in range(1, pages + 1)} if pages > 1 else {},
            thresholds={"days": funnel.MIN_OBSERVED_DAYS, "views": funnel.MIN_VIEWS, "rate": int(funnel.LOW_CONTACT_RATE * 100),
                        "contacts": funnel.MIN_CONTACTS})
