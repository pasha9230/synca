"""The filters, the sort, the page and the view of a list are kept in the person's account (user_pref, the key «f.<page>»), so that they
survive a reload and a walk to another page (the owner 2026-10-29). One rule for every page of PAGES, in before_request:
- the page's own parameters chosen on the page itself (its filter form, its tabs, its pages; the Referer is the same page) are saved (only the
  page's keys; nothing saved when all are empty);
- an address with its own parameters from elsewhere (a tile of the home page, a link of another page) opens exactly them: nothing saved is
  mixed in and nothing saved is overwritten (the lead 2026-10-30);
- the bare address coming from elsewhere (the menu, another page, a bookmark) goes to the saved choice (or to the page's default, `default`);
- the bare address from the page itself (its «Сбросить», «Все») is the choice «nothing»: the saved one is forgotten; so is «?reset=1».
Requests of the page's scripts (X-Requested-With: fetch) are never touched. Each login has its own (a point, a client: their pages, their
values); the keys a point cannot choose (the page fixes them to its own) are never saved for it."""
from __future__ import annotations

from urllib.parse import urlencode, urlsplit

from flask import current_app, redirect, request, session

from .. import user_prefs

# endpoint: (the keys of its choice, those that do not count as «a filter is on» (the view, the sort, the page), the keys a point never keeps)
PAGES = {
    "catalog": ("w h d size q s t st b xb o r sup n p", "o r n p", ""),
    "chats_page": ("f acc q", "", "acc"),
    "orders_page": ("v p t q s page", "v page", "p"),
    "orders_report": ("from to pt", "", ""),
    "deleted_page": ("k", "", ""),
    "economics_page": ("s d sup b", "", ""),
    "economics_sales": ("p from to b a pt g rev", "rev", ""),
    "economics_funnel": ("p c pg", "pg", ""),
    "history": ("f", "", ""),
    "history_ads": ("f q pg", "pg", ""),
    "history_points": ("p", "", ""),
    "analytics_page": ("from to by", "by", ""),
    "calls_page": ("days missed q account", "", "account"),
    "mappings": ("q b a c", "", ""),
    "photos_review": ("tab show", "tab", ""),
    "clients_page": ("q tag", "", "tag"),
    "mypoint_page": ("tab q p", "tab", "p"),
    "shop_page": ("size q s t wh b xb o r p", "o r p", ""),
    "new_positions": ("days", "", ""),
}
# a group of pages that are one place: «Настройки» opens the tab last looked at, its tabs move between themselves freely
# (the tabs, the other pages under the same head of tabs: their links to a tab are moves inside the place too)
TABS = {"settings": (("settings_page", "settings_brands_page", "settings_exclusions_page"), ("settings_brand_card",))}
VALUE_MAX = 200
SEARCH = ("q",)          # the text of a search is never kept (the owner 2026-10-30): the rest of the choice is, as before


def _key(endpoint: str) -> str:
    return "f." + endpoint


def _keys(endpoint: str, role: str) -> list:
    keys, _, point_skip = PAGES[endpoint]
    return [k for k in keys.split() if not (role == "point" and k in point_skip.split())]


def _from_same(endpoints) -> bool:
    """The request comes from a page of `endpoints` (its own link: «Сбросить», a tab), by the Referer of our own host."""
    parts = urlsplit(request.headers.get("Referer", ""))
    if not parts.path or (parts.netloc and parts.netloc != request.host):
        return False
    try:
        endpoint, _ = current_app.url_map.bind(request.host).match(parts.path, method="GET")
    except Exception:
        return False
    return endpoint in endpoints


def _chosen(endpoint: str, role: str) -> dict:
    """The page's own parameters of this address, empty values left out: {key: [values]}."""
    out = {}
    for key in _keys(endpoint, role):
        if key in SEARCH:
            continue
        values = [v[:VALUE_MAX] for v in request.args.getlist(key) if v.strip()][:50]
        if values:
            out[key] = values
    return out


