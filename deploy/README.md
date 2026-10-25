# Развёртывание панели

Файлы этой папки установлены на сервере вручную (см. `docs/superpowers/plans`): юнит systemd, конфигурация nginx, заглушка для неизвестных хостов.

## Мастер-ключ и зависимости (один раз на сервере)

1. Установить зависимость: `/opt/tires-sync/venv/bin/pip install cryptography "Pillow>=10"` (Pillow нужен для вида фото аккаунта; без него отдаётся картинка поставщика)
2. Создать ключ (файл не перезаписывается, если уже есть):
   `PYTHONPATH=/opt/tires-sync/app/src /opt/tires-sync/venv/bin/python -c "from tires_sync.secretbox import create_key_file; create_key_file('/etc/tires-sync/secret.key')"`
3. Выдать права: `chown root:tires /etc/tires-sync/secret.key && chmod 640 /etc/tires-sync/secret.key`
4. Добавить в `/etc/tires-sync/panel.env` строку `TIRES_SECRET_KEY_FILE=/etc/tires-sync/secret.key`
5. Перезапустить: `systemctl restart tires-panel`
6. Сохранить копию `secret.key` отдельно от базы (потеря ключа означает повторный ввод секретов).

## Скачать выгрузку вручную по сохранённому подключению

`sudo -u tires env TIRES_SECRET_KEY_FILE=/etc/tires-sync/secret.key PYTHONPATH=/opt/tires-sync/app/src /opt/tires-sync/venv/bin/python -m tires_sync fetch-unload --db /var/lib/tires-sync/state.db --out /var/lib/tires-sync/unload.xml`

Подключение (адрес, режим, логин, пароль или API-ключ) задаётся на странице «Подключения» в панели. Файл заменяется только если выгрузка корректна и не уменьшилась более чем вдвое; рядом пишется `unload.xml.meta.json`.

## Цикл по таймеру (каждый час)

Файлы `tires-cycle.service` и `tires-cycle.timer` копируются в `/etc/systemd/system/`, затем `systemctl daemon-reload`. Таймер включать только после первого успешного ручного запуска:

1. Ручной запуск (без скачивания, на уже лежащей выгрузке):
   `sudo -u tires env TIRES_SECRET_KEY_FILE=/etc/tires-sync/secret.key PYTHONPATH=/opt/tires-sync/app/src /opt/tires-sync/venv/bin/python -m tires_sync run-cycle --db /var/lib/tires-sync/state.db --unload /var/lib/tires-sync/unload.xml --reference /var/lib/tires-sync/reference.txt --feed-root /var/lib/tires-sync/feed`
2. Включить таймер: `systemctl enable --now tires-cycle.timer`; проверить: `systemctl list-timers tires-cycle.timer`, журнал `journalctl -u tires-cycle`.

Цикл ничего не отправляет на Авито: он скачивает выгрузку, считает решения и пишет черновик фида каждого аккаунта в `/var/lib/tires-sync/feed/<токен>/next/avito_feed.xml`. Публичным (его читает Авито) черновик становится в том же запуске таймера (`run-cycle --publish`), в панели («Применить и опубликовать сейчас») или командами `feed-publish` / `avito-publish --confirm`, и только если публикация аккаунта включена на странице «Подключения» (флаг live). Сборка и публикация идут под общей блокировкой `feed/.lock`: таймер и панель не пересекаются. Если фид сократился более чем вдвое, публикация откажется без `--allow-shrink`. В `panel.env` нужна строка `TIRES_FEED_ROOT=/var/lib/tires-sync/feed` (для счётчика на странице включения). Публичная ссылка: `https://panel.example.com/feed/<токен>/avito_feed.xml` (токен виден на странице «Подключения»). Интервал меняется в `OnCalendar` таймера. Таймер срабатывает **каждый час в :40** (решение владельца 2026-10-20, до этого раз в 6 часов) и дополнительно в **06:45** (последняя сверка до чтения фида Авито в 07:00; позиции, пропавшие у поставщика, снимаются сразу, ожидание утра выключено: `decide.WAIT_FOR_MORNING_REFILL = False`; проверки каталога в 05:30 и 06:30 показывают реальное время наполнения).

