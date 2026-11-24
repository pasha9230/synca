"""ТЗ №88 (the owner 24.11.2026): the client's name and phone changed in the panel go to every row of the order in the Google table,
over a correction in the cell; an unchanged one leaves the cell alone. The script checks the row by the phone the panel wrote before.
One record of the table per send, never the values. A row rewritten by hand stays alone and never raises the alarm."""
from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from test_order_sheet import FakeScript, events, placed, stage, turn_on
from test_sheet_restore import NewScript
from test_webmim_create import NOW, db
from tires_sync import order_sheet as sheet, orders, store
from tires_sync.secretbox import SecretBox

LATER = "2026-10-23T12:30:00"
LATEST = "2026-10-23T13:00:00"
OLD_PHONE = "89111112233"                     # the phone of test_webmim_create.card as the table keeps it


class LostAnswer(NewScript):
    """The script writes the rows, then its answer never reaches the panel (`lose`: the next POST)."""
    lose = False

    def __call__(self, method, url, headers, body):
        answer = super().__call__(method, url, headers, body)
        if method == "POST" and self.lose:
            self.lose = False
            raise OSError("timed out")
        return answer


def table_with(tmp_path, fake):
    conn = db(tmp_path)
    box = SecretBox(Fernet.generate_key())
    turn_on(conn, box)
    return conn, box, fake


@pytest.fixture
def table(tmp_path):
    return table_with(tmp_path, FakeScript())


def failing(conn, card_id):
    return conn.execute("SELECT failing FROM sheet_order WHERE order_id = ?", (card_id,)).fetchone()[0]


