"""The money of a position of the supplier's unload for an order: sale price of today (brand percent), purchase, terms and fees."""
from __future__ import annotations

import logging

from . import connections, economics, orders, terms as terms_store, webmim_orders
from .webmim import season_from_text
from .transport import urllib_transport
from .panel import economics_view, service


def account_for(conn, chat_row=None, card_id=None, point_id=None):
    """The Avito account whose ad prices a position of an order (the owner 2026-10-26: «калькуляция с объявления аккаунта авито, по которому
    идет обращение»): the account of the chat; else of the order's earliest chat; else of the order's point (or of `point_id`, the point of a
    window not saved yet); else the main one. The first one found decides: an account without a feed has no ad, the main one prices it then.
    None when no account has a feed."""
    feed = [a["id"] for a in connections.list_feed_accounts(conn)]
    if not feed:
        return None
    found = None
    if chat_row is not None:
        row = conn.execute("SELECT account_id FROM avito_chat WHERE id = ?", (chat_row,)).fetchone()
        found = row[0] if row else None
    if found is None and card_id is not None:
        row = conn.execute("SELECT account_id FROM avito_chat WHERE order_id = ? ORDER BY id LIMIT 1", (card_id,)).fetchone()
        found = row[0] if row else None
        if found is None and point_id is None:
            row = conn.execute("SELECT point_id FROM order_card WHERE id = ?", (card_id,)).fetchone()
            point_id = row[0] if row else None
    if found is None and point_id:
        row = conn.execute("SELECT avito_account_id FROM point WHERE id = ?", (point_id,)).fetchone()
        found = row[0] if row else None
    return found if found in feed else feed[0]


def effective_settings(conn, settings, account_id=None):
    """The general settings as the feed of the account sees them (its markup and its brands' markups, once); the main account by default.
    `settings` are the general ones, never those of an account already."""
    if account_id is None:
        account_id = account_for(conn)
    return connections.effective_settings(conn, account_id, settings) if account_id is not None else settings


def item_args(conn, settings, parsed, ref, goods_id, day: str, account_id=None) -> dict:
    """The keyword arguments of orders.add_item for a position of the unload, priced as the ad of `account_id` (account_for; the main one
    by default) from the general `settings`. Raises OrderError when the position or its price is not there."""
    offer = parsed.offers.get(goods_id)
    if offer is None:
        raise orders.OrderError("Этой позиции нет в выгрузке поставщика.")
    only = service._Filtered({offer.goods_id: offer}, getattr(parsed, "issues", {}))
    params = terms_store.get_params(conn)
    rows = economics_view.positions(conn, effective_settings(conn, settings, account_id), only, ref, day, params)
    row = rows[0] if rows else None
    if row is None or not row.get("sale"):
        raise orders.OrderError("Для этой позиции не рассчитана цена продажи: задайте процент бренда.")
    season = economics_view.season_of(offer)
    found = economics.pick_terms(terms_store.list_terms(conn), offer.brand_key, season, offer.diameter, day)
    return dict(goods_id=offer.goods_id, code=offer.code, name=offer.name_orig or offer.name, brand_key=offer.brand_key, season=season,
                diameter=offer.diameter, territory=offer.territory_rn, sale_unit=row["sale"], supplier_price=float(offer.base_price),
                terms=found, fee_pct=params["fee_pct"], promo_pct=params["promo_pct"])


def point_item_args(conn, name, sale_unit) -> dict:
    """The keyword arguments of orders.add_item for a tyre of the point's stock that the unload has not: the name as typed, the price typed
    (required: the panel has none for it), no purchase yet (0, typed by hand later), no supplier's terms; the fees of Avito as for any position."""
    name = " ".join(str(name or "").split())
    if not name:
        raise orders.OrderError("Шина с точки: впишите модель и размер.")
    if not str(sale_unit or "").strip():
        raise orders.OrderError("Шина с точки «%s»: впишите цену за шт., в панели её цены нет." % name[:80])
    season = season_from_text(name).value
    params = terms_store.get_params(conn)
    return dict(goods_id=orders.POINT_GOODS, code="", name=name, brand_key="", season=season if season in ("winter", "summer", "all") else "all",
                diameter=0, territory="", sale_unit=sale_unit, supplier_price=0, terms={}, fee_pct=params["fee_pct"], promo_pct=params["promo_pct"])


def api_client(conn, app_config):
    """The WebMIM orders API client of the first supplier (its server, login and password from «Подключения»); None without the master key."""
    box = app_config.get("SECRET_BOX")
    if box is None:
        return None
    owner = connections.supplier_owner(1)
    config = connections.get_config(conn, "webmim_orders", owner)
    server = config.get("server") if config.get("server") in webmim_orders.SERVERS else webmim_orders.DEFAULT_SERVER
    password = connections.get_secret(conn, box, "webmim_orders", owner, "password") or ""
    return webmim_orders.OrdersClient(webmim_orders.SERVERS[server], config.get("login", ""), password,
                                      http=app_config.get("WEBMIM_HTTP") or urllib_transport)


def webmim_purchase(conn, card: dict, parsed, app_config):
    """(goods_id → purchase of one, source) for the positions of an order placed in WebMIM.
    The switch WEBMIM_PURCHASE_API (off by default; the live API is not given yet) takes the prices of the WebMIM order itself by its «ID заказа WebMIM»
    (webmim_orders.OrdersClient.order_items). Off, without the id or when the API fails: the supplier's price of today's unload, the same the position took
    when it was added. TODO: switch on once the owner has the live API user (docs/integrations/webmim-orders-api-v2.md)."""
    if app_config.get("WEBMIM_PURCHASE_API") and str(card.get("webmim_id") or "").strip():
        try:
            client = api_client(conn, app_config)
            if client is not None:
                return client.order_items(card["webmim_id"]), "webmim_api"
        except Exception as err:                    # the network (OSError), the key, the answer: saving the card never depends on WebMIM
            logging.getLogger(__name__).warning("WebMIM purchase of order %s: %s; the unload is used", card.get("id"), err)
    offers = getattr(parsed, "offers", {}) or {}
    prices = {}
    for item in card.get("items") or []:
        offer = offers.get(item["goods_id"])
        if offer is not None and offer.base_price:
            prices[item["goods_id"]] = float(offer.base_price)
    return prices, "unload"