## Публикация на Авито (вручную, по явному подтверждению)

Показать план (только чтение, ничего не меняется):
`sudo -u tires env TIRES_SECRET_KEY_FILE=/etc/tires-sync/secret.key PYTHONPATH=/opt/tires-sync/app/src /opt/tires-sync/venv/bin/python -m tires_sync avito-publish --db /var/lib/tires-sync/state.db --account 1 --public-base https://panel.example.com --feed-root /var/lib/tires-sync/feed`

Указать ссылку на фид в профиле автозагрузки и запустить загрузку (запись на Авито): добавить `--confirm`. Загрузку можно запускать не чаще раза в час; остальные настройки профиля (почта, расписание, другие фиды) сохраняются, поле соглашения не отправляется.

Состояние последней загрузки и объявлений (только чтение):
`... -m tires_sync avito-status --db /var/lib/tires-sync/state.db --account 1`

## Резервные копии базы

Службы `tires-backup.service` и `tires-backup.timer` делают раз в сутки (03:30) сжатую копию `state.db` в `/var/backups/tires-sync` (хранятся 14 последних, права 600). Каталог создаётся один раз: `install -d -o tires -g tires -m 700 /var/backups/tires-sync`. Эта копия защищает от порчи данных, но не от потери сервера: периодически забирайте её к себе: `scp tires-server:/var/backups/tires-sync/state-<дата>.db.gz .`. Мастер-ключ `/etc/tires-sync/secret.key` храните отдельно от копии базы (без него секреты из копии не расшифровать).

## Резервные копии и ключ
- Раз в сутки `tires-backup.timer` делает копию базы в `/var/backups/tires-sync` (14 последних) и сразу проверяет её: целостность и наличие всех таблиц. Если копия повреждена, команда падает, служба сообщает в Telegram (`OnFailure`).
- Копии лежат на том же сервере: при потере сервера пропадут вместе с ним. Раз в неделю забирайте копию к себе: `scp tires-server:/var/backups/tires-sync/state-$(date +%F).db.gz ~/Backups/tires/` (подойдёт любой скрипт или планировщик на вашем компьютере).
- Секреты в базе зашифрованы мастер-ключом `/etc/tires-sync/secret.key`. Без него копия базы бесполезна для ключей и паролей. Сохраните ключ ОДИН раз отдельно от копий базы (менеджер паролей): `ssh tires-server cat /etc/tires-sync/secret.key`. Не храните его рядом с копиями.
- Восстановление: раздел «Восстановление базы из копии» ниже.
- Служба `tires-alert@.service` отправляет сообщение в Telegram, когда падает цикл или бэкап (нужны токен бота и chat_id на вкладке «Подключения → Telegram»).


