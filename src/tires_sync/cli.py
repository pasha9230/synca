from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import ad_stats, avito_report, avito_stock, catalog_archive, catalog_probe, chat_deals, chats, mirror_feed, order_engine, users, store
from .candidates import evaluate
from .catalog import parse_reference
from .config import load_settings
from .cycle import remember_pass_removals, run_for_accounts, stock_pass
from . import connections, tochki_feed
from .avito import AvitoClient, AvitoError
from .suppliers import load_all_parsed, unload_path_for
from .backup import BackupError, backup_db, verify_backup
from . import offsite
from . import notify
from .notify import NotifyError, notifier_for
from .fetcher import fetch_for_connection
from .publish import PublishError, count_ads, ensure_feed, plan_feed, promote_feed, status_report, upload_now
from .publishing import clear_expected, expected_for, feed_lock
from .mappings import MappingError, auto_mappings, confirm_mapping, pending_mappings
from .secretbox import SecretKeyError, load_box
from .settings_store import SettingsNotInitialized, active_settings, get_active, init_settings
from .sync import run_cycle
from .transport import urllib_transport
from .webmim import parse_unload
from .webmim_client import WebmimError


def _now() -> str:
    """UTC, as the panel's now_iso: the server's own zone (Moscow on tires-server) never leaks into the base."""
    return datetime.utcnow().isoformat(timespec="seconds")


def _http():
    return urllib_transport


def moscow_now() -> datetime:
    """The clock of the supplier's catalog: it empties its baskets at midnight Moscow time."""
    return datetime.utcnow() + timedelta(hours=3)


def _epoch() -> float:
    return time.time()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tires_sync")
    sub = parser.add_subparsers(dest="command", required=True)

    dry = sub.add_parser("dry-run", help="посчитать цикл и собрать фид локально, ничего не отправляя")
    dry.add_argument("--xml", required=True)
    dry.add_argument("--reference", required=True)
    dry.add_argument("--settings", help="файл настроек; без него берутся активные настройки из БД")
    dry.add_argument("--db", required=True)
    dry.add_argument("--out", required=True)

    pq = sub.add_parser("photo-queue", help="какие наборы фото нужны и поставить задания генерации в очередь")
    pq.add_argument("--db", required=True)
    pq.add_argument("--unload", required=True)
    pq.add_argument("--reference", required=True)
    pq.add_argument("--dry-run", action="store_true", help="только показать наборы, ничего не записывать")
    pa = sub.add_parser("photo-agent-tick", help="запустить агента фото, если в панели нажали «Сгенерировать» (без запроса сразу выходит)")
    pa.add_argument("--db", required=True)
    pa.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    pa.add_argument("--agent-bin", default="/opt/tires-sync/agent/tire-photo-agent")
    pa.add_argument("--agent-dir", default="/var/lib/tires-sync/agent", help="папка агента: pending (оплаченные, но не отданные кадры) и backgrounds (фото точки выдачи)")

    st = sub.add_parser("settings", help="настройки в БД")
    st_sub = st.add_subparsers(dest="action", required=True)
    init = st_sub.add_parser("init")
    init.add_argument("--file", required=True)
    init.add_argument("--db", required=True)
    show = st_sub.add_parser("show")
    show.add_argument("--db", required=True)

    mp = sub.add_parser("mappings", help="сопоставления бренд/модель")
    mp_sub = mp.add_subparsers(dest="action", required=True)
    pend = mp_sub.add_parser("pending")
    pend_src = pend.add_mutually_exclusive_group(required=True)
    pend_src.add_argument("--xml", help="выгрузка первого поставщика")
    pend_src.add_argument("--unload", help="выгрузка первого поставщика; файлы остальных лежат рядом (все поставщики)")
    pend.add_argument("--reference", required=True)
    pend.add_argument("--db", required=True)
    auto = mp_sub.add_parser("auto", help="прогнать все бренды по справочнику Авито и принять безопасные предложения")
    auto.add_argument("--unload", required=True)
    auto.add_argument("--reference", required=True)
    auto.add_argument("--db", required=True)
    auto.add_argument("--apply", action="store_true", help="записать (без флага только показать)")
    conf = mp_sub.add_parser("confirm")
    conf.add_argument("--brand", required=True)
    conf.add_argument("--model", required=True)
    conf.add_argument("--to", required=True)
    conf.add_argument("--reference", required=True)
    conf.add_argument("--db", required=True)
    om = sub.add_parser("old-model-import", help="прежние названия моделей ({old_model}) из CSV brand,model,former_name; без --apply только показывает")
    om.add_argument("file")
    om.add_argument("--db", required=True)
    om.add_argument("--reference", required=True)
    om.add_argument("--apply", action="store_true", help="записать одной транзакцией (сделайте копию базы до этого); без него ничего не пишется")
    om.add_argument("--skip-bad", action="store_true", help="с --apply записать хорошие строки, даже если в файле есть отказы")
    run = sub.add_parser("run-cycle", help="цикл по всем аккаунтам Авито (без отправки на Авито), фид в папке аккаунта")
    run.add_argument("--db", required=True)
    run.add_argument("--unload", required=True)
    run.add_argument("--reference", required=True)
    run.add_argument("--feed-root", required=True)
    run.add_argument("--fetch", action="store_true", help="сначала скачать свежую выгрузку WebMIM")
    run.add_argument("--publish", action="store_true", help="после цикла опубликовать фид у нас для аккаунтов с включённой публикацией")
    run.add_argument("--stock-only", action="store_true",
                     help="лёгкий проход между циклами: только снять позиции с остатком ниже порога и пропавшие из выгрузки (цены, названия, фото, Id не меняются)")
    run.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    pub = sub.add_parser("avito-publish", help="указать ссылку на фид в автозагрузке Авито и запустить загрузку")
    pub.add_argument("--db", required=True)
    pub.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    pub.add_argument("--account", type=int, required=True)
    pub.add_argument("--public-base", required=True, help="например https://panel.example.com")
    pub.add_argument("--feed-root", required=True)
    pub.add_argument("--confirm", action="store_true", help="без этого флага только читаем профиль и печатаем план")
    pub.add_argument("--allow-shrink", action="store_true", help="разрешить фид, сократившийся более чем вдвое")
    mg = sub.add_parser("mirror-go-live", help="вывести аккаунт с фидом-зеркалом в эфир одним проверенным шагом: пересобрать, сверить Id, опубликовать, указать ссылку, запустить загрузку")
    mg.add_argument("--db", required=True)
    mg.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    mg.add_argument("--account", type=int, required=True)
    mg.add_argument("--public-base", required=True)
    mg.add_argument("--feed-root", required=True)
    mg.add_argument("--unload", required=True, help="выгрузка поставщика: объявления позиций, которых у него нет или мало, не публикуются")
    mg.add_argument("--confirm", action="store_true", help="без этого флага только собираем, сверяем и печатаем план")
    fp = sub.add_parser("feed-publish", help="сделать черновик фида публичным у нас (на Авито ничего не отправляется)")
    fp.add_argument("--db", required=True)
    fp.add_argument("--account", type=int, required=True)
    fp.add_argument("--feed-root", required=True)
    fp.add_argument("--allow-shrink", action="store_true", help="разрешить фид, сократившийся более чем вдвое")
    st = sub.add_parser("avito-status", help="состояние последней загрузки и объявлений на Авито (только чтение)")
    st.add_argument("--db", required=True)
    st.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    st.add_argument("--account", type=int, required=True)
    cp = sub.add_parser("catalog-probe", help="скачать выгрузку и записать, сколько наших позиций сейчас в ней (ничего не публикует)")
    cp.add_argument("--db", required=True)
    cp.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    cp.add_argument("--work-file", required=True, help="временный файл для выгрузки (не тот, из которого работает панель)")
    ot = sub.add_parser("orders-tick", help="часовая проверка заказов: задачи по правилам «завис на этапе» и оповещение ответственному об окончании резерва на точке (клиентам ничего не отправляет)")
    ot.add_argument("--db", required=True)
    ot.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    se = sub.add_parser("sheet-enroll", help="поставить в очередь Google-таблицы открытые заказы, оформленные до её включения (строки только добавляются; запуск вручную)")
    se.add_argument("--db", required=True)
    se.add_argument("--open", action="store_true", required=True, help="открытые заказы: от «Заказ оформлен» до «На ОТХ» и «Возврат передан»")
    se.add_argument("--dry-run", action="store_true", help="только показать список, ничего не ставить в очередь")
    vc = sub.add_parser("vk-catchup", help="подборщик ВК: прочитать переписку с точками и выполнить их сообщения, о которых ВК не прислал уведомление (не старше суток, каждое один раз); для таймера каждые 10–15 минут")
    vc.add_argument("--db", required=True)
    vc.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    vm = vc.add_mutually_exclusive_group()
    vm.add_argument("--dry-run", action="store_true", help="только показать, что было бы подобрано (ничего не выполняется и не отправляется)")
    vm.add_argument("--seed", action="store_true", help="запомнить, где сейчас кончается переписка каждой точки, ничего не выполняя (первое включение без прошлых суток)")
    ws = sub.add_parser("webmim-orders-sync", help="прочитать заказы WebMIM за последние дни в снимок для сверки с заказами панели (только чтение: в WebMIM ничего не создаётся и не меняется)")
    ws.add_argument("--db", required=True)
    ws.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    ws.add_argument("--days", type=int, default=60, help="за сколько дней читать (по дате создания в WebMIM), по умолчанию 60")
    fi = sub.add_parser("feed-import", help="загрузить чужой фид (файл) в аккаунт: его объявления отдаются как есть, с теми же Id (режим «зеркало»); Авито не трогаем")
    fi.add_argument("--db", required=True)
    fi.add_argument("--account", type=int, required=True)
    fi.add_argument("--file", required=True)
    fc = sub.add_parser("feed-check", help="проверить фиды аккаунтов по правилам проверки (feed_check_rules.json); ничего не меняет и не отправляет")
    fc.add_argument("--db", required=True)
    fc.add_argument("--unload", required=True)
    fc.add_argument("--reference", required=True)
    fc.add_argument("--feed-root", required=True)
    fc.add_argument("--published", action="store_true", help="проверить опубликованный фид, а не черновик")
    fc.add_argument("--account", type=int, default=0, help="номер аккаунта; без него все")
    oc = sub.add_parser("overrides-clear-aged", help="снять ручное «вкл» у всех позиций «старше N лет» (исключение «старше» снова работает); без --apply только показывает список")
    oc.add_argument("--db", required=True)
    oc.add_argument("--unload", required=True)
    oc.add_argument("--apply", action="store_true", help="записать новую версию настроек (автор cli); без него ничего не пишется")
    mr = sub.add_parser("match-report", help="одна шина у разных поставщиков (tire_match): пары, сомнительные, что изменилось бы в фидах при включении склейки; ничего не меняет")
    mr.add_argument("--db", required=True)
    mr.add_argument("--unload", required=True)
    mr.add_argument("--reference", required=True)
    mr.add_argument("--account", type=int, default=0, help="номер аккаунта; без него все")
    mr.add_argument("--limit", type=int, default=40, help="сколько строк каждого списка показать")
    mr.add_argument("--tochki-mode", choices=("no_twin", "merge"), default="",
                    help="режим пробы 4 точек для сценария «если включить пробу» (по умолчанию из настроек, иначе no_twin)")
    ms = sub.add_parser("match-synonym-add", help="записать, что у бренда модель ALIAS это та же модель CANONICAL (для tire_match; только добавление)")
    ms.add_argument("--db", required=True)
    ms.add_argument("brand")
    ms.add_argument("alias")
    ms.add_argument("canonical")
    ms.add_argument("--author", default="владелец")
    mf = sub.add_parser("match-pair-forget", help="забыть запомненную пару (tire_match): память её больше не использует; строка остаётся, "
                                                  "запись в журнале; --undo возвращает")
    mf.add_argument("--db", required=True)
    mf.add_argument("code_a")
    mf.add_argument("code_b")
    mf.add_argument("--undo", action="store_true", help="вернуть забытую пару")
    mf.add_argument("--author", default="владелец")
    mn = sub.add_parser("match-not-pair", help="«это не пара» (tire_match): две позиции разных поставщиков никогда не склеиваются, даже если "
                                               "обе в выгрузке, память их тоже не использует; отметка, запись в журнале; --undo снимает запрет")
    mn.add_argument("--db", required=True)
    mn.add_argument("code_a")
    mn.add_argument("code_b")
    mn.add_argument("--undo", action="store_true", help="снять запрет")
    mn.add_argument("--author", default="владелец")
    ad = sub.add_parser("mirror-adopt", help="перевести аккаунт с зеркала на нашу сборку, сохранив Id и ссылки на фото его объявлений; без --confirm только показывает план")
    ad.add_argument("--db", required=True)
    ad.add_argument("--account", type=int, required=True)
    ad.add_argument("--confirm", action="store_true")
    ir = sub.add_parser("ids-reconcile", help="найти в кабинете Авито живые объявления с Id, которых нет в нашем фиде, но та же шина есть в фиде под другим Id; привязать позицию к живому Id; без --confirm только показывает")
    ir.add_argument("--db", required=True)
    ir.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    ir.add_argument("--account", type=int, required=True)
    ir.add_argument("--feed-root", required=True)
    ir.add_argument("--confirm", action="store_true")
    ih = sub.add_parser("ids-heal", help="по отчёту Авито: позиции, чьё объявление удалено или заблокировано, берут Id живого объявления той же шины; новый Id только с --renew (по слову владельца); без --confirm только показывает")
    ih.add_argument("--db", required=True)
    ih.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    ih.add_argument("--account", type=int, required=True)
    ih.add_argument("--feed-root", required=True)
    ih.add_argument("--renew", action="store_true", help="вместе с --confirm выдать новые Id удалённым без живого двойника (только по слову владельца)")
    ih.add_argument("--confirm", action="store_true")
    iu = sub.add_parser("ids-unrenew", help="отменить новые Id позиций (вернуть прежний Id); без --confirm только показывает")
    iu.add_argument("--db", required=True)
    iu.add_argument("--account", type=int, required=True)
    iu.add_argument("--codes", required=True, help="коды позиций через запятую")
    iu.add_argument("--confirm", action="store_true")
    ia = sub.add_parser("ids-alias", help="Id объявлений аккаунта без кода поставщика (буквы и цифры, свои у каждого аккаунта); без --confirm только показывает; --off возвращает прежние Id")
    ia.add_argument("--db", required=True)
    ia.add_argument("--account", type=int, required=True)
    ia.add_argument("--off", action="store_true")
    ia.add_argument("--confirm", action="store_true")
    ai = sub.add_parser("avito-ids-sync", help="привязать позиции к Id объявлений, которые уже есть в кабинете Авито (чтобы Авито не блокировал «повторное размещение»); без --confirm только показывает")
    ai.add_argument("--db", required=True)
    ai.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    ai.add_argument("--account", type=int, required=True)
    ai.add_argument("--feed-root", required=True)
    ai.add_argument("--allow-shared-until", default="", help="ГГГГ-ММ-ДД: разрешить тот же Id, что у другого аккаунта, до этой даты (потом позиция получит свой Id)")
    ai.add_argument("--confirm", action="store_true")
    sp = sub.add_parser("stock-push", help="число «в наличии» у объявлений на Авито по остатку поставщика (API остатков Авито); без --confirm только показывает, что ушло бы; --info только читает, что показывает Авито; --restore возвращает числа, которые были до нас")
    sp.add_argument("--db", required=True)
    sp.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    sp.add_argument("--account", type=int, required=True)
    sp.add_argument("--feed-root", required=True)
    sp.add_argument("--unload", required=True)
    sp.add_argument("--dry-run", action="store_true", help="только показать (так и без --confirm)")
    sp.add_argument("--info", action="store_true", help="только чтение: остатки нескольких наших объявлений, как их показывает Авито")
    sp.add_argument("--limit", type=int, default=5, help="сколько объявлений читать с --info")
    sp.add_argument("--restore", action="store_true", help="вернуть числа, которые Авито показывал до нашей первой отправки (аккаунт должен быть выключен)")
    sp.add_argument("--confirm", action="store_true")
    ri = sub.add_parser("mirror-reissue", help="дать объявлениям зеркала новые Id (Авито не возвращает удалённое из кабинета под прежним Id); без --confirm только показывает")
    ri.add_argument("--db", required=True)
    ri.add_argument("--account", type=int, required=True)
    ri.add_argument("--ids", required=True, help="Id объявлений через запятую")
    ri.add_argument("--stamp", default="", help="метка в новом Id, ГГГГММДД (по умолчанию сегодня по Москве)")
    ri.add_argument("--confirm", action="store_true")
    ts = sub.add_parser("chat-test-send", help="ПРОБНАЯ отправка одного сообщения в чат (запись на Авито); без --confirm только показывает, что будет отправлено")
    ts.add_argument("--db", required=True)
    ts.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    ts.add_argument("--chat", type=int, required=True, help="номер чата в панели (Ч-N)")
    ts.add_argument("--text", required=True)
    ts.add_argument("--confirm", action="store_true", help="действительно отправить")
    tg = sub.add_parser("chat-test-send-image", help="ПРОБНАЯ отправка одного фото в чат (запись на Авито); без --confirm только показывает, что будет отправлено")
    tg.add_argument("--db", required=True)
    tg.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    tg.add_argument("--chat", type=int, required=True, help="номер чата в панели (Ч-N)")
    tg.add_argument("--file", required=True, help="файл с фотографией")
    tg.add_argument("--confirm", action="store_true", help="действительно отправить")
    ti = sub.add_parser("telegram-inbox", help="прочитать, что вы переслали боту уведомлений (только сообщения из вашего чата; прочитанное помечается)")
    ti.add_argument("--db", required=True)
    ti.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    ti.add_argument("--save-to", default="", help="папка, куда скачать файлы и фото (скачиваются до того, как сообщения помечаются прочитанными)")
    cs = sub.add_parser("chats-sync", help="прочитать чаты Авито аккаунтов с включённым чтением чатов и привязать их к сделкам (только чтение у Авито)")
    cs.add_argument("--db", required=True)
    cs.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    cs.add_argument("--account", type=int, default=0, help="номер аккаунта; без него все аккаунты с чатами")
    cs.add_argument("--unload", default="", help="не используется (оставлено для совместимости юнита; позицию в сделку кнопка «Создать сделку» берёт из панели)")
    cs.add_argument("--reference", default="", help="не используется (см. --unload)")
    ca = sub.add_parser("calls-sync", help="прочитать звонки через подменные номера Авито всех аккаунтов с ключами, скачать записи (хранятся 7 дней) и удалить старые (только чтение у Авито)")
    ca.add_argument("--db", required=True)
    ca.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    ca.add_argument("--account", type=int, default=0, help="номер аккаунта; без него все аккаунты с ключами")
    ca.add_argument("--dry-run", action="store_true", help="только прочитать и показать, сколько звонков: в базу ничего не пишется, записи не скачиваются")
    for name, text in (("avito-webhook-subscribe", "подписать аккаунт на вебхук мессенджера Авито (без --confirm только показать; запись в Авито — с --confirm)"),
                       ("avito-webhook-unsubscribe", "снять наши подписки вебхука у аккаунта (без --confirm только показать; запись в Авито — с --confirm)"),
                       ("avito-webhook-status", "подписки вебхука аккаунтов: что у нас и что у Авито (только чтение)")):
        wh = sub.add_parser(name, help=text)
        wh.add_argument("--db", required=True)
        wh.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
        wh.add_argument("--account", type=int, required=name != "avito-webhook-status", default=0, help="номер аккаунта")
        wh.add_argument("--base-url", default="", help="адрес панели снаружи (по умолчанию TIRES_PUBLIC_BASE, иначе public_base в базе)")
        if name != "avito-webhook-status":
            wh.add_argument("--confirm", action="store_true", help="действительно записать в Авито")
    ss = sub.add_parser("avito-stats", help="собрать статистику Авито (просмотры, контакты, избранное) по дням (только чтение)")
    ss.add_argument("--db", required=True)
    ss.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    ss.add_argument("--account", type=int, default=0, help="номер аккаунта; без него все аккаунты с ключами")
    ss.add_argument("--days", type=int, default=7, help="сколько последних дней обновлять (до 270; первый запуск удобно с 30)")
    al = sub.add_parser("alert", help="отправить сообщение в Telegram (для служб systemd при сбое)")
    al.add_argument("--db", required=True)
    al.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    al.add_argument("--message", required=True)
    for name, text in (("user-add", "добавить пользователя панели"), ("user-passwd", "сменить пароль пользователя (закрывает его сессии)"),
                       ("user-remove", "удалить пользователя панели"), ("user-list", "список пользователей панели")):
        up = sub.add_parser(name, help=text)
        up.add_argument("--db", required=True)
        if name != "user-list":
            up.add_argument("--login", required=True)
        if name in ("user-add", "user-passwd"):
            up.add_argument("--password-stdin", action="store_true", help="прочитать пароль из стандартного ввода (иначе спросит скрытно)")
        if name == "user-add":
            up.add_argument("--role", default="owner", choices=sorted(users.ROLES), help="роль (по умолчанию owner; client — клиент-закупщик)")
            up.add_argument("--point-id", type=int, default=None, help="точка, для роли point")
            up.add_argument("--name", default="", help="имя или компания, как в панели")
    cl = sub.add_parser("clients-link", help="связать старые заказы и чаты с клиентами по телефону и собеседнику Авито: без --confirm только отчёт")
    cl.add_argument("--db", required=True)
    cl.add_argument("--confirm", action="store_true", help="записать (сделайте копию базы до этого); без него ничего не пишется")
    bk = sub.add_parser("backup", help="резервная копия базы (сжатая, с ротацией)")
    bk.add_argument("--db", required=True)
    bk.add_argument("--out-dir", required=True)
    bk.add_argument("--keep", type=int, default=14)
    bo = sub.add_parser("backup-offsite", help="зашифрованная копия базы в облачное хранилище (S3), проверка, что она читается")
    bo.add_argument("--db", required=True)
    bo.add_argument("--config", default="/etc/tires-sync/offsite.conf", help="файл настроек хранилища; нет файла: копия не настроена")
    bg = sub.add_parser("backup-offsite-get", help="для восстановления: скачать и расшифровать копию в обычный state-<дата>.db.gz")
    bg.add_argument("--out", required=True, help="куда записать .db.gz (существующий файл не перезаписывается)")
    bg.add_argument("--config", default="/etc/tires-sync/offsite.conf")
    bg.add_argument("--name", default="", help="имя копии (state-ГГГГ-ММ-ДД.db.gz.enc); без него самая свежая")
    bg.add_argument("--in", dest="in_file", default="", help="файл копии, скачанный вручную (тогда хранилище не нужно)")
    bg.add_argument("--key-file", default="", help="ключ копий; без него берётся из файла настроек")
    bn = sub.add_parser("backup-key-new", help="создать новый ключ шифрования копий (существующий файл не перезаписывается)")
    bn.add_argument("--out", required=True)
    fetch = sub.add_parser("fetch-unload", help="скачать выгрузку WebMIM по сохранённому подключению")
    fetch.add_argument("--db", required=True)
    fetch.add_argument("--out", required=True)
    fetch.add_argument("--supplier", type=int, default=1, help="номер поставщика (по умолчанию первый)")
    fetch.add_argument("--key-file", default=os.environ.get("TIRES_SECRET_KEY_FILE", ""))
    return parser


