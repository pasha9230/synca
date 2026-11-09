"""Только страница прокручивается целиком (слово владельца 2026-11-09: «убери внутренний скрол — везде, например в заказе»).

Блоки страницы (окно заказа «Нового вида», чаты обоих видов, массовые действия, шаблоны сообщений, фильтры магазина,
таблицы брендов) не имеют своей высоты с прокруткой внутри. Выпадающие окна, модальные окна и каркас страницы
(.sidebar, .main) прокручиваются как раньше; прокрутка таблиц вбок (overflow-x) остаётся."""
import re
from pathlib import Path

PANEL = Path(__file__).resolve().parent.parent / "src" / "tires_sync" / "panel"
STATIC = PANEL / "static"


def css(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def rules(text: str, selector: str) -> list:
    """Тела всех правил, у которых в списке селекторов есть ровно этот селектор."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    found = []
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
        if selector in [s.strip() for s in sel.split(",")]:
            found.append(re.sub(r"\s+", "", body))
    return found


def scrolls(body: str) -> bool:
    """Своя прокрутка по вертикали: overflow / overflow-y auto|scroll или высота, ограниченная max-height / height."""
    return bool(re.search(r"(^|;)overflow(-y)?:(auto|scroll)", body) or re.search(r"(^|;)(max-)?height:(calc|max|min|\d)", body))


PAGE_BLOCKS = [
    ("office2.css", "body.o2-card .o2c-body"),          # окно заказа: три колонки
    ("office2.css", "body.o2-card .o2c-scroll"),
    ("office2.css", "body.o2 .o2-bulk"),                # массовые действия
    ("office2.css", "body.o2 .o2-bulk-body"),
    ("office2.css", "body.o2 .o2-bulk-list"),
    ("office2-pages.css", ".o2 .chats-layout.o2-cl"),   # чаты «Нового вида»
    ("office2-pages.css", ".o2 .o2-cl-list"),
    ("office2-pages.css", ".o2 .o2-cl-pane"),
    ("office2-pages.css", ".o2 .o2-cl-msgs"),
    ("panel.css", ".chats-list"),                       # чаты старого вида
    ("panel.css", ".chat-thread"),
    ("point.css", "body.pt-chats-page .chats-list"),    # чаты точки на телефоне
    ("point.css", "body.pt-chats-page .chat-thread"),
    ("audit.css", ".tpl2-list"),                        # шаблоны сообщений
    ("shop.css", ".shop-blist"),                        # фильтры магазина
    ("shop.css", ".shop2-filters .shop-blist"),
]


def test_shop_filters_scroll_with_the_page():
    # на большом экране фильтры идут со страницей; на телефоне это шторка снизу (position:fixed), она прокручивается сама, как окно
    assert not scrolls(rules(css("shop.css"), ".shop2-side")[0])


def test_page_blocks_have_no_scroll_of_their_own():
    for name, selector in PAGE_BLOCKS:
        for body in rules(css(name), selector):
            assert not scrolls(body), (name, selector, body)


def test_no_fixed_height_of_the_order_window_or_the_chats():
    assert "100vh - 150px" not in css("office2.css") and "100vh - 170px" not in css("office2.css")
    assert "height:calc(100vh" not in css("office2-pages.css")


def test_side_blocks_do_not_stick_taller_than_the_screen():
    # а sticky-блок без своей высоты, выше экрана, обрезал бы свой низ: массовые действия и фильтры магазина идут со страницей
    assert not any("position:sticky" in b for b in rules(css("office2.css"), "body.o2 .o2-bulk"))
    assert not any("position:sticky" in b for b in rules(css("shop.css"), ".shop-side"))


def test_the_reply_field_of_the_new_view_stays_at_the_bottom():
    body = rules(css("office2-pages.css"), ".o2 .o2-cl-comp")[0]
    assert "position:sticky" in body and "bottom:0" in body


def test_brand_tables_in_templates_have_no_height_of_their_own():
    for name in ("_client_brands.html", "_exclusions.html"):
        text = (PANEL / "templates" / name).read_text(encoding="utf-8")
        assert "max-height:5" not in text and "max-height:4" not in text, name


def test_popups_modals_and_the_frame_keep_their_scroll():
    panel = css("panel.css")
    assert any("overflow-y:auto" in b for b in rules(panel, ".sidebar"))
    assert any("overflow-y:auto" in b for b in rules(panel, ".main"))
    assert any("overflow:auto" in b for b in rules(panel, ".modal-backdrop"))
    assert any("overflow-y:auto" in b for b in rules(panel, ".suggest"))
    assert any("overflow-y:auto" in b for b in rules(css("bell.css"), ".bell-list"))
    assert any("overflow-y:auto" in b for b in rules(css("topbar.css"), ".as-point-pop"))
    assert any("overflow:auto" in b for b in rules(panel, ".cols-menu .menu-list"))
    assert any("overflow-y:auto" in b for b in rules(css("office2-pages.css"), ".o2 .o2-cl-panel"))   # панель заказа у чата: окно на краю экрана
    assert any("overflow-x:auto" in b for b in rules(panel, ".table-wrap"))


def test_the_chat_opens_at_the_last_message():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function chatToEnd()" in js
    assert 'if (document.querySelector(".chat-thread") && !window.location.hash) chatToEnd();' in js
    assert "thread.scrollTop" not in js          # лента без своей прокрутки: новое сообщение показывает страница