## Восстановление базы из копии
Для копии перед изменением (`/var/lib/tires-sync/pre-<что>-<дата>.db`) и для ночной (`/var/backups/tires-sync/state-<дата>.db.gz`). База может быть в режиме WAL (`PRAGMA journal_mode` отвечает `wal`): тогда рядом с ней лежат `state.db-wal` и `state.db-shm`, и в `state.db-wal` бывают подтверждённые записи, которых ещё нет в самом файле. Если подменить `state.db`, а старые `-wal`/`-shm` оставить, sqlite применит журнал старой базы к восстановленной и испортит её. Поэтому: всё остановить, журналы старой базы удалить, копию переносить через sqlite (`.backup`), а не `cp`. Все команды sqlite от `tires`: файлы, которые создаст root, панель потом не сможет писать.
1. Автовыкладку выключить, если она включена, и запомнить это (старая метка от брошенной процедуры сначала удаляется): `sudo rm -f /root/tires-deploy-was-on; sudo test -e /etc/tires-deploy/enabled && sudo mv /etc/tires-deploy/enabled /etc/tires-deploy/enabled.off && echo вернуть | sudo tee /root/tires-deploy-was-on`. Если файла `enabled` нет, автовыкладка уже выключена и возвращать её не нужно. Проверить, что выкладка не идёт прямо сейчас: `sudo flock -n /run/tires-deploy.lock true && echo свободно`. Если не «свободно», дождаться конца выкладки (GitHub → Actions → deploy).
2. Запомнить работающие таймеры, остановить их и дождаться, пока запущенные службы закончат свою работу. Службы не убивать посреди дела: чаты или заказы могли уже что-то отправить наружу. Цикл может идти до часа.
   `systemctl list-units 'tires-*.timer' --state=active --no-legend --plain | awk '{print $1}' | sudo tee /root/tires-timers-before`
   `sudo systemctl stop 'tires-*.timer'`
   `systemctl list-units 'tires-*.service' --state=active,activating --no-legend` — повторять, пока не останется одна `tires-panel.service`.
   Затем `sudo systemctl stop tires-panel` и ещё раз проверить: список пустой.
3. Копия текущей базы (чтобы вернуть, если восстановили не ту): `sudo -u tires sqlite3 /var/lib/tires-sync/state.db ".backup /var/lib/tires-sync/pre-restore-$(date +%Y%m%d-%H%M%S).db"`
4. Восстанавливаемую копию во временный файл рядом с базой:
   - `pre-…db`: `sudo -u tires sqlite3 /var/lib/tires-sync/pre-<что>-<дата>.db ".backup /var/lib/tires-sync/state.db.new"`
   - `.db.gz`: `sudo -u tires sh -c 'gunzip -c /var/backups/tires-sync/state-ГГГГ-ММ-ДД.db.gz > /var/lib/tires-sync/state.db.new'`
5. Проверить её: `sudo -u tires sqlite3 /var/lib/tires-sync/state.db.new 'PRAGMA integrity_check;'` → `ok`.
6. Убрать журналы старой базы и подменить файл: `sudo rm -f /var/lib/tires-sync/state.db-wal /var/lib/tires-sync/state.db-shm /var/lib/tires-sync/state.db-journal && sudo chmod 640 /var/lib/tires-sync/state.db.new && sudo mv /var/lib/tires-sync/state.db.new /var/lib/tires-sync/state.db`
7. `ls -l /var/lib/tires-sync/state.db*`: только `state.db`, владелец `tires:tires`.
8. Запустить панель и ровно те таймеры, что работали до остановки: `sudo systemctl start tires-panel $(sudo cat /root/tires-timers-before)`. Автовыкладку вернуть, только если она была включена: `sudo test -e /root/tires-deploy-was-on && sudo mv /etc/tires-deploy/enabled.off /etc/tires-deploy/enabled && sudo rm /root/tires-deploy-was-on`.
9. Проверить: панель открывается, `journalctl -u tires-chats -n 20` без `database is locked`/`readonly`. Режим журнала: `sudo -u tires sqlite3 /var/lib/tires-sync/state.db 'PRAGMA journal_mode;'`. Копия передаёт режим своей базы: если восстановили копию, снятую до включения WAL, ответ будет `delete`, и WAL нужно включить заново (`docs/runs/2026-10-22-tails2/report-wal.md`, «Включение WAL на сервере», шаги 2–6).

Всё, что пришло после копии (заказы, чаты, продажи), в восстановленной базе отсутствует.

## Тревоги
Тревога приходит при сбое цикла или скачивания, при срабатывании защиты от массового снятия (объявления удерживаются со старыми ценами, цикл завершается кодом 3), при удержании объявлений из-за скачка цены у поставщика и при недоступности дополнительного поставщика. Одинаковый текст не повторяется чаще раза в 10 минут; собственная тревога службы (`tires-alert@`) не дублирует только что отправленную.

