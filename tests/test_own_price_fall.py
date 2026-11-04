"""Своя цена в % пошла вниз за закупкой без вопроса price_guard (решение владельца 2026-11-04): одна сводка за цикл ответственному аккаунта,
иначе владельцу, и строка в журнале; каждое изменение один раз; цены и фид не трогаются."""
from __future__ import annotations

import json

import pytest

from test_stock_fresh import cycle, prepared, published, rows
from tires_sync import cli, connections, order_notify, own_price, price_guard, staging, store, users
from tires_sync.settings_store import active_settings, confirm_draft

NOW = "2026-11-04T10:00:00"


def own(conn, value, accounts=(1,), gid="101"):
    staging.set_own_price(conn, gid, list(accounts), value, NOW)
    confirm_draft(conn, staging.summary(conn)["draft_id"], NOW, "owner", strong=True)


def at(tmp_path, base, *extra):
    assert cycle(tmp_path, rows(dict(NNOMMODIF="101", NPRICE_DELAYED=str(base))), *extra) == 0


def told(conn):
    """Тексты сводок в журнале, старые первыми."""
    notices = [e["detail"]["text"] for e in store.list_audit(conn, 200) if e["action"] == "notice"]
    return [t for t in reversed(notices) if "своя цена в процентах" in t]


def ready(tmp_path, base=5000, pct=10):
    conn = prepared(tmp_path)
    own(conn, {"pct": pct})
    at(tmp_path, base)                                 # первый взгляд и публикация
    at(tmp_path, base)
    assert told(conn) == []
    return conn


def test_a_fall_of_one_percent_is_told_once_and_no_price_changes_because_of_it(tmp_path):
    conn = ready(tmp_path)
    at(tmp_path, 4950)
    assert published(tmp_path)["101"].price == 5450                       # 4950 × 1,1 вверх до десятков: как и без уведомления
    assert len(told(conn)) == 1
    line = told(conn)[0]
    assert "пошла вниз за ней (1)" in line and "A · " in line
    assert "закупка 5 000 ₽ → 4 950 ₽, наша цена 5 500 ₽ → 5 450 ₽" in line
    at(tmp_path, 4950)
    at(tmp_path, 4950)
    assert len(told(conn)) == 1                                           # закупка та же: без повтора
    at(tmp_path, 4900)
    assert len(told(conn)) == 2 and "закупка 4 950 ₽ → 4 900 ₽" in told(conn)[1]


def test_a_fall_of_five_percent_goes_through_the_guard_only_and_its_accept_is_not_told(tmp_path):
    conn = ready(tmp_path)
    at(tmp_path, 4000)                                                    # −20 %: вопрос price_guard
    assert price_guard.load(conn)["anchors"]["101"]["pending"] == 4000.0
    assert told(conn) == []
    at(tmp_path, 3950)
    assert told(conn) == []
    assert price_guard.accept(conn, ["101"], NOW, "owner") == 1           # якорь становится 3950 (ожидание обновилось в прошлом цикле)
    at(tmp_path, 3950)
    assert told(conn) == []                                               # «Принять» нажал сам владелец
    at(tmp_path, 3900)
    assert len(told(conn)) == 1 and "закупка 3 950 ₽ → 3 900 ₽" in told(conn)[0]


def test_a_big_fall_from_above_the_anchor_is_the_guards_only(tmp_path):
    conn = ready(tmp_path)
    at(tmp_path, 5200)                                                    # якорь 5000, метка 5200
    at(tmp_path, 4000)                                                    # держим от 5000: для нашей цены это падение с 5200, но вопрос уже задан
    assert price_guard.load(conn)["anchors"]["101"]["pending"] == 4000.0
    assert told(conn) == []


def test_a_fall_held_then_back_within_five_percent_is_told_from_the_price_held(tmp_path):
    conn = ready(tmp_path)
    at(tmp_path, 4000)
    assert told(conn) == []
    at(tmp_path, 4800)                                                    # вопрос снят, наша цена идёт вниз от прежних 5000
    assert len(told(conn)) == 1 and "закупка 5 000 ₽ → 4 800 ₽, наша цена 5 500 ₽ → 5 280 ₽" in told(conn)[0]


def test_a_rise_is_not_told_and_a_fall_after_it_is_even_when_it_exceeds_five_percent_of_the_last_price(tmp_path):
    conn = ready(tmp_path)
    at(tmp_path, 5200)                                                    # +4 %: якорь 5000 остаётся
    at(tmp_path, 5600)                                                    # +12 % от якоря: якорь 5600
    assert told(conn) == []
    at(tmp_path, 5700)                                                    # +1,8 %: якорь 5600
    at(tmp_path, 5350)                                                    # −6,1 % от 5700, но −4,5 % от якоря: price_guard молчит, цена пошла вниз
    assert price_guard.load(conn)["anchors"]["101"]["pending"] == 0
    assert len(told(conn)) == 1 and "закупка 5 700 ₽ → 5 350 ₽" in told(conn)[0]