def _cmd_old_model_import(args) -> int:
    from . import old_model_import
    ref = parse_reference(Path(args.reference).read_text(encoding="utf-8"))
    if not ref:
        raise ValueError("справочник Авито пуст: %s" % args.reference)
    conn = store.connect(args.db)
    try:
        return old_model_import.run(conn, ref, args.file, args.apply, args.skip_bad, datetime.utcnow().isoformat(timespec="seconds"))   # UTC like the panel
    except (csv.Error, sqlite3.Error) as err:                       # exit 2 like any error: 1 means a refused row
        raise ValueError("%s (ничего не записано)" % err)
    finally:
        conn.close()


def _cmd_dry_run(args) -> int:
    conn = store.connect(args.db)
    settings = load_settings(args.settings) if args.settings else active_settings(conn)
    ref = parse_reference(Path(args.reference).read_text(encoding="utf-8"))
    report = run_cycle(Path(args.xml).read_bytes(), settings, ref, conn, date.today(), out_dir=args.out)
    print("actions:", json.dumps(report.counts, ensure_ascii=False))
    print("feed:", report.feed_path, "ads:", report.feed_count)
    print("guard tripped:", report.guard_tripped)
    print("other territories:", report.other_territories)
    print("unmatched models:", len(report.unmatched))
    for item in report.unmatched[:20]:
        print("  ", item["goods_id"], item["brand"], item["model"], "->", item["suggestions"])
    return 0


def _cmd_photo_queue(args) -> int:
    from . import photo_library
    conn = store.connect(args.db)
    settings = active_settings(conn)
    ref = parse_reference(Path(args.reference).read_text(encoding="utf-8"))
    parsed, _, _ = load_all_parsed(conn, settings, Path(args.unload))
    ev = evaluate(parsed.offers, parsed.issues, settings, ref, store.load_confirmed_mappings(conn), store.load_brand_map(conn))
    plans = photo_library.needed_sets(ev.ok, settings.min_stock)
    for p in plans:
        print("%-60s позиций %-3d остаток %-5d %-12s %s" % (p.key.text(), p.positions, p.stock, p.size_hint, p.state))
    print("наборов: %d" % len(plans))
    if args.dry_run:
        return 0
    now = _now()
    photo_library.sync_sets(conn, plans, now)
    print("новых заданий: %d" % photo_library.enqueue(conn, now))
    return 0


def _cmd_fetch(args) -> int:
    box = load_box(args.key_file)
    if box is None:
        print("error: мастер-ключ недоступен (проверьте --key-file или TIRES_SECRET_KEY_FILE)", file=sys.stderr)
        return 2
    conn = store.connect(args.db)
    if connections.get_supplier(conn, args.supplier) is None:
        print("error: поставщик №%d не найден" % args.supplier, file=sys.stderr)
        return 2
    meta = fetch_for_connection(conn, box, Path(args.out), _now(), http=_http(), supplier_id=args.supplier)
    print("items: %d, fetched_at: %s" % (meta["items"], meta["fetched_at"]))
    return 0


ALERT_PAUSE_SECONDS = 600
REPEAT_PAUSE_SECONDS = 6 * 3600     # the same text again: the cycle runs every hour since 2026-10-20, a lasting failure is told as before (every 6 hours)


SAID_PREFIX = "alert-said:"         # app_state: the moment each text was last said (epoch); a text said within six hours is not said again


def _alert(conn, box, text: str, service: bool = False, notice: bool = False, recovery: bool = False) -> bool:
    """Record the message in the journal and send it to Telegram. The same failure text is not repeated within six hours, whatever went
    between (each text keeps its own mark, taken in one statement: of two processes with the same text, one sends it); the
    service's own failure alert (service=True) is skipped if any message went out meanwhile, since the cycle has just
    said the same. notice=True marks information (not a failure: new models without a pair, a reminder): it keeps no mark of its own (only
    the same text twice in a row is quiet). recovery=True is a notice that a failure is over («снова отвечает», «таблица снова принимает»):
    only it, once handled, frees the failures' marks — the next failure after a recovery is told; any other notice leaves them (хвост N1).
    True: handled (sent, recorded or already said). False: Telegram refused it; the marks are given back, so the next call sends it again."""
    notice = notice or recovery
    notify = notifier_for(conn, box, http=_http())
    last = store.get_state(conn, "last_alert_at")
    since = _epoch() - float(last) if last else None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    if since is not None and ((service and since < ALERT_PAUSE_SECONDS)
                              or (since < REPEAT_PAUSE_SECONDS and store.get_state(conn, "last_alert_text") == digest)):   # also the marks of before
        return True
    before, stamp = (last, store.get_state(conn, "last_alert_text")), str(_epoch())
    said_key, window = SAID_PREFIX + digest, _epoch() - REPEAT_PAUSE_SECONDS
    earlier = store.get_state(conn, said_key)
    taken = notice or conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value "
                                   "WHERE CAST(value AS REAL) <= ?", (said_key, stamp, window)).rowcount == 1
    conn.execute("DELETE FROM app_state WHERE key LIKE ? AND CAST(value AS REAL) <= ?", (SAID_PREFIX + "%", window))   # never pile up
    conn.commit()
    if not taken:
        return True                                                      # said within six hours, here or by another process
    store.set_state(conn, "last_alert_at", stamp)
    store.set_state(conn, "last_alert_text", digest)
    store.write_audit(conn, _now(), "system", "notice" if notice else "alert", {"text": text})   # the journal keeps it without Telegram
    if notify is None:
        return _told(conn, recovery)
    try:
        notify(text)
    except NotifyError as err:
        print("не удалось отправить уведомление: %s" % err, file=sys.stderr)
        if (store.get_state(conn, "last_alert_at"), store.get_state(conn, "last_alert_text")) == (stamp, digest):   # not another process's since
            store.set_state(conn, "last_alert_at", before[0] or "")  # not said: the next call sends it, never «said already»
            store.set_state(conn, "last_alert_text", before[1] or "")
        if not notice:
            conn.execute("UPDATE app_state SET value = ? WHERE key = ? AND value = ?", (earlier or "0", said_key, stamp))   # not said: free again
        conn.commit()
        return False
    return _told(conn, recovery)


def _told(conn, recovery: bool) -> bool:
    """A recovery handled: the failures' marks go, so the next failure after it is told at once. Other news keeps them."""
    if recovery:
        conn.execute("DELETE FROM app_state WHERE key LIKE ?", (SAID_PREFIX + "%",))
        conn.commit()
    return True


GUARD_LIST_LIMIT = 20


def _snapshot_title(snapshot) -> str:
    try:
        return (ET.fromstring(snapshot).findtext("Title") or "").strip() if snapshot else ""
    except ET.ParseError:
        return ""


def _guard_alert_text(conn, tripped, parsed) -> str:
    """The alert of the guard against mass removal, with the positions it keeps (owner, 2026-10-24): code, name, why it would go. The same
    list gives the same text, so a guard that lasts is not told again within six hours (_alert)."""
    offers = getattr(parsed, "offers", {}) or {}
    stocks = getattr(parsed, "stocks", {}) or {}
    lines = ["Tires Sync: сработала защита от массового снятия (%s). Объявления удерживаются со старыми ценами и остатками. "
             "Проверьте выгрузку поставщика." % ", ".join(r.name for r in tripped)]
    room = GUARD_LIST_LIMIT                                        # for all accounts together: the message stays within Telegram's limit
    for r in tripped:
        items = sorted(r.guarded)
        lines.append("%s, удерживается %d:" % (r.name, len(items)))
        if getattr(r, "removed_lately", 0):
            lines.append("  (за последний час проверка остатков уже сняла %d, защита считает их вместе)" % r.removed_lately)
        shown, room = items[:room], room - len(items[:room])
        for code, reason in shown:
            offer = offers.get(code)
            stock = offer.stock if offer is not None else stocks.get(code)
            why = ("остаток %d" % stock if reason == "low_stock" and stock is not None
                   else "нет в выгрузке" if reason == "absent_from_supplier" else reason)
            name = offer.name if offer is not None else _snapshot_title(store.get_snapshot(conn, code, r.account_id))
            lines.append("  %s %s — %s" % (code, name, why) if name else "  %s — %s" % (code, why))
        if len(items) > len(shown):
            lines.append("  … и ещё %d" % (len(items) - len(shown)))
    return "\n".join(lines)


def _record_economics(conn, parsed, ref) -> None:
    """The day's picture of the catalog's unit economics, for the dynamics chart. It never stops a cycle."""
    try:
        from . import terms as terms_store
        from .panel import economics_view, service
        from .settings_store import get_active
        accounts = connections.list_feed_accounts(conn)
        settings = active_settings(conn)
        effective = connections.effective_settings(conn, accounts[0]["id"], settings) if accounts else settings
        day = (datetime.utcnow() + timedelta(hours=3)).date().isoformat()        # the owner's day: Moscow
        visible = service.hide_excluded(parsed, get_active(conn)[1])
        rows = economics_view.positions(conn, effective, visible, ref, day, terms_store.get_params(conn))
        terms_store.record_snapshot(conn, day, economics_view.by_brand(rows))
    except Exception as err:                                       # noqa: BLE001 - a picture must not break a cycle
        print("предупреждение: снимок юнит-экономики не записан: %s" % err, file=sys.stderr)


def _alert_new_unmatched(conn, box, results) -> None:
    """Models that wait for a pair and were not announced before: one message, so that nobody has to look in.
    A failed cycle says nothing and changes nothing; the list is remembered only once the message went through."""
    ok = [r for r in results if r.ok]
    if not ok:
        return
    current = set().union(*[set(r.unmatched) for r in ok])
    try:
        known = set(json.loads(store.get_state(conn, "alerted_unmatched") or "[]"))
    except ValueError:
        known = set()
    fresh = sorted(current - known)
    if fresh:
        shown = "; ".join(fresh[:10]) + ("; и ещё %d" % (len(fresh) - 10) if len(fresh) > 10 else "")
        delivered = _alert(conn, box, "Tires Sync: новые модели не сопоставлены (%d): %s. Сопоставьте их на странице «Сопоставления»: "
                           "пока этого не сделано, объявления этих моделей не публикуются." % (len(fresh), shown), notice=True)
        if not delivered:
            return
    store.set_state(conn, "alerted_unmatched", json.dumps(sorted(current), ensure_ascii=False))


def _alert_own_below_cost(conn, box, offers, settings=None) -> None:
    """Own sale prices that the supplier's purchase price went above: one message for the new ones; the prices are kept (the owner 2026-10-29).
    The marks are kept only once the message went through, so a refused one is told by the next cycle. A side notice: whatever fails in it
    (the settings, the marks, the message, a busy base) is printed and never stops the publishing of the feeds."""
    from . import own_price
    try:
        lines, keep = own_price.fresh_events(conn, settings if settings is not None else active_settings(conn), offers)
        if lines:
            shown = "\n".join(lines[:20]) + ("\nи ещё %d" % (len(lines) - 20) if len(lines) > 20 else "")
            if not _alert(conn, box, "Tires Sync: своя цена ниже закупки (%d). Цены оставлены как вы поставили; поменять — в карточке позиции "
                          "каталога, «Своя цена».\n%s" % (len(lines), shown), notice=True):
                return
        own_price.remember(conn, keep)
    except Exception as err:
        try:
            conn.rollback()
        except Exception:
            pass
        print("проверка «своя цена ниже закупки» не удалась: %s: %s" % (type(err).__name__, err), file=sys.stderr)


def _alert_price_guard(conn, box) -> None:
    """The falls of the supplier's price held for the owner and the rises followed (price_guard, the owner 2026-10-30): one summary by brand
    with the link to «Цена поставщика изменилась», to the owner's chat and to every other owner's own; each change once (told only once it
    went). A side notice: whatever fails in it is printed and never stops the publishing of the feeds."""
    from . import order_notify, price_guard, users
    try:
        found = price_guard.news(conn)
        if not found["marks"]:
            return
        base = (os.environ.get("TIRES_PUBLIC_BASE") or store.get_state(conn, "public_base") or "").rstrip("/")
        text = price_guard.text(found, (base + "/prices/changes") if base else "панель → Товар и фид → Цены поставщика")
        if not _alert(conn, box, text, notice=True):
            return
        owner = order_notify.get_owner_login(conn)
        others = [u["login"] for u in users.list_users(conn) if u["role"] == "owner" and u["active"] and u["login"] != owner]
        if others:
            order_notify.tell_people(conn, box, others, text)
        price_guard.told(conn, found)
    except Exception as err:
        try:
            conn.rollback()
        except Exception:
            pass
        print("сводка «цена поставщика изменилась» не удалась: %s: %s" % (type(err).__name__, err), file=sys.stderr)


def _fetch_suppliers(conn, box, args, quiet: bool = False) -> list:
    """Download the unload of every supplier. The first one failing stops the cycle (as before); a secondary
    supplier failing is reported and its previous unload stays in use. Returns the problems of secondary suppliers.
    quiet: no alert (the light stock pass; the hourly cycle tells the same trouble)."""
    problems = []
    for supplier in connections.list_suppliers(conn):
        sid = supplier["id"]
        config = connections.get_config(conn, "webmim", connections.supplier_owner(sid))
        if sid != 1 and not config.get("base_url"):
            continue
        dest = unload_path_for(Path(args.unload), sid)
        try:
            meta = fetch_for_connection(conn, box, dest, _now(), http=_http(), supplier_id=sid)
        except (WebmimError, SecretKeyError, OSError, ValueError) as err:
            if not quiet:
                _alert(conn, box, "Tires Sync: не удалось скачать выгрузку WebMIM (%s). %s" % (supplier["name"], err))
            if sid == 1:
                raise
            print("error: поставщик «%s»: %s" % (supplier["name"], err), file=sys.stderr)
            problems.append(supplier["name"])
            continue
        print("выгрузка скачана: %d товаров" % meta["items"] if sid == 1
              else "выгрузка поставщика «%s» скачана: %d товаров" % (supplier["name"], meta["items"]))
    return problems


def _load_tochki(conn, box, args, started: datetime) -> None:
    """The 4tochki catalog, at most once an hour (counted from the start of the cycle), as the last step of the cycle: its walk may take
    minutes and must hold up neither the feeds nor the reminders; the positions it brings are used from the next cycle on. Whatever goes
    wrong with it is reported and the previous 4tochki positions stay; it never stops the cycle or changes its exit code (stage 1: the
    positions are shown in the catalog only). The alert text is always the same, so a trouble that lasts is not repeated every hour;
    the reason is in the journal and on the 4tochki connection page."""
    if conn.in_transaction:                    # the walk holds no write lock of the cycle (the panel and the chats write meanwhile)
        conn.commit()
    try:
        result = tochki_feed.refresh(conn, box, Path(args.unload), started, http=_http())
    except Exception as err:
        result = {"status": "error", "message": "4 точки: загрузка не удалась (%s), позиции прежние." % type(err).__name__}
    if result["status"] == "ok":
        print(result["message"])
    elif result["status"] == "error":
        print("error: " + result["message"], file=sys.stderr)
        try:
            _alert(conn, box, "Tires Sync: 4 точки: загрузка не удалась, позиции прежние. Причина: «Подключения → 4 точки».")
        except Exception as err:
            print("error: оповещение о 4 точках не отправлено (%s)" % type(err).__name__, file=sys.stderr)


def _queue_photo_sets(db_path, parsed, ref, settings, now=None) -> None:
    """Plan photo sets and queue generation jobs on its own connection; never affects the cycle."""
    conn = None
    try:
        from . import photo_library
        conn = store.connect(db_path)
        ev = evaluate(parsed.offers, parsed.issues, settings, ref, store.load_confirmed_mappings(conn), store.load_brand_map(conn))
        if not ev.ok:                                           # a bad unload: keep the sets and jobs as they are, do not retire everything
            return
        stamp = now if isinstance(now, str) else (now or datetime.utcnow()).isoformat(timespec="seconds")   # UTC like the panel, which stamps the handout and results
        photo_library.sync_sets(conn, photo_library.needed_sets(ev.ok, settings.min_stock), stamp)
        photo_library.enqueue(conn, stamp)
    except Exception as err:
        print("очередь фото не обновлена (%s: %s)" % (type(err).__name__, err), file=sys.stderr)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn is not None:
            conn.close()


STOCK_PASS_SILENT = 60 * 60          # a pass that cannot publish for this long in a row is told to the owner
STOCK_PASS_GAP = 50 * 60             # longer between two failed passes (more than one skipped: the feeds busy, no unload): not «in a row»
STOCK_PASS_FAILING = "stock_pass_failing"   # app_state: {account: [epoch of the first failed pass in a row, of the last one]}


