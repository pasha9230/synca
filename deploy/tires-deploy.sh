#!/usr/bin/env bash
# Выкладка Tires Sync на сервере: тот же порядок, что в CLAUDE.md («Выкладка») и docs/deploy-prompt.md.
# Ставит владелец вручную, от root (обновление скрипта тоже только вручную, не из workflow):
#   install -o root -g root -m 0755 deploy/tires-deploy.sh /usr/local/sbin/tires-deploy
# Раннер GitHub (пользователь tires-deploy) вызывает его через sudo (deploy/sudoers-tires-deploy).
# Код берётся не из папки раннера, а из своей копии репозитория /opt/tires-sync/repo (ключ только на чтение),
# и только последний коммит master. RED выкладывается так же, без подтверждения (решение владельца 2026-10-19):
# ворота это тесты и ревью до слияния в master; скрипт только пишет в журнал, какие RED-файлы затронуты.
#
#   tires-deploy deploy <sha40>                выложить последний коммит master
#   tires-deploy rollback                      вернуть код предыдущей выкладки; база не трогается
#   tires-deploy status                        что выложено, выпуски, копии базы
#   tires-deploy timers                        поставить таймеры из deploy/ выложенного коммита (то же делают deploy и rollback)
#
# Код лежит в /opt/tires-sync/releases/<время>-<sha7>/{src,docs}; /opt/tires-sync/app/src и app/docs это
# ссылки, их замена атомарна (tires-chats и tires-orders не увидят смесь старых и новых файлов).
#
# Коды выхода: 0 чисто; 1 выкладка не начата (нет копии базы, не последний master, не дождался тишины, прочие
# ошибки до замены кода, включая ненулевые коды git); 2 панель не прошла проверку, код возвращён; 3 возврат не помог, нужен человек;
# 4 код выложен, feed-check нашёл ошибки (отката нет, решение за владельцем; если и таймер не обновлён, это в том же сообщении);
# 5 код выложен, таймер не обновлён (прежний оставлен).
set -euo pipefail
shopt -s inherit_errexit   # set -e и внутри $(...): ошибка git в classify не превращается в «обычную»
trap '' PIPE               # раннер отменил задачу и закрыл вывод: скрипт не умирает посреди переключения
umask 022
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C.UTF-8

BASE=/opt/tires-sync
REPO=$BASE/repo
APP=$BASE/app
RELEASES=$BASE/releases
STATE=$BASE/deploy-state
ENABLED_FLAG=/etc/tires-deploy/enabled   # выключатель на сервере: без него раннер ничего не выкладывает и не откатывает
RUNNER_USER=tires-deploy
DATA=/var/lib/tires-sync
DB=$DATA/state.db
PY=$BASE/venv/bin/python
PANEL_URL=http://127.0.0.1:8000
LOCK=/run/tires-deploy.lock
LOGFILE=/var/log/tires-deploy.log
CYCLE_GAP_MIN=5      # ближе к циклу не выкладывать
WAIT_MAX_MIN=75      # сколько ждать тишины (цикл, заказы), потом отказ
NEXT_UNKNOWN_MAX_MIN=5   # сколько подряд ждать, пока таймер цикла не покажет время следующего запуска, потом отказ
KEEP_RELEASES=5
KEEP_BACKUPS=10      # и не моложе BACKUP_MIN_DAYS дней
BACKUP_MIN_DAYS=14
UNITS=/etc/systemd/system
TIMERS_BACKUP=$STATE/timers

log() {
  local line
  line="$(date '+%F %T') $*"
  printf '%s\n' "$line" >> "$LOGFILE" 2>/dev/null || true
  printf '%s\n' "$line" 2>/dev/null || true
}
die() { local code=$1; shift; log "СТОП: $*" >&2 || true; exit "$code"; }

lock() {
  exec 9>"$LOCK"
  flock -w 600 9 || die 1 "идёт другая выкладка или откат"
}