def test_a_ready_price_in_rubles_is_not_told(tmp_path):
    conn = prepared(tmp_path)
    own(conn, {"price": 7000})
    at(tmp_path, 5000)
    at(tmp_path, 5000)
    at(tmp_path, 4950)
    assert told(conn) == []


def test_a_position_without_an_own_price_is_not_told(tmp_path):
    conn = prepared(tmp_path)
    at(tmp_path, 5000)
    at(tmp_path, 5000)
    at(tmp_path, 4950)
    assert told(conn) == []


def test_an_own_percent_set_later_counts_from_there_and_its_own_change_says_nothing(tmp_path):
    conn = prepared(tmp_path)
    at(tmp_path, 5000)
    at(tmp_path, 4950)
    own(conn, {"pct": 10})
    at(tmp_path, 4950)
    assert told(conn) == []                                               # падение было до своей цены
    own(conn, {"pct": 20})
    at(tmp_path, 4950)
    assert told(conn) == []                                               # процент поменял человек, закупка та же
    at(tmp_path, 4900)
    assert len(told(conn)) == 1 and "наша цена 5 940 ₽ → 5 880 ₽" in told(conn)[0]


def test_a_cycle_without_publish_says_nothing_and_a_later_one_tells(tmp_path):
    conn = ready(tmp_path)
    (tmp_path / "unload.xml").write_bytes(rows(dict(NNOMMODIF="101", NPRICE_DELAYED="4950")))
    assert cli.main(["run-cycle", "--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"),
                     "--reference", str(tmp_path / "reference.txt"), "--feed-root", str(tmp_path / "feed")]) == 0
    assert told(conn) == []
    at(tmp_path, 4950)
    assert len(told(conn)) == 1


def test_a_position_off_avito_is_not_told_and_no_news_when_it_comes_back(tmp_path):
    conn = ready(tmp_path)
    conn.execute("DELETE FROM published_state WHERE goods_id = '101'")
    conn.commit()
    offers = {"101": _offer(4950)}
    cli._alert_own_price_falls(conn, None, offers, [1])
    assert told(conn) == []
    _publish(conn, 1)
    cli._alert_own_price_falls(conn, None, offers, [1])
    assert told(conn) == []


def _publish(conn, account):
    conn.execute("INSERT INTO published_state (account_id, goods_id, price, base_price, point, snapshot) VALUES (?, '101', 5500, 5000, 'p', '{}')", (account,))
    conn.commit()


def _offer(base):
    from helpers import make_offer
    return make_offer(goods_id="101", base_price=float(base))


def test_a_failed_account_keeps_its_marks_and_is_told_later(tmp_path):
    conn = ready(tmp_path)
    cli._alert_own_price_falls(conn, None, {"101": _offer(4950)}, [])       # фид аккаунта в этом цикле не собран
    assert told(conn) == []
    cli._alert_own_price_falls(conn, None, {"101": _offer(4950)}, [1])
    assert len(told(conn)) == 1


def test_a_position_away_from_the_unload_keeps_its_mark(tmp_path):
    conn = ready(tmp_path)
    cli._alert_own_price_falls(conn, None, {}, [1])
    cli._alert_own_price_falls(conn, None, {"101": _offer(4950)}, [1])
    assert len(told(conn)) == 1


def test_an_account_not_live_is_not_told(tmp_path):
    conn = ready(tmp_path)
    connections.set_live(conn, 1, False, NOW, "owner")
    cli._alert_own_price_falls(conn, None, {"101": _offer(4950)}, [1])
    assert told(conn) == []


def _two_accounts(tmp_path, monkeypatch):
    conn = prepared(tmp_path)
    second = connections.create_account(conn, "B", NOW, "owner")
    connections.set_live(conn, second, True, NOW, "owner")
    own(conn, {"pct": 10}, accounts=(1, second))
    sent = []

    def tell(conn_, box, logins, text, tg_http=None, now="", skip_logins=()):
        sent.append((logins, text))
        return "Telegram: отправлено (%s)" % ", ".join(logins)
    monkeypatch.setattr(order_notify, "tell_people", tell)
    for account in (1, second):
        _publish(conn, account)
    cli._alert_own_price_falls(conn, None, {"101": _offer(5000)}, [1, second])
    assert sent == []
    return conn, second, sent