def _watch_stock_pass(conn, box, results) -> None:
    """A pass whose draft fails its check, or whose publication is refused, is told only in the journal (the hourly cycle says the same).
    Failing for over STOCK_PASS_SILENT in a row, it alerts, one text per account (the same each time: at most once in six hours). A pass of
    the account that goes through, or finds nothing to take off, starts the count again, and so does a gap of more than one skipped pass;
    a broken pass (it alerts itself) and the guard (the hourly cycle's) keep it going without an alert of their own."""
    try:
        since = json.loads(store.get_state(conn, STOCK_PASS_FAILING) or "{}")
    except ValueError:
        since = {}
    since = {k: v for k, v in since.items() if isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v)} \
        if isinstance(since, dict) else {}
    failing = {str(r.account_id): r for r in results if not r.ok and not r.broken}
    left = {str(r.account_id) for r in results if r.broken or (r.ok and r.guard_tripped)}
    now = _epoch()
    marks = {k: [v[0], now] for k, v in since.items() if k in left}   # not published either: the count goes on
    for key in failing:
        first, last = since.get(key, (now, now))
        marks[key] = [now if now - last > STOCK_PASS_GAP else first, now]
    if marks != since:
        store.set_state(conn, STOCK_PASS_FAILING, json.dumps(marks))
    for key in sorted(failing):
        if now - marks[key][0] >= STOCK_PASS_SILENT:
            _alert(conn, box, "Tires Sync: проверка остатков между циклами больше часа не может опубликовать фид (%s): снимает только "
                   "часовой цикл. Причина в журнале tires-stock." % failing[key].name)


def _cmd_stock_pass(args) -> int:
    """`run-cycle --stock-only`: the light pass between the hourly cycles (cycle.stock_pass). Another cycle or a publication holds the feeds:
    this pass is skipped. The unload did not come: skipped quietly (the hourly cycle alerts). It never stops anything else. What the hourly
    cycle tells anyway (the guard, a failed check, a refused publication) goes only into the journal: every 15 minutes the owner would get
    the same news again; only a pass that breaks by itself alerts (always the same text, so at most once in six hours), and one that cannot
    publish for over an hour in a row (_watch_stock_pass)."""
    conn = store.connect(args.db)
    box = load_box(args.key_file) if args.fetch else None
    if args.fetch:                                                     # outside the lock, as in the cycle: the panel is not kept waiting
        if box is None:
            print("проверка остатков пропущена: мастер-ключ недоступен", file=sys.stderr)
            return 0
        try:
            _fetch_suppliers(conn, box, args, quiet=True)
        except Exception as err:                                       # noqa: BLE001 - the hourly cycle fetches again and alerts
            print("проверка остатков пропущена: выгрузка не скачана (%s: %s)" % (type(err).__name__, err))
            return 0
    with feed_lock(args.feed_root, wait=0, strict=False) as ours:
        if not ours:
            print("проверка остатков пропущена: фид сейчас собирает или публикует другой процесс")
            return 0
        ref = parse_reference(Path(args.reference).read_text(encoding="utf-8"))
        parsed, _sha, notes = load_all_parsed(conn, active_settings(conn), Path(args.unload))
        for note in notes:
            print(note)
        results = stock_pass(conn, parsed, ref, date.today(), args.feed_root, now=moscow_now())
        failed = [r for r in results if not r.ok]
        tripped = [r for r in results if r.ok and r.guard_tripped]
        expected = expected_for(conn, active_settings(conn))
        sent = []                                                     # the accounts whose feed this pass published
        for r in results:
            if not r.ok or r.guard_tripped:
                continue
            if r.left:
                print("%s: не найдено объявление в опубликованном фиде, снимет часовой цикл: %s" % (r.name, ", ".join(r.left)))
            if r.error:
                print("%s: предупреждение: %s" % (r.name, r.error), file=sys.stderr)
            if not r.removed:
                continue
            print("%s: сняты по остатку (%d): %s" % (r.name, len(r.removed), ", ".join(r.removed)))
            if not args.publish:
                continue
            account = connections.get_account(conn, r.account_id)
            folder = Path(args.feed_root) / account["feed_token"]
            try:
                done = promote_feed(folder, expected_removals=expected)
                _record_state(conn, account, folder)
                try:
                    remember_pass_removals(conn, r.account_id, len(r.removed))
                except Exception as err:                                # noqa: BLE001 - published and recorded: only the hour's count is short
                    print("%s: снятия прохода не отмечены для защиты (%s: %s)" % (r.name, type(err).__name__, err), file=sys.stderr)
                print("%s: опубликован, объявлений: %d (было %d)" % (r.name, done["new"], done["old"]))
                sent.append(r)
            except (PublishError, store.StateError) as err:
                r.ok, r.error = False, str(err)
                failed.append(r)
        for r in failed:
            print("%s: error, фид прежний: %s" % (r.name, r.error))
        for r in tripped:
            print("%s: защита от массового снятия, снятие оставлено часовому циклу: %s" % (
                r.name, ", ".join("%s (%s)" % item for item in r.guarded)))
        broken = [r for r in failed if r.broken]
        if broken:
            _alert(conn, box, "Tires Sync: проверка остатков между циклами не работает (%s), снимает только часовой цикл. Подробности в журнале "
                   "tires-stock." % ", ".join("%s: %s" % (r.name, r.error.split(":")[0]) for r in broken))
        _watch_stock_pass(conn, box, results)
    if sent:                                                          # the number in stock on Avito: only accounts switched on, outside the lock
        avito_stock.after_cycle(conn, box if box is not None else load_box(args.key_file), args.feed_root, parsed, sent, http=_http())
    return 0


def _cmd_run_cycle(args) -> int:
    if getattr(args, "stock_only", False):
        return _cmd_stock_pass(args)
    started = datetime.fromisoformat(_now())
    conn = store.connect(args.db)
    box = load_box(args.key_file)
    fetch_problems = []
    if args.fetch:
        if box is None:
            print("error: мастер-ключ недоступен (проверьте --key-file или TIRES_SECRET_KEY_FILE)", file=sys.stderr)
            return 2
        fetch_problems = _fetch_suppliers(conn, box, args)
    ref = parse_reference(Path(args.reference).read_text(encoding="utf-8"))
    parsed, input_sha, notes = load_all_parsed(conn, active_settings(conn), Path(args.unload))
    for note in notes:
        print(note)
    if notes:
        _alert(conn, box, "Tires Sync: поставщик не используется в этом цикле, его объявления удерживаются.\n" + "\n".join(notes))
    _record_probe(conn, parsed, "cycle")
    _record_archive(conn, box, parsed)
    with feed_lock(args.feed_root):
        results = run_for_accounts(conn, None, ref, date.today(), args.feed_root, parsed=parsed, input_sha=input_sha, now=moscow_now())
        if args.publish:
            applied = _prebind_accounts(conn, box, args.feed_root, results, parsed=parsed)           # new codes take the Id of the live ad of their tire...
            if applied:                                                                              # ...and the feeds are built again before anything is published
                results = run_for_accounts(conn, None, ref, date.today(), args.feed_root, parsed=parsed, input_sha=input_sha, now=moscow_now())
                _prebind_report(conn, box, args.feed_root, applied, offers=getattr(parsed, "offers", None),
                                unavailable=getattr(parsed, "unavailable", frozenset()))
        if parsed is not None:
            _queue_photo_sets(args.db, parsed, ref, active_settings(conn))
        for r in results:
            if r.ok:
                print("%s: ok, объявлений в фиде: %d, действия: %s" % (r.name, r.feed_count, json.dumps(r.counts, ensure_ascii=False)))
                if r.error:
                    print("%s: предупреждение: %s" % (r.name, r.error), file=sys.stderr)
            else:
                print("%s: error, %s" % (r.name, r.error))
        failed = [r for r in results if not r.ok]
        tripped = [r for r in results if r.ok and r.guard_tripped]
        for r in tripped:
            print("%s: сработала защита от массового снятия, объявления удерживаются" % r.name)
        if tripped:
            _alert(conn, box, _guard_alert_text(conn, tripped, parsed))
        for r in results:
            if r.ok and not r.guard_tripped and r.new_waiting:
                st = active_settings(conn)
                limit = max(st.guard_min, int(st.guard_fraction * len(store.load_published(conn, r.account_id))))
                if r.new_waiting > limit:
                    _alert(conn, box, "Tires Sync: сразу %d позиций (%s) пропали из выгрузки поставщика и ждут утра. Обычно это корзины других "
                           "продавцов; если выгрузка выглядит сломанной, проверьте её." % (r.new_waiting, r.name))
        held_titles = [r for r in results if r.ok and r.titles_held]
        for r in held_titles:
            print("%s: поставщик сразу переименовал шины у %d объявлений, их названия оставлены прежними" % (r.name, r.titles_held))
        if held_titles:
            _alert(conn, box, "Tires Sync: поставщик сразу переименовал шины у многих объявлений (%s). Названия на Авито оставлены прежними, "
                   "чтобы не менять их все разом. Проверьте выгрузку поставщика." % ", ".join("%s: %d" % (r.name, r.titles_held) for r in held_titles))
        _alert_new_unmatched(conn, box, results)
        if parsed is not None and any(r.ok for r in results):
            _alert_own_below_cost(conn, box, parsed.offers)
            if args.publish:                                        # «наша цена поднята / оставлена»: true only of a published feed
                _alert_price_guard(conn, box)
        if args.publish:
            expected = expected_for(conn, active_settings(conn))
            promoted = 0
            for r in results:
                account = connections.get_account(conn, r.account_id)
                if not r.ok or not account["live"]:
                    continue
                folder = Path(args.feed_root) / account["feed_token"]
                try:
                    done = promote_feed(folder, expected_removals=expected)
                    _record_state(conn, account, folder)
                    promoted += 1
                    print("%s: опубликован, объявлений: %d (было %d)" % (r.name, done["new"], done["old"]))
                except (PublishError, store.StateError) as err:
                    print("%s: error, %s" % (r.name, err))
                    r.ok, r.error = False, str(err)
                    failed.append(r)
            if promoted and not failed:
                clear_expected(conn)
        if failed:
            _alert(conn, box, "Tires Sync: цикл завершился с ошибками.\n" + "\n".join(
                "%s: %s" % (r.name, r.error) for r in failed))
    if results and not failed and not tripped:                      # a broken unload must not leave a dip in the dynamics
        _record_economics(conn, parsed, ref)
    _collect_avito_reports(conn, box)
    if getattr(args, "publish", False):
        _heal_accounts(conn, box, getattr(args, "feed_root", None))
        if not fetch_problems and not tripped:                       # the waiting clients are not judged by a cycle that was stopped or lacked a fresh unload (without --fetch the file is the last one)
            _check_waiting(conn, parsed, box)
    try:
        _send_reminders(conn, box)
    except Exception as err:                                      # a reminder must never fail a cycle
        print("напоминания не отправлены (%s)" % err, file=sys.stderr)
    if args.fetch:
        _load_tochki(conn, box, args, started)
    if getattr(args, "publish", False):
        avito_stock.after_cycle(conn, box, args.feed_root, parsed, results, http=_http())    # the number in stock on Avito, last: only accounts switched on
    return 0 if not failed and not fetch_problems and not notes and not tripped else 3


def _avito_client(args, write: bool):
    box = load_box(args.key_file)
    if box is None:
        raise ValueError("мастер-ключ недоступен (проверьте --key-file или TIRES_SECRET_KEY_FILE)")
    conn = store.connect(args.db)
    account = connections.get_account(conn, args.account)
    if account is None:
        raise ValueError("аккаунт №%d не найден" % args.account)
    client_id = connections.get_secret(conn, box, "avito", account["id"], "client_id")
    client_secret = connections.get_secret(conn, box, "avito", account["id"], "client_secret")
    if not client_id or not client_secret:
        raise ValueError("ключи аккаунта «%s» не заданы на странице «Подключения»" % account["name"])
    return conn, account, AvitoClient(client_id, client_secret, http=_http(), now=_epoch, allow_write=write)


def _record_state(conn, account, folder: Path) -> None:
    feed_bytes = (folder / "avito_feed.xml").read_bytes()
    if account["feed_mode"] == "mirror" and not store.pending_matches(conn, account["id"], feed_bytes):
        return                                    # a plan left by another feed (an older cycle) is not this feed's plan
    if not store.has_pending(conn, account["id"]):
        print("предупреждение: нет ожидающего состояния цикла, учёт опубликованного не обновлён")
        return
    count = store.commit_pending(conn, account["id"], feed_bytes)
    print("состояние записано: решений учтено %d" % count)


def _cmd_feed_publish(args) -> int:
    conn = store.connect(args.db)
    account = connections.get_account(conn, args.account)
    if account is None:
        raise ValueError("аккаунт №%d не найден" % args.account)
    folder = Path(args.feed_root) / account["feed_token"]
    result = promote_feed(folder, allow_shrink=args.allow_shrink)
    print("фид опубликован у нас на сервере: было %d, стало %d объявлений" % (result["old"], result["new"]))
    _record_state(conn, account, folder)
    return 0


def _cmd_avito_publish(args) -> int:
    conn, account, client = _avito_client(args, write=args.confirm)
    folder = Path(args.feed_root) / account["feed_token"]
    candidate = folder / "next" / "avito_feed.xml"
    if not candidate.exists() or candidate.stat().st_size == 0:
        raise ValueError("фид ещё не создан: %s (запустите run-cycle)" % candidate)
    if args.confirm and not account["live"]:
        raise ValueError("публикация аккаунта выключена: включите её на странице «Подключения»")
    key = "avito_last_upload_%d" % account["id"]
    last = store.get_state(conn, key)
    if args.confirm and last and _epoch() - float(last) < 3600:
        raise PublishError("Загрузку можно запускать не чаще раза в час: прошлая была совсем недавно.")
    ads = count_ads(candidate.read_bytes())
    feed_url = "%s/feed/%s/avito_feed.xml" % (args.public_base.rstrip("/"), account["feed_token"])
    print("аккаунт: %s, объявлений в черновике фида: %d" % (account["name"], ads))
    print("ссылка на фид: %s" % feed_url)
    plan = plan_feed(client, feed_url)
    print("профиль автозагрузки: действие со ссылкой — %s" % plan["action"])
    if not args.confirm:
        print("Ничего не изменено. Чтобы указать ссылку и запустить загрузку, добавьте --confirm.")
        return 0
    result = promote_feed(folder, allow_shrink=args.allow_shrink)
    print("фид опубликован у нас на сервере: было %d, стало %d объявлений" % (result["old"], result["new"]))
    _record_state(conn, account, folder)
    ensure_feed(client, feed_url, plan=plan)
    upload_now(client, float(last) if last else None)
    store.set_state(conn, key, repr(_epoch()))
    print("загрузка запущена")
    return 0


def _cmd_mirror_go_live(args) -> int:
    """Bring a mirrored account to Avito: rebuild its feed, check that not one stored ad is missing, publish, attach the link, start the upload."""
    conn, account, _client = _avito_client(args, write=False)
    if account["feed_mode"] != "mirror":
        raise ValueError("аккаунт «%s» не в режиме «зеркало»" % account["name"])
    stored = mirror_feed.stored_ids(conn, account["id"])
    if not stored:
        raise ValueError("для аккаунта не загружен файл (команда feed-import)")
    folder = Path(args.feed_root) / account["feed_token"]
    with feed_lock(args.feed_root):
        settings = active_settings(conn)
        try:
            offers = parse_unload(Path(args.unload).read_bytes(), settings.territory_rn).offers
        except Exception as err:
            raise ValueError("выгрузка поставщика не читается (%s): без неё нельзя понять, что есть в наличии; на Авито ничего не отправлено" % type(err).__name__) from None
        if not offers:
            raise ValueError("в выгрузке поставщика нет позиций; на Авито ничего не отправлено")
        eff = connections.effective_settings(conn, account["id"], settings)
        skip = mirror_feed.unavailable(conn, account["id"], offers, eff.min_stock)
        prices = mirror_feed.prices_for(conn, account["id"], offers, eff)
        count = mirror_feed.write_candidate(conn, account, args.feed_root, moscow_now().date(), settings, skip=skip, prices=prices, runflat=mirror_feed.runflat_for(conn, account["id"], offers),
                                          contact_phone=(lambda i: i["phone"] if i["phone_source"] == "cabinet" else "")(connections.get_avito_identity(conn, account["id"])))
        missing = mirror_feed.missing_ids(conn, account["id"], (folder / "next" / "avito_feed.xml").read_bytes(), skip=skip)
        if missing or count != len(stored) - len(skip):
            raise ValueError("в собранном фиде не хватает объявлений: %d из %d (например %s); на Авито ничего не отправлено" % (len(missing), len(stored), ", ".join(missing[:3])))
        absent = sum(1 for r in skip.values() if r == "нет у поставщика")
        print("аккаунт «%s»: собран фид зеркала, объявлений %d из %d; не публикуем: нет у поставщика: %d, остаток ниже порога: %d" % (
            account["name"], count, len(stored), absent, len(skip) - absent))
        if not args.confirm:
            print("Ничего не изменено. Чтобы вывести аккаунт в эфир (публикация, ссылка в автозагрузке, запуск загрузки), добавьте --confirm.")
            return 0
        was_live = bool(account["live"])
        connections.set_live(conn, account["id"], True, moscow_now().isoformat(timespec="seconds"), "mirror-go-live")
        conn.commit()
        args.allow_shrink = False
        try:
            code = _cmd_avito_publish(args)
        except BaseException:
            code = None
            raise
        finally:
            if code != 0 and not was_live:                          # it did not go out: the account must not be left switched on for the timer to publish
                connections.set_live(conn, account["id"], False, moscow_now().isoformat(timespec="seconds"), "mirror-go-live")
                conn.commit()
        return code


def _probe_min_stock(conn) -> int:
    settings = active_settings(conn)
    accounts = connections.list_feed_accounts(conn)
    return connections.effective_settings(conn, accounts[0]["id"], settings).min_stock if accounts else settings.min_stock


def _record_probe(conn, parsed, source: str) -> None:
    try:
        catalog_probe.record(conn, parsed, _probe_min_stock(conn), moscow_now().isoformat(timespec="seconds"), source)
    except Exception as err:          # a note about the catalog must never stop a cycle
        print("предупреждение: проверка полноты каталога не записана (%s)" % err, file=sys.stderr)


def _record_archive(conn, box, parsed) -> None:
    """The permanent base of catalog positions follows every cycle; its trouble is only noted, never stops a cycle."""
    try:
        stats = catalog_archive.record(conn, parsed, moscow_now().isoformat(timespec="seconds"))
        for supplier in stats["suspicious"]:
            left = catalog_archive.BELIEVE_AFTER - stats["streak"].get(supplier, 1)
            text = "у поставщика №%d пропала большая часть позиций разом; в базе каталога они пока не помечены пропавшими (если так будет ещё в %d циклах подряд, пометятся)" % (
                supplier, left)
            print("предупреждение: " + text, file=sys.stderr)
            if left == catalog_archive.BELIEVE_AFTER - 1:                   # told once, at the start: the cycle runs every hour (2026-10-20)
                _alert(conn, box, "Tires Sync: " + text)
        for supplier in stats["believed"]:
            _alert(conn, box, "Tires Sync: у поставщика №%d большая часть позиций не вернулась несколько циклов подряд: они помечены в каталоге «нет у поставщика»." % supplier)
    except Exception as err:
        conn.rollback()
        print("предупреждение: база позиций каталога не обновлена (%s)" % err, file=sys.stderr)


def _cmd_catalog_probe(args) -> int:
    box = load_box(args.key_file)
    if box is None:
        raise ValueError("мастер-ключ недоступен (проверьте --key-file или TIRES_SECRET_KEY_FILE)")
    conn = store.connect(args.db)
    dest = Path(args.work_file)
    try:
        fetch_for_connection(conn, box, dest, _now(), http=_http(), supplier_id=1)
        parsed = parse_unload(dest.read_bytes(), active_settings(conn).territory_rn)
    finally:
        for leftover in (dest, dest.with_name(dest.name + ".meta.json")):
            try:
                leftover.unlink()
            except OSError:
                pass
    row = catalog_probe.record(conn, parsed, _probe_min_stock(conn), moscow_now().isoformat(timespec="seconds"), "probe")
    print("каталог: %d товаров; наших опубликованных %d: на месте %d, нет в выгрузке %d, остаток ниже порога %d" % (
        row["items"], row["published"], row["present"], row["absent"], row["low"]))
    return 0


