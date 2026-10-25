"""«Поиск»: the one search of the top bar (tires_sync.search): JSON for the list under the field, a page for Enter."""
from __future__ import annotations

from flask import jsonify, render_template, request

from .. import search, store, tochki_feed
from ..catalog import resolve_brand
from ..settings_store import get_active
from . import service
from .common import current_user, get_conn, login_required, point_account
from .routes_catalog import _gone_view
from .routes_main import context
from .routes_orders import local_now

PAGE_LIMIT = 20                 # the page of Enter shows more of each group than the list under the field


def register(app) -> None:
    def scope():
        user = current_user()
        return search.scope_for(user["role"], user["point_id"], point_account())

    fields = {}                 # goods_id -> (the offer, its Avito brand, offer_fields): the same offer object while the unload is the same

    def positions(text):
        """The positions of the catalog page (the unload as the page shows it, the gone ones after) that its search finds."""
        conn, _, parsed, ref = context(app)
        _, raw = get_active(conn)
        view, _ = tochki_feed.catalog_view(conn, parsed, app.config["UNLOAD_PATH"])
        view = service.hide_excluded(view, raw, conn, ref)          # as routes_catalog.build, without the settings the list does not show
        terms = service.search_terms(text)
        brand_map = store.load_brand_map(conn)
        avito = {}

        def matches(offer):
            if offer.brand_key not in avito:
                avito[offer.brand_key] = (resolve_brand(offer.brand_key, ref, brand_map) or "").lower()
            kept = fields.get(offer.goods_id)
            if kept is None or kept[0] is not offer or kept[1] != avito[offer.brand_key]:      # a new unload, a new name of the brand
                kept = fields[offer.goods_id] = (offer, avito[offer.brand_key], service.offer_fields(offer, avito[offer.brand_key]))
            return service.fields_match(kept[2], terms)
        for offer in view.offers.values():
            if matches(offer):
                yield offer, None
        for offer, info in _gone_view(conn, raw, {}, None, None, set(view.offers), ref):
            if matches(offer):
                yield offer, info

    def answer(limit):
        return search.run(get_conn(), request.args.get("q", ""), scope(), local_now().replace(tzinfo=None), positions, limit)

    @app.route("/search.json")
    @login_required
    def search_json():
        return jsonify(answer(search.LIMIT))

    @app.route("/search", endpoint="search")
    @login_required
    def search_page():
        return render_template("search.html", result=answer(PAGE_LIMIT))