fetch_master() {
  git -C "$REPO" fetch --quiet origin master
  MASTER=$(git -C "$REPO" rev-parse origin/master)
}

deployed_sha() { cat "$STATE/deployed-sha" 2>/dev/null || true; }

# Только для журнала. RED, если: на сервере нет записи о выложенном коммите; меняются deploy/ или .github/; меняется файл, которого
# нет в deploy/safe-paths.txt выложенного (не нового) коммита; в сообщении коммита есть [RED].
# При любой ошибке git скрипт падает (set -e на присваиваниях), а не считает выкладку обычной.
classify() {
  local new=$1 deployed f p log_text diff_text safe_text safe
  local -a reasons=() changed=() patterns=()
  deployed=$(deployed_sha)
  if [ -z "$deployed" ] || ! git -C "$REPO" cat-file -e "$deployed^{commit}" 2>/dev/null; then
    reasons+=("на сервере неизвестный коммит (${deployed:-нет записи}): первая выкладка")
  elif ! git -C "$REPO" cat-file -e "$deployed:deploy/safe-paths.txt" 2>/dev/null; then
    reasons+=("в выложенном коммите нет deploy/safe-paths.txt")
  else
    diff_text=$(git -C "$REPO" -c core.quotePath=false diff --no-renames --name-only "$deployed" "$new")
    safe_text=$(git -C "$REPO" show "$deployed:deploy/safe-paths.txt")
    [ -n "$diff_text" ] && mapfile -t changed <<<"$diff_text"
    mapfile -t patterns < <(grep -vE '^[[:space:]]*(#|$)' <<<"$safe_text" || true)
    for f in "${changed[@]}"; do
      case "$f" in deploy/*|.github/*) reasons+=("$f (выкладка и проверки)"); continue ;; esac
      safe=0
      for p in "${patterns[@]}"; do
        # shellcheck disable=SC2053  # шаблон намеренно без кавычек
        if [[ "$f" == $p ]]; then safe=1; break; fi
      done
      [ "$safe" = 1 ] || reasons+=("$f")
    done
    log_text=$(git -C "$REPO" log --format='%s%n%b' "$deployed..$new")
    if grep -q '\[RED\]' <<<"$log_text"; then reasons+=("[RED] в сообщении коммита"); fi
  fi
  if [ "${#reasons[@]}" -gt 0 ]; then
    printf 'RED: %s\n' "${reasons[@]}" >&2
    echo "red=true"
  else
    echo "обычная выкладка: ${#changed[@]} файлов" >&2
    echo "red=false"
  fi
}

# Раннер (пользователь tires-deploy) работает только при включённом флаге на сервере; владелец от root может всегда.
require_enabled() {
  [ "${SUDO_USER:-root}" != "$RUNNER_USER" ] || [ -e "$ENABLED_FLAG" ] || die 1 "автовыкладка выключена на сервере ($ENABLED_FLAG)"
}

# Служба занята, пока она не в покое: у oneshot-служб (цикл, заказы) во время работы состояние activating.
busy() {
  case "$(systemctl show -p ActiveState --value "$1" 2>/dev/null || true)" in
    activating|active|deactivating|reloading) return 0 ;;
    *) return 1 ;;
  esac
}

next_cycle_epoch() {   # время следующего запуска таймера ($1, по умолчанию цикла) в секундах эпохи; 0, если неизвестно
  local v timer=${1:-tires-cycle.timer}
  v=$(systemctl show -p NextElapseUSecRealtime --value --timestamp=unix "$timer" 2>/dev/null || true)
  v=${v#@}
  if [[ "$v" =~ ^[0-9]+$ ]]; then echo "$v"; return; fi
  v=$(systemctl show -p NextElapseUSecRealtime --value "$timer" 2>/dev/null || true)
  if [[ "$v" =~ ^[0-9]+$ ]]; then echo $(( v / 1000000 )); return; fi
  # Старый systemd без --timestamp: «Mon 2026-10-19 21:40:00 MSK», время местное.
  if [[ "$v" =~ ([0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}) ]] \
      && v=$(date -d "${BASH_REMATCH[1]}" +%s 2>/dev/null); then echo "$v"; return; fi
  echo 0
}

# Ждать, пока не идут tires-cycle, tires-orders и tires-stock и до следующего цикла не меньше CYCLE_GAP_MIN минут.
# Пока идёт цикл, таймер не показывает время следующего: это ожидание, а не ошибка. Ничего не идёт, а времени нет
# NEXT_UNKNOWN_MAX_MIN минут подряд: отказ. До прохода остатков (:10/:25/:55) тоже не меньше CYCLE_GAP_MIN минут, чтобы
# он не начался посреди выкладки; его таймера нет или время непонятно (в прошлом дольше зазора) — не помеха.
wait_quiet() {
  local deadline=$(( $(date +%s) + WAIT_MAX_MIN * 60 )) next stock now unknown_since=
  while :; do
    now=$(date +%s)
    next=$(next_cycle_epoch)
    stock=$(next_cycle_epoch tires-stock.timer)
    if [ "$next" -gt 0 ]; then unknown_since=; fi
    if busy tires-cycle.service; then
      log "идёт tires-cycle, жду"; unknown_since=
    elif busy tires-orders.service; then
      log "идёт tires-orders, жду"; unknown_since=
    elif busy tires-stock.service; then
      log "идёт tires-stock, жду"; unknown_since=
    elif busy tires-mirror-golive.service || busy tires-mirror-check.service; then
      log "идёт задача зеркала, жду"; unknown_since=
    elif [ "$next" -le 0 ]; then
      unknown_since=${unknown_since:-$now}
      [ $(( now - unknown_since )) -lt $(( NEXT_UNKNOWN_MAX_MIN * 60 )) ] || die 1 "не удалось узнать время следующего цикла (systemctl show tires-cycle.timer: '$(systemctl show -p NextElapseUSecRealtime --value tires-cycle.timer 2>&1 | head -1)', $(systemctl is-active tires-cycle.timer 2>&1 | head -1)), выкладку не начинаю"
      log "время следующего цикла пока неизвестно, жду"
    elif [ $(( next - now )) -lt $(( CYCLE_GAP_MIN * 60 )) ]; then
      # Время в прошлом: таймер ждёт RandomizedDelaySec, цикл вот-вот начнётся.
      log "до цикла $(( (next - now) / 60 )) мин, жду его окончания"
    elif [ "$stock" -gt 0 ] && [ $(( stock - now )) -gt $(( -CYCLE_GAP_MIN * 60 )) ] && [ $(( stock - now )) -lt $(( CYCLE_GAP_MIN * 60 )) ]; then
      log "до проверки остатков $(( (stock - now) / 60 )) мин, жду"
    else
      return 0
    fi
    [ "$now" -lt "$deadline" ] || die 1 "не дождался тишины за $WAIT_MAX_MIN мин"
    sleep 60
  done
}

# Копия базы через .backup (tires-chats пишет в базу каждую минуту, отсюда таймаут и повторы).
# sqlite3 от tires, не от root: в режиме WAL первый открывший создаёт state.db-wal/-shm, и они должны принадлежать tires.
backup_db() {
  local tag=$1 out ok i keep_last
  out="$DATA/pre-deploy-$tag-$(date +%Y%m%d-%H%M%S).db"
  for i in 1 2 3; do
    ok=$( { runuser -u tires -- sqlite3 -cmd '.timeout 30000' "$DB" ".backup '$out'" \
            && runuser -u tires -- sqlite3 "$out" 'PRAGMA integrity_check;'; } 2>&1 || true)
    if [ -s "$out" ] && [ "$ok" = "ok" ]; then
      chown tires:tires "$out"; chmod 640 "$out"
      log "копия базы: $out"
      keep_last=$(cat "$STATE/last-backup" 2>/dev/null || true)
      echo "$out" > "$STATE/last-backup"
      # чистка своих копий: только сверх KEEP_BACKUPS последних и старше BACKUP_MIN_DAYS дней; прошлую последнюю не трогать
      ls -1t "$DATA"/pre-deploy-*.db 2>/dev/null | tail -n +$(( KEEP_BACKUPS + 1 )) | while read -r f; do
        [ "$f" = "$keep_last" ] && continue
        [ -n "$(find "$f" -maxdepth 0 -mtime +"$BACKUP_MIN_DAYS")" ] && rm -f -- "$f"
      done || true
      return 0
    fi
    log "копия базы не удалась (попытка $i): $ok"
    rm -f -- "$out"
    [ "$i" -lt 3 ] && sleep 60
  done
  die 1 "нет копии базы, выкладку не продолжаю"
}

# Первый запуск: app/src и app/docs ещё папки. Переносим их в выпуск и ставим ссылки.
adopt_layout() {
  local rel sha
  [ -L "$APP/src" ] && return 0
  trap '' TERM INT HUP   # между переносом папки и ссылкой не прерываться
  sha=$(deployed_sha)
  rel="$RELEASES/$(date +%Y%m%d-%H%M%S)-${sha:0:7}"
  [ -n "$sha" ] || rel="${rel}unknown"
  mkdir -p "$rel"
  cp -a "$APP/src" "$rel/src"
  if [ -d "$APP/docs" ] && [ ! -L "$APP/docs" ]; then cp -a "$APP/docs" "$rel/docs"; else mkdir -p "$rel/docs"; fi
  echo "$sha" > "$rel/sha"
  log "текущий код перенесён в выпуск $rel"
  mv -T "$APP/src" "$APP/src.pre-releases"
  ln -sfn "$rel/src" "$APP/src"
  if [ -d "$APP/docs" ] && [ ! -L "$APP/docs" ]; then mv -T "$APP/docs" "$APP/docs.pre-releases"; fi
  ln -sfn "$rel/docs" "$APP/docs"
  rm -rf -- "$APP/src.pre-releases" "$APP/docs.pre-releases"
  trap - TERM INT HUP
}

current_release() { dirname "$(readlink -f "$APP/src")"; }

switch_to() {   # атомарная замена ссылок на выпуск $1
  ln -sfn "$1/src" "$APP/src.new" && mv -T "$APP/src.new" "$APP/src"
  ln -sfn "$1/docs" "$APP/docs.new" && mv -T "$APP/docs.new" "$APP/docs"
}

prune_releases() {   # оставить KEEP_RELEASES последних; текущий и предыдущий не трогать
  local cur prev d
  cur=$(current_release); prev=$(cat "$STATE/previous-release" 2>/dev/null || true)
  ls -1d "$RELEASES"/*/ 2>/dev/null | sort | head -n -"$KEEP_RELEASES" | while read -r d; do
    d=${d%/}
    [ "$d" = "$cur" ] || [ "$d" = "$prev" ] || rm -rf -- "$d"
  done || true
}