def _send_reminders(conn, box) -> int:
    """Reminders for changes agreed in advance (reminders.due): each is sent once; a failed send is tried again next cycle."""
    from . import reminders
    sent = 0
    for key, text in reminders.due(conn, moscow_now().date()):
        if _alert(conn, box, text, notice=True):
            store.set_state(conn, key, moscow_now().isoformat(timespec="seconds"))
            sent += 1
    return sent


def _collect_avito_reports(conn, box) -> None:
    """Keep what Avito says about the last upload (read only). A failure here is only a warning: it must not stop a cycle."""
    if box is None:
        return
    for account in connections.list_feed_accounts(conn):
        try:
            client_id = connections.get_secret(conn, box, "avito", account["id"], "client_id")
            client_secret = connections.get_secret(conn, box, "avito", account["id"], "client_secret")
            if not client_id or not client_secret:
                continue
            avito_report.collect(conn, AvitoClient(client_id, client_secret, http=_http(), now=_epoch), account["id"], moscow_now().isoformat(timespec="seconds"))
        except Exception as err:      # noqa: BLE001
            print("предупреждение: отчёт Авито не получен (%s: %s)" % (type(err).__name__, str(err)[:120]), file=sys.stderr)


def _cmd_orders_tick(args) -> int:
    conn = store.connect(args.db)
    made = order_engine.fire_stale(conn, moscow_now().isoformat(timespec="seconds"))
    print("новых задач по правилам «завис на этапе»: %d" % made)
    _reserve_alerts(conn, args.key_file)
    _daily_summary(conn, args.key_file)
    _webmim_snapshot(conn, args.key_file)
    _google_sheet(conn, args.key_file)
    return 0


def _cmd_sheet_enroll(args) -> int:
    """sheet-enroll --open: the open orders placed before the Google table was switched on go into its queue (the hourly orders-tick or the
    next request sends them). --dry-run lists them only. Run by hand after a copy of the base (the owner 2026-10-30)."""
    from . import order_sheet
    conn = store.connect(args.db)
    found = order_sheet.open_unlisted(conn)
    for card in found:
        print("№%d · %s · %s" % (card["id"], card["stage"]["name"], card["client_name"] or "без имени"))
    if args.dry_run:
        print("открытых заказов не в таблице: %d (ничего не поставлено в очередь)" % len(found))
        return 0
    try:
        done = order_sheet.enroll_open(conn, moscow_now().isoformat(timespec="seconds"))
    except order_sheet.SheetError as err:
        print("ошибка: %s" % err, file=sys.stderr)
        return 1
    print("в очередь Google-таблицы: %d заказов; строки запишет ближайшая отправка (orders-tick)" % len(done))
    return 0


def _google_sheet(conn, key_file) -> None:
    """The hourly sending of the owner's Google table (order_sheet.flush, every queued order looked at): the retries of failures and what
    changed without a request of the panel. Off or not set up: nothing. A failure is only a warning."""
    try:
        from . import order_sheet
        box, now = load_box(key_file) if key_file else None, moscow_now().isoformat(timespec="seconds")
        done = order_sheet.flush(conn, box, _http(), now, full=True)
        if done:
            print("Google-таблица: %d заказ(ов) просмотрено, с ошибкой: %d" % (len(done), sum(1 for v in done.values() if v.startswith("ошибка"))))
        said = order_sheet.lasting_alarm(conn, now)                 # a failure of hours: one alert to the owner, one note when it is over
        if said and _alert(conn, box, said[0], notice=said[1], recovery=said[1]):        # said[1]: only «снова принимает» (RECOVERED)
            order_sheet.alarm_told(conn, now, said[1])
    except Exception as err:      # noqa: BLE001
        conn.rollback()
        print("предупреждение: Google-таблица не отправлена (%s)" % type(err).__name__, file=sys.stderr)


def _webmim_snapshot(conn, key_file, days: int = 60) -> bool:
    """The hourly reading of the WebMIM orders for «Сверка с WebMIM» (webmim_match.run). Not set up or failed: only a line in the journal."""
    try:
        from . import webmim_match
        status, line = webmim_match.run(conn, load_box(key_file) if key_file else None, _http(), _now(), moscow_now().date(), days)
        print(line, file=sys.stderr if status == "failed" else sys.stdout)       # not set up: not a failure of the tick
        return status == "read"
    except Exception as err:      # noqa: BLE001
        conn.rollback()
        print("предупреждение: снимок WebMIM не прочитан (%s)" % type(err).__name__, file=sys.stderr)
        return False


def _cmd_vk_catchup(args) -> int:
    """The backstop of VK's Callback (vk_catchup.run). A failure of VK is a warning, never a failure of the timer (it runs every 10–15
    minutes: a lasting failure would be an alert each time)."""
    from . import vk_catchup
    conn = store.connect(args.db)
    box = load_box(args.key_file) if args.key_file else None
    mode = "dry" if args.dry_run else ("seed" if args.seed else "act")
    done = vk_catchup.run(conn, box, datetime.utcnow().replace(microsecond=0), lambda db, local: vk_catchup.channel(db, local, box, _http(), _http(), _http()),
                          tg_http=_http(), vk_http=_http(), mode=mode)
    if done["no_key"]:                                                    # without the key Telegram is out of reach too: systemd tells it
        print("ошибка: сообщество ВК настроено, но ключ сообщества не сохранён или не читается (мастер-ключ?): переписка не читается", file=sys.stderr)
        return 1
    if not done["set_up"]:
        print("ВК не настроен (нет сообщества): переписка не читается")
        return 0
    if mode == "dry":
        for line in done["would"]:
            print("было бы подобрано: точка %s, сообщение %s: %s" % (line["peer"], line["message_id"], line["text"][:120]))
        print("ВК (проверка без действий): точек %d, было бы подобрано %d, ошибок %d" % (done["points"], len(done["would"]), done["errors"]))
        return 0
    if mode == "seed":
        print("ВК: место в переписке запомнено для точек: %d, ошибок %d (прошлое не выполняется)" % (done["points"] - done["errors"], done["errors"]))
        return 0
    print("ВК: точек %d, подобрано сообщений без уведомления %d, ошибок %d" % (done["points"], done["picked"], done["errors"]))
    if done["errors"]:
        print("предупреждение: часть переписки ВК не прочитана, подробности выше в журнале", file=sys.stderr)
    return 0


def _cmd_webmim_orders_sync(args) -> int:
    if not 1 <= args.days <= 400:
        print("--days: от 1 до 400", file=sys.stderr)
        return 2
    return 0 if _webmim_snapshot(store.connect(args.db), args.key_file, args.days) else 1


def _daily_summary(conn, key_file) -> None:
    """The morning summary in Telegram (off unless the owner switched it on; once a day after 9:00). A failure is only a warning."""
    try:
        from . import daily_summary
        result = daily_summary.maybe_send(conn, load_box(key_file) if key_file else None, moscow_now().replace(tzinfo=None), http=_http())
        if result:
            print(result, file=sys.stderr if "отправлено 0," in result else sys.stdout)      # nobody got it: Telegram is not set or refused
    except Exception as err:      # noqa: BLE001
        conn.rollback()
        print("предупреждение: утренняя сводка не отправлена (%s: %s)" % (type(err).__name__, str(err)[:120]), file=sys.stderr)


def _reserve_alerts(conn, key_file) -> None:
    """The responsible manager is told the day before the reservation at the point ends and on the day; on the day the point is asked in VK
    whether the order is still there, and its «на точке» asks the client once in the order's Avito chat (order_reserve). A failure is only a warning."""
    try:
        from . import order_notify, order_reserve, orders, vk_send
        box = load_box(key_file) if key_file else None
        if box is None:
            print("предупреждение: нет ключа шифрования, оповещения о конце резерва не отправляются", file=sys.stderr)
            return
        now = moscow_now().isoformat(timespec="seconds")

        def deliver(chat_row, text, token):                     # as the bot's channel (panel.routes_vk.channel): the chat's own account
            chat = chats.get_chat(conn, chat_row)
            if chat is None:
                raise chats.ChatError("Чат не найден.")
            client_id = connections.get_secret(conn, box, "avito", chat["account_id"], "client_id")
            client_secret = connections.get_secret(conn, box, "avito", chat["account_id"], "client_secret")
            if not client_id or not client_secret:
                raise chats.ChatError("У аккаунта не заданы ключи Авито.")
            chats.send_reply(conn, AvitoClient(client_id, client_secret, http=_http(), now=_epoch, allow_write=True), chat_row, text, token, now, "система")
        made = order_reserve.check(conn, now, lambda card, text: order_notify.deliver(conn, box, card, text, now, tg_http=_http(), vk_http=_http()), deliver,
                                   vk_send.sender_for(conn, box, http=_http()))     # the point is asked first, through the community (2026-10-26)
        print("оповещений о конце резерва: %d" % made)
    except Exception as err:      # noqa: BLE001
        conn.rollback()
        print("предупреждение: конец резерва не проверен (%s: %s)" % (type(err).__name__, str(err)[:120]), file=sys.stderr)


def _cmd_feed_check(args) -> int:
    from . import cycle, feed_check
    conn = store.connect(args.db)
    settings = active_settings(conn)
    parsed, _, notes = load_all_parsed(conn, settings, Path(args.unload))      # the positions of every supplier, as the cycle checks them
    for note in notes:
        print(note)
    ref = parse_reference(Path(args.reference).read_text(encoding="utf-8"))
    worst = 0
    for account in connections.list_feed_accounts(conn):
        if args.account and account["id"] != args.account:
            continue
        mode = "mirror" if account["feed_mode"] == "mirror" else "generated"
        eff = connections.effective_settings(conn, account["id"], settings)
        source = "published" if args.published else "next"
        held, id_codes = cycle.check_inputs(conn, account, args.feed_root, source) if mode == "generated" else ((), None)   # as the cycle judged it
        findings = cycle.gate(conn, account, mode, eff, parsed, ref, args.feed_root, source=source, held=held, id_codes=id_codes)
        bad = feed_check.critical(findings)
        status = "ОШИБКИ" if bad else ("предупреждения" if findings else "чисто")
        print("аккаунт №%d «%s» (%s): %s" % (account["id"], account["name"], mode, status))
        for f in findings[:40]:
            print("   [%s] %s %s: %s" % (f.level, f.rule, f.ad_id or "-", f.text))
        if len(findings) > 40:
            print("   … и ещё %d" % (len(findings) - 40))
        worst = max(worst, 3 if bad else 0)
    return worst


def _cmd_overrides_clear_aged(args) -> int:
    from . import overrides_clear
    conn = store.connect(args.db)
    version, raw = get_active(conn)
    parsed, _, notes = load_all_parsed(conn, active_settings(conn), Path(args.unload))
    for note in notes:
        print(note)
    found = overrides_clear.find_aged_on(parsed.offers, raw.get("overrides", {}))
    print("код | название | остаток | в фиде сейчас")
    for gid, offer in found:
        where = overrides_clear.in_feed_now(conn, gid)
        print("%s | %s | %d | %s" % (gid, offer.name, offer.stock, "в фиде: " + ", ".join(where) if where else "не в фиде"))
    print("Всего: %d" % len(found))
    if parsed.unavailable:
        print("ВНИМАНИЕ: выгрузка поставщиков %s недоступна, их позиции не проверены." % ", ".join(str(i) for i in sorted(parsed.unavailable)))
        blind = overrides_clear.blind_on(raw.get("overrides", {}), parsed.unavailable)
        if blind:
            print("Ручное вкл у позиций недоступных поставщиков (не проверены): %d" % len(blind))
            if args.apply:
                raise ValueError("не записано: список неполный (недоступна выгрузка поставщика с ручным вкл)")
    if not found:
        return 0
    print("Что будет: ручное вкл снимется (не станет «выкл»), исключение «старше» сработает, позиции в фиде снимутся со следующим циклом.")
    if not args.apply:
        print("Ничего не записано. Чтобы снять, добавьте --apply.")
        return 0
    new = overrides_clear.apply_clear(conn, found, version, _now())
    print("Готово: новая версия настроек №%d (было №%d), снято: %d." % (new, version, len(found)))
    return 0


def _cmd_match_synonym_add(args) -> int:
    from . import tire_match
    conn = store.connect(args.db)
    try:
        added, brand, alias, canonical = tire_match.add_synonym(conn, args.brand, args.alias, args.canonical, args.author,
                                                                 datetime.utcnow().isoformat(timespec="seconds"))
    except store.SynonymConflict as err:
        print("не записано: %s" % err, file=sys.stderr)
        return 2
    print("%s: %s = %s (%s)" % (brand, alias, canonical, "записано" if added else "уже было"))
    return 0


def _cmd_match_pair_forget(args) -> int:
    from . import tire_match
    conn = store.connect(args.db)
    try:
        done = tire_match.forget_pair(conn, args.code_a, args.code_b, args.author, datetime.utcnow().isoformat(timespec="seconds"), undo=args.undo)
    except ValueError as err:
        print("не сделано: %s" % err, file=sys.stderr)
        return 2
    finally:
        conn.close()
    if args.undo:
        print("%s | %s: %s" % (args.code_a, args.code_b, "пара возвращена, память снова её использует" if done else "пара и так используется"))
    else:
        print("%s | %s: %s" % (args.code_a, args.code_b, "пара забыта, память её больше не использует (строка осталась)" if done else "пара уже забыта"))
    return 0


def _cmd_match_not_pair(args) -> int:
    from . import tire_match
    conn = store.connect(args.db)
    try:
        done = tire_match.not_a_pair(conn, args.code_a, args.code_b, args.author, datetime.utcnow().isoformat(timespec="seconds"), undo=args.undo)
    except ValueError as err:
        print("не сделано: %s" % err, file=sys.stderr)
        return 2
    finally:
        conn.close()
    if args.undo:
        print("%s | %s: %s" % (args.code_a, args.code_b, "запрет снят, позиции снова могут склеиваться" if done else "запрета и так нет"))
    else:
        print("%s | %s: %s" % (args.code_a, args.code_b, "запрет записан: эти позиции не склеиваются, даже если обе в выгрузке"
                               if done else "запрет уже есть"))
    return 0


MATCH_LIVE = ("publish", "update", "keep", "hold", "defer")      # the decisions that leave (or put) the position in the feed


def _cmd_match_report(args) -> int:
    """Read only: the pairs of one tire at two suppliers and what the switch `tire_match.feed` would change, account by account, «как
    сейчас» and «если 4 точки включить в фид». Nothing is written (the base is rolled back and its counter of changes is checked)."""
    import copy
    from . import cycle, same_tire, sync, tire_match
    conn = store.connect(args.db)
    changes_before = conn.total_changes
    settings = active_settings(conn)
    parsed, _, notes = load_all_parsed(conn, settings, Path(args.unload))
    for note in notes:
        print(note)
    ref = parse_reference(Path(args.reference).read_text(encoding="utf-8"))
    with_tochki, _shown = tochki_feed.catalog_view(conn, parsed, Path(args.unload))     # 4tochki added for viewing when its switch is off
    offers = with_tochki.offers
    match = tire_match.for_feed(conn, offers)
    ev = evaluate(offers, getattr(with_tochki, "issues", {}), settings, ref, store.load_confirmed_mappings(conn), store.load_brand_map(conn))
    limit = max(args.limit, 0)
    by_supplier: dict = {}
    for gid in offers:
        by_supplier[tire_match.supplier_of(gid)] = by_supplier.get(tire_match.supplier_of(gid), 0) + 1
    print("склейка в фиде: %s" % ("ВКЛЮЧЕНА" if tire_match.switch_on(conn) else "выключена"))
    print("позиций по поставщикам: %s" % ", ".join("№%d: %d" % kv for kv in sorted(by_supplier.items())))

    def line(gid):
        if gid not in offers:                                                   # a remembered side that has left the unload
            return "%s (нет в выгрузке, пара запомнена)" % gid
        o, cand = offers[gid], ev.ok.get(gid)
        model = ("модель Авито «%s»" % cand.avito_model) if cand else "не в фиде (%s)" % (
            ev.excluded.get(gid) or ",".join(ev.problems.get(gid, [])[:1]) or "—")
        return "%s «%s», остаток %d, %s" % (gid, o.name_orig or o.name, o.stock, model)

    apart = [g for g in match.groups if len({ev.ok[c].avito_model for c in g if c in ev.ok}) > 1]
    print("пар (групп одной шины у разных поставщиков): %d, из них модели Авито расходятся: %d" % (len(match.groups), len(apart)))
    for group in match.groups[:limit]:
        print("  %s%s" % ("[модели Авито расходятся] " if group in apart else "", " | ".join(line(g) for g in group)))
    print("неоднозначных групп (не склеиваются): %d" % len(match.ambiguous))
    for group in match.ambiguous[:limit]:
        print("  " + " | ".join(line(g) for g in group))
    print("сомнительных позиций (название противоречит полям, не склеиваются): %d" % len(match.doubtful))
    for gid, why in sorted(match.doubtful.items())[:limit]:
        print("  %s «%s»: %s" % (gid, offers[gid].name_orig or offers[gid].name, why))
    synonyms = store.load_model_synonyms(conn)
    print("синонимов моделей владельца: %d" % len(synonyms))
    for (brand, alias), canonical in list(synonyms.items())[:limit]:
        print("  %s: %s = %s" % (brand, alias, canonical))
    stored = store.load_tire_pairs(conn)
    forgotten = store.forgotten_tire_pairs(conn)
    not_pairs = store.not_tire_pairs(conn)
    print("сохранённых пар (запомнены циклом при включённой склейке): %d, используется %d из %d" % (
        len(stored), sum(1 for p in stored if (p[0], p[1]) not in forgotten and (p[0], p[1]) not in not_pairs), len(stored)))

    def here_or_not(code):
        return " ".join((code, "" if code in offers else "(нет в выгрузке)")).strip()
    for code_a, code_b, _ia, _ib, found_at in stored[:limit]:
        mark = forgotten.get((code_a, code_b))
        print("  %s | %s, найдена %s%s%s" % (here_or_not(code_a), here_or_not(code_b), found_at, " (забыта %s UTC, %s)" % mark if mark else "",
                                             " (запрет: это не пара)" if (code_a, code_b) in not_pairs else ""))
    print("запретов «это не пара» (владелец: не склеивать, даже если обе в выгрузке): %d" % len(not_pairs))
    for (code_a, code_b), (at, author) in sorted(not_pairs.items())[:limit]:
        print("  %s | %s, %s UTC, %s" % (here_or_not(code_a), here_or_not(code_b), at, author))
    print("групп, не склеенных из-за запрета внутри: %d" % len(match.refused))
    for group in match.refused[:limit]:
        print("  " + " | ".join(line(g) for g in group))
    print("групп, дополненных памятью (одна сторона ушла из выгрузки): %d" % len(match.remembered))
    for group in match.remembered[:limit]:
        print("  " + " | ".join(line(g) for g in group))
    today = date.today()
    tochki = tochki_feed.config(conn)
    trial_mode = args.tochki_mode or (tochki["mode"] if tochki["mode"] in tochki_feed.TRIAL_MODES else "no_twin")
    for account in connections.list_feed_accounts(conn):
        if (args.account and account["id"] != args.account) or account["feed_mode"] == "mirror":
            continue
        eff = connections.effective_settings(conn, account["id"], settings)
        left_out = set(connections.get_excluded_types(conn, account["id"]))
        switches = store.last_match_switches(conn, account["id"])
        print("аккаунт №%d «%s»: объявлений пар, у которых менялся поставщик: %d (следующая смена не раньше чем через сутки)" % (
            account["id"], account["name"], len(switches)))
        for ad_id, (from_code, to_code, at) in sorted(switches.items())[:limit]:
            until = (datetime.fromisoformat(at[:19]) + tire_match.SWITCH_PAUSE).isoformat(timespec="seconds")
            print("   Id %s: %s → %s, %s; следующая смена не раньше %s" % (ad_id, from_code, to_code, at, until))
        retired_model = {ad_id: key[1] for _g, ad_id, key, _at in store.load_retired(conn, account["id"], date.today())}
        scenarios = [("как сейчас", parsed, None)]
        if tochki["supplier_id"]:                                               # the trial in this account (plan tochki-feed §6)
            scenarios.append(("если включить пробу 4 точек (режим %s)" % trial_mode, with_tochki,
                              dict(tochki, mode=trial_mode, accounts=[account["id"]])))
        for title, source, trial in scenarios:
            account_parsed = source
            if left_out:
                account_parsed = copy.copy(source)
                account_parsed.offers = {k: v for k, v in source.offers.items() if cycle.feed_tire_type(v) not in left_out}
            now = moscow_now()                                                  # as the cycle: the wait for the morning refill counts
            _, ev_off, ds_off = sync.plan_decisions(conn, account_parsed, ref, eff, account["id"], today, now=now, cross_supplier=False,
                                                    tochki_trial=trial)
            _, ev_on, ds_on = sync.plan_decisions(conn, account_parsed, ref, eff, account["id"], today, now=now, cross_supplier=True,
                                                  tochki_trial=trial)
            live_off = {d.goods_id for d in ds_off.decisions if d.action.value in MATCH_LIVE}
            live_on = {d.goods_id for d in ds_on.decisions if d.action.value in MATCH_LIVE}
            gone, came = sorted(live_off - live_on), sorted(live_on - live_off)
            ids = sorted(g for g in set(ev_off.inherit) | set(ev_on.inherit) if ev_off.inherit.get(g) != ev_on.inherit.get(g))
            models = sorted(g for g in live_on & live_off if g in ev_on.ok and g in ev_off.ok
                            and ev_on.ok[g].avito_model != ev_off.ok[g].avito_model)
            print("аккаунт №%d «%s», %s: объявлений %d → %d; ушли бы из фида %d, пришли бы %d, сменили бы Id %d, сменили бы модель %d" % (
                account["id"], account["name"], title, len(live_off), len(live_on), len(gone), len(came), len(ids), len(models)))
            for gid in gone[:limit]:
                print("   ушло бы: %s (%s)" % (gid, ev_on.excluded.get(gid, "—")))
            for gid in came[:limit]:
                print("   пришло бы: %s" % gid)
            for gid in ids[:limit]:
                taken = ev_on.inherit.get(gid)
                own = same_tire.identity(ev_on.ok[gid]) if gid in ev_on.ok else None
                changes = taken in retired_model and own is not None and retired_model[taken] != own[1]   # a retired ad has no Avito model name
                print("   Id: %s %s → %s%s" % (gid, ev_off.inherit.get(gid, "свой"), taken or "свой",
                                           " (модель сменится: у снятого объявления была другая)" if changes else ""))
            for gid, (ad_id, until) in sorted(ev_on.match.waiting.items())[:limit] if ev_on.match is not None else ():
                print("   ждёт смены поставщика: %s (Id %s, не раньше %s)" % (gid, ad_id, until))
            for gid in models[:limit]:
                print("   модель: %s «%s» → «%s»" % (gid, ev_off.ok[gid].avito_model, ev_on.ok[gid].avito_model))
            if trial is not None:
                _tochki_lines(conn, account_parsed, tochki["supplier_id"], ev_on, live_on, limit)
    conn.rollback()
    wrote = conn.total_changes - changes_before
    conn.close()
    if wrote:
        print("ВНИМАНИЕ: отчёт изменил строки базы (%d), это ошибка — сообщите разработчику" % wrote, file=sys.stderr)
        return 3
    return 0