## Пользователи панели
Общий пароль владельца задаётся в `panel.env` (`PANEL_PASSWORD_HASH`, вход с пустым логином или логином `owner`). Дополнительных пользователей добавляют на сервере (пароль вводится скрытно, имена `owner`, `server`, `system` заняты):

`sudo -u tires env PYTHONPATH=/opt/tires-sync/app/src /opt/tires-sync/venv/bin/python -m tires_sync user-add --db /var/lib/tires-sync/state.db --login anna`

Аналогично `user-passwd` (смена пароля закрывает сессии пользователя), `user-remove`, `user-list`. Хеши паролей лежат в `state.db`, поэтому копии бэкапов храните так же бережно, как саму базу.

В `panel.env` должна быть строка `PANEL_TRUST_PROXY=1`, иначе ограничение попыток входа считает всех с адресом 127.0.0.1 и пять неверных паролей от кого угодно закроют вход всем на 15 минут.

## Проверки полноты каталога (ночью)
`tires-probe.service` и `tires-probe.timer` (00:40, 01:30, 03:00, 04:30 по времени сервера) скачивают выгрузку во временный файл `/var/lib/tires-sync/probe-unload.xml`, записывают в таблицу `catalog_probe`, сколько наших опубликованных позиций сейчас в ней, и удаляют файл. Ничего не публикуют и не меняют. Установка: скопировать оба файла в `/etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable --now tires-probe.timer`. Результат: «История» → «Каталог по часам». Обычный цикл тоже пишет такую строку.

`tires-orders.service` и `tires-orders.timer` (каждый час в :07) запускают `orders-tick`: для правил «завис на этапе» создают задачи в карточках заказов, а с 2026-10-22 ещё читают заказы WebMIM за 60 дней (только чтение, `webmim-orders-sync`; сервер — тот, что выбран в «Заказы в WebMIM») для сверки с заказами панели. В WebMIM ничего не создают и не меняют, клиентам ничего не отправляют. Правила в режиме «сразу» могут сами поставить тег, перенести заказ или записать продажу, остальное остаётся задачами для вас. Установка: скопировать оба файла в `/etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable --now tires-orders.timer`.

`tires-calls.service` и `tires-calls.timer` (каждые 2 минуты) запускают `calls-sync`: читают у Авито звонки через подменные номера всех аккаунтов с ключами (только чтение; у аккаунта без коллтрекинга — пропуск без тревоги), записывают звонок, точку по аккаунту и номер клиента в базу клиентов, скачивают записи разговоров в `/var/lib/tires-sync/calls/<аккаунт>/<звонок>.mp3` (0640, nginx их не раздаёт, слушают в панели по праву) и через 7 дней от звонка удаляют их (решение владельца 2026-10-21). Автовыкладка новые таймеры не ставит: установка по слову владельца — сначала копия базы `pre-calls-<дата>.db`, затем проверка `sudo -u tires … calls-sync --db /var/lib/tires-sync/state.db --dry-run` (ничего не пишет), скопировать оба файла в `/etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable --now tires-calls.timer`. Откат: `systemctl disable --now tires-calls.timer`.

`tires-chats.service` и `tires-chats.timer` (каждую минуту: пять запусков подряд лёгкие, читают только новейшие чаты, каждый пятый читает весь список; запуски не пересекаются, блокировка `.chats.lock`) запускают `chats-sync`: читают чаты Авито у аккаунтов с включённым режимом «читать чаты» (только запросы чтения), обновляют телефон и заметку «написал снова» у сделок, созданных менеджером (сами сделки с 2026-10-15 не создают), чистят сообщения старше 180 дней. Ничего не отправляют клиентам. Установка: скопировать оба файла в `/etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable --now tires-chats.timer`. Режимы аккаунта: «Подключения → Авито → аккаунт → Для чего используется аккаунт».

