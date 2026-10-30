"""The message about the manual ads of a cabinet: the cabinet named plainly, the whole list with a link to each ad, cut into parts Telegram
takes (owner, 2026-10-29)."""
from __future__ import annotations

from test_same_tire import setup
from tires_sync import cli, connections, mirror_feed, notify


def manual(conn, account, count, title="Шина %d", ad_id=None):
    ads = [dict(avito_id=4000000000 + n, title=title % n if "%d" in title else title, **({"ad_id": ad_id} if ad_id else {})) for n in range(count)]
    mirror_feed.set_manual_ads(conn, account, ads)
    conn.commit()
    return ads


def report(conn, tmp_path, monkeypatch, *accounts):
    said = []
    monkeypatch.setattr(cli, "_alert", lambda c, b, text, **k: said.append((text, k)) or True)
    cli._prebind_report(conn, None, tmp_path / "feed", [(connections.get_account(conn, a), [], True) for a in accounts])
    return said


def test_the_cabinet_is_named_with_its_number_name_and_profile(tmp_path, monkeypatch):
    conn, account = setup(tmp_path)
    other = connections.create_account(conn, "Шина-Колесо", "2026-10-14T12:00:00", "o")
    connections.set_avito_identity(conn, account, 31415926, "Кабинет")
    manual(conn, account, 1)
    manual(conn, other, 1)
    said = report(conn, tmp_path, monkeypatch, account, other)
    assert len(said) == 1 and said[0][1] == {"notice": True}
    text = said[0][0]
    assert "в кабинете Авито №%d «Кабинет» (профиль Авито 31415926): ручных объявлений: 1" % account in text      # the cabinet's own name (2026-10-30)
    assert "в кабинете Авито №%d «Шина-Колесо»: ручных объявлений: 1" % other in text          # no profile read: no bracket
    assert "профиль Авито None" not in text


def test_all_17_ads_are_listed_each_with_its_link(tmp_path, monkeypatch):
    conn, account = setup(tmp_path)
    ads = manual(conn, account, 17, ad_id="T1-125")
    [(text, _)] = report(conn, tmp_path, monkeypatch, account)
    for ad in ads:
        assert "   %d «%s» https://www.avito.ru/%d (живо вне нашего файла, Id T1-125)" % (ad["avito_id"], ad["title"], ad["avito_id"]) in text
    assert "и ещё" not in text and text.count("https://www.avito.ru/") == 17


def test_a_long_list_goes_in_parts_telegram_takes_each_one_said(tmp_path, monkeypatch):
    conn, account = setup(tmp_path)
    other = connections.create_account(conn, "Шина-Колесо", "2026-10-14T12:00:00", "o")
    ads = manual(conn, account, 120, title="Очень длинное название шины номер %d для проверки деления сообщения")
    manual(conn, other, 3)
    sent = []
    monkeypatch.setattr(cli, "notifier_for", lambda *a, **k: sent.append)
    cli._prebind_report(conn, None, tmp_path / "feed", [(connections.get_account(conn, a), [], True) for a in (account, other)])
    total = len(sent)
    assert total >= 3 and all(len(part) <= notify.TELEGRAM_LIMIT for part in sent)             # each one whole: send_message cuts nothing
    assert sent[0].startswith(cli.MANUAL_HEAD + " (1 из %d)" % total)
    for n, part in enumerate(sent[1:], 2):
        assert part.startswith("Tires Sync: объявления, сделанные вручную (продолжение %d из %d)\n" % (n, total))
        assert "\nв кабинете Авито №" in part                                                 # each part says which cabinet it is about
    joined = "\n".join(sent)
    assert all("https://www.avito.ru/%d" % ad["avito_id"] in joined for ad in ads) and joined.count("https://www.avito.ru/") == 123
    journal = [r[0] for r in conn.execute("SELECT detail FROM audit WHERE action = 'notice'")]
    assert len(journal) == total                                                              # the journal keeps every part
    cli._prebind_report(conn, None, tmp_path / "feed", [(connections.get_account(conn, a), [], True) for a in (account, other)])
    assert len(sent) == 2 * total                                                             # told again later: no part is taken for a repeat


def test_a_title_with_markup_characters_comes_through_as_it_is(tmp_path, monkeypatch):
    conn, account = setup(tmp_path)
    manual(conn, account, 1, title="Шина <R16> & «Зима»")
    [(text, _)] = report(conn, tmp_path, monkeypatch, account)
    assert "   4000000000 «Шина <R16> & «Зима»» https://www.avito.ru/4000000000" in text           # plain text: nothing to escape, nothing lost


def test_a_part_never_ends_with_a_header_alone():
    first, second = "в кабинете Авито №1 «А»: ручных объявлений: 2", "в кабинете Авито №2 «Б»: ручных объявлений: 1"
    for size in range(1600, 1900, 7):                                                         # one of them leaves room for the header only
        parts = cli._manual_parts([(first, ["x" * 1900, "y" * size]), (second, ["z" * 50])])
        assert all(len(p) <= notify.TELEGRAM_LIMIT for p in parts)
        assert not any(p.endswith(first) or p.endswith(second) for p in parts)
        assert "\n".join(parts).count("z" * 50) == 1