def _tochki_lines(conn, parsed, supplier_id: int, ev, live: set, limit: int) -> None:
    """The 4tochki positions of the trial scenario (with the merge): how many go in, why the others do not, which picture the new ones show."""
    from . import sync
    prefix = "s%d-" % supplier_id
    mine = sorted(g for g in parsed.offers if g.startswith(prefix))
    inside = [g for g in mine if g in live]
    why: dict = {}
    for gid in mine:
        if gid not in live:
            reason = ev.excluded.get(gid) or (ev.problems.get(gid) or ["—"])[0].split(":")[0]
            why[reason] = why.get(reason, 0) + 1
    print("   4 точки: позиций %d, в фиде %d; не вошли: %s" % (len(mine), len(inside), ", ".join(
        "%s %d" % kv for kv in sorted(why.items(), key=lambda kv: (-kv[1], kv[0]))) or "—"))
    photos = sync.ModelPhotos(conn, parsed.offers, prefix)
    of_webmim = [g for g in inside if g not in ev.inherit and photos.code_for(parsed.offers[g])]
    heirs = [g for g in inside if g in ev.inherit]
    print("   4 точки, фото: продолжают объявление WebMIM (его фото) %d, картинка WebMIM той же модели %d, картинка 4 точек %d" % (
        len(heirs), len(of_webmim), len(inside) - len(heirs) - len(of_webmim)))
    for gid in heirs[:limit]:
        print("   продолжит Id %s: %s" % (ev.inherit[gid], gid))


def _cmd_mirror_adopt(args) -> int:
    conn = store.connect(args.db)
    account = connections.get_account(conn, args.account)
    if account is None:
        raise ValueError("аккаунт №%d не найден" % args.account)
    if account["feed_mode"] != "mirror":
        raise ValueError("аккаунт «%s» уже не в режиме «зеркало»" % account["name"])
    plan = mirror_feed.adopt_plan(conn, args.account)
    print("аккаунт «%s»: сохраним Id объявлений: %d, ссылки на фото: %d; дальше фид собираем сами (адреса, шаблон текста, правила аккаунта)" % (
        account["name"], len(plan["ids"]), len(plan["images"])))
    if not args.confirm:
        print("Ничего не изменено. Чтобы перевести, добавьте --confirm (на Авито ничего не отправляется до ближайшей публикации фида).")
        return 0
    done = mirror_feed.adopt(conn, args.account, moscow_now().isoformat(timespec="seconds"), "mirror-adopt")
    print("переведено: Id %d, ссылок на фото %d" % (done["ids"], done["images"]))
    return 0


def _foreign_ids(conn, account_id: int, feed_root) -> set:
    """The Ids the feeds of the other accounts have now (published and draft): a position never takes one of them."""
    out: set = set()
    for other in connections.list_feed_accounts(conn):
        if other["id"] == account_id:
            continue
        for name in ("avito_feed.xml", "next/avito_feed.xml"):
            try:
                out |= {a.findtext("Id") or "" for a in ET.parse(str(Path(feed_root) / other["feed_token"] / name)).getroot().findall("Ad")}
            except (OSError, ET.ParseError):
                pass
    return out - {""}


def _cabinet_items(client, retry: bool = True) -> dict:
    """The ads of the cabinet (active and blocked) as {avito_id: {status, title, ad_id}}: what Avito knows of us (a read)."""
    items: dict = {}
    for status in ("active", "blocked"):
        for item in client.items(status) if retry else client.items(status, retry=False):
            items[int(item["id"])] = {"status": status, "title": item.get("title") or ""}
    ids = list(items)
    for start in range(0, len(ids), 50):
        answer = client.ad_ids_for(ids[start:start + 50]) if retry else client.ad_ids_for(ids[start:start + 50], retry=False)
        for avito_id, xml_id in answer.items():
            items[avito_id]["ad_id"] = xml_id
    return items


def _one_variant(parsed, code: str) -> bool:
    """Whether the supplier knows ONE kind of the tire behind `code` (the title of an ad does not tell studded from not, or RunFlat from plain): all the
    positions of the unload with the same brand, model, size and indices must agree on both. An unknown code is not trusted."""
    from . import reconcile
    offers = getattr(parsed, "offers", {}) or {}
    mine = offers.get(code)
    if mine is None:
        return False
    same = lambda o: (o.brand_key, reconcile._norm(o.model_raw), o.width, o.height, o.diameter, o.load, o.speed) == (
        mine.brand_key, reconcile._norm(mine.model_raw), mine.width, mine.height, mine.diameter, mine.load, mine.speed)
    return len({(bool(o.studded), bool(o.runflat)) for o in offers.values() if same(o)}) == 1


def _same_kind(conn, account_id: int, orphan: str, code, new_ad) -> bool:
    """The saved copy of the old ad (its published XML or the memory of the ads taken off) must say the same studded/RunFlat kind as the position that
    takes its Id. No copy: not bound (the title of an ad does not tell the kind, and Avito would rewrite the live ad into another tire)."""
    from . import same_tire
    old = None
    for goods in ([code] if code else []) + [mirror_feed.supplier_code(orphan)]:
        snap = store.get_snapshot(conn, goods, account_id)
        old = same_tire.identity_of_ad(snap) if snap else None
        if old:
            break
    if not old:
        last = sorted((r for r in store.load_retired(conn, account_id, None) if r[1] == orphan), key=lambda r: r[3] or "")
        old = last[-1][2] if last else None
    new = same_tire.identity_of_ad(ET.tostring(new_ad, encoding="unicode")) if new_ad is not None else None
    return bool(old and new and (old[5], old[8]) == (new[5], new[8]))


class PictureNotTrusted(RuntimeError):
    """What the cabinet says is not trusted this cycle: the binding of the account is skipped and the owner is told."""


MANUAL_LIMIT = 10                                                     # live ads without their Id from the file that a cabinet may have before it is not trusted
REENABLED_LIMIT = 30                                                  # ads switched on by hand under an Id of a file (17 in account 2 on 2026-10-22)


def _prebind_account(conn, account, client, feed_root, parsed, taken=frozenset(), changed=None) -> list:
    """Before a feed is published: a position that came back under a NEW supplier code while the first ad of the same tire is alive in the cabinet takes
    the Id of that ad (the rule of `ids-heal`, applied before Avito sees the new Id, so that no duplicate is made and blocked). Reads Avito (without
    retries: a slow answer skips the account, the cycle must not wait), writes only our own data. Returns the pairs bound.
    The live ads without an Id from our file are remembered as manual (`mirror_feed.set_manual_ads`; the feed leaves their tires out, `changed` gets the
    account when the list moved); more than MANUAL_LIMIT of them means the picture is incomplete and nothing is done. In account 2 the ads switched
    on by hand under an Id of a file are manual too (`_reenabled_ads`, owner 2026-10-22) and no new code takes them over.
    Left alone: a code shared with another account until a date, a code that is
    already bound to that Id, a tire the supplier has in two kinds (studded and not, RunFlat and plain), an Id that another account has or just took."""
    from . import heal
    path = Path(feed_root) / account["feed_token"] / "next" / "avito_feed.xml"
    feed = {a.findtext("Id"): a for a in ET.parse(str(path)).getroot().findall("Ad") if a.findtext("Id")}
    items = _cabinet_items(client, retry=False)
    from .reconcile import key_title
    known = dict(conn.execute("SELECT avito_id, ad_id FROM avito_ad WHERE account_id = ? AND ad_id IS NOT NULL AND ad_id != ''",
                              (account["id"],)).fetchall())
    manual = [{"avito_id": k, "title": i["title"]} for k, i in items.items()
              if i["status"] == "active" and not i.get("ad_id")
              and known.get(k) not in feed                              # our own ad whose Id Avito did not give this time is not manual
              and key_title(i["title"])]                                # nor is an ad that names no tire (a rim, a service)
    if len(manual) > MANUAL_LIMIT:                                     # that many without their Id: Avito did not give the whole picture
        raise PictureNotTrusted("часть живых объявлений кабинета пришла без Id из файла (%d): привязка пропущена" % len(manual))
    inverse = {v: k for k, v in mirror_feed.adopted_maps(conn, account["id"])[0].items()}
    reenabled = _reenabled_ads(conn, account, items, feed_root, parsed, inverse)
    by_hand_ids = {a["avito_id"] for a in reenabled}
    manual = [a for a in manual if a["avito_id"] not in by_hand_ids] + reenabled     # one Avito left the Id out of: still the one switched on by hand
    foreign = set(_foreign_ids(conn, account["id"], feed_root)) | set(taken)
    for other in connections.list_feed_accounts(conn):
        if other["id"] != account["id"]:
            foreign |= set(mirror_feed.adopted_maps(conn, other["id"])[0].values())
    today = moscow_now().date()
    result = heal.plan(feed, [], list(items.values()), inverse, mirror_feed.reissued_own(conn, account["id"]), today, foreign_ids=foreign, recent=())
    bound, shared = mirror_feed.adopted_maps(conn, account["id"])[0], mirror_feed.shared_codes(conn, account["id"])
    by_hand = {a["ad_id"] for a in reenabled}                             # owner, 2026-10-22: an ad switched on by hand is not taken over
    binds = [e for e in result["bind"] if e["code"] not in shared and bound.get(e["code"]) != e["orphan"] and e["orphan"] not in by_hand
             and _one_variant(parsed, e["code"])
             and _same_kind(conn, account["id"], e["orphan"], inverse.get(e["orphan"]), feed.get(e["feed_id"]))]
    if binds:
        now = moscow_now().isoformat(timespec="seconds")
        mirror_feed.bind_ids(conn, account["id"], {e["code"]: e["orphan"] for e in binds}, now, "ids-prebind")
        try:
            from .feed import clean_title
            mirror_feed.set_bound_titles(conn, account["id"], {e["orphan"]: e["title"] for e in binds if e.get("title") and clean_title(e["title"]) == e["title"]})     # the ad keeps the title it has on Avito
        except Exception as err:      # noqa: BLE001
            conn.rollback()
            print("предупреждение: название живого объявления не запомнено (%s)" % type(err).__name__, file=sys.stderr)
        try:
            mirror_feed.unreissue_own(conn, account["id"], [e["code"] for e in binds], now, "ids-prebind")
        except Exception as err:      # noqa: BLE001
            conn.rollback()
            print("предупреждение: метка нового Id не снята (%s)" % type(err).__name__, file=sys.stderr)
    if mirror_feed.set_manual_ads(conn, account["id"], manual) and changed is not None:     # last: a failure above leaves the list to the next cycle
        changed.append(account)                                         # a manual ad came or went: the feeds are built again
    return binds


def _reenabled_ads(conn, account, items: dict, feed_root, parsed, inverse: dict) -> list:
    """The live ads switched on by hand under an Id of a file (`mirror_feed.reenabled_ads`: Avito took our published file without that Id and the
    ad stays), of the ones that name a tire or whose Id is of one of our positions. More than REENABLED_LIMIT: the picture is not trusted."""
    if account["id"] not in mirror_feed.REENABLED_ACCOUNTS:
        return []
    from .reconcile import key_title
    path = Path(feed_root) / account["feed_token"] / "avito_feed.xml"            # the file Avito takes
    if not path.exists():
        if mirror_feed.stray_seen(conn, account["id"]):
            raise PictureNotTrusted("нет опубликованного файла: объявления, включённые вручную, не проверены, привязка пропущена")
        return []                                                                 # nothing of ours published yet: no ad was taken off by us
    published = {a.findtext("Id") for a in ET.parse(str(path)).getroot().findall("Ad") if a.findtext("Id")}
    left: dict = {}                                                   # when our feed last took an Id (or a position) off: Moscow, as moscow_now
    for goods_id, ad_id, _key, removed_at in store.load_retired(conn, account["id"], None):
        try:
            when = datetime.fromisoformat(removed_at)
        except (TypeError, ValueError):
            continue
        for name in (ad_id, "goods:%s" % goods_id):
            left[name] = max(left.get(name, when), when)
    offers = getattr(parsed, "offers", {}) or {}
    found = [a for a in mirror_feed.reenabled_ads(conn, account["id"], items, published, left, avito_report.last_started(conn, account["id"]),
                                                  moscow_now())
             if key_title(a["title"]) or a["ad_id"] in inverse or a["code"] in offers]
    if len(found) > REENABLED_LIMIT:
        raise PictureNotTrusted("слишком много объявлений, включены вручную под Id из файла (%d): привязка пропущена" % len(found))
    return found


def _prebind_accounts(conn, box, feed_root, results, parsed=None, client_for=None) -> list:
    """Of the feeds just built, for the live accounts: bind the new codes to the live ads of their tires before anything is published. Returns
    [(account, binds)]; when it is not empty the feeds are built again and `_prebind_report` says what really came of it. Any trouble (Avito does not
    answer in time) is only a warning: the cycle goes on as it did before."""
    if not feed_root:
        return []
    applied: list = []
    taken: set = set()
    for r in results:
        if not r.ok:
            continue
        try:
            account = connections.get_account(conn, r.account_id)
            if account["feed_mode"] == "mirror" or not account["live"]:
                continue
            if client_for is not None:
                client = client_for(account)
            else:
                if box is None:
                    continue
                client_id = connections.get_secret(conn, box, "avito", account["id"], "client_id")
                client_secret = connections.get_secret(conn, box, "avito", account["id"], "client_secret")
                if not client_id or not client_secret:
                    continue
                client = AvitoClient(client_id, client_secret, http=_http(), now=_epoch)
            moved: list = []
            binds = _prebind_account(conn, account, client, feed_root, parsed, taken, moved)
        except Exception as err:      # noqa: BLE001
            conn.rollback()
            print("предупреждение: привязка Id до публикации не выполнена (%s: %s)" % (type(err).__name__, str(err)[:120]), file=sys.stderr)
            if isinstance(err, PictureNotTrusted):                        # not a passing slowness of Avito: it stays until a human looks
                _alert(conn, box, "Tires Sync: аккаунт «%s»: %s." % (connections.display_name(conn, account["id"]), err), notice=True)
            continue
        if binds or moved:                                             # a bare change of the manual ads: built again, nothing to report as bound
            applied.append((account, binds, bool(moved)))
            taken |= {e["orphan"] for e in binds}
    return applied


def _prebind_report(conn, box, feed_root, applied, offers=None, unavailable=frozenset()) -> None:
    """After the feeds were built again: what came of the bindings. Only what is really in the new feed is called bound; the rest is a warning.
    `offers` (the unload of the cycle): a manual ad of a code the supplier has not and «На ОТХ» has not is marked «снимите вручную»; never
    one of a supplier not used this cycle (`unavailable`: its positions are held, not gone)."""
    lines, bad, manual = [], [], {}
    on_oth = None
    for entry in applied:
        account, binds = entry[0], entry[1]
        if len(entry) > 2 and entry[2]:                                # the manual ads of the cabinet changed: told once, when they change
            ads = mirror_feed.manual_ads(conn, account["id"])
            if offers is not None and on_oth is None:
                from . import order_oth
                on_oth = order_oth.goods_on_oth(conn)
            gone = [a for a in ads if _gone_everywhere(a, offers, on_oth, unavailable)]
            listed = [_manual_line(a, gone=True) for a in gone] + [_manual_line(a) for a in ads if a not in gone]     # the ones to take off first
            whom = connections.account_responsible(conn, account["id"])   # the cabinet's responsible gets its list (the owner 2026-10-30)
            manual.setdefault(whom, []).append((_cabinet_header(conn, account, len(ads)), listed))     # the whole list (owner, 2026-10-29)
        try:
            now_ids = {a.findtext("Id") for a in ET.parse(str(Path(feed_root) / account["feed_token"] / "next" / "avito_feed.xml")).getroot().findall("Ad")}
        except (OSError, ET.ParseError):
            now_ids = set()
        done = [e for e in binds if e["orphan"] in now_ids]
        shown = connections.display_name(conn, account["id"])         # the cabinet's own name: these lines go to Telegram (2026-10-30)
        bad += ["аккаунт «%s»: %s -> позиция %s" % (shown, e["orphan"], e["code"]) for e in binds if e["orphan"] not in now_ids]
        if done:
            lines.append("аккаунт «%s»: привязано до публикации: %d" % (shown, len(done)))
            lines += ["   %s «%s» -> позиция %s" % (e["orphan"], e["title"], e["code"]) for e in done[:10]]
    if lines:
        print("\n".join(lines))
        _alert(conn, box, "Tires Sync: шины, которые поставщик вернул под новым кодом, привязаны к живым объявлениям до публикации (дубли не создаются).\n" + "\n".join(lines), notice=True)
    if bad:
        print("предупреждение: привязка не применилась в фиде (другое правило задало Id), нужен разбор:\n" + "\n".join(bad), file=sys.stderr)
    from . import order_notify
    owner_login = order_notify.get_owner_login(conn) if manual else ""
    for whom, groups in manual.items():
        parts = _manual_parts(groups)
        print("\n".join(parts))
        person = _person_telegram(conn, box, whom) if whom and whom != owner_login else None
        for part in parts:                                            # each part on its own: Telegram cuts a longer message
            if person is not None:
                try:
                    person(part)
                    store.write_audit(conn, _now(), "system", "notice", {"text": part, "to": whom})
                    continue
                except Exception as err:                               # noqa: BLE001  (the person's chat failed: the owner gets it)
                    print("предупреждение: Telegram «%s» не принял сообщение (%s), отправлено владельцу" % (whom, type(err).__name__), file=sys.stderr)
            _alert(conn, box, part, notice=True)