# Проверка панели: служба active, страницы 200/302, в журнале с момента рестарта нет падений.
# Журнал запросов gunicorn тоже идёт в журнал службы, поэтому ищем не слово error, а признаки падения.
smoke() {
  local since=$1 code path i bad=0 errs
  systemctl is-active --quiet tires-panel || { log "tires-panel не active"; return 1; }
  for path in /login /; do
    code=000
    for i in $(seq 1 15); do
      code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$PANEL_URL$path" || true)
      case "$code" in 200|302) break ;; esac
      sleep 2
    done
    log "страница $path: $code"
    case "$code" in 200|302) ;; *) bad=1 ;; esac
  done
  # сначала в переменную: при закрытом выводе (отменённая задача) grep не должен «не найти» ошибку
  errs=$(journalctl -u tires-panel --since "@$since" --no-pager -q | grep -E 'Traceback|\[ERROR\]|\[CRITICAL\]|Worker failed|Exception in worker' || true)
  if [ -n "$errs" ]; then
    log "$errs"; log "в журнале панели ошибки"; bad=1
  fi
  return "$bad"
}

restart_and_check() {
  local since
  systemctl restart tires-panel
  since=$(date +%s)   # после рестарта: остановка старых воркеров в проверку не попадает
  sleep 3
  smoke "$since"
}