def remember():
    """before_request, after check_access (the person may open the page): save or bring back the choice of a list page."""
    endpoint, login = request.endpoint, session.get("user")
    if request.method != "GET" or not login or request.headers.get("X-Requested-With") == "fetch":
        return None
    posted = session.get(POSTED) == request.path                      # this GET is the redirect after a form (PRG), not a link of the page
    if posted:
        session.pop(POSTED)
    from .common import current_user, get_conn
    for group, (pages, members) in TABS.items():
        if endpoint in pages:
            conn = get_conn()
            saved = user_prefs.get(conn, login, _key(group))
            if endpoint == pages[0] and not request.args and not _from_same(pages + members):
                if saved in pages[1:]:
                    return redirect(current_app.url_map.bind(request.host).build(saved))
            if saved != endpoint:
                user_prefs.put(conn, login, _key(group), endpoint, _now())
            return None
    if endpoint not in PAGES:
        return None
    conn, role = get_conn(), current_user()["role"]
    if request.args.get("reset"):
        _forget(conn, login, endpoint)
        return redirect(request.path)
    if request.args.get("saved"):                            # «Мои фильтры» of a one-off view: the saved choice (or the page's default)
        return redirect(_saved_url(conn, login, endpoint, role) or request.path + "?reset=1")
    if any(k in request.args for k in _keys(endpoint, role)):
        if not _from_same((endpoint,)):
            return None
        chosen = _chosen(endpoint, role)
        if chosen != user_prefs.get(conn, login, _key(endpoint), {}):
            try:
                if chosen:
                    user_prefs.put(conn, login, _key(endpoint), chosen, _now())
                else:
                    _forget(conn, login, endpoint)
            except user_prefs.PrefError:                    # a choice too large to keep: the page still opens, nothing saved
                pass
        return None
    if request.args or request.view_args:                    # a one-off address (a chat opened, a window): neither saved nor replaced
        return None
    if _from_same((endpoint,)) and not posted:
        _forget(conn, login, endpoint)
        return None
    target = _saved_url(conn, login, endpoint, role)
    return redirect(target) if target else None


def after_post(response):
    """after_request: a form sent (POST) and answered with a redirect marks the address it goes to as its PRG: that bare list address keeps
    the saved filters (it is not «Сбросить» of the page, though the browser names the list page as the Referer). The mark is the path, so
    a GET of another tab in between does not take it."""
    if request.method == "POST" and response.status_code in (301, 302, 303) and session.get("user") \
            and request.headers.get("X-Requested-With") != "fetch":
        session[POSTED] = urlsplit(response.headers.get("Location", "")).path
    return response


POSTED = "pp_post"


def _forget(conn, login: str, endpoint: str) -> None:
    if user_prefs.get(conn, login, _key(endpoint)) is not None:
        user_prefs.drop(conn, login, _key(endpoint))


def _saved(conn, login: str, endpoint: str, role: str) -> dict:
    saved = user_prefs.get(conn, login, _key(endpoint), {})
    keys = _keys(endpoint, role)
    return {k: v for k, v in saved.items() if k in keys and k not in SEARCH and isinstance(v, list)} if isinstance(saved, dict) else {}


def _saved_url(conn, login: str, endpoint: str, role: str) -> str:
    """The address of the page with the saved choice (or with `default`), "" when there is none."""
    saved = _saved(conn, login, endpoint, role) or default(conn, login, endpoint)
    return request.path + "?" + urlencode([(k, v) for k, vs in saved.items() for v in vs]) if saved else ""


def default(conn, login: str, endpoint: str) -> dict:
    """The choice a page opens with when the person has saved none ({key: [values]}; {} = the page's own default). The one place for it:
    the chats will open the person's own Avito account here once a person is bound to one (the lead 2026-10-30: «ответственный по
    умолчанию за аккаунт», later); until then every page, the chats too (all the accounts), opens with its own default."""
    return {}


def _now() -> str:
    from .common import now_iso
    return now_iso()


def state() -> dict:
    """For the page's mark «Фильтр включён»: how many filters of the address are on and the link that resets them; {} on other pages."""
    endpoint = request.endpoint
    if endpoint not in PAGES or not session.get("user"):
        return {}
    from .common import current_user, get_conn
    role = current_user()["role"]
    chosen = _chosen(endpoint, role)
    neutral = PAGES[endpoint][1].split()
    count = len([k for k in chosen if k not in neutral])
    if not count:
        return {}
    saved = _saved(get_conn(), session["user"], endpoint, role)
    if saved and saved != chosen:                            # a one-off view (a tile, a link): its reset must not wipe the saved choice
        return {"count": count, "mine": request.path + "?saved=1"}
    return {"count": count, "reset": request.path + "?reset=1"}