def _gone_everywhere(ad: dict, offers, on_oth, unavailable=frozenset()) -> bool:
    """A manual ad of a code (an ad switched on by hand under an Id of a file) the supplier has not and «На ОТХ» has not: nothing to sell.
    A supplier not used this cycle (`unavailable`) says nothing of its codes."""
    from .suppliers import supplier_of_id
    code = ad.get("code")
    return (bool(code) and offers is not None and supplier_of_id(code) not in (unavailable or ()) and code not in offers
            and not (on_oth or {}).get(code))


def _person_telegram(conn, box, login: str):
    """A function that sends text to the person's own Telegram chat (order_notify.user_chats) by the owner's bot; None without one."""
    from . import order_notify
    chat = order_notify.user_chats(conn).get(login) if login else None
    if not chat or box is None:
        return None
    try:
        token = connections.get_secret(conn, box, "telegram", 0, "bot_token")
    except SecretKeyError:
        return None
    return (lambda text: notify.send_message(token, chat, text, http=_http())) if token else None


MANUAL_HEAD = ("Tires Sync: в кабинете есть объявления, сделанные вручную. Их система не снимает, а та же шина из фида рядом с ними "
               "в этом аккаунте не публикуется (чтобы не было «Повторного размещения»).")
MANUAL_MORE = "Tires Sync: объявления, сделанные вручную (продолжение %d из %d)"
AVITO_AD_URL = "https://www.avito.ru/%s"


def _cabinet_header(conn, account, count: int) -> str:
    """The cabinet named plainly: its number and name here, and the Avito profile when the cabinet was read (owner, 2026-10-29)."""
    profile = connections.get_avito_identity(conn, account["id"]).get("id")
    return "в кабинете Авито №%s «%s»%s: ручных объявлений: %d" % (account["id"], connections.display_name(conn, account["id"]),
                                                                   " (профиль Авито %s)" % profile if profile else "", count)


MANUAL_GONE = " — нет у поставщика — снимите вручную"


def _manual_line(ad: dict, gone: bool = False) -> str:
    """Telegram gets plain text (no parse mode): the link stands on its own in the line, Telegram makes it clickable."""
    return "   %s «%s» %s%s%s" % (ad["avito_id"], ad["title"], AVITO_AD_URL % ad["avito_id"],
                                " (живо вне нашего файла, Id %s)" % ad["ad_id"] if ad.get("ad_id") else "", MANUAL_GONE if gone else "")


def _manual_parts(groups, limit: int = notify.TELEGRAM_LIMIT) -> list:
    """[(header, lines)] as messages of at most `limit` characters, cut only between lines. A part that goes on with a cabinet's list
    repeats its header, so each part says which cabinet it is about; the parts are marked «(1 из N)», «(продолжение 2 из N)»."""
    budget = limit - len(MANUAL_MORE % (999, 999)) - 2                  # room for the mark, whatever N comes out
    parts: list = [[MANUAL_HEAD]]

    def put(line: str, header: str) -> None:
        if len("\n".join(parts[-1] + [line])) > budget:
            parts.append([header + " (дальше)"] if header and line != header else [])
        parts[-1].append(line[:budget - len("\n".join(parts[-1])) - 1])  # a line longer than a message is cut to its part, never past the limit

    for header, lines in groups:
        if lines and len("\n".join(parts[-1] + [header, lines[0]])) > budget:   # a header never ends a part alone
            parts.append([])
        put(header, header)
        for line in lines:
            put(line, header)
    total = len(parts)
    if total == 1:
        return ["\n".join(parts[0])]
    return ["\n".join([(MANUAL_HEAD + " (1 из %d)" % total) if n == 1 else MANUAL_MORE % (n, total)] + (part[1:] if n == 1 else part))
            for n, part in enumerate(parts, 1)]


def _heal_account(conn, account, client, feed_root, confirm: bool, renew: bool = False) -> dict:
    """The plan of `heal.plan` for one account (reads Avito, writes nothing); with `confirm` applies the bindings to our own data, and the new Ids
    only with `renew` too (owner, 2026-10-19: a new Id only by his word, never by the cycle). Returns the plan."""
    from . import heal
    path = Path(feed_root) / account["feed_token"] / "avito_feed.xml"
    feed = {a.findtext("Id"): a for a in ET.parse(str(path)).getroot().findall("Ad") if a.findtext("Id")}
    items: dict = {}
    for status in ("active", "blocked"):
        for item in client.items(status):
            items[int(item["id"])] = {"status": status, "title": item.get("title") or ""}
    ids = list(items)
    for start in range(0, len(ids), 50):
        for avito_id, xml_id in client.ad_ids_for(ids[start:start + 50]).items():
            items[avito_id]["ad_id"] = xml_id
    report_items = list(client.upload_items("current"))
    inverse = {v: k for k, v in mirror_feed.adopted_maps(conn, account["id"])[0].items()}
    today = moscow_now().date()
    result = heal.plan(feed, report_items, list(items.values()), inverse, mirror_feed.reissued_own(conn, account["id"]), today,
                       foreign_ids=_foreign_ids(conn, account["id"], feed_root), recent=_recently_back(conn, account["id"], today))
    by_hand = {a["ad_id"] for a in mirror_feed.manual_ads(conn, account["id"]) if a.get("ad_id")}
    by_hand |= {e.get("ad_id") for e in mirror_feed.stray_seen(conn, account["id"]).values() if isinstance(e, dict) and e.get("confirmed")}
    result["bind"] = [e for e in result["bind"] if e["orphan"] not in by_hand]          # owner, 2026-10-22: switched on by hand, not taken over
    if confirm and (result["bind"] or (renew and result["renew"])):
        now = moscow_now().isoformat(timespec="seconds")
        if result["bind"]:
            mirror_feed.bind_ids(conn, account["id"], {e["code"]: e["orphan"] for e in result["bind"]}, now, "ids-heal")
            mirror_feed.unreissue_own(conn, account["id"], [e["code"] for e in result["bind"]], now, "ids-heal")
        if renew and result["renew"]:
            mirror_feed.reissue_own(conn, account["id"], result["renew"], today.strftime("%Y%m%d"), now, "ids-heal")
    return result


def _recently_back(conn, account_id: int, today) -> set:
    """The codes taken off the feed that were published again within `heal.RECENT_DAYS`: Avito takes them back from the archive under the same Id."""
    from . import heal
    cutoff = (today - timedelta(days=heal.RECENT_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT DISTINCT p.goods_id FROM ad_event p WHERE p.account_id = ? AND p.event = 'published' AND p.at >= ? AND EXISTS "
        "(SELECT 1 FROM ad_event r WHERE r.account_id = p.account_id AND r.goods_id = p.goods_id AND r.event = 'removed' AND r.at < p.at)",
        (account_id, cutoff)).fetchall()
    return {row[0] for row in rows}


def _describe_heal(account, result: dict, renewed: bool = False) -> str:
    """`renewed`: the new Ids were really given (`ids-heal --confirm --renew`); otherwise they are only proposed and wait for the owner."""
    renew_head = "новый Id" if renewed else "просят новый Id (не выдан, только по слову владельца)"
    lines = ["аккаунт «%s»: привязать к живым объявлениям: %d; %s: %d; повторно удалены (не переиздаются неделю): %d; сверх лимита: %d; недавно вернулись из архива (Id не меняется): %d; живое объявление рядом (Id не меняется): %d" % (
        account["name"], len(result["bind"]), renew_head, len(result["renew"]), len(result["repeat"]), len(result["over_limit"]), len(result["recent"]), len(result["alive"]))]
    lines += ["   позиция %s «%s»: Id в фиде %s -> Id живого объявления %s" % (e["code"], e["title"], e["feed_id"], e["orphan"]) for e in result["bind"][:10]]
    lines += ["   %s: %s" % (renew_head, ", ".join(result["renew"][:10]))] if result["renew"] else []
    lines += ["   повторно удалены: %s" % ", ".join(result["repeat"][:10])] if result["repeat"] else []
    lines += ["   вернулись из архива, Id прежний: %s" % ", ".join(result["recent"][:10])] if result["recent"] else []
    lines += ["   не переиздаются: рядом живое объявление того же кода (нужен ручной разбор): %s" % ", ".join(result["alive"][:10])] if result["alive"] else []
    return "\n".join(lines)


def _cmd_ids_heal(args) -> int:
    conn, account, client = _avito_client(args, write=False)                    # reads only
    result = _heal_account(conn, account, client, args.feed_root, args.confirm, args.renew)
    print(_describe_heal(account, result, renewed=args.confirm and args.renew))
    if not args.confirm:
        print("Ничего не изменено. Чтобы применить привязки, добавьте --confirm; новые Id выдаются только с --confirm --renew (по слову владельца).")
    elif result["renew"] and not args.renew:
        print("Новые Id не выданы: только по слову владельца, с --confirm --renew.")
    return 0


def _cmd_ids_unrenew(args) -> int:
    """Take back new Ids given to positions (`ids-heal`, `mirror-reissue` of our own positions): they go back to the Id they had before."""
    conn = store.connect(args.db)
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    known = mirror_feed.reissued_own(conn, args.account)
    found = [c for c in dict.fromkeys(codes) if c in known]
    missing = [c for c in codes if c not in known]
    for code in found:
        print("   позиция %s: новый Id с меткой %s отменяется, вернётся прежний Id" % (code, known[code]))
    if missing:
        print("без нового Id (ничего не меняется): %s" % ", ".join(missing[:10]))
    if not args.confirm or not found:
        print("Ничего не изменено. Чтобы отменить, добавьте --confirm." if found else "Ничего не изменено.")
        return 0
    gone = mirror_feed.unreissue_own(conn, args.account, found, moscow_now().isoformat(timespec="seconds"), "ids-unrenew")
    print("аккаунт №%d: отменено новых Id: %d; на Авито ничего не отправлено, фид обновится в ближайшем цикле" % (args.account, len(gone)))
    return 0


def _heal_accounts(conn, box, feed_root) -> None:
    """After a publishing cycle: what Avito did not take is healed in our own data (nothing is written to Avito); a failure is only a warning."""
    if box is None or not feed_root:
        return
    lines, asked = [], {}
    done = False                                                   # something was really changed (not only waiting for a human)
    for account in connections.list_feed_accounts(conn):
        try:
            if account["feed_mode"] == "mirror":
                continue
            client_id = connections.get_secret(conn, box, "avito", account["id"], "client_id")
            client_secret = connections.get_secret(conn, box, "avito", account["id"], "client_secret")
            if not client_id or not client_secret:
                continue
            client = AvitoClient(client_id, client_secret, http=_http(), now=_epoch)
            result = _heal_account(conn, account, client, feed_root, True)                  # bindings only: a new Id waits for the owner (2026-10-19)
            key = "alerted_renew_%d" % account["id"]
            try:
                known = set(json.loads(store.get_state(conn, key) or "[]"))
            except ValueError:
                known = set()
            asked[key] = sorted(set(result["renew"]) | set(result["over_limit"]))
            fresh = bool(set(asked[key]) - known)                          # a proposal already told is not repeated every cycle
            if result["bind"] or fresh or result["repeat"] or result["alive"]:       # a bare `recent` is not worth a message; `alive` needs a human
                lines.append(_describe_heal(account, result))
                done = done or bool(result["bind"])                            # only a binding changes our data; the rest waits for a human
        except Exception as err:      # noqa: BLE001
            print("предупреждение: исправление Id не выполнено (%s: %s)" % (type(err).__name__, str(err)[:120]), file=sys.stderr)
    if lines:
        head = ("Tires Sync: исправлены Id объявлений, которые Авито не принял." if done
                else "Tires Sync: нужен ручной разбор Id объявлений (ничего не исправлено).")
        if not _alert(conn, box, head + "\n" + "\n".join(lines), notice=True):
            return                                                 # not delivered: the proposals are told again next cycle
    for key, codes in asked.items():
        store.set_state(conn, key, json.dumps(codes, ensure_ascii=False))


def _check_waiting(conn, parsed, box=None) -> None:
    """After a publishing cycle: the cards in «Ожидает наличие» are looked at against every offer of every supplier (tags and tasks for the
    responsible manager; nothing is sent). A failure is only a warning: it never fails a cycle."""
    try:
        from . import order_notify, order_wait, orders
        now = moscow_now().isoformat(timespec="seconds")
        notify = None if box is None else (lambda card_id, title, body: order_notify.deliver(
            conn, box, orders.get_card(conn, card_id), "%s\n%s" % (title, body), now, tg_http=_http(), vk_http=_http()))
        found = order_wait.check(conn, list(parsed.offers.values()), now, notify=notify)
        if found["exact"] or found["analogs"]:
            print("лист ожидания: появилось (заказы %s), есть аналоги (заказы %s)" % (
                ", ".join(map(str, found["exact"])) or "—", ", ".join(map(str, found["analogs"])) or "—"))
    except Exception as err:      # noqa: BLE001
        conn.rollback()                                            # what the check had begun is not left for the next commit
        print("предупреждение: лист ожидания не проверен (%s: %s)" % (type(err).__name__, str(err)[:120]), file=sys.stderr)


def _cmd_ids_reconcile(args) -> int:
    from . import reconcile
    conn, account, client = _avito_client(args, write=False)                    # reads only
    path = Path(args.feed_root) / account["feed_token"] / "avito_feed.xml"
    feed = {a.findtext("Id"): a for a in ET.parse(str(path)).getroot().findall("Ad") if a.findtext("Id")}
    items: dict = {}
    for status in ("active", "blocked"):
        for item in client.items(status):
            items[int(item["id"])] = {"status": status, "title": item.get("title") or ""}
    ids = list(items)
    for start in range(0, len(ids), 50):
        for avito_id, xml_id in client.ad_ids_for(ids[start:start + 50]).items():
            items[avito_id]["ad_id"] = xml_id
    found = reconcile.plan(feed, list(items.values()), _foreign_ids(conn, args.account, args.feed_root))
    inverse = {v: k for k, v in mirror_feed.adopted_maps(conn, args.account)[0].items()}
    print("аккаунт «%s»: в кабинете активных %d; живых с Id, которого нет в фиде и которые совпали с шиной из фида: %d" % (
        account["name"], sum(1 for i in items.values() if i["status"] == "active"), len(found)))
    for entry in found:
        entry["code"] = inverse.get(entry["feed_id"]) or mirror_feed.supplier_code(entry["feed_id"])
        print("   %s «%s» -> позиция %s (в фиде сейчас Id %s)" % (entry["orphan"], entry["title"], entry["code"], entry["feed_id"]))
    if not args.confirm or not found:
        print("Ничего не изменено. Чтобы привязать, добавьте --confirm." if found else "Ничего не изменено.")
        return 0
    now = moscow_now().isoformat(timespec="seconds")
    mirror_feed.bind_ids(conn, args.account, {e["code"]: e["orphan"] for e in found}, now, "ids-reconcile")
    gone = mirror_feed.unreissue_own(conn, args.account, [e["code"] for e in found], now, "ids-reconcile")
    print("привязано позиций: %d (отменено переизданий: %d); фид обновится в ближайшем цикле" % (len(found), len(gone)))
    return 0


def _cmd_ids_alias(args) -> int:
    conn = store.connect(args.db)
    account = connections.get_account(conn, args.account)
    if account is None:
        raise ValueError("аккаунт №%d не найден" % args.account)
    now = moscow_now().isoformat(timespec="seconds")
    published = [r[0] for r in conn.execute("SELECT goods_id FROM published_state WHERE account_id = ?", (args.account,))]
    codes = [c for c in published if "-x" not in c]
    on = mirror_feed.alias_scheme(conn, args.account)
    if args.off:
        print("аккаунт «%s»: схема Id %s; вернуть прежние Id вида <код>%s" % (account["name"], "«псевдонимы»" if on else "уже прежняя", connections.feed_style(conn, args.account)[0]))
        if not args.confirm:
            print("Ничего не изменено. Чтобы вернуть, добавьте --confirm.")
            return 0
        mirror_feed.set_alias_scheme(conn, args.account, False, now, "ids-alias")
        print("схема Id возвращена; уже запомненные Id остаются привязанными (меняются только новые позиции)")
        return 0
    sample = ", ".join("%s -> %s" % (c, mirror_feed.alias_id(args.account, c)) for c in codes[:3])
    print("аккаунт «%s»: позиций в фиде %d, у всех будет новый Id (например %s); старые Id (<код>%s) из фида исчезнут, Авито создаст объявления заново" % (
        account["name"], len(codes), sample or "-", connections.feed_style(conn, args.account)[0]))
    if on:
        print("схема уже включена")
        return 0
    if not args.confirm:
        print("Ничего не изменено. Чтобы включить, добавьте --confirm (на Авито ничего не отправляется до ближайшей публикации фида).")
        return 0
    mirror_feed.set_alias_scheme(conn, args.account, True, now, "ids-alias")
    print("схема включена; Id получат позиции в ближайшем цикле")
    return 0


def _cmd_stock_push(args) -> int:
    write = args.confirm and not args.dry_run and not args.info
    conn, account, client = _avito_client(args, write=write)
    if write and not args.restore and not account["live"]:
        print("ничего не отправлено: публикация аккаунта «%s» выключена" % account["name"])
        return 3
    now = moscow_now()
    if args.restore:
        lines = avito_stock.restore_lines(conn, account["id"])
        for line in lines:
            print("%s\t%s\titem_id %s\t%s → %s" % (line.code, line.ad_id, line.avito_id, line.last, line.quantity))
        print("%s: вернуть прежние числа: %d объявлений" % (account["name"], len(lines)))
        lost = avito_stock.unrestorable(conn, account["id"])
        if lost:
            print("не вернуть: до нас Авито показывал «без ограничения», API так поставить не умеет: %d объявлений" % lost)
        if not write:
            print("ничего не отправлено (нужен --confirm)")
            return 0
        print(json.dumps(avito_stock.restore(conn, client, account, now, author="cli"), ensure_ascii=False))
        return 0
    parsed, _, notes = load_all_parsed(conn, active_settings(conn), Path(args.unload))
    for note in notes:
        print(note)
    if args.info:
        rows, note = avito_stock.info(conn, client, account, parsed, args.feed_root, limit=args.limit)
        if note:
            print("%s: %s" % (account["name"], note))
        for row in rows:
            print("%s\t%s\titem_id %s\tнаш остаток %s\tАвито: %s" % (row["code"], row["ad_id"], row["item_id"], row["ours"],
                  json.dumps(row["answer"], ensure_ascii=False) if row["answer"] is not None else "ошибка: " + row["error"]))
        return 0
    cfg = avito_stock.config(conn, account["id"])
    print("%s: режим «%s»%s%s" % (account["name"], avito_stock.MODE_LABELS[cfg["mode"]],
          ", кодов в списке: %d" % len(cfg["codes"]) if cfg["mode"] == "list" else "", ", потолок %d" % cfg["cap"] if cfg["cap"] else ""))
    if cfg["mode"] == "off":
        if write:
            print("ничего не отправлено: остатки у аккаунта выключены (панель «Подключения»)")
            return 3
        print("показано, что ушло бы в режиме «все объявления»:")
        cfg = dict(cfg, mode="all")
    p = avito_stock.plan(conn, account, parsed, args.feed_root, cfg, client=client, now=now)
    if p.note:
        print("ничего не отправлено: %s" % p.note)
        return 3
    if p.stale:
        print("внимание: последний цикл собирал не опубликованный сейчас файл: коды части Id установлены без его записи")
        if write:
            print("ничего не отправлено: сначала цикл с публикацией (run-cycle --publish), затем повторите")
            return 3
    skipped: dict = {}
    for line in p.lines:
        if line.quantity is not None and not line.skip and line.avito_id is not None:
            print("%s\t%s\titem_id %s\t%s → %d%s" % (line.code, line.ad_id, line.avito_id, "?" if line.last is None else line.last,
                  line.quantity, " (%s)" % avito_stock.GONE if line.gone else ""))
        elif line.skip:
            skipped[line.skip] = skipped.get(line.skip, 0) + 1
    todo = sum(1 for line in p.lines if line.quantity is not None and not line.skip and line.avito_id is not None)
    print("к отправке: %d; пропущено: %s" % (todo, ", ".join("%s %d" % kv for kv in sorted(skipped.items())) or "нет"))
    if not write:
        print("ничего не отправлено (нужен --confirm)")
        return 0
    print(json.dumps(avito_stock.push(conn, client, account, p.lines, now, author="cli"), ensure_ascii=False))
    return 0


def _cmd_avito_ids_sync(args) -> int:
    conn, account, client = _avito_client(args, write=False)                    # reads only
    draft = Path(args.feed_root) / account["feed_token"] / "next" / "avito_feed.xml"
    ads = ET.parse(str(draft)).getroot().findall("Ad")
    feed_ids = [a.findtext("Id") or "" for a in ads]
    wanted = {mirror_feed.supplier_code(i) for i in feed_ids}
    taken = set()                                                           # Ids the feeds of the other accounts use: accounts do not repeat Ids
    for other in connections.list_feed_accounts(conn):
        if other["id"] != account["id"]:
            for name in ("avito_feed.xml", "next/avito_feed.xml"):
                try:
                    taken |= {a.findtext("Id") or "" for a in ET.parse(str(Path(args.feed_root) / other["feed_token"] / name)).getroot().findall("Ad")}
                except (OSError, ET.ParseError):
                    pass
    avito_ids = [int(item["id"]) for item in client.items("active") if item.get("id")]
    by_code: dict = {}
    by_code_all: dict = {}
    collisions: list = []
    for start in range(0, len(avito_ids), 50):
        for avito_id, xml_id in sorted(client.ad_ids_for(avito_ids[start:start + 50]).items()):
            if xml_id and mirror_feed.supplier_code(xml_id) in wanted:
                by_code_all.setdefault(mirror_feed.supplier_code(xml_id), []).append(str(xml_id))
                if str(xml_id) in taken:
                    collisions.append(str(xml_id))
                    continue
                by_code.setdefault(mirror_feed.supplier_code(xml_id), []).append(str(xml_id))
    now = moscow_now().isoformat(timespec="seconds")
    if not args.confirm:
        probe = {}
        for current in feed_ids:
            code = mirror_feed.supplier_code(current)
            if by_code.get(code) and current not in by_code[code]:
                probe[code] = by_code[code][0]
        print("аккаунт «%s»: объявлений в кабинете (активных) %d; позиций, у которых Id в фиде отличается от Id уже существующего объявления: %d" % (
            account["name"], len(avito_ids), len(probe)))
        for code, xml_id in list(probe.items())[:5]:
            print("   %s -> %s" % (code, xml_id))
        if collisions:
            print("не привязаны (такой же Id уже у другого аккаунта, Id не повторяем): %d, например %s" % (len(collisions), ", ".join(collisions[:3])))
        print("Ничего не изменено. Чтобы привязать, добавьте --confirm.")
        return 0
    shared = {}
    if args.allow_shared_until:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.allow_shared_until):
            raise ValueError("--allow-shared-until: дата ГГГГ-ММ-ДД")
        for current in feed_ids:
            code = mirror_feed.supplier_code(current)
            if any(x in taken for x in by_code_all.get(code, [])):
                shared[code] = args.allow_shared_until
    done = mirror_feed.rebind_ids(conn, account["id"], feed_ids, by_code_all if shared else by_code, now, "avito-ids-sync",
                                  shared=shared, taken=taken)
    print("привязано позиций к существующим Id: %d (из них с общим Id до %s: %d); не привязано из-за такого же Id у другого аккаунта: %d; "
          "фид обновится в ближайшем цикле" % (len(done), args.allow_shared_until or "-", sum(1 for c in done if c in shared),
                                               0 if shared else len(collisions)))
    return 0


