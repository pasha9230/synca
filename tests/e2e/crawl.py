"""The screen audit: every role signs in, opens every page of its menu (and the tabs on them), every order card and the windows that open on
a click, at 1280×800 and 375×812 with «Новый вид» on, and saves a full-page screenshot of each. Only GET and clicks that open things: no form
is sent but the login one (the temporary base only). The checks of each screen go to <out>/report.json; the report itself is written by a person.

    python tests/e2e/crawl.py [out]            # default out: tests/e2e/shots
    E2E_ROLES=point1,client1 python tests/e2e/crawl.py [out]     # some roles only: <out>/report-<roles>.json (summarize.py merges them)

Chromium: the one of the Playwright package, or E2E_CHROMIUM=<path> (the cloud session has /opt/pw-browsers/chromium)."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlparse

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE)]

import seed  # noqa: E402
import serve  # noqa: E402

VIEWPORTS = {"1280": {"width": 1280, "height": 800}, "375": {"width": 375, "height": 812}}
ROLES = ("owner", "partner1", "office1", "viewer1", "point1", "client1")
NEVER = re.compile(r"/logout|/hook/|/static/|/img/|\.(png|jpe?g|webp|svg|pdf|xlsx|csv|xml|zip)(\?|$)|/download|/export|/feed/|/f/")
TAB_LINKS = (".tabs a[href]", ".tabs2 a[href]", ".seg a[href]", ".mp-tabs a[href]", ".shop2-seg a[href]", "[role=tablist] a[href]",
             ".o2-tabs a[href]")
MENU_LINKS = "aside.sidebar a[href], .topbar a[href], header nav a[href], .shop-nav a[href], .shop2-nav a[href], nav a[href]"
MAX_TABS = 12          # per page: a long list of filters is not a list of screens

# the old palette of panel.css :root that «Строгий» (body.ui-new) redefines: a colour of it on a new-view screen is a leftover
OLD_COLORS = {
    "#7F56D9", "#9E77ED", "#6941C6", "#53389E", "#F9F5FF", "#F4EBFF", "#E9D7FE", "#D6BBFB", "#FCFAFF",
    "#FCFCFD", "#F9FAFB", "#F2F4F7", "#EAECF0", "#98A2B3", "#667085", "#475467", "#344054", "#1D2939", "#101828",
    "#2E90FA", "#0BA5EC", "#EF6820", "#0E9384", "#079455",
}
OLD_FONTS = ("Inter", "Instrument Sans")
SCALE = {"11px", "12px", "13px", "14px", "16px", "18px", "20px", "22px", "24px", "30px"}

UNROLL = (".app{height:auto!important;min-height:100vh}.main{overflow:visible!important}"           # the page scrolls .main
          ".o2c-body{height:auto!important;overflow:visible!important}.o2c-scroll{overflow:visible!important}"   # the order card: its columns
          ".o2-cl-list,.o2-cl-pane,.o2-cl-panel,.o2-cl-msgs{overflow:visible!important;max-height:none!important}")   # chats

PROBE = r"""
(args) => {
  const [old, oldFonts, scale] = args;
  const W = window.innerWidth, out = {};
  const hex = c => { const m = c && c.match(/rgba?\((\d+), (\d+), (\d+)(?:, ([\d.]+))?\)/); if (!m || m[4] === '0') return null;
    return '#' + [m[1], m[2], m[3]].map(x => (+x).toString(16).padStart(2, '0')).join('').toUpperCase(); };
  const name = el => { let s = el.tagName.toLowerCase(); if (el.id) s += '#' + el.id; if (el.classList.length) s += '.' + [...el.classList].slice(0, 3).join('.');
    return s; };
  const path = el => { const p = []; for (let e = el; e && e !== document.body && p.length < 4; e = e.parentElement) p.unshift(name(e)); return p.join(' > '); };
  const noise = el => !!el.closest('.sr-only, .p2-sr, .menu-list, .tour, details:not([open]) > :not(summary), .modal-backdrop:not(.is-open)');
  const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && +s.opacity !== 0; };
  const main = document.querySelector('.main');      // the panel scrolls .main, not the page: .main is the page here
  const inScroller = el => { for (let e = el.parentElement; e && e !== document.body && e !== main; e = e.parentElement) {
    const o = getComputedStyle(e).overflowX; if (o === 'auto' || o === 'scroll' || o === 'hidden' || o === 'clip') return true; } return false; };
  out.bodyClass = document.body.className;
  out.title = document.title;
  out.viewportMeta = (document.querySelector('meta[name=viewport]') || {}).content || '';
  out.innerW = W;
  out.h1 = [...document.querySelectorAll('h1')].map(h => h.innerText.trim()).filter(Boolean);
  out.scrollW = Math.max(document.documentElement.scrollWidth, main ? main.scrollWidth + (W - main.clientWidth) : 0);
  const all = [...document.body.querySelectorAll('*')].filter(visible);
  out.overflow = [];
  if (out.scrollW > W + 1) {
    for (const el of all) { const r = el.getBoundingClientRect();
      if (r.right > W + 1 && !inScroller(el) && ![...el.children].some(c => c.getBoundingClientRect().right > W + 1))
        out.overflow.push({el: path(el), right: Math.round(r.right), text: (el.innerText || '').trim().slice(0, 60)}); }
    out.overflow = out.overflow.slice(0, 12);
  }
  out.clipped = [];
  for (const el of all) { const s = getComputedStyle(el);
    if (!noise(el) && !el.children.length && (el.innerText || '').trim() && (s.overflowX === 'hidden' || s.overflow === 'hidden') && s.textOverflow !== 'ellipsis'
        && el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0)
      out.clipped.push({el: path(el), text: el.innerText.trim().slice(0, 60)}); }
  out.clipped = out.clipped.slice(0, 12);
  const hits = [...document.querySelectorAll('a, button, .btn, input, select, .badge, label')].filter(e => visible(e) && !noise(e)).map(e => [e, e.getBoundingClientRect()]);
  out.overlap = [];
  for (let i = 0; i < hits.length && out.overlap.length < 10; i++) for (let j = i + 1; j < hits.length; j++) {
    const [a, ra] = hits[i], [b, rb] = hits[j]; if (a.contains(b) || b.contains(a)) continue;
    const x = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left), y = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
    if (x > 4 && y > 4 && getComputedStyle(a).position !== 'absolute' && getComputedStyle(b).position !== 'absolute') {
      out.overlap.push({a: path(a), b: path(b), ta: (a.innerText || a.value || '').trim().slice(0, 40), tb: (b.innerText || b.value || '').trim().slice(0, 40)}); break; } }
  const oldHits = {}, fonts = {}, sizes = {};
  for (const el of all) { const s = getComputedStyle(el);
    for (const p of ['color', 'backgroundColor', 'borderTopColor', 'borderLeftColor']) { const h = hex(s[p]);
      if (h && old.includes(h) && (p !== 'borderTopColor' || parseFloat(s.borderTopWidth) > 0) && (p !== 'borderLeftColor' || parseFloat(s.borderLeftWidth) > 0)) {
        const k = h + ' ' + p; (oldHits[k] = oldHits[k] || {n: 0, el: path(el), text: (el.innerText || '').trim().slice(0, 40)}).n++; } }
    if ((el.innerText || '').trim() && !el.children.length) {
      const f = s.fontFamily.split(',')[0].replace(/"/g, '').trim(); if (oldFonts.includes(f)) (fonts[f] = fonts[f] || {n: 0, el: path(el), text: el.innerText.trim().slice(0, 40)}).n++;
      if (!scale.includes(s.fontSize)) (sizes[s.fontSize] = sizes[s.fontSize] || {n: 0, el: path(el), text: el.innerText.trim().slice(0, 40)}).n++; } }
  out.oldColors = oldHits; out.oldFonts = fonts; out.offScale = sizes;
  out.inlineStyles = [...document.querySelectorAll('[style*="#"], font, [color], [bgcolor]')].filter(visible).slice(0, 10)
                      .map(e => ({el: path(e), style: (e.getAttribute('style') || '').slice(0, 100)}));
  const seen = {}; out.repeated = [];
  for (const el of document.querySelectorAll('h1, h2, h3, h4, p, .hint, .muted, .note, .alert, .banner, .flash, .callout, .empty, .lead, .card-title, dt, legend, .badge')) {
    if (!visible(el)) continue; const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
    if (t.length < 14) continue; if (seen[t]) { if (seen[t] === 1) out.repeated.push(t.slice(0, 120)); seen[t]++; } else seen[t] = 1; }
  out.links = [...document.querySelectorAll('a[href]')].map(a => a.href);
  out.modals = [...new Set([...document.querySelectorAll('[data-open-modal]')].filter(b => visible(b) || b.closest('details'))
                .map(b => b.getAttribute('data-open-modal')))];
  out.empty = [...document.querySelectorAll('.empty, .empty-state, .muted, td[colspan]')].filter(visible).map(e => e.innerText.trim()).filter(t => t && t.length < 120).slice(0, 8);
  out.text = document.body.innerText.slice(0, 6000);
  return out;
}
"""


KEEP_VALUES = {"tab", "view", "kind", "section", "mode", "status", "stage"}      # a tab differs by these; a sort, a size or a period is one screen


def signature(path: str) -> str:
    """One screen per page and set of filters: /catalog?n=10 and /catalog?n=50 are the same screen, ?tab=here and ?tab=oth are two."""
    p = urlparse(path)
    keys = sorted(parse_qsl(p.query, keep_blank_values=True))
    return p.path + "?" + "&".join(k + ("=" + v if k in KEEP_VALUES else "") for k, v in keys)


def slug(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]+", "_", text.strip("/") or "home")[:70].strip("_") or "home"


class Crawl:
    def __init__(self, base: str, out: Path, seeded: dict, chromium: str = ""):
        self.base, self.out, self.seeded = base, out, seeded
        self.rows, self.links = [], {}
        (out / "img").mkdir(parents=True, exist_ok=True)
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(**({"executable_path": chromium} if chromium else {}))

    def close(self):
        self.browser.close()
        self.pw.stop()

    def login(self, ctx, login: str):
        page = ctx.new_page()
        page.goto(self.base + "/login")
        if login != "owner":
            page.fill("input[name=login]", login)
        page.fill("input[name=password]", seed.OWNER_PASSWORD if login == "owner" else seed.PASSWORD)
        page.click("form button")
        page.wait_for_load_state("networkidle")
        return page

    def shot(self, page, role, vp, key, url, status, extra=None):
        errors, bad = getattr(page, "_e2e_errors", []), getattr(page, "_e2e_bad", [])
        try:
            probe = page.evaluate(PROBE, [sorted(OLD_COLORS), list(OLD_FONTS), sorted(SCALE)])
        except Exception as err:                                # a page that navigated away under the probe
            probe = {"error": str(err)[:200], "links": [], "modals": []}
        name = "%s__%s__%s.jpg" % (role, vp, slug(key))
        unroll = not (extra or {}).get("modal")
        if unroll:                                  # the whole of .main, not its first 800 px (a window stays as it is seen)
            page.evaluate("css => { const s = document.createElement('style'); s.dataset.e2e = '1'; s.textContent = css;"
                          " document.head.appendChild(s); }", UNROLL)
        page.screenshot(path=str(self.out / "img" / name), full_page=True, type="jpeg", quality=40)
        if unroll:
            page.evaluate("() => document.querySelectorAll('style[data-e2e]').forEach(s => s.remove())")
        for link in probe.get("links", []):
            if link.startswith(self.base):
                self.links.setdefault(link.split("#")[0], "%s %s" % (role, key))
        row = dict(role=role, vp=vp, key=key, url=url, status=status, shot=name, console=list(errors), bad_requests=list(bad),
                   **{k: v for k, v in probe.items() if k != "links"}, **(extra or {}))
        self.rows.append(row)
        errors.clear()
        bad.clear()
        return probe

    def watch(self, page):
        page._e2e_errors, page._e2e_bad = [], []
        page.on("console", lambda m: m.type == "error" and page._e2e_errors.append(m.text[:300]))
        page.on("pageerror", lambda e: page._e2e_errors.append("pageerror: %s" % str(e)[:300]))
        page.on("response", lambda r: r.status >= 400 and page._e2e_bad.append("%d %s" % (r.status, r.url.replace(self.base, ""))))

    def visit(self, page, role, vp, path, key=None, click_modals=True):
        try:
            resp = page.goto(self.base + path, wait_until="networkidle", timeout=20000)
            status = resp.status if resp else 0
        except Exception as err:
            status = "error: %s" % str(err)[:120]
        landed = page.url.replace(self.base, "")
        probe = self.shot(page, role, vp, key or path, path, status, {"landed": landed})
        if click_modals:
            for modal in probe.get("modals", [])[:6]:
                try:
                    page.evaluate("m => { const b = document.querySelector('[data-open-modal=\"' + m + '\"]'); const d = b && b.closest('details');"
                                  " if (d) d.open = true; }", modal)          # an opener in the «Ещё» menu: open the menu first, as a person does
                    page.click('[data-open-modal="%s"] >> visible=true' % modal, timeout=3000)
                    page.wait_for_timeout(300)
                    self.shot(page, role, vp, "%s#%s" % (key or path, modal), path, status, {"modal": modal})
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(150)
                except Exception as err:
                    self.rows.append(dict(role=role, vp=vp, key="%s#%s" % (key or path, modal), url=path, status=status,
                                          modal_error=str(err)[:200]))
        return probe

    def menu(self, page):
        found = []
        for href in page.eval_on_selector_all(MENU_LINKS, "els => els.map(e => e.getAttribute('href'))"):
            url = urljoin(self.base + "/", href or "")
            if url.startswith(self.base) and not NEVER.search(url):
                p = urlparse(url)
                path = p.path + ("?" + p.query if p.query else "")
                if path not in found:
                    found.append(path)
        return found

    def tabs(self, page, done):
        found = []
        for sel in TAB_LINKS:
            for href in page.eval_on_selector_all(sel, "els => els.map(e => e.getAttribute('href'))"):
                url = urljoin(page.url, href or "")
                if url.startswith(self.base) and not NEVER.search(url):
                    p = urlparse(url)
                    path = p.path + ("?" + p.query if p.query else "")
                    if path not in done and path not in found:
                        found.append(path)
        return found[:MAX_TABS]

    def role(self, login: str, orders_of: list):
        for vp, size in VIEWPORTS.items():
            phone = dict(is_mobile=True, has_touch=True) if vp == "375" else {}     # a phone reads <meta viewport>, as a real one does
            ctx = self.browser.new_context(viewport=size, device_scale_factor=1, locale="ru-RU", **phone)
            page = self.login(ctx, login)
            self.watch(page)
            start = page.url.replace(self.base, "") or "/"
            menu = [start] + [p for p in self.menu(page) if p != start]
            seen = set()
            for item in menu:                                   # a page of the menu, then its tabs (not the tabs of a tab)
                if signature(item) in seen:
                    continue
                seen.add(signature(item))
                self.visit(page, login, vp, item)
                for tab in self.tabs(page, []):
                    if signature(tab) not in seen:
                        seen.add(signature(tab))
                        self.visit(page, login, vp, tab)
            for cid in orders_of:
                self.visit(page, login, vp, "/orders/%d" % cid)
            ctx.close()

    def check_links(self, login: str):
        """Every internal link seen on the screens of the role, opened once by GET (the role's session): a 404 or 500 is a broken link."""
        ctx = self.browser.new_context(viewport=VIEWPORTS["1280"])
        self.login(ctx, login)
        broken = []
        mine = sorted(link for link, where in self.links.items() if where.startswith(login + " "))
        for link in mine:
            if NEVER.search(link):
                continue
            r = ctx.request.get(link, max_redirects=5, fail_on_status_code=False, timeout=20000)
            if r.status >= 400 and r.status != 403:
                broken.append(dict(role=login, link=link.replace(self.base, ""), status=r.status, seen_on=self.links[link]))
        ctx.close()
        return broken


def main(out: Path) -> int:
    started = time.time()
    roles = [r for r in os.environ.get("E2E_ROLES", ",".join(ROLES)).split(",") if r]
    base, server, seeded = serve.start()
    chromium = os.environ.get("E2E_CHROMIUM") or ("/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else "")
    out.mkdir(parents=True, exist_ok=True)
    crawl = Crawl(base, out, seeded, chromium)
    every = sorted(seeded["orders"].values())
    own = seeded["own_orders"]
    plan = {"owner": every, "partner1": every, "office1": every, "viewer1": every, "point1": own[: len(own)] + [seeded["foreign_order"]],
            "client1": []}
    broken = []
    try:
        for login in roles:
            print("role %s ..." % login, flush=True)
            crawl.role(login, plan[login])
            broken += crawl.check_links(login)
    finally:
        crawl.close()
        server.shutdown()
    report = dict(base=base, seconds=round(time.time() - started), orders=seeded["orders"], screens=crawl.rows, broken_links=broken,
                  outside_calls=serve.OUTSIDE)
    name = "report.json" if roles == list(ROLES) else "report-%s.json" % "-".join(roles)
    (out / name).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    bad = [r for r in crawl.rows if isinstance(r.get("status"), int) and r["status"] >= 500]
    print("screens %d, 5xx %d, broken links %d, outside calls %d, %ds" % (len(crawl.rows), len(bad), len(broken), len(serve.OUTSIDE),
                                                                          report["seconds"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "shots"))