def test_each_account_goes_to_its_responsible_else_to_the_owner(tmp_path, monkeypatch):
    conn, second, sent = _two_accounts(tmp_path, monkeypatch)
    users.add_user(conn, "olya", "Passw0rd-long-enough", NOW, role="partner")
    connections.set_responsible(conn, second, "olya", ["olya"], NOW, "owner")
    cli._alert_own_price_falls(conn, None, {"101": _offer(4950)}, [1, second])
    assert [s[0] for s in sent] == [["olya"]]
    assert "· " in sent[0][1] and connections.display_name(conn, second) + " ·" in sent[0][1]
    assert connections.display_name(conn, 1) + " ·" not in sent[0][1]
    texts = told(conn)
    assert len(texts) == 2                                                  # журнал: и ей, и владельцу
    assert [t for t in texts if connections.display_name(conn, 1) + " ·" in t and connections.display_name(conn, second) + " ·" not in t]
    assert any(e["detail"].get("to") == "olya" for e in store.list_audit(conn, 50) if e["action"] == "notice")
    sent.clear()
    cli._alert_own_price_falls(conn, None, {"101": _offer(4950)}, [1, second])
    assert sent == [] and len(told(conn)) == 2


def test_two_accounts_of_one_person_are_one_summary(tmp_path, monkeypatch):
    conn, second, sent = _two_accounts(tmp_path, monkeypatch)
    users.add_user(conn, "olya", "Passw0rd-long-enough", NOW, role="partner")
    for account in (1, second):
        connections.set_responsible(conn, account, "olya", ["olya"], NOW, "owner")
    cli._alert_own_price_falls(conn, None, {"101": _offer(4950)}, [1, second])
    assert len(sent) == 1 and "(2)" in sent[0][1]


def test_a_responsible_without_telegram_means_the_owner_and_a_refused_message_is_repeated(tmp_path, monkeypatch):
    conn, second, sent = _two_accounts(tmp_path, monkeypatch)
    users.add_user(conn, "olya", "Passw0rd-long-enough", NOW, role="partner")
    connections.set_responsible(conn, 1, "olya", ["olya"], NOW, "owner")
    connections.set_responsible(conn, second, "olya", ["olya"], NOW, "owner")
    answers = ["Telegram не отправлен (olya: 403)", "Telegram: ни у кого нет чата (olya)"]
    monkeypatch.setattr(order_notify, "tell_people", lambda *a, **k: answers.pop(0))
    cli._alert_own_price_falls(conn, None, {"101": _offer(4950)}, [1, second])
    assert told(conn) == []                                                 # отказ: метка на месте, ни журнала, ни «сказано»
    cli._alert_own_price_falls(conn, None, {"101": _offer(4950)}, [1, second])
    assert len(told(conn)) == 1                                             # чата нет: ушло владельцу
    assert not answers
    cli._alert_own_price_falls(conn, None, {"101": _offer(4950)}, [1, second])
    assert len(told(conn)) == 1


def test_a_switched_off_responsible_means_the_owner(tmp_path, monkeypatch):
    conn, second, sent = _two_accounts(tmp_path, monkeypatch)
    users.add_user(conn, "olya", "Passw0rd-long-enough", NOW, role="partner")
    connections.set_responsible(conn, second, "olya", ["olya"], NOW, "owner")
    conn.execute("UPDATE panel_user_role SET active = 0 WHERE login = 'olya'")
    conn.commit()
    cli._alert_own_price_falls(conn, None, {"101": _offer(4950)}, [1, second])
    assert sent == [] and len(told(conn)) == 1


def test_a_broken_notice_never_stops_the_cycle(tmp_path, monkeypatch, capsys):
    conn = ready(tmp_path)

    def broken(*args):
        raise RuntimeError("boom")
    monkeypatch.setattr(own_price, "falls", broken)
    at(tmp_path, 4950)
    assert "boom" in capsys.readouterr().err
    assert published(tmp_path)["101"].price == 5450


def test_a_broken_mark_is_as_no_mark(tmp_path):
    conn = ready(tmp_path)
    conn.execute("UPDATE app_state SET value = 'x' WHERE key = ?", (own_price.FALL_MARK + "1:101",))
    conn.commit()
    cli._alert_own_price_falls(conn, None, {"101": _offer(4950)}, [1])
    assert told(conn) == []
    assert json.loads(store.get_state(conn, own_price.FALL_MARK + "1:101")) == [4950.0, 5450]


def test_a_mirrored_account_is_told_of_the_positions_of_its_stored_ads_only(tmp_path):
    conn = ready(tmp_path)
    conn.execute("DELETE FROM published_state")
    conn.execute("UPDATE account SET feed_mode = 'mirror' WHERE id = 1")
    conn.execute("INSERT INTO imported_ad (account_id, ad_id, position, raw, imported_at) VALUES (1, '202_1', 1, '<Ad/>', 'x')")
    conn.commit()
    cli._alert_own_price_falls(conn, None, {"101": _offer(4950)}, [1])
    assert told(conn) == []                                                 # 101 у зеркала нет
    conn.execute("INSERT INTO imported_ad (account_id, ad_id, position, raw, imported_at) VALUES (1, '101-x4_1', 2, '<Ad/>', 'x')")
    conn.commit()
    cli._alert_own_price_falls(conn, None, {"101": _offer(4900)}, [1])
    assert len(told(conn)) == 1