def _cmd_mirror_reissue(args) -> int:
    conn = store.connect(args.db)
    now = moscow_now().isoformat(timespec="seconds")
    ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    stamp = args.stamp or now[:10].replace("-", "")
    if not args.confirm:
        stored = set(mirror_feed.stored_ids(conn, args.account))
        missing = [i for i in ids if i not in stored and not re.fullmatch(r"\d+(-x\d+)?", i)]
        if missing:
            raise ValueError("Этих Id нет в файле аккаунта: %s." % ", ".join(missing[:5]))
        print("аккаунт №%d: будут переизданы %d объявлений (новый Id = старый + _r%s); например %s -> %s_r%s" % (args.account, len(ids), stamp, ids[0], ids[0], stamp))
        print("Ничего не изменено. Чтобы переиздать, добавьте --confirm (на Авито они появятся как новые при ближайшей публикации фида).")
        return 0
    stored = set(mirror_feed.stored_ids(conn, args.account))
    own = [i for i in ids if i not in stored and re.fullmatch(r"\d+(-x\d+)?", i)]       # positions we make ourselves have the supplier code as the Id
    done = mirror_feed.reissue_ids(conn, args.account, [i for i in ids if i not in own], stamp, now, "mirror-reissue") if len(own) < len(ids) else {}
    if own:
        done.update(mirror_feed.reissue_own(conn, args.account, own, stamp, now, "mirror-reissue"))
    print("аккаунт №%d: переиздано %d объявлений; на Авито ничего не отправлено, фид обновится в ближайшем цикле" % (args.account, len(done)))
    return 0


def _cmd_feed_import(args) -> int:
    conn = store.connect(args.db)
    data = Path(args.file).read_bytes()
    result = mirror_feed.import_feed(conn, args.account, data, moscow_now().isoformat(timespec="seconds"))
    print("аккаунт №%d: загружено объявлений %d (с картинками %d), режим «зеркало»; на Авито ничего не отправлено" % (args.account, result["ads"], result["with_images"]))
    return 0


def _cmd_chat_test_send(args) -> int:
    box = load_box(args.key_file)
    if box is None:
        raise ValueError("мастер-ключ недоступен (проверьте --key-file или TIRES_SECRET_KEY_FILE)")
    conn = store.connect(args.db)
    chat = chats.get_chat(conn, args.chat)
    if chat is None:
        raise ValueError("чат Ч-%d не найден" % args.chat)
    account = connections.get_account(conn, chat["account_id"])
    print("аккаунт «%s», чат Ч-%d, собеседник «%s», текст (%d знаков) будет отправлен клиенту от имени аккаунта" % (account["name"], chat["id"], chat["peer_name"], len(args.text)))
    if not args.confirm:
        print("Ничего не отправлено. Чтобы отправить, добавьте --confirm.")
        return 0
    client_id = connections.get_secret(conn, box, "avito", account["id"], "client_id")
    client_secret = connections.get_secret(conn, box, "avito", account["id"], "client_secret")
    client = AvitoClient(client_id, client_secret, http=_http(), now=_epoch, allow_write=True)
    try:
        chats.send_reply(conn, client, chat["id"], args.text, "test-%d-%s" % (chat["id"], __import__("secrets").token_hex(6)), moscow_now().isoformat(timespec="seconds"), "пробная отправка", force=True)
    except chats.ChatError as err:
        print("не отправлено: %s" % err, file=sys.stderr)
        return 1
    print("отправлено")
    return 0


