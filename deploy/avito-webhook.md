# Вебхук мессенджера Авито: nginx и включение

Адрес приёма: `https://panel.example.com/hook/avito/<секрет>` (маршрут `avito_hook`, `panel/routes_avito_webhook.py`).
Открыт без входа (Авито не подписывает запросы), защищён секретом в адресе (свой на аккаунт, в базе только хэш).
Делает тимлид по слову владельца; сервер дорожка не трогала.

## 1. nginx (до подписки)

Секрет — в адресе, поэтому адрес не должен попадать в журналы nginx. По `deploy/README.md` («Приёмник ВК») на сервере уже есть блок
`location ^~ /hook/` (лимит `zone=share`, `access_log off`, `error_log /dev/null crit`). В `deploy/nginx-panel.conf` репозитория его нет — проверить на сервере:

```bash
sudo nginx -T 2>/dev/null | grep -n -A8 "location ^~ /hook/"
```

Если блока нет — добавить в `server` панели перед `location /` и перезагрузить nginx (`sudo nginx -t && sudo systemctl reload nginx`):

```nginx
    # Вебхуки ВК и Авито: без входа, секрет в адресе — не пишем адрес в журналы; тела маленькие.
    location ^~ /hook/ {
        limit_req zone=share burst=150 nodelay;
        limit_req_status 429;
        client_max_body_size 64k;
        access_log off;
        error_log /dev/null crit;
        proxy_pass http://127.0.0.1:8000;
        include /etc/nginx/snippets/tires-proxy.conf;
    }
```

Если блок есть — можно добавить в него `client_max_body_size 64k;` (приложение и так отвечает 413 на тело больше 64 КБ).

Проверка снаружи (Авито требует 200 за 2 секунды; неизвестный секрет — 404):

```bash
curl -s -o /dev/null -w "%{http_code} %{time_total}\n" --connect-timeout 2 -d '{}' https://panel.example.com/hook/avito/not-a-real-secret-000000
```

Ожидается `404` за доли секунды.

## 2. Подписка (запись в Авито — только по слову владельца)

На сервере, от имени службы, после копии базы:

```bash
sudo -u tires env PYTHONPATH=/opt/tires-sync/app/src TIRES_SECRET_KEY_FILE=/etc/tires-sync/secret.key /opt/tires-sync/venv/bin/python -m tires_sync avito-webhook-status --db /var/lib/tires-sync/state.db --base-url https://panel.example.com
sudo -u tires env PYTHONPATH=/opt/tires-sync/app/src TIRES_SECRET_KEY_FILE=/etc/tires-sync/secret.key /opt/tires-sync/venv/bin/python -m tires_sync avito-webhook-subscribe --db /var/lib/tires-sync/state.db --base-url https://panel.example.com --account 1
sudo -u tires env PYTHONPATH=/opt/tires-sync/app/src TIRES_SECRET_KEY_FILE=/etc/tires-sync/secret.key /opt/tires-sync/venv/bin/python -m tires_sync avito-webhook-subscribe --db /var/lib/tires-sync/state.db --base-url https://panel.example.com --account 1 --confirm
```

(Пользователь, `PYTHONPATH` и ключ — как в `deploy/tires-chats.service`.)

## 3. Откат

```bash
sudo -u tires env PYTHONPATH=/opt/tires-sync/app/src TIRES_SECRET_KEY_FILE=/etc/tires-sync/secret.key /opt/tires-sync/venv/bin/python -m tires_sync avito-webhook-unsubscribe --db /var/lib/tires-sync/state.db --base-url https://panel.example.com --account 1 --confirm
```

Снимает только наши адреса `…/hook/avito/…` у кабинета и забывает секрет. Опрос `tires-chats` раз в минуту работает всё время и без вебхука.