def test_a_new_name_and_phone_go_to_every_row_checked_by_the_phone_written_before(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    assert {r["Телефон"] for r in fake.rows.values()} == {OLD_PHONE}
    orders.update_card(conn, mine, {"client_name": "Иван Петров", "client_phone": "+7 922 333-44-55"}, LATER, "pavel")
    sheet.flush(conn, box, fake, LATER)
    sent = fake.data_posts()[-1]["rows"]
    assert len(sent) == 2 and not any(r["create"] for r in sent)
    assert all(r["fields"] == {"Имя": "Иван Петров", "Телефон": "89223334455"} for r in sent)
    assert all(r["check"]["Телефон"] == OLD_PHONE for r in sent)                  # the old phone: the script finds the row as it is
    assert {(r["Имя"], r["Телефон"]) for r in fake.rows.values()} == {("Иван Петров", "89223334455")}
    said = events(conn, mine)
    assert said[-1] == "Google-таблица: имя и телефон обновлены (2 стр.)."
    assert said.count(said[-1]) == 1 and not any("Google-таблица обновлена" in e for e in said)   # one record, no values
    assert sheet.flush(conn, box, fake, LATER) == {}                               # its own record is no change of the order
    orders.update_card(conn, mine, {"client_phone": "+7 933 444-55-66"}, LATEST, "pavel")
    sheet.flush(conn, box, fake, LATEST)
    again = fake.data_posts()[-1]["rows"]
    assert all(r["fields"] == {"Телефон": "89334445566"} and r["check"]["Телефон"] == "89223334455" for r in again)
    assert events(conn, mine)[-1] == "Google-таблица: телефон обновлён (2 стр.)."
    assert failing(conn, mine) == 0


def test_a_correction_in_the_cell_stays_until_the_name_changes_in_the_panel(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    first = sorted(fake.rows)[0]
    fake.rows[first]["Имя"] = "Иван (постоянный)"                                  # the owner corrected the cell
    orders.move_card(conn, mine, stage(conn, "at_point")["id"], LATER, "pavel")
    sheet.flush(conn, box, fake, LATER)
    assert all(set(r["fields"]) == {"Этап"} for r in fake.data_posts()[-1]["rows"])
    assert fake.rows[first]["Имя"] == "Иван (постоянный)"                        # the name did not change in the panel: the cell stays
    orders.update_card(conn, mine, {"client_name": "Пётр"}, LATEST, "pavel")
    sheet.flush(conn, box, fake, LATEST)
    assert all(r["fields"] == {"Имя": "Пётр"} for r in fake.data_posts()[-1]["rows"])
    assert {r["Имя"] for r in fake.rows.values()} == {"Пётр"}                    # changed in the panel: the panel wins
    assert events(conn, mine)[-1] == "Google-таблица: имя обновлено (2 стр.)."


def test_the_same_number_in_another_form_is_no_change(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    orders.update_card(conn, mine, {"client_phone": "8 (911) 111-22-33"}, LATER, "pavel")
    assert sheet.flush(conn, box, fake, LATER) == {mine: "нечего отправлять"}
    assert len(fake.data_posts()) == 1
    rows = sheet.rows_of(conn, mine, NOW)                                         # a row written as «+7 911 …» before 2026-10-30
    written = {k: dict(v, **{"Телефон": "+7 911 111-22-33", "Имя": " Иван "}) for k, v in rows.items()}
    assert sheet.changes(rows, written) == []


def test_a_row_rewritten_by_hand_is_left_alone_and_never_raises_the_alarm(tmp_path):
    conn, box, fake = table_with(tmp_path, NewScript())
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    first, second = sorted(fake.rows)
    fake.rows[first]["Телефон"] = "89990000000"                                   # the owner wrote another phone into the row
    orders.update_card(conn, mine, {"client_phone": "+7 922 333-44-55"}, LATER, "pavel")
    sheet.flush(conn, box, fake, LATER)
    assert fake.rows[first]["Телефон"] == "89990000000" and fake.rows[second]["Телефон"] == "89223334455"
    said = events(conn, mine)
    assert sheet.EDITED_LINE % 1 in said and "Google-таблица: телефон обновлён (1 стр.)." in said
    assert failing(conn, mine) == 0                                               # not a failure: no retry
    stored = json.loads(conn.execute("SELECT rows FROM sheet_order WHERE order_id = ?", (mine,)).fetchone()[0])
    assert stored[first].get("_missing") and not stored[second].get("_missing")
    posts = len(fake.data_posts())
    assert sheet.flush(conn, box, fake, LATEST) == {}
    assert sheet.flush(conn, box, fake, LATEST, full=True) == {mine: "нечего отправлять"}
    assert len(fake.data_posts()) == posts                                        # never sent again
    late = "2026-10-24T12:00:00"
    assert sheet.lasting(conn, late) == [] and sheet.escalate(conn, late) == []   # no «Нужно действие»


# ---- the review of 24.11: a lost answer, a phone typed by hand first, the changes made before the deploy -----------------------------

def stored(conn, card_id):
    return json.loads(conn.execute("SELECT rows FROM sheet_order WHERE order_id = ?", (card_id,)).fetchone()[0])


def test_a_lost_answer_is_sent_again_and_the_row_is_not_said_edited(tmp_path):
    conn, box, fake = table_with(tmp_path, LostAnswer())
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    orders.update_card(conn, mine, {"client_phone": "+7 922 333-44-55"}, LATER, "pavel")
    fake.lose = True
    assert sheet.flush(conn, box, fake, LATER)[mine].startswith("ошибка")
    assert {r["Телефон"] for r in fake.rows.values()} == {"89223334455"} and failing(conn, mine) == 1   # written, the panel never knew
    sheet.flush(conn, box, fake, "2026-10-23T12:40:00")                           # the retry: checked by the old phone, then by the new
    first, second = fake.data_posts()[-2:]
    assert all(r["check"]["Телефон"] == OLD_PHONE for r in first["rows"])
    assert all(r["check"]["Телефон"] == "89223334455" and r["fields"] == {"Телефон": "89223334455"} for r in second["rows"])
    said = events(conn, mine)
    assert sheet.EDITED_LINE % 2 not in said and said[-1] == "Google-таблица: телефон обновлён (2 стр.)."
    rows = stored(conn, mine)
    assert failing(conn, mine) == 0 and all(not r.get("_missing") and r["Телефон"] == "89223334455" for r in rows.values())


def test_a_new_phone_the_owner_typed_into_the_cell_first_is_no_edit(tmp_path):
    conn, box, fake = table_with(tmp_path, NewScript())
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    for row in fake.rows.values():
        row["Телефон"] = "89223334455"                                            # the owner typed the new number before the panel had it
    orders.update_card(conn, mine, {"client_phone": "+7 922 333-44-55"}, LATER, "pavel")
    sheet.flush(conn, box, fake, LATER)
    said = events(conn, mine)
    assert not any("изменена вручную" in e for e in said) and said[-1] == "Google-таблица: телефон обновлён (2 стр.)."
    assert all(not r.get("_missing") and r["Телефон"] == "89223334455" for r in stored(conn, mine).values())


def test_a_change_made_before_the_deploy_never_rewrites_the_cell_and_a_later_one_does(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    first = sorted(fake.rows)[0]
    fake.rows[first]["Имя"] = "Иван (постоянный)"                                  # the owner corrected the cell
    store.set_state(conn, sheet.BASELINE_KEY, "")                                 # the deploy: its first pass is still to come
    orders.update_card(conn, mine, {"client_name": "Иван Петров", "client_phone": "+7 922 333-44-55"}, LATER, "pavel")
    conn.execute("UPDATE sheet_order SET seen_event = ? WHERE order_id = ?", (sheet._last_event(conn, mine), mine))   # the old panel saw it
    conn.commit()
    posts = len(fake.data_posts())
    assert sheet.flush(conn, box, fake, LATEST) == {}
    assert sheet.flush(conn, box, fake, LATEST, full=True) == {mine: "нечего отправлять"}
    assert len(fake.data_posts()) == posts and store.get_state(conn, sheet.BASELINE_KEY) == LATEST
    assert fake.rows[first]["Имя"] == "Иван (постоянный)" and {r["Телефон"] for r in fake.rows.values()} == {OLD_PHONE}
    assert not any(e.startswith("Google-таблица:") for e in events(conn, mine))
    orders.update_card(conn, mine, {"client_phone": "+7 933 444-55-66"}, "2026-10-23T14:00:00", "pavel")   # after the deploy: it follows
    sheet.flush(conn, box, fake, "2026-10-23T14:00:00")
    sent = fake.data_posts()[-1]["rows"]
    assert all(r["fields"] == {"Телефон": "89334445566"} and r["check"]["Телефон"] == OLD_PHONE for r in sent)   # checked by the cell's
    assert fake.rows[first]["Имя"] == "Иван (постоянный)" and {r["Телефон"] for r in fake.rows.values()} == {"89334445566"}
    assert all(sheet.SEEN_PHONE not in r for r in stored(conn, mine).values())


def test_a_change_after_the_deploy_before_the_first_pass_still_goes(table):
    conn, box, fake = table
    mine = placed(conn)
    sheet.flush(conn, box, fake, NOW)
    store.set_state(conn, sheet.BASELINE_KEY, "")
    orders.update_card(conn, mine, {"client_name": "Пётр"}, LATER, "pavel")      # not looked at yet: a change after the deploy
    sheet.flush(conn, box, fake, LATER)
    assert all(r["fields"] == {"Имя": "Пётр"} for r in fake.data_posts()[-1]["rows"])
    assert {r["Имя"] for r in fake.rows.values()} == {"Пётр"} and store.get_state(conn, sheet.BASELINE_KEY) == LATER