def _cmd_chat_test_send_image(args) -> int:
    box = load_box(args.key_file)
    if box is None:
        raise ValueError("мастер-ключ недоступен (проверьте --key-file или TIRES_SECRET_KEY_FILE)")
    conn = store.connect(args.db)
    chat = chats.get_chat(conn, args.chat)
    if chat is None:
        raise ValueError("чат Ч-%d не найден" % args.chat)
    account = connections.get_account(conn, chat["account_id"])
    data = Path(args.file).read_bytes()
    try:
        picture = chats.prepare_image(data)
    except chats.ChatError as err:
        print("не отправлено: %s" % err, file=sys.stderr)
        return 1
    print("аккаунт «%s», чат Ч-%d, собеседник «%s», фото (%d КБ после уменьшения) будет отправлено клиенту от имени аккаунта" % (
        account["name"], chat["id"], chat["peer_name"] or "без имени", len(picture) // 1024))
    if not args.confirm:
        print("Ничего не отправлено. Чтобы отправить, добавьте --confirm.")
        return 0
    client_id = connections.get_secret(conn, box, "avito", account["id"], "client_id")
    client_secret = connections.get_secret(conn, box, "avito", account["id"], "client_secret")
    client = AvitoClient(client_id, client_secret, http=_http(), now=_epoch, allow_write=True)
    try:
        chats.send_image(conn, client, chat["id"], data, "test-%d-%s" % (chat["id"], __import__("secrets").token_hex(6)),
                         moscow_now().isoformat(timespec="seconds"), "пробная отправка", force=True)
    except chats.ChatError as err:
        print("не отправлено: %s" % err, file=sys.stderr)
        return 1
    print("отправлено")
    return 0


CHAT_ALARM_AFTER_SECONDS = 15 * 60                                                # a short outside trouble (network, Avito 429/5xx) is an alarm only when the account stays unread longer
CHAT_FULL_EVERY = 5


def _chat_miss_is_short(err: BaseException) -> bool:
    """A trouble outside of us that passes by itself: a cut or timed out connection, an SSL error, a busy base, Avito's 429 and 5xx.
    Everything else (401/403, other 4xx, an odd answer, a bug) is real: it does not pass by waiting."""
    if isinstance(err, AvitoError):
        return err.status == 429 or err.status >= 500
    return isinstance(err, (OSError, sqlite3.OperationalError))                 # URLError, socket.timeout and ssl.SSLError are OSError


def _chat_unread_for(since: str, now: str) -> Optional[float]:
    """Seconds from the first miss of the series to now; None when the stored time is unusable (broken, from the future): the series starts over."""
    try:
        seconds = (datetime.fromisoformat(now) - datetime.fromisoformat(since)).total_seconds()
    except (ValueError, TypeError):
        return None
    return seconds if seconds >= 0 else None


def _cmd_telegram_inbox(args) -> int:
    box = load_box(args.key_file)
    if box is None:
        raise ValueError("мастер-ключ недоступен (проверьте --key-file или TIRES_SECRET_KEY_FILE)")
    conn = store.connect(args.db)
    chat_id = connections.get_config(conn, "telegram", 0).get("chat_id")
    token = connections.get_secret(conn, box, "telegram", 0, "bot_token")
    if not chat_id or not token:
        raise ValueError("Telegram не настроен: нужны токен бота и chat_id в «Подключения»")
    try:
        got = notify.read_inbox(token, chat_id, http=_http(), download_to=args.save_to or None)
    except notify.NotifyError as err:
        raise ValueError(str(err)) from None
    print("сообщений от вас: %d, чужих проигнорировано: %d" % (len(got["messages"]), got["ignored"]))
    for number, m in enumerate(got["messages"], 1):
        when = datetime.fromtimestamp(m["at"]).isoformat(timespec="minutes") if m.get("at") else ""
        print("--- %d %s%s" % (number, when, " (пересланное)" if m["forwarded"] else ""))
        print(m["text"])
        for f in m.get("files", []):
            if f.get("path"):
                print("    сохранён: %s" % f["path"])
    return 0


def _cmd_chats_sync(args) -> int:
    box = load_box(args.key_file)
    if box is None:
        raise ValueError("мастер-ключ недоступен (проверьте --key-file или TIRES_SECRET_KEY_FILE)")
    conn = store.connect(args.db)
    now = moscow_now().isoformat(timespec="seconds")
    with chats.sync_lock(Path(args.db).resolve().parent / ".chats.lock") as held:
        if not held:
            print("чтение чатов уже идёт, этот запуск пропущен")
            return 0
        return _chats_sync_locked(conn, box, args, now)


def _cmd_calls_sync(args) -> int:
    from . import calls
    box = load_box(args.key_file)
    if box is None:
        raise ValueError("мастер-ключ недоступен (проверьте --key-file или TIRES_SECRET_KEY_FILE)")
    conn = store.connect(args.db)
    now = moscow_now().isoformat(timespec="seconds")

    def client_for(account):
        client_id = connections.get_secret(conn, box, "avito", account["id"], "client_id")
        client_secret = connections.get_secret(conn, box, "avito", account["id"], "client_secret")
        return AvitoClient(client_id, client_secret, http=_http(), now=_epoch) if client_id and client_secret else None
    with chats.sync_lock(Path(args.db).resolve().parent / ".calls.lock") as held:
        if not held:
            print("чтение звонков уже идёт, этот запуск пропущен")
            return 0
        return calls.run(conn, client_for, now, Path(args.db).resolve().parent / "calls", time.sleep, account=args.account, dry_run=args.dry_run,
                         warn=lambda text: print(text, file=sys.stderr), alert=lambda text: _alert(conn, box, text))


def _cmd_clients_link(args) -> int:
    """The orders and chats made before the clients came: a report of what joins (read only), or with --confirm the writing in one go."""
    from . import clients
    conn = store.connect(args.db)
    if args.confirm:
        conn.execute("BEGIN IMMEDIATE")                                 # the plan and its writing see the same base: the panel waits
    plan = clients.plan_links(conn)
    print("новых клиентов: %d, к уже известным: %d, заказов свяжется: %d, чатов: %d, спорных групп (объединить вручную): %d" % (
        len(plan["new_clients"]), len(plan["to_existing"]), plan["orders"], plan["chats"], len(plan["conflicts"])))
    for entry in plan["new_clients"][:20]:
        print("  новый клиент: заказы %s, чаты %s, телефоны %d" % (
            ", ".join("№%d" % o for o in entry["orders"]) or "—", ", ".join("Ч-%d" % h for h in entry["chats"]) or "—", len(entry["phones"])))
    if plan["large"]:
        print("крупные группы (проверьте до --confirm: один человек или общий номер?):")
    for entry in plan["large"]:
        print("  %d заказов, %d чатов, телефоны %s, имена: %s" % (len(entry["orders"]), len(entry["chats"]),
                                                               ", ".join(clients.display(p) for p in entry["phones"]) or "—", ", ".join(entry["names"])[:200] or "—"))
    for entry in plan["conflicts"][:20]:
        print("  спорно: клиенты %s, заказы %s, чаты %s" % (", ".join("№%d" % c for c in entry["clients"]),
                                                          ", ".join("№%d" % o for o in entry["orders"]) or "—", ", ".join("Ч-%d" % h for h in entry["chats"]) or "—"))
    if not args.confirm:
        print("это отчёт: ничего не записано; запись — с --confirm (сначала копия базы)")
        return 0
    made = clients.apply_links(conn, plan, moscow_now().isoformat(timespec="seconds"))
    print("записано: клиентов %d, заказов %d, чатов %d" % (made["clients"], made["orders"], made["chats"]))
    return 0


def _handoff_look(conn, box, now: str) -> None:
    """Every minute with the chats: the clients the points handed over (15 minutes to take one; nothing goes to the point, 2026-10-23).
    In its own guard: a failure here is a warning, never a failed reading of the chats (and never the alarm of tires-chats)."""
    try:
        from . import order_manager, order_notify
        made = order_manager.handoff_tick(conn, now, notify=lambda card, text: order_notify.deliver(conn, box, card, text, now, tg_http=_http(),
                                                                                                     vk_http=_http()))
        if made and made.get("overdue"):
            print("клиенты с точек: просрочено %d" % made["overdue"])
    except Exception as err:      # noqa: BLE001
        conn.rollback()
        print("предупреждение: клиенты с точек не проверены (%s: %s)" % (type(err).__name__, str(err)[:120]), file=sys.stderr)


def _inquiry_look(conn, box, now: str) -> None:
    """Every minute with the chats: the statuses of the inquiries (three days to «отказ»), the start of «Срочно ответить», and the safety
    net of the points in the panel (an alert nobody opened in 15 working minutes goes to the owner). In its own guard, as _handoff_look."""
    try:
        from . import inquiry, order_alerts, order_notify
        if not store.get_state(conn, order_alerts.URGENT_START_KEY):
            store.set_state(conn, order_alerts.URGENT_START_KEY, now)            # a client who waited from before this release is the past
        made = inquiry.tick(conn, now)
        if any(made.values()):
            print("обращения: " + ", ".join("%s %d" % (inquiry.STATUSES[k], v) for k, v in made.items() if v))
    except Exception as err:      # noqa: BLE001
        conn.rollback()
        print("предупреждение: обращения не проверены (%s: %s)" % (type(err).__name__, str(err)[:120]), file=sys.stderr)
    try:                                                                         # the safety net never waits on the inquiries
        from . import order_notify
        passed = order_notify.escalate_unseen(conn, box, now, tg_http=_http())
        if passed:
            print("точки: не открыли оповещение за 15 минут, передано владельцу: %d" % passed)
    except Exception as err:      # noqa: BLE001
        conn.rollback()
        print("предупреждение: оповещения точек не проверены (%s: %s)" % (type(err).__name__, str(err)[:120]), file=sys.stderr)


def _settle_look(conn, client, account, client_id, client_secret, now: str) -> None:
    """Every minute after the chats of the account are read: the sends Avito refused with a 402 go again, the unknown ones are looked up in
    the chat (chats.settle_sends, the owner 2026-10-25). In its own guard, as _handoff_look: a failure here never fails the reading."""
    try:
        made = chats.settle_sends(conn, client, lambda: AvitoClient(client_id, client_secret, http=_http(), now=_epoch, allow_write=True),
                                  account["id"], now, clock=lambda: moscow_now().isoformat(timespec="seconds"))
        if made:
            print("аккаунт «%s»: отправки клиентам разобраны: %s" % (account["name"], ", ".join("%s %d" % kv for kv in sorted(made.items()))))
    except Exception as err:      # noqa: BLE001
        conn.rollback()
        print("предупреждение: аккаунт «%s»: отправки клиентам не разобраны (%s)" % (account["name"], type(err).__name__), file=sys.stderr)


def _chats_sync_locked(conn, box, args, now) -> int:
    runs = int(store.get_state(conn, "chats_runs") or "0") + 1
    store.set_state(conn, "chats_runs", str(runs))
    full = (runs % CHAT_FULL_EVERY == 1)                                        # the minute runs are light (the newest chats); every fifth reads the whole list
    failed = 0
    for account in connections.list_chat_accounts(conn):
        if args.account and account["id"] != args.account:
            continue
        try:
            client_id = connections.get_secret(conn, box, "avito", account["id"], "client_id")
            client_secret = connections.get_secret(conn, box, "avito", account["id"], "client_secret")
        except SecretKeyError:                                                   # one account whose key cannot be read must not stop the others
            print("аккаунт «%s»: ключи не читаются (повреждены или другой мастер-ключ), пропущен" % account["name"], file=sys.stderr)
            failed += 1
            continue
        if not client_id or not client_secret:
            print("аккаунт «%s»: ключи не заданы, пропущен" % account["name"])
            continue
        client = AvitoClient(client_id, client_secret, http=_http(), now=_epoch)
        try:
            stats = chats.sync_account(conn, client, account["id"], now, max_pages=6 if full else 1)
            if full:
                try:
                    healed = chats.retry_history(conn, client, account["id"], now)
                    if healed["opened"]:
                        print("аккаунт «%s»: Авито открыл историю у %d чатов из %d проверенных" % (account["name"], healed["opened"], healed["tried"]))
                except (AvitoError, OSError):
                    pass
        except Exception as err:                                                 # a network miss, but also an odd answer of Avito or a busy base: one account never stops the others
            conn.rollback()
            reason = err if isinstance(err, (AvitoError, OSError)) else type(err).__name__          # of an odd failure only its kind is written
            key = "chats_fail_%d" % account["id"]
            short = _chat_miss_is_short(err)
            misses, unread = 0, 0.0
            try:
                misses = int(store.get_state(conn, key) or "0") + 1
                store.set_state(conn, key, str(misses))
                since = store.get_state(conn, key + "_since") or ""
                unread = _chat_unread_for(since, now) if since else None
                if unread is None:                                               # the first miss of a series (or a time that cannot be used)
                    unread = 0.0
                    store.set_state(conn, key + "_since", now)
            except sqlite3.Error:                                                # the base is busy: the miss cannot be counted this time, the run still goes on
                conn.rollback()
                print("аккаунт «%s»: счётчик пропусков не записан (база занята)" % account["name"], file=sys.stderr)
            if not short:                                                        # a real trouble (refused access, an odd answer, a bug) is not waited out
                failed += 1
                print("аккаунт «%s»: нужно внимание: чаты не прочитаны (%s), тревога сразу" % (account["name"], reason), file=sys.stderr)
                continue
            if unread > CHAT_ALARM_AFTER_SECONDS:                                # Avito or the network is away: only a long silence is an alarm
                failed += 1
            print("аккаунт «%s»: чаты не прочитаны (%s), подряд %d, уже %d мин" % (account["name"], reason, misses, unread // 60), file=sys.stderr)
            continue
        try:
            store.set_state(conn, "chats_fail_%d_since" % account["id"], "")             # the start time first: a half done reset must not leave an old start
            store.set_state(conn, "chats_fail_%d" % account["id"], "0")
        except sqlite3.Error:                                                    # a busy base after a good read: the chats are in, the count is cleared next time
            conn.rollback()
        print("аккаунт «%s»: чатов %d, новых %d, сообщений %d, ошибок %d" % (account["name"], stats["chats"], stats["new_chats"], stats["messages"], stats["errors"]))
        _settle_look(conn, client, account, client_id, client_secret, now)
    made = chat_deals.process_all(conn, now)
    _handoff_look(conn, box, now)
    _inquiry_look(conn, box, now)
    if full:
        chats.clean_old(conn, now)
    print("сделок создано %d, чатов привязано %d, телефонов записано %d, имён из чата %d" % (made["created"], made["linked"], made["phones"], made.get("names", 0)))
    return 1 if failed else 0


def _webhook_base(args, conn=None):
    """The panel's address outside for a webhook command into `args.base_url`: --base-url, else TIRES_PUBLIC_BASE, else `public_base` of
    the base (the connection is opened only for that one and returned)."""
    base = (args.base_url or os.environ.get("TIRES_PUBLIC_BASE", "")).strip()
    if not base:
        conn = conn or store.connect(args.db)
        base = (store.get_state(conn, "public_base") or "").strip()
    if not base:
        raise ValueError("не задан адрес панели: укажите --base-url или TIRES_PUBLIC_BASE")
    if not base.startswith("https://"):
        raise ValueError("адрес панели (--base-url, TIRES_PUBLIC_BASE или public_base): только https://")
    args.base_url = base
    return conn


def _webhook_setup(args, write: bool = False, conn=None):
    """(connection, account, Avito client) of a webhook command; the client may write only when `write`."""
    conn = _webhook_base(args, conn)
    box = load_box(args.key_file)
    if box is None:
        raise ValueError("мастер-ключ недоступен (проверьте --key-file или TIRES_SECRET_KEY_FILE)")
    conn = conn or store.connect(args.db)
    account = connections.get_account(conn, args.account)
    if account is None:
        raise ValueError("аккаунт %d не найден" % args.account)
    client_id = connections.get_secret(conn, box, "avito", account["id"], "client_id")
    client_secret = connections.get_secret(conn, box, "avito", account["id"], "client_secret")
    if not client_id or not client_secret:
        raise ValueError("у аккаунта «%s» не заданы ключи Авито" % account["name"])
    return conn, account, AvitoClient(client_id, client_secret, http=_http(), now=_epoch, allow_write=write)


def _cmd_avito_webhook_subscribe(args) -> int:
    from . import avito_webhook
    conn = _webhook_base(args) or store.connect(args.db)
    account = connections.get_account(conn, args.account)
    if account is None:
        raise ValueError("аккаунт %d не найден" % args.account)
    if not account["chats_enabled"]:
        raise ValueError("у аккаунта «%s» не включено «Читать чаты»: вебхук только для аккаунтов с чатами" % account["name"])
    print("аккаунт «%s»: Авито будет присылать новые сообщения на %s<новый секрет>; наши прежние адреса %s у этого кабинета будут сняты, "
          "другие подписки кабинета не трогаются" % (account["name"], args.base_url.rstrip("/") + avito_webhook.HOOK_PATH, avito_webhook.HOOK_PATH))
    if not args.confirm:
        print("Ничего не записано, Авито не вызывался. Чтобы подписать, добавьте --confirm.")
        return 0
    conn, account, client = _webhook_setup(args, write=True, conn=conn)
    before = avito_webhook.subscriptions(client)
    now = moscow_now().isoformat(timespec="seconds")
    url = avito_webhook.hook_url(args.base_url, avito_webhook.new_secret(conn, account["id"], now))
    old = [u for u in before if avito_webhook.ours(u, args.base_url) and u != url]
    try:
        avito_webhook.subscribe(client, url)
        avito_webhook.mark_subscribed(conn, account["id"], now)
        for u in old:
            avito_webhook.unsubscribe(client, u)
    except Exception:
        # the new secret is already ours, the old address answers 404; Avito may or may not have taken the new one: a repeat sets it right
        print("подписка не завершена: посмотрите avito-webhook-status и повторите avito-webhook-subscribe --confirm "
              "(повтор снимет все наши прежние адреса); сообщения пока читаются раз в минуту", file=sys.stderr)
        raise
    store.write_audit(conn, now, "команда", "avito_webhook_subscribe", {"account_id": account["id"], "url": avito_webhook.masked(url), "replaced": len(old)})
    conn.commit()
    seen = url in avito_webhook.subscriptions(client)
    print("подписан: %s; прежних наших адресов снято %d; в списке Авито %s" % (avito_webhook.masked(url), len(old), "есть" if seen else "НЕТ — проверьте avito-webhook-status"))
    return 0 if seen else 1


def _cmd_avito_webhook_unsubscribe(args) -> int:
    from . import avito_webhook
    conn, account, client = _webhook_setup(args, write=bool(args.confirm))
    mine = [u for u in avito_webhook.subscriptions(client) if avito_webhook.ours(u, args.base_url)]
    print("аккаунт «%s»: наших подписок у Авито %d%s" % (account["name"], len(mine), (": " + ", ".join(avito_webhook.masked(u) for u in mine)) if mine else ""))
    if not args.confirm:
        print("Ничего не записано. Чтобы снять подписки и забыть секрет, добавьте --confirm.")
        return 0
    for u in mine:
        avito_webhook.unsubscribe(client, u)
    avito_webhook.forget(conn, account["id"])
    now = moscow_now().isoformat(timespec="seconds")
    store.write_audit(conn, now, "команда", "avito_webhook_unsubscribe", {"account_id": account["id"], "removed": len(mine)})
    conn.commit()
    print("снято %d; секрет забыт, сообщения читаются раз в минуту" % len(mine))
    return 0


def _cmd_avito_webhook_status(args) -> int:
    from . import avito_webhook
    conn = _webhook_base(args) or store.connect(args.db)
    accounts = [connections.get_account(conn, args.account)] if args.account else connections.list_chat_accounts(conn)
    failed = 0
    for account in accounts:
        if account is None:
            raise ValueError("аккаунт %d не найден" % args.account)
        st = avito_webhook.state(conn, account["id"], moscow_now())
        print("аккаунт «%s»: у нас %s, последнее событие %s, за сутки %d" % (
            account["name"], ("подписан с %s" % st["subscribed_at"]) if st["subscribed"] else "не подписан", st["last_event_at"] or "—", st["day"]))
        try:
            _conn, _account, client = _webhook_setup(argparse.Namespace(**dict(vars(args), account=account["id"])), conn=conn)
            urls = avito_webhook.subscriptions(client)
        except (ValueError, SecretKeyError, AvitoError, OSError) as err:
            failed += 1
            print("  у Авито: не прочитано (%s)" % err, file=sys.stderr)
            continue
        for u in urls or ["—"]:
            print("  у Авито: %s%s" % (avito_webhook.masked(u), " (наш)" if avito_webhook.ours(u, args.base_url) else ""))
    return 1 if failed else 0


def _cmd_avito_stats(args) -> int:
    box = load_box(args.key_file)
    if box is None:
        raise ValueError("мастер-ключ недоступен (проверьте --key-file или TIRES_SECRET_KEY_FILE)")
    conn = store.connect(args.db)
    accounts = [connections.get_account(conn, args.account)] if args.account else connections.list_feed_accounts(conn)
    failed = 0
    for account in accounts:
        if account is None:
            raise ValueError("аккаунт №%d не найден" % args.account)
        client_id = connections.get_secret(conn, box, "avito", account["id"], "client_id")
        client_secret = connections.get_secret(conn, box, "avito", account["id"], "client_secret")
        if not client_id or not client_secret:
            print("аккаунт «%s»: ключи не заданы, пропущен" % account["name"])
            continue
        client = AvitoClient(client_id, client_secret, http=_http(), now=_epoch)
        try:
            result = ad_stats.collect(conn, client, account["id"], date.today(), days=args.days, now_iso=_now())
        except (AvitoError, OSError) as err:
            failed += 1
            print("аккаунт «%s»: статистика не собрана (%s)" % (account["name"], err), file=sys.stderr)
            continue
        print("аккаунт «%s»: объявлений %d, новых сопоставлений с нашими %d, строк статистики %d" % (
            account["name"], result["ads"], result["mapped"], result["days"]))
    _collect_avito_reports(conn, box)
    return 1 if failed else 0


def _cmd_avito_status(args) -> int:
    conn, account, client = _avito_client(args, write=False)
    report = status_report(client)
    for name in ("current", "last"):
        up = report[name]
        print("%s: %s" % ("текущая" if name == "current" else "последняя успешная",
                          "нет" if up is None else "№%s, %s, начата %s, объявлений %s" % (
                              up["upload_id"], up["status"], up["started_at"], up["count"])))
    for status, count in sorted(report["counts"].items()):
        print("%s: %d" % (status, count))
    for problem in report["problems"]:
        print("  %s [%s] %s" % (problem["ad_id"], problem["status"], problem["text"]))
    return 0


def _cmd_backup(args) -> int:
    path = backup_db(args.db, args.out_dir, date.today(), keep=args.keep)
    info = verify_backup(path)
    print("резервная копия: %s (проверена: таблиц %d)" % (path, info["tables"]))
    return 0


def _offsite_http():
    return offsite.storage_transport


def _cmd_backup_offsite(args) -> int:
    """Not set up (no settings file): nothing is done, code 0. Any failure: code 1, the service tells Telegram (OnFailure)."""
    try:
        cfg = offsite.read_config(args.config)
        if cfg is None:
            print("копия вне сервера не настроена: нет файла %s (как настроить: инструкция «Копия базы вне сервера» у владельца)" % args.config)
            return 0
        info = offsite.send_copy(args.db, cfg, date.today(), http=_offsite_http())
    except (offsite.OffsiteError, BackupError, OSError, sqlite3.Error) as err:
        print("error: копия вне сервера не сделана: %s" % err, file=sys.stderr)
        return 1
    print("копия вне сервера: %s, %d КБ, проверена (таблиц %d); удалено старых %d, в хранилище %d" % (
        info["name"], info["size"] // 1024, info["tables"], info["removed"], info["kept"]))
    return 0


def _cmd_backup_offsite_get(args) -> int:
    cfg = None
    if not (args.in_file and args.key_file):                                    # a file and a key at hand: no storage needed
        cfg = offsite.read_config(args.config)
        if cfg is None:
            raise offsite.OffsiteError("нет файла настроек %s: укажите --in и --key-file" % args.config)
    key = offsite.load_key(args.key_file or cfg["key_file"])
    info = offsite.fetch_copy(args.out, key, cfg=cfg, name=args.name, in_file=args.in_file, http=_offsite_http())
    print("копия %s расшифрована и проверена (таблиц %d): %s" % (info["name"], info["tables"], args.out))
    return 0


def _cmd_backup_key_new(args) -> int:
    offsite.new_key_file(args.out)
    print("ключ копий записан в %s (права 600). Сохраните его содержимое у себя вне сервера: без него копии не открыть." % args.out)
    return 0


def _read_password(args) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    import getpass
    first = getpass.getpass("Пароль: ")
    if first != getpass.getpass("Пароль ещё раз: "):
        raise users.UserError("Пароли не совпали.")
    return first


def _cmd_users(args) -> int:
    conn = store.connect(args.db)
    if args.command == "user-list":
        for user in users.list_users(conn):
            print("%s  (создан %s)" % (user["login"], user["created"]))
        return 0
    if args.command == "user-add":
        users._check_role(conn, args.role, args.point_id)                     # a point of a manager of a point is there
        login = users.add_user(conn, args.login, _read_password(args), _now(), role=args.role, point_id=args.point_id, display_name=args.name)
        print("пользователь «%s» добавлен, роль %s" % (login, users.ROLES[args.role]))
    elif args.command == "user-passwd":
        users.set_password(conn, args.login, _read_password(args), _now())
        print("пароль изменён, открытые сессии пользователя закрыты")
    else:
        users.remove_user(conn, args.login, _now())
        print("пользователь удалён, его сессии закрыты")
    return 0


def _cmd_alert(args) -> int:
    conn = store.connect(args.db)
    box = load_box(args.key_file) if args.key_file else None
    if box is None:
        return 0
    _alert(conn, box, args.message, service=True)
    return 0


def _cmd_settings(args) -> int:
    conn = store.connect(args.db)
    if args.action == "init":
        raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
        print("initialized, version", init_settings(conn, raw, _now(), "cli"))
        return 0
    version, raw = get_active(conn)
    print("version:", version)
    for key in ("territory_rn", "min_stock", "hysteresis", "min_diameter", "portion_limit", "stale_days",
                "point_mode", "point_values", "enabled"):
        print("%s: %s" % (key, json.dumps(raw[key], ensure_ascii=False)))
    return 0


def _cmd_mappings(args) -> int:
    conn = store.connect(args.db)
    ref = parse_reference(Path(args.reference).read_text(encoding="utf-8"))
    if args.action == "confirm":
        confirm_mapping(conn, args.brand, args.model, args.to, ref, _now(), "cli")
        print("confirmed: %s / %s -> %s" % (args.brand, args.model, args.to))
        return 0
    if args.action == "auto":
        parsed, _, _ = load_all_parsed(conn, active_settings(conn), Path(args.unload))
        report = auto_mappings(conn, parsed, ref, _now(), "cli", apply=args.apply)
        print("%s: %d, оставлено вам: %d" % ("принято" if args.apply else "будет принято", len(report["accepted"]), len(report["held"])))
        for a in report["accepted"]:
            print("  + %-12s %-32s x%-3d -> %s" % (a["brand"], a["model"], a["goods"], a["to"]))
        for h in report["held"]:
            print("  ? %-12s %-32s x%-3d %-9s %s" % (h["brand"], h["model"], h["goods"], h["tier"], h["options"]))
        return 0
    settings = active_settings(conn)
    if args.unload:
        parsed, _, _ = load_all_parsed(conn, settings, Path(args.unload))
    else:
        parsed = parse_unload(Path(args.xml).read_bytes(), settings.territory_rn)
    ev = evaluate(parsed.offers, parsed.issues, settings, ref, store.load_confirmed_mappings(conn),
                  store.load_brand_map(conn))
    pending = pending_mappings(ev, ref)
    print("pairs awaiting confirmation: %d (goods: %d)" % (len(pending), sum(p.goods_count for p in pending)))
    for p in pending:
        print("%-14s %-34s x%-3d -> %s" % (p.brand, p.model_raw, p.goods_count, p.suggestions))
    return 0


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    from . import reliability                                       # the breaker of the outside services keeps its state in this base
    reliability.bind(getattr(args, "db", None), reliability.owner_alert(lambda: load_box(args.key_file) if getattr(args, "key_file", None) else None))
    try:
        if args.command == "dry-run":
            return _cmd_dry_run(args)
        if args.command == "photo-queue":
            return _cmd_photo_queue(args)
        if args.command == "photo-agent-tick":
            from . import photo_agent
            return photo_agent.tick_server(store.connect(args.db), lambda: load_box(args.key_file) if args.key_file else None, args.agent_bin,
                                           args.agent_dir, datetime.utcnow().isoformat(timespec="seconds"))   # UTC like the panel
        if args.command == "settings":
            return _cmd_settings(args)
        if args.command == "fetch-unload":
            return _cmd_fetch(args)
        if args.command == "run-cycle":
            return _cmd_run_cycle(args)
        if args.command == "alert":
            return _cmd_alert(args)
        if args.command == "feed-publish":
            return _cmd_feed_publish(args)
        if args.command == "avito-publish":
            return _cmd_avito_publish(args)
        if args.command == "avito-status":
            return _cmd_avito_status(args)
        if args.command == "avito-stats":
            return _cmd_avito_stats(args)
        if args.command == "catalog-probe":
            return _cmd_catalog_probe(args)
        if args.command == "mirror-go-live":
            return _cmd_mirror_go_live(args)
        if args.command == "feed-import":
            return _cmd_feed_import(args)
        if args.command == "mirror-reissue":
            return _cmd_mirror_reissue(args)
        if args.command == "avito-ids-sync":
            return _cmd_avito_ids_sync(args)
        if args.command == "stock-push":
            return _cmd_stock_push(args)
        if args.command == "mirror-adopt":
            return _cmd_mirror_adopt(args)
        if args.command == "ids-alias":
            return _cmd_ids_alias(args)
        if args.command == "ids-reconcile":
            return _cmd_ids_reconcile(args)
        if args.command == "ids-heal":
            return _cmd_ids_heal(args)
        if args.command == "ids-unrenew":
            return _cmd_ids_unrenew(args)
        if args.command == "feed-check":
            return _cmd_feed_check(args)
        if args.command == "overrides-clear-aged":
            return _cmd_overrides_clear_aged(args)
        if args.command == "match-report":
            return _cmd_match_report(args)
        if args.command == "match-pair-forget":
            return _cmd_match_pair_forget(args)
        if args.command == "match-not-pair":
            return _cmd_match_not_pair(args)
        if args.command == "match-synonym-add":
            return _cmd_match_synonym_add(args)
        if args.command == "chat-test-send":
            return _cmd_chat_test_send(args)
        if args.command == "chat-test-send-image":
            return _cmd_chat_test_send_image(args)
        if args.command == "telegram-inbox":
            return _cmd_telegram_inbox(args)
        if args.command == "avito-webhook-subscribe":
            return _cmd_avito_webhook_subscribe(args)
        if args.command == "avito-webhook-unsubscribe":
            return _cmd_avito_webhook_unsubscribe(args)
        if args.command == "avito-webhook-status":
            return _cmd_avito_webhook_status(args)
        if args.command == "chats-sync":
            return _cmd_chats_sync(args)
        if args.command == "calls-sync":
            return _cmd_calls_sync(args)
        if args.command == "clients-link":
            return _cmd_clients_link(args)
        if args.command == "orders-tick":
            return _cmd_orders_tick(args)
        if args.command == "sheet-enroll":
            return _cmd_sheet_enroll(args)
        if args.command == "vk-catchup":
            return _cmd_vk_catchup(args)
        if args.command == "webmim-orders-sync":
            return _cmd_webmim_orders_sync(args)
        if args.command == "old-model-import":
            return _cmd_old_model_import(args)
        if args.command == "backup":
            return _cmd_backup(args)
        if args.command == "backup-offsite":
            return _cmd_backup_offsite(args)
        if args.command == "backup-offsite-get":
            return _cmd_backup_offsite_get(args)
        if args.command == "backup-key-new":
            return _cmd_backup_key_new(args)
        if args.command in ("user-add", "user-passwd", "user-remove", "user-list"):
            return _cmd_users(args)
        return _cmd_mappings(args)
    except (SettingsNotInitialized, MappingError, ValueError, WebmimError, SecretKeyError, OSError, PublishError, AvitoError, FileNotFoundError, store.StateError, BackupError, users.UserError, offsite.OffsiteError) as err:
        print("error: %s" % err, file=sys.stderr)
        return 2