feed_check() {
  local rc=0 kind out
  for kind in --published ""; do
    log "feed-check ${kind:-(черновик)}"
    # вывод в переменную: при закрытом выводе (отменённая задача) feed-check не падает на записи
    out=$(runuser -u tires -- env PYTHONPATH="$APP/src" TIRES_SECRET_KEY_FILE=/etc/tires-sync/secret.key \
      "$PY" -m tires_sync feed-check --db "$DB" --unload "$DATA/unload.xml" \
      --reference "$DATA/reference.txt" --feed-root "$DATA/feed" $kind 2>&1) || rc=$?
    log "$out"
  done
  return "$rc"
}

# Таймеры (решение владельца 2026-10-20: выкладка сама ставит таймеры из репозитория). Только deploy/*.timer, которые уже
# установлены на сервере (новые не ставятся и не включаются; выключенный владельцем не включается); разовые таймеры зеркала
# (tires-mirror-*, дата в прошлом) не трогаются. Файл только из печатных ASCII-знаков (systemd считает концом строки и \r) и
# только из строк расписания, без обратной косой (продолжение строки): таймер не может запустить другую службу (Unit=) или что-то
# кроме своей. Прежний файл сохраняется в $TIMERS_BACKUP; не запустился: возвращается прежний. Функцию зовут через ||, там set -e
# не действует, поэтому каждый шаг проверяется явно. Код возврата 0, если всё на месте.
TIMER_KEYS='^(\[(Unit|Timer|Install)\]|Description=[^\\]*|OnCalendar=[-0-9A-Za-z*/,.: ~]+|Persistent=(true|false)|RandomizedDelaySec=[0-9]+[a-z]*|AccuracySec=[0-9]+[a-z]*|WantedBy=timers\.target|#[^\\]*|)$'
restore_timer() {
  local name=$1
  if install -o root -g root -m 0644 "$TIMERS_BACKUP/$name" "$UNITS/$name" && systemctl daemon-reload \
      && { [ "$2" != active ] || { systemctl restart "$name" && systemctl is-active --quiet "$name"; }; }; then
    log "таймер $name: возвращён прежний"
  else
    log "таймер $name: прежний вернуть не удалось, нужен человек (копия: $TIMERS_BACKUP/$name)"
  fi
}
sync_timers() {
  local sha=$1 tmp f name v old bad=0
  local -a changed=() was_active=()
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { log "таймеры: неизвестный коммит (${sha:-нет}), таймеры не сверены"; return 1; }
  tmp=$(mktemp -d)
  if ! git -C "$REPO" archive "$sha" deploy | tar -x -C "$tmp"; then
    rm -rf -- "$tmp"; log "таймеры: в $sha нет deploy/, таймеры не сверены"; return 1
  fi
  mkdir -p "$TIMERS_BACKUP"
  for f in "$tmp"/deploy/*.timer; do
    if [ ! -f "$f" ] || [ -L "$f" ]; then continue; fi
    name=$(basename "$f")
    case "$name" in tires-mirror-*) continue ;; esac
    if [ ! -e "$UNITS/$name" ]; then continue; fi                         # на сервере не установлен: не ставится
    if cmp -s "$f" "$UNITS/$name"; then continue; fi
    if LC_ALL=C grep -q $'[^\t -~]' "$f" || grep -vqE "$TIMER_KEYS" "$f"; then
      log "таймер $name: строки вне расписания или непечатные знаки, не ставлю"; bad=1; continue
    fi
    while IFS= read -r v || [ -n "$v" ]; do
      if ! systemd-analyze calendar "$v" >/dev/null 2>&1; then
        log "таймер $name: неверное расписание «$v», не ставлю"; bad=1; continue 2
      fi
    done < <(sed -n 's/^OnCalendar=//p' "$f")
    if ! cp -a "$UNITS/$name" "$TIMERS_BACKUP/$name"; then
      log "таймер $name: не сохранить прежний, не ставлю"; bad=1; continue
    fi
    if systemctl is-active --quiet "$name"; then was_active+=("$name"); fi
    if ! install -o root -g root -m 0644 "$f" "$UNITS/$name"; then
      log "таймер $name: не удалось записать"; restore_timer "$name" none; bad=1; continue
    fi
    changed+=("$name")
  done
  rm -rf -- "$tmp"
  [ "${#changed[@]}" -gt 0 ] || return "$bad"
  if ! systemctl daemon-reload; then
    log "таймеры: daemon-reload не прошёл, возвращаю прежние"
    for name in "${changed[@]}"; do install -o root -g root -m 0644 "$TIMERS_BACKUP/$name" "$UNITS/$name" || true; done
    systemctl daemon-reload || log "таймеры: daemon-reload снова не прошёл, нужен человек"
    return 1
  fi
  for name in "${changed[@]}"; do
    old=$(sed -n 's/^OnCalendar=//p' "$TIMERS_BACKUP/$name" | paste -sd ';')
    v=$(sed -n 's/^OnCalendar=//p' "$UNITS/$name" | paste -sd ';')
    if [[ " ${was_active[*]} " != *" $name "* ]]; then
      log "таймер $name: файл обновлён (было ${old:-нет}, стало $v); он не был запущен и не запускается"; continue
    fi
    if systemctl restart "$name" && systemctl is-active --quiet "$name"; then
      log "таймер $name обновлён: было ${old:-нет}, стало $v (Persistent: если по новому расписанию срок уже прошёл, служба запустится сразу)"
    else
      log "таймер $name не запустился"; restore_timer "$name" active; bad=1
    fi
  done
  return "$bad"
}

cmd_deploy() {
  local sha=${1:-} prev_rel rel red feed_rc timers_rc docs
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || die 1 "нужен полный хеш коммита (40 знаков)"
  require_enabled
  lock
  fetch_master
  [ "$sha" = "$MASTER" ] || die 1 "коммит $sha не последний в master ($MASTER); старый код возвращается только через rollback"
  [ "$(deployed_sha)" = "$sha" ] && { log "коммит $sha уже выложен"; exit 0; }
  red=$(classify "$sha" 2>&1)
  log "$red"
  wait_quiet
  backup_db "${sha:0:7}"
  adopt_layout
  rel="$RELEASES/$(date +%Y%m%d-%H%M%S)-${sha:0:7}"
  mkdir -p "$rel"
  docs=$(git -C "$REPO" ls-tree --name-only "$sha" docs)   # docs/ только если она есть в коммите: после открытия кода документы в приватном репозитории
  git -C "$REPO" archive "$sha" src $docs | tar -x -C "$rel"
  find "$rel" -name __pycache__ -prune -exec rm -rf -- {} +
  [ -f "$rel/src/tires_sync/__init__.py" ] || die 1 "в коммите нет src/tires_sync"
  mkdir -p "$rel/docs"
  echo "$sha" > "$rel/sha"
  chown -R root:root "$rel"
  prev_rel=$(current_release)
  log "выкладываю $sha (было: $(deployed_sha), выпуск $prev_rel)"
  # от переключения до записи учёта не прерываться: отмена задачи здесь оставила бы ссылки и учёт врозь
  trap '' TERM INT HUP
  switch_to "$rel"
  if ! restart_and_check; then
    log "проверка панели не прошла, возвращаю $prev_rel"
    switch_to "$prev_rel"
    restart_and_check || die 3 "возврат не помог, панель не в порядке"
    die 2 "выкладка $sha не прошла, панель на прежнем коде"
  fi
  echo "$prev_rel" > "$STATE/previous-release"
  echo "$sha" > "$STATE/deployed-sha"
  trap - TERM INT HUP
  prune_releases
  log "панель в порядке, выложено $sha"
  feed_rc=0 timers_rc=0
  feed_check || feed_rc=$?
  sync_timers "$sha" || timers_rc=$?           # после feed-check: новый таймер может сразу запустить цикл
  [ "$feed_rc" = 0 ] || die 4 "feed-check нашёл ошибки; код не откатываю, решение за владельцем$( [ "$timers_rc" = 0 ] || echo "; и таймер не обновлён")"
  [ "$timers_rc" = 0 ] || die 5 "код выложен, но таймер не обновлён (см. журнал выше), действует прежний"
  log "готово"
}

cmd_rollback() {
  local prev cur
  require_enabled
  lock
  prev=$(cat "$STATE/previous-release" 2>/dev/null || true)
  [ -n "$prev" ] && [ -d "$prev/src" ] || die 1 "нет предыдущего выпуска (откат уже сделан или выкладок ещё не было)"
  cur=$(current_release)
  if ! cmp -s "$prev/src/tires_sync/store.py" "$cur/src/tires_sync/store.py"; then
    die 1 "между выпусками менялся store.py (схема или открытие базы): откат кнопкой не делаю, нужен человек: git revert и обычная выкладка, а если менялась схема, ещё восстановление базы (deploy/README.md, «Восстановление базы из копии»)"
  fi
  wait_quiet
  backup_db "rollback"
  log "возвращаю выпуск $prev (коммит $(cat "$prev/sha" 2>/dev/null || true))"
  trap '' TERM INT HUP   # от переключения до записи учёта не прерываться
  switch_to "$prev"
  if ! restart_and_check; then
    switch_to "$cur"; restart_and_check || true
    die 3 "после отката панель не в порядке, вернул как было; нужен человек"
  fi
  cat "$prev/sha" > "$STATE/deployed-sha" 2>/dev/null || : > "$STATE/deployed-sha"
  : > "$STATE/previous-release"   # второй откат подряд не прыгает обратно на плохой выпуск
  trap - TERM INT HUP
  sync_timers "$(cat "$prev/sha" 2>/dev/null || true)" || log "ВНИМАНИЕ: таймеры прежнего выпуска не вернулись, проверьте: systemctl list-timers 'tires-*'"
  log "откат выполнен; база не менялась. В master плохой коммит остался: нужен git revert, иначе следующая выкладка вернёт его"
}

cmd_timers() {
  require_enabled
  lock
  sync_timers "$(deployed_sha)" || die 5 "таймер не обновлён (см. журнал выше), действует прежний"
  log "таймеры сверены с выложенным коммитом"
}

cmd_status() {
  echo "выложено:        $(deployed_sha)"
  echo "выпуск сейчас:   $( if [ -L "$APP/src" ]; then current_release; else echo 'ещё не переведено на выпуски'; fi )"
  echo "для отката:      $(cat "$STATE/previous-release" 2>/dev/null || true)"
  echo "последняя копия: $(cat "$STATE/last-backup" 2>/dev/null || true)"
  echo "выпуски:"; ls -1 "$RELEASES" 2>/dev/null | sort | sed 's/^/  /' || true
  systemctl list-timers 'tires-cycle*' --no-pager | head -3 || true
}

mkdir -p "$RELEASES" "$STATE"
chmod 755 "$STATE"
case "${1:-}" in
  deploy)   cmd_deploy "${2:-}" ;;
  rollback) cmd_rollback ;;
  status)   cmd_status ;;
  timers)   cmd_timers ;;
  *) echo "использование: tires-deploy deploy <sha40> | rollback | status | timers" >&2; exit 1 ;;
esac