def test_a_line_longer_than_a_message_is_cut_to_fit_its_part():
    header = "в кабинете Авито №1 «А»: ручных объявлений: 3"
    parts = cli._manual_parts([(header, ["a" * 3000, "b" * 5000, "c" * 10])])
    assert all(len(p) <= notify.TELEGRAM_LIMIT for p in parts) and parts[-1].endswith("c" * 10)


# ---- the owner 2026-10-30: «нет у поставщика — снимите вручную» first; the cabinet's responsible gets its own list ------------------------

def test_an_ad_the_supplier_has_not_and_not_on_oth_is_marked_and_first(tmp_path, monkeypatch):
    from tires_sync import order_oth
    conn, account = _fresh(tmp_path)
    mirror_feed.set_manual_ads(conn, account, [dict(avito_id=4000000001, title="Есть у поставщика", ad_id="100"),
                                               dict(avito_id=4000000002, title="Нет нигде", ad_id="900"),
                                               dict(avito_id=4000000003, title="Лежит на ОТХ", ad_id="901"),
                                               dict(avito_id=4000000004, title="Сделано вручную")])
    conn.commit()
    monkeypatch.setattr(order_oth, "goods_on_oth", lambda conn: {"901": 2})
    said = []
    monkeypatch.setattr(cli, "_alert", lambda c, b, text, **k: said.append(text) or True)
    cli._prebind_report(conn, None, tmp_path / "feed", [(connections.get_account(conn, account), [], True)], offers={"100": object()})
    text = "\n".join(said)
    lines = [l for l in text.split("\n") if l.startswith("   ")]
    assert lines[0].startswith("   4000000002") and "нет у поставщика — снимите вручную" in lines[0]
    assert sum("нет у поставщика" in l for l in lines) == 1                       # on «На ОТХ», at the supplier, or no code: not marked


def test_the_list_of_a_cabinet_goes_to_its_responsible_and_the_owners_to_the_owner(tmp_path, monkeypatch):
    from tires_sync import order_notify, users
    conn, first = _fresh(tmp_path)
    second = connections.create_account(conn, "Шина-Колесо", "2026-10-14T12:00:00", "o")
    users.add_user(conn, "zaman", "длинный-пароль-1", "2026-10-14T12:00:00", role="owner")
    connections.set_responsible(conn, second, "zaman", ["owner", "zaman"], "2026-10-14T12:00:00", "owner")
    order_notify.set_user_chat(conn, "zaman", "777", "2026-10-14T12:00:00", "owner")
    conn.commit()
    manual(conn, first, 1, title="Своя %d")
    manual(conn, second, 1, title="Заманова %d")
    to_owner, to_person = [], []
    monkeypatch.setattr(cli, "_alert", lambda c, b, text, **k: to_owner.append(text) or True)
    monkeypatch.setattr(cli, "_person_telegram", lambda conn, box, login: (lambda text: to_person.append((login, text))))
    cli._prebind_report(conn, None, tmp_path / "feed", [(connections.get_account(conn, a), [], True) for a in (first, second)])
    assert "Своя 0" in "\n".join(to_owner) and "Заманова 0" not in "\n".join(to_owner)
    assert [login for login, _t in to_person] == ["zaman"] and "Заманова 0" in to_person[0][1] and "Своя 0" not in to_person[0][1]


def test_a_responsible_without_a_chat_leaves_the_list_with_the_owner(tmp_path, monkeypatch):
    from tires_sync import users
    conn, first = _fresh(tmp_path)
    second = connections.create_account(conn, "Шина-Колесо", "2026-10-14T12:00:00", "o")
    users.add_user(conn, "zaman", "длинный-пароль-1", "2026-10-14T12:00:00", role="owner")
    connections.set_responsible(conn, second, "zaman", ["owner", "zaman"], "2026-10-14T12:00:00", "owner")
    conn.commit()
    manual(conn, second, 1, title="Заманова %d")
    to_owner = []
    monkeypatch.setattr(cli, "_alert", lambda c, b, text, **k: to_owner.append(text) or True)
    cli._prebind_report(conn, None, tmp_path / "feed", [(connections.get_account(conn, second), [], True)])
    assert "Заманова 0" in "\n".join(to_owner)


def _fresh(tmp_path):
    from tires_sync import store
    conn = store.connect(tmp_path / "fresh.db")
    account = connections.create_account(conn, "Основной", "2026-10-14T12:00:00", "o")
    conn.commit()
    return conn, account


def test_a_supplier_not_used_this_cycle_marks_none_of_its_ads(tmp_path, monkeypatch):
    """A supplier missing from the cycle (no file, a broken one): its positions are held, not gone — nothing to take off by hand."""
    from tires_sync import order_oth
    conn, account = _fresh(tmp_path)
    mirror_feed.set_manual_ads(conn, account, [dict(avito_id=4000000002, title="Второй склад", ad_id="s2-900")])
    conn.commit()
    monkeypatch.setattr(order_oth, "goods_on_oth", lambda conn: {})
    said = []
    monkeypatch.setattr(cli, "_alert", lambda c, b, text, **k: said.append(text) or True)
    cli._prebind_report(conn, None, tmp_path / "feed", [(connections.get_account(conn, account), [], True)], offers={"100": object()}, unavailable={2})
    assert "нет у поставщика" not in "\n".join(said)
