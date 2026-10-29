"""«Каталог для клиента» (/shop): the only page of a client (a buyer, the owner 2026-10-29), in its own small frame (shop_base.html: no menu,
no search, no bell, no counters). Build 1 is the stub; the catalog with the client's own prices comes with build 2 (client_catalog)."""
from __future__ import annotations

from flask import render_template


def register(app) -> None:
    @app.route("/shop")
    def shop_page():
        return render_template("shop.html")
