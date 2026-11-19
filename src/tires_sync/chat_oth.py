"""Internal availability hints for the tire discussed in an Avito chat."""
import re

from . import catalog_archive, order_wait, oth_stock


def _words(text):
    return " ".join(re.findall(r"[^\W_]+", str(text or "").casefold()))


def _contains(text, name):
    words = _words(name)
    return bool(words) and f" {words} " in f" {text} "


def _size(offer):
    return offer.width, offer.height, offer.diameter


def _text_size(text):
    found = order_wait.SIZE_IN_TITLE.search(text or "")
    return tuple(map(int, found.groups())) if found else None


def _brand(offer):
    return offer.brand_raw or order_wait._offer_brand(offer)


def _model_key(offer):
    return order_wait._offer_brand(offer), _words(offer.model_raw)


def _identity(text, offers):
    """Recognize catalog names only when a unique brand or model is mentioned."""
    words = _words(text)
    brands, models, labels = {}, {}, {}
    for offer in offers:
        brand = order_wait._offer_brand(offer)
        labels[brand] = _brand(offer)
        if _contains(words, labels[brand]) or _contains(words, brand):
            brands[brand] = labels[brand]
        if _contains(words, offer.model_raw):
            models[_model_key(offer)] = offer.model_raw
    if brands:
        models = {key: label for key, label in models.items() if key[0] in brands}
    # A complete numbered model must win over its unnumbered name in the same text.
    models = {key: label for key, label in models.items() if not any(
        brand == key[0] and model != key[1] and _contains(model, key[1]) for brand, model in models)}
    if len(models) == 1:
        key, model = next(iter(models.items()))
        return labels[key[0]], model, key
    return next(iter(brands.values())) if len(brands) == 1 else "", "", None


def suggest(conn, live, ad_id, title, messages, point_id=None):
    """Use the latest explicit size from the customer, otherwise a recognized model or the ad."""
    ad = (live.get(ad_id) or catalog_archive.last_offer(conn, ad_id)) if ad_id else None
    incoming = [m["text"] for m in reversed(messages) if m["direction"] == "in" and m["kind"] == "text"]
    request = next(((text, size) for text in incoming if (size := _text_size(text))), None)
    text, size = request or (title or "", _text_size(title))
    if ad and not request:
        size = _size(ad)

    stock = {}
    for row in oth_stock._rows(conn):
        if point_id is not None and row["point_id"] != point_id:
            continue
        key = (row["goods_id"], row["point_id"])
        if key not in stock:
            stock[key] = dict(row)
        else:
            stock[key]["qty"] += row["qty"]

    inventory = {goods_id: live.get(goods_id) or catalog_archive.last_offer(conn, goods_id)
                 for goods_id in {row["goods_id"] for row in stock.values()}}
    # Without a size, identify only models known from the ad or actual OTH stock.
    known = [offer for offer in inventory.values() if offer is not None]
    if ad:
        known.append(ad)
    model_request = None
    if not request and known:
        for message in incoming[:10]:
            identity = _identity(message, known)
            if identity[2]:
                model_request = identity
                break
    from_message = bool(request or model_request)
    if model_request:
        brand, model, wanted = model_request
        size = _size(ad) if ad and _model_key(ad) == wanted else None
    elif ad and not request:
        brand, model, wanted = _brand(ad), ad.model_raw, _model_key(ad)
    elif text:
        candidates = {**inventory, **live}
        offers = (o for o in candidates.values() if o and _size(o) == size) if size else known
        brand, model, wanted = _identity(text, offers)
        if not brand:
            brand = order_wait.line_from_title(text, {order_wait._offer_brand(o) for o in live.values()}).get("brand", "")
    elif not ad_id:
        return None
    else:
        brand, model, wanted = "", "", None
    exact, alternatives = [], []
    for row in stock.values():
        offer = inventory[row["goods_id"]]
        is_ad = row["goods_id"] == ad_id and not from_message
        row_size = _size(offer) if offer else _text_size(row["name"])
        known_model = wanted and wanted[1] and offer is not None and _model_key(offer) == wanted
        if not is_ad and (row_size != size if size else not known_model):
            continue
        if known_model and ad and not from_message:
            known_model = (offer.season, offer.studded, offer.runflat) == (ad.season, ad.studded, ad.runflat)
        match = exact if is_ad or known_model else alternatives
        match.append({"name": offer.name if offer else row["name"] or row["goods_id"],
                      "point": row["point"], "qty": row["qty"]})

    if not size and not exact:
        return None
    for matches in (exact, alternatives):
        matches.sort(key=lambda row: (row["name"].casefold(), (row["point"] or "").casefold()))
    return {"brand": brand, "model": model, "size": "%s/%s R%s" % size if size else "",
            "source": "из сообщения" if from_message else "из объявления", "exact": exact, "alternatives": alternatives}