## Ссылки для клиентов (публичная страница без входа)
- Маршруты `/s/<токен>` и `/s/<токен>/p/<файл>` открыты без входа; всё остальное в панели за паролем. Токен 192 бита, в базе только его хеш; ссылка действует 7 дней, отзывается в разделе «Ссылки для клиентов».
- Фото клиентских ссылок лежат в `/var/lib/tires-sync/share/<id>/` (или `TIRES_SHARE_ROOT` в `panel.env`); каталог создаётся сам, удаляется вместе с ссылкой через сутки после истечения или отзыва.
- nginx: зона `share` (60 запросов в минуту с адреса, всплеск 150 под страницу с 60 фото, ответ 429) в `tires-limits.conf` и блок `location ^~ /s/` с `access_log off` в `nginx-panel.conf`. Установка: боевой файл `sites-enabled/tires-panel` правил certbot (HTTPS), поэтому файл из репозитория целиком не копировать: добавить в него только блок `location ^~ /s/`, а в `tires-limits.conf` строку зоны, затем `nginx -t` и `systemctl reload nginx` (так сделано 2026-10-12, копии прежних файлов в `/root/nginx-backup-*`).
- Токен не пишется ни в журнал nginx (`access_log off`, `error_log /dev/null` в блоке `/s/`), ни в журнал gunicorn: служба запускается с `--logger-class tires_sync.panel.gunicorn_logger.Logger`, он заменяет токен на `***`.
- Запрос поиска (`?q=` с телефонами и именами) не пишется в журналы. gunicorn: тот же `gunicorn_logger.Logger` заменяет значение `q` на `***` (в адресе, в referer, в атоме строки запроса). nginx: `map $http_referer $panel_referer` и `log_format panel` (`$uri` вместо `$request`, referer до `?`) в начале `nginx-panel.conf`. Установка: копия боевого `sites-enabled/tires-panel`, добавить `map` и `log_format` перед первым `server`, строку `access_log /var/log/nginx/access.log panel;` в **оба** блока: серверный с `listen 443` (его правил certbot) и блок-редирект с `listen 80`; файл целиком не копировать; `nginx -T | rg access_log` (на уровне `http` не должно быть другого файла журнала), `nginx -t`, `systemctl reload nginx`. Проверка: открыть в браузере `/orders?q=test`, затем `tail -5 /var/log/nginx/access.log` — нет `q=` ни в адресе, ни в referer. Остаётся: `error.log` nginx при 502 и тайм-ауте (например, во время перезапуска панели) и при срабатывании `limit_req` пишет `request: "GET /orders?q=…"` и `referrer`; уровень не меняем, ошибки нужны.

## Агент фото: загрузки в панель (условие выкладки)
- Агент фото присылает PNG 2–4 МБ на `/api/image-jobs/<id>/result`, `/api/photo-copies`, `/api/review-sheet`. Общий `client_max_body_size 1m` в `nginx-panel.conf` отвечает на них 413 ещё до Flask, поэтому в файле блок `location ~ ^/api/(image-jobs|photo-copies|review-sheet)(/|$)` с `client_max_body_size 32m` (проксирует как `location /`).
- Установка: как с блоком `/s/`, боевой `sites-enabled/tires-panel` целиком не заменять (его правил certbot): добавить в серверный блок с `listen 443` только этот `location`, сделать копию прежнего файла, `nginx -t`, `systemctl reload nginx`. Без блока агент не загрузит ни одного фото.
- Flask на сервере должен быть не ниже 3.1 (`request.max_content_length` задаётся в маршрутах агента): проверить `/opt/tires-sync/venv/bin/python -c "import importlib.metadata as m; print(m.version('flask'))"` до выкладки.

