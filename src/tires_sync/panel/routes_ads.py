"""«Аналитика рекламы» (the owner 2026-11-11, stage 1: read only): the spending on Avito per ad of the feed accounts against its sales, the
ДРР and the prices of a view. The section «analytics» with the money right (the owner and the partner); the figures are those of ads_spend.
2026-11-12, «как в Roistat»: the tree of groups of ads_tree (the levels `g`, the sort `s` and `d`, the filter `f`, the period `p` are in the
address; the levels and «Колонки» are kept for the person)."""
from __future__ import annotations

from urllib.parse import urlencode

from flask import abort, redirect, render_template, request

from .. import ads_spend, ads_tree, users
from .common import current_user, get_conn, local_today, login_required, now_iso, safe_path

PERIOD_NAMES = {7: "7 дней", 30: "30 дней", 90: "90 дней"}
FILTER_NAMES = {"": "Все", "sales": "Только с продажами", "spend": "Только с расходами"}
REASON_NAMES = {ads_spend.NO_CHAT: "у заказа нет чата Авито и звонка", ads_spend.NO_ITEM: "в чате заказа нет объявления",
                ads_spend.NO_FEED: "чат или звонок аккаунта без фида", ads_spend.CALL_NO_ITEM: "звонок без объявления, аккаунт без фида",
                ads_spend.MANUAL: "продажа вручную"}
PATH = "/economics/ads"


def address(p: int, g, s: str, d: str, f: str) -> str:
    """The address of the page with this choice: the default sort (by the spending, the most first) is spelled too, «d=asc» only when up."""
    query = [("p", p), ("g", ",".join(g)), ("s", s)] + ([("d", "asc")] if d == "asc" else []) + ([("f", f)] if f else [])
    return PATH + "?" + urlencode(query, safe=",")


def register(app) -> None:
    @app.route(PATH)
    @login_required
    def ads_analytics_page():
        conn, login = get_conn(), current_user()["login"]
        try:
            days = int(request.args.get("p", ""))
        except ValueError:
            days = 30
        days = days if days in ads_spend.PERIODS else 30
        asked = request.args.getlist("l") and ",".join(request.args.getlist("l")) or request.args.get("g", "")
        levels = ads_tree.parse_levels(asked)
        if levels:
            ads_tree.save_levels(conn, login, levels, now_iso())
        else:
            levels = ads_tree.saved_levels(conn, login)
        sort = request.args.get("s", "")
        desc = request.args.get("d", "") != "asc"
        only = request.args.get("f", "")
        data = ads_tree.build(conn, local_today(), days, levels, sort, desc, only)
        hidden = set(ads_tree.hidden_columns(conn, login))
        shown = [c for c in ads_tree.COLUMNS if c[0] not in hidden and c[0] in data["filled"] | {"spend"}]
        state = {"p": data["days"], "g": data["levels"], "s": data["sort"], "d": "" if data["desc"] else "asc", "f": data["only"]}

        def url(**changes):
            return address(**dict(state, **changes))

        return render_template("ads_analytics.html", data=data, periods=PERIOD_NAMES, filters=FILTER_NAMES, reasons=REASON_NAMES,
                               levels=ads_tree.LEVELS, columns=ads_tree.COLUMNS, shown=shown, hidden=hidden, url=url,
                               here=url(), no_data=ads_tree.NO_DATA)

    @app.route(PATH + "/columns", methods=["POST"])
    @login_required
    def ads_analytics_columns():
        """«Колонки»: the columns shown, kept for the person; back to the page with the same choice."""
        role = current_user()["role"]
        if users.level(role, "analytics") < users.VIEW or not users.has_right(role, "money"):     # one's own setting of a page one sees
            abort(403)
        ads_tree.save_columns(get_conn(), current_user()["login"], request.form.getlist("show"), now_iso())
        back = safe_path(request.form.get("back", ""))
        return redirect(back if back.startswith(PATH) else PATH)