## Агент фото на сервере (генерация из панели, решение владельца 2026-10-23)
Кнопки «Сгенерировать» в «Генерации» ставят запрос; таймер `tires-photo-agent` раз в минуту запускает `python -m tires_sync photo-agent-tick`: без запроса он сразу выходит, с запросом расшифровывает ключ OpenAI и прокси из «Подключения → OpenAI (фото)», выпускает одноразовый токен агента и передаёт всё это бинарнику `tire-photo-agent tick --stdin-secrets` через stdin (не в файлах, не в env, не в журнале); после запуска токен стирается. Запросы к `api.openai.com` идут через прокси (OpenAI не отвечает на запросы из России), в панель и за фото агент ходит напрямую по `public_base`.

Установка (один раз, root; `tires-deploy` службы и новые таймеры сам не ставит):
1. `uname -m` = `x86_64`; `apt install fonts-liberation` (шрифт карточек размеров: без него агент карточки не делает, а не делает их пустыми).
2. На Маке `agent/build-server.sh` (печатает sha256), затем `scp agent/target/x86_64-unknown-linux-musl/release/tire-photo-agent tires-server:/tmp/`; на сервере `install -D -o root -g root -m 0755 /tmp/tire-photo-agent /opt/tires-sync/agent/tire-photo-agent`, `sha256sum` сверить.
3. `install -d -o tires -g tires -m 0700 /var/lib/tires-sync/agent /var/lib/tires-sync/agent/pending /var/lib/tires-sync/agent/backgrounds`; фото точки выдачи (с Мака `~/.config/tires/backgrounds/*.jpg|png`) положить в `backgrounds`, владелец `tires` (без них общий фон, как раньше при пустой папке).
4. Связь с самой собой: `curl -sI https://panel.example.com/login` с сервера отвечает 200/302.
5. Прокси для OpenAI (например, локальный SOCKS5 xray на `127.0.0.1:1080`) проверить: `curl -s -o /dev/null -w '%{http_code}' --socks5-hostname 127.0.0.1:1080 https://api.openai.com/v1/models` даёт 401 (без ключа), а не 403.
6. `cp deploy/tires-photo-agent.service deploy/tires-photo-agent.timer /etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable --now tires-photo-agent.timer`.
7. Владелец вводит ключ и прокси в «Подключения → OpenAI (фото)», жмёт «Сгенерировать» у одной модели; смотреть строку состояния в «Генерации» и `journalctl -u tires-photo-agent -n 50`.

Откат: `systemctl disable --now tires-photo-agent.timer` (без таймера служба не стартует), удалить `/opt/tires-sync/agent/tire-photo-agent`; ключ — «Удалить ключ» в «Подключения → OpenAI (фото)». Одобренные кадры и фид не затрагиваются. Оплаченные, но не отданные в панель кадры лежат в `/var/lib/tires-sync/agent/pending` и отдаются при следующем запуске.

## Приёмник ВК (Callback API)
Адрес `/hook/vk/<секретный путь>` открыт без входа (ВК не может передать наш CSRF-токен), защищён секретным путём, номером сообщества и секретным ключом ВК. В nginx для него отдельный блок `location ^~ /hook/` с лимитом `zone=share`, `access_log off` и `error_log /dev/null crit` (путь секретный); в журнале gunicorn путь маскируется (`--logger-class …gunicorn_logger.Logger`).

## Вывод аккаунта-зеркала в эфир (разовая задача на 2026-10-14)

`mirror-go-live --account N --confirm` пересобирает фид зеркала, сверяет, что в нём каждый сохранённый Id, включает публикацию, публикует фид у нас, добавляет ссылку в автозагрузку Авито и запускает загрузку (не чаще раза в час). Без `--confirm` только собирает, сверяет и печатает план. Разовые таймеры `tires-mirror-golive.timer` (06:47) и `tires-mirror-check.timer` (07:30, чтение отчёта Авито в журнал) установлены для аккаунта №2 на 2026-10-14; после срабатывания их можно убрать: `systemctl disable --now tires-mirror-golive.timer tires-mirror-check.timer` и удалить файлы из `/etc/systemd/system/`.
