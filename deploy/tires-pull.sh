#!/usr/bin/env bash
# Выкладка опросом, без раннера GitHub на сервере (этап 3 docs/plans/2026-10-24-open-repo.md).
# Запускает таймер tires-pull.timer раз в 2 минуты от пользователя tires-pull (без доступа к базе и ключам).
# Ставит владелец вручную, от root (обновление тоже только вручную, выкладка его не ставит):
#   install -o root -g root -m 0755 deploy/tires-pull.sh /usr/local/sbin/tires-pull
# Шаги установки: deploy/AUTODEPLOY.md, раздел «Выкладка опросом (tires-pull)».
#
# Что делает за прогон (одна строка в журнал, journalctl -u tires-pull):
#   1. читает у GitHub последний коммит master (только чтение; токен на чтение из $CREDENTIALS_DIRECTORY,
#      без файла запросы идут без токена);
#   2. если он не выложен, проверка тестов (по умолчанию «tests / pytest» от github-actions) у него
#      зелёная не меньше MIN_AGE минут, он потомок выложенного, раньше не выкладывался (откат, неудача) и
#      последним действием tires-deploy был не откат (после отката только ручная выкладка), зовёт `sudo -n /usr/local/sbin/tires-deploy deploy <sha>`. Блокировку,
#      копию базы, ожидание тишины, проверку панели, возврат кода, таймеры и feed-check делает tires-deploy;
#   3. красный, идущий или непонятный коммит не выкладывается никогда; сбой сети: ничего не делает.
# Повторы после неудачи и тревоги (код 1 = тревога через OnFailure=tires-alert@, на одно событие один раз):
#   tires-deploy не начал выкладку (код 1 и любой неизвестный): повтор через 10, 30, 60 минут, после 4-й
#   неудачи коммит блокируется и тревога; коды 2/3: блокировка и тревога; 4/5: код выложен, тревога; сеть:
#   тревога после 30 сбоев подряд; master не выложен 3 часа (тесты стоят, замок занят): тревога.
#   Заблокированный коммит больше сам не выкладывается; снять: строку в $STATE/blocked.
#
# Переменные (задаёт служба, владелец меняет через `systemctl edit tires-pull.service`):
#   TIRES_PULL_REPO          владелец/репозиторий на GitHub
#   TIRES_PULL_CHECK         имя проверки тестов (по умолчанию «tests / pytest»)
#   TIRES_PULL_MIN_AGE_MIN   сколько минут тесты должны быть зелёными до выкладки (0..1440)
# Остальные TIRES_PULL_* (пути) нужны только тестам: tests/test_deploy_pull.py.
# Пишется под bash 3.2 тоже (тесты на Маке), на сервере bash 5.
set -uo pipefail
umask 077

REPO_SLUG=${TIRES_PULL_REPO:-pasha9230/tires-avito-sync}
CHECK_NAME=${TIRES_PULL_CHECK:-tests / pytest}
CHECK_APP=github-actions
MIN_AGE_MIN=${TIRES_PULL_MIN_AGE_MIN:-0}
API=https://api.github.com
STATE=${TIRES_PULL_STATE:-${STATE_DIRECTORY:-/var/lib/tires-pull}}
DEPLOY_STATE=${TIRES_PULL_DEPLOY_STATE:-/opt/tires-sync/deploy-state}
RELEASES=${TIRES_PULL_RELEASES:-/opt/tires-sync/releases}
DEPLOY_LOCK=${TIRES_PULL_DEPLOY_LOCK:-/run/tires-deploy.lock}
DEPLOY=/usr/local/sbin/tires-deploy
TOKEN_NAME=tires-pull.github-token
NET_ALERT_AFTER=30           # сбоев сети подряд (около часа) до тревоги
STALL_ALERT_MIN=180          # master не выложен столько минут (тесты стоят, замок занят): тревога
RECHECK_MIN=5                # без токена: тесты невыкладываемого коммита спрашивать не чаще (лимит 60 запросов в час)
BACKOFF_MIN=(10 30 60)       # паузы после 1-й, 2-й, 3-й неудачной попытки; 4-я неудача: блокировка
SHA_RE='^[0-9a-f]{40}$'

TMP=
cleanup() { [ -z "$TMP" ] || rm -rf -- "$TMP"; }
trap cleanup EXIT

NOW=$(date +%s)
[[ "$NOW" =~ ^[0-9]{9,12}$ ]] || { echo "СТОП: не узнать время"; exit 1; }

# Единственная строка прогона: текст и код выхода. Счётчик сбоев сети сбрасывает любой прогон, который
# дошёл до решения (не через net_fail): сбой на любом шаге (master, тесты, сравнение) копится до тревоги.
# Прогон без тревоги, а master не выложен уже STALL_ALERT_MIN минут: одна тревога на коммит.
IN_NET_FAIL=
STALL_SHA= STALL_SINCE=
end() {
  local rc=$1 msg
  shift; msg=$*
  [ -n "$IN_NET_FAIL" ] || rm -f -- "$STATE/net-fails" 2>/dev/null || true
  if [ "$rc" = 0 ] && [ -n "$STALL_SHA" ] && [ $(( NOW - STALL_SINCE )) -ge $(( STALL_ALERT_MIN * 60 )) ] \
      && [ ! -e "$STATE/alerted-stall-$STALL_SHA" ]; then
    : > "$STATE/alerted-stall-$STALL_SHA" 2>/dev/null || true
    rc=1 msg="master ${STALL_SHA:0:7} не выложен уже $(( (NOW - STALL_SINCE) / 60 )) мин: $msg"
  fi
  printf '%s\n' "$msg"
  exit "$rc"
}

# Тревога на событие один раз: первый раз код 1 (OnFailure), дальше код 0 с той же строкой.
alert_once() {
  local key=$1; shift
  if [ -e "$STATE/alerted-$key" ]; then end 0 "$* (тревога уже была)"; fi
  : > "$STATE/alerted-$key" 2>/dev/null || true
  end 1 "$*"
}
clear_alert() { rm -f -- "$STATE/alerted-$1"; }

is_blocked() { [ -f "$STATE/blocked" ] && grep -q "^$1 " "$STATE/blocked"; }
block() {   # $1 sha (40 hex, проверен), $2 причина; тревога один раз: дальше is_blocked
  local sha=$1; shift
  printf '%s %s %s\n' "$sha" "$NOW" "$*" 2>/dev/null >> "$STATE/blocked"
  # блокировка не записалась (диск): попытка в $STATE/fail остаётся, и следующий прогон заблокирует снова
  is_blocked "$sha" || alert_once state "СТОП ${sha:0:7}: $*; блокировку не записать в $STATE/blocked, ничего не выкладываю"
  rm -f -- "$STATE/fail"
  end 1 "СТОП ${sha:0:7}: $*; сам больше не выкладываю, жду новый коммит в master"
}

# Сбой сети или API: ничего не делаем, тревога ровно на NET_ALERT_AFTER-м сбое подряд.
net_fail() {
  local n
  n=$(cat "$STATE/net-fails" 2>/dev/null || true)
  [[ "$n" =~ ^[0-9]{1,6}$ ]] || n=0
  n=$(( n + 1 ))
  echo "$n" > "$STATE/net-fails"
  IN_NET_FAIL=1
  if [ "$n" -eq "$NET_ALERT_AFTER" ]; then end 1 "GitHub недоступен $n раз подряд, ничего не выкладываю: $*"; fi
  end 0 "пропуск, GitHub недоступен ($n подряд): $*"
}

# Без управляющих знаков ASCII и не длиннее 300 байт: в журнал не попадёт управляющая последовательность из
# ответа сети (кириллица из журнала tires-deploy остаётся).
clean() { printf '%s' "$1" | LC_ALL=C tr -d '\000-\037\177' | head -c 300; }

CURL_AUTH=
api_get() {   # $1 путь API, $2 Accept, $3 файл ответа; при ошибке причина в API_ERR
  local code
  API_ERR=
  # -q: без ~/.curlrc; без -L: ни на какой другой адрес токен не уйдёт
  code=$(curl -q -sS -m 20 --proto =https --max-filesize 5000000 -o "$3" -w '%{http_code}' \
        -H "Accept: $2" -H 'X-GitHub-Api-Version: 2022-11-28' -H 'User-Agent: tires-pull' \
        -K - "$API$1" 2>"$TMP/curl.err" <<<"$CURL_AUTH") \
    || { API_ERR="curl: $(clean "$(head -1 "$TMP/curl.err")")"; return 1; }
  if [ "$code" != 200 ]; then
    case "$code" in
      401|403|404) API_ERR="ответ $code (нет доступа: токен не задан, просрочен или без прав, или лимит запросов)" ;;
      429) API_ERR="ответ 429 (лимит запросов)" ;;
      *) API_ERR="ответ $(clean "$code")" ;;
    esac
    return 1
  fi
}

mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }   # GNU (сервер) или BSD (тесты)

# Учёт выкладки читается под её замком (fd 7, на миг): иначе посреди чужой выкладки (раннер, владелец) выпуск
# нового коммита уже есть, а deployed-sha ещё старый, и это выглядело бы как откат. Замок занят: пропуск.
# Нет файла замка (после перезагрузки выкладок ещё не было): читаем без него. Есть, но не открыть: пропуск.
# Заполняет DEPLOYED, RELEASED (выпуск коммита $1), DEPLOYED_REL (выпуск выложенного), PREV (previous-release:
# пусто после отката, «нет» если файла нет) и ROLLBACK_AT (время отката).
read_state() {
  local f s
  if [ -e "$DEPLOY_LOCK" ]; then
    [ -r "$DEPLOY_LOCK" ] || end 0 "пропуск ${1:0:7}: не открыть замок выкладки $DEPLOY_LOCK"
    exec 7<"$DEPLOY_LOCK"
    flock -n 7 || { exec 7<&-; end 0 "пропуск ${1:0:7}: идёт выкладка или откат"; }
  fi
  DEPLOYED=$(head -c 41 "$DEPLOY_STATE/deployed-sha" 2>/dev/null | tr -d '\n')
  RELEASED= DEPLOYED_REL= RELEASES_OK=
  if [ -d "$RELEASES" ] && [ -r "$RELEASES" ] && [ -x "$RELEASES" ]; then
    RELEASES_OK=1
    for f in "$RELEASES"/*/sha; do
      [ -e "$f" ] || continue
      if ! [ -r "$f" ]; then RELEASES_OK=; continue; fi
      s=$(head -c 41 "$f" | tr -d '\n')
      [ "$s" != "$1" ] || RELEASED=$(dirname "$f")
      [ "$s" != "$DEPLOYED" ] || DEPLOYED_REL=$(dirname "$f")
    done
  fi
  PREV=нет ROLLBACK_AT=0
  if [ -f "$DEPLOY_STATE/previous-release" ]; then
    PREV=$(head -c 300 "$DEPLOY_STATE/previous-release" | tr -d '\n')
    ROLLBACK_AT=$(mtime "$DEPLOY_STATE/previous-release")
  fi
  exec 7<&-
}

# --- настройки ---
mkdir -p "$STATE" 2>/dev/null || true
{ [ -d "$STATE" ] && [ -w "$STATE" ]; } || end 1 "СТОП: нет папки состояния $STATE"
if ! [[ "$REPO_SLUG" =~ ^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$ ]] || [[ "$REPO_SLUG" == *..* ]] \
    || ! [[ "$CHECK_NAME" =~ ^[A-Za-z0-9\ _./()-]{1,100}$ ]] \
    || ! [[ "$MIN_AGE_MIN" =~ ^[0-9]{1,4}$ ]] || [ "$MIN_AGE_MIN" -gt 1440 ]; then
  alert_once config "СТОП: неверные настройки (TIRES_PULL_REPO, TIRES_PULL_CHECK или TIRES_PULL_MIN_AGE_MIN), ничего не выкладываю"
fi
{ command -v jq && command -v curl && command -v flock && command -v sudo; } >/dev/null \
  || alert_once config "СТОП: нет jq, curl, flock или sudo, ничего не выкладываю"
clear_alert config

# Свой замок: ручной запуск во время прогона по таймеру ничего не делает.
exec 8>>"$STATE/lock"
flock -n 8 || end 0 "пропуск: уже идёт другой прогон tires-pull"

# Токен только на чтение: файл root 0600 в /etc/credstore, systemd отдаёт его службе (ImportCredential).
# В argv curl не попадает (stdin, -K -), в окружение детей и в журнал тоже. Нет файла: запросы без токена.
if [ -n "${CREDENTIALS_DIRECTORY:-}" ] && [ -f "$CREDENTIALS_DIRECTORY/$TOKEN_NAME" ]; then
  TOKEN=
  IFS= read -r TOKEN < "$CREDENTIALS_DIRECTORY/$TOKEN_NAME" || true
  TOKEN=${TOKEN%$'\r'}
  [[ "$TOKEN" =~ ^[A-Za-z0-9_]{20,255}$ ]] \
    || alert_once token "СТОП: токен GitHub в неверном формате ($TOKEN_NAME), ничего не выкладываю"
  clear_alert token
  CURL_AUTH="header = \"Authorization: Bearer $TOKEN\""
  unset TOKEN
fi

TMP=$(mktemp -d "$STATE/tmp.XXXXXX") || end 1 "СТОП: не создать временную папку в $STATE"

# --- последний коммит master ---
api_get "/repos/$REPO_SLUG/commits/master" application/vnd.github.sha "$TMP/master" || net_fail "master: $API_ERR"
MASTER=$(head -c 41 "$TMP/master")
[[ "$MASTER" =~ $SHA_RE ]] || net_fail "master: ответ не похож на коммит"

# С какого времени этот master не выложен (для тревоги «стоит», в том числе при вечно занятом замке выкладки).
s_sha= s_at=
[ ! -f "$STATE/seen" ] || read -r s_sha s_at < "$STATE/seen" || true
if [ "$s_sha" != "$MASTER" ] || ! [[ "$s_at" =~ ^[0-9]{1,12}$ ]]; then
  s_at=$NOW
  printf '%s %s\n' "$MASTER" "$NOW" > "$STATE/seen"
fi
STALL_SHA=$MASTER STALL_SINCE=$s_at

read_state "$MASTER"
if [ "$MASTER" = "$DEPLOYED" ]; then
  STALL_SHA=
  rm -f -- "$STATE/fail" "$STATE/seen" "$STATE/checked" "$STATE"/alerted-stall-* 2>/dev/null
  end 0 "выложен ${MASTER:0:7}, нового нет"
fi
if is_blocked "$MASTER"; then
  STALL_SHA=
  end 0 "пропуск ${MASTER:0:7}: заблокирован раньше (см. $STATE/blocked), жду новый коммит"
fi

# После отката сам ничего не выкладываем, пока владелец не выложит вручную. Откат (tires-deploy rollback) оставляет
# previous-release пустым до следующей выкладки. Любой коммит поверх откатанного (следующая влитая ветка, запись
# в docs/) несёт откатанное изменение, а по времени или сообщению коммита не понять, убрано ли оно. Ручная выкладка
# от root (`tires-deploy deploy <коммит>`) снова заполняет previous-release и снимает паузу.
if [ -z "$PREV" ]; then
  STALL_SHA=
  alert_once "rollback-${ROLLBACK_AT//[^0-9]/}" "пауза после отката на ${DEPLOYED:0:7}: сам ничего не выкладываю. Проверьте master (git revert плохого коммита) и выложите вручную: sudo /usr/local/sbin/tires-deploy deploy <коммит>, это снимает паузу"
fi

[[ "$DEPLOYED" =~ $SHA_RE ]] \
  || block "$MASTER" "на сервере нет записи о выложенном коммите ($DEPLOY_STATE/deployed-sha): первую выкладку делает владелец"
# Не видно выпусков или среди них нет выложенного: правило «откатанный не выкладывать» не проверить.
[ -n "$RELEASES_OK" ] && [ -n "$DEPLOYED_REL" ] \
  || alert_once releases "СТОП ${MASTER:0:7}: не прочитать выпуски $RELEASES или среди них нет выложенного ${DEPLOYED:0:7}, ничего не выкладываю"
clear_alert releases

# Выпуск этого коммита уже есть, а выложен другой: откат или неудачная выкладка (код 2/3, в том числе раннером).
# Сам такой коммит больше не выкладывается: раннер выкладывал только по push, а опрос вернул бы откатанный через 2 минуты.
[ -z "$RELEASED" ] \
  || block "$MASTER" "этот коммит уже выкладывался ($RELEASED), а сейчас выложен ${DEPLOYED:0:7}: был откат или выкладка не прошла"

# Пауза после неудачной попытки (записывается до вызова tires-deploy).
ATTEMPTS=0
if [ -f "$STATE/fail" ]; then
  f_sha= f_n= f_next=
  read -r f_sha f_n f_next < "$STATE/fail" || true
  if [ "$f_sha" = "$MASTER" ] && [[ "$f_n" =~ ^[0-9]{1,3}$ ]] && [[ "$f_next" =~ ^[0-9]{1,12}$ ]]; then
    ATTEMPTS=$f_n
    if [ "$ATTEMPTS" -gt "${#BACKOFF_MIN[@]}" ]; then
      block "$MASTER" "tires-deploy не начал выкладку $ATTEMPTS раз подряд, причина в $STATE/last-deploy.log"
    fi
    if [ "$NOW" -lt "$f_next" ]; then
      end 0 "пауза ${MASTER:0:7}: после неудачной попытки $f_n повтор через $(( (f_next - NOW + 59) / 60 )) мин"
    fi
  fi
fi

# Без токена: тесты невыкладываемого коммита спрашиваем не чаще раза в RECHECK_MIN минут.
not_ready() {
  if [ -z "$CURL_AUTH" ]; then printf '%s %s %s\n' "$MASTER" "$(( NOW + RECHECK_MIN * 60 ))" "$1" > "$STATE/checked"; fi
  end 0 "$1"
}
if [ -z "$CURL_AUTH" ] && [ -f "$STATE/checked" ]; then
  c_sha= c_next= c_msg=
  read -r c_sha c_next c_msg < "$STATE/checked" || true
  if [ "$c_sha" = "$MASTER" ] && [[ "$c_next" =~ ^[0-9]{1,12}$ ]] && [ "$NOW" -lt "$c_next" ]; then
    end 0 "$c_msg (тесты спрошу снова через $(( (c_next - NOW + 59) / 60 )) мин)"
  fi
fi

# --- проверка тестов этого коммита (только от github-actions и только для этого коммита) ---
NAME_URI=$(jq -rn --arg s "$CHECK_NAME" '$s|@uri')
api_get "/repos/$REPO_SLUG/commits/$MASTER/check-runs?check_name=$NAME_URI&filter=latest&per_page=100" \
  application/vnd.github+json "$TMP/checks" || net_fail "тесты ${MASTER:0:7}: $API_ERR"
CHECK=$(jq -r --arg name "$CHECK_NAME" --arg app "$CHECK_APP" --arg sha "$MASTER" '
  [ (.check_runs // [])[] | select(.name == $name and .app.slug == $app and .head_sha == $sha) ]
  | sort_by(.id) | last
  | if . == null then "none"
    else "\(.status) \(.conclusion // "none") \(if (.completed_at // "") == "" then 0 else (.completed_at | fromdateiso8601) end)"
    end' "$TMP/checks" 2>/dev/null) || net_fail "тесты ${MASTER:0:7}: ответ не разобран"
[[ "$CHECK" =~ ^(none|[a-z_]{1,20}\ [a-z_]{1,20}\ [0-9]{1,12})$ ]] || net_fail "тесты ${MASTER:0:7}: ответ не разобран"
[ "$CHECK" != none ] || not_ready "ждём ${MASTER:0:7}: проверки «$CHECK_NAME» ещё нет"
read -r C_STATUS C_CONCL C_DONE <<<"$CHECK"
[ "$C_STATUS" = completed ] || not_ready "ждём ${MASTER:0:7}: тесты $C_STATUS"
[ "$C_CONCL" = success ] || not_ready "не выкладываю ${MASTER:0:7}: тесты $C_CONCL"
[ "$C_DONE" -gt 0 ] || net_fail "тесты ${MASTER:0:7}: нет времени окончания"
if [ "$MIN_AGE_MIN" -gt 0 ] && [ $(( NOW - C_DONE )) -lt $(( MIN_AGE_MIN * 60 )) ]; then
  not_ready "ждём ${MASTER:0:7}: тесты зелёные $(( (NOW - C_DONE) / 60 )) мин, выкладываю после $MIN_AGE_MIN"
fi

# --- новый коммит потомок выложенного ---
api_get "/repos/$REPO_SLUG/compare/$DEPLOYED...$MASTER?per_page=1" application/vnd.github+json "$TMP/compare" \
  || net_fail "сравнение ${DEPLOYED:0:7}...${MASTER:0:7}: $API_ERR"
REL=$(jq -r '.status // "none"' "$TMP/compare" 2>/dev/null) || net_fail "сравнение: ответ не разобран"
case "$REL" in
  ahead) ;;
  behind|diverged|identical) block "$MASTER" "master не потомок выложенного ${DEPLOYED:0:7} (GitHub: $REL)" ;;
  *) net_fail "сравнение: непонятный ответ" ;;
esac

# --- выкладка ---
# Перед вызовом ещё раз под замком: учёт не изменился (чужая выкладка, откат) и выпуска коммита нет. Окно до
# замка самого tires-deploy остаётся в миллисекунды; на том же коммите он ответит «уже выложен» (код 0).
was="$DEPLOYED|$PREV"
read_state "$MASTER"
[ "$DEPLOYED|$PREV" = "$was" ] && [ -z "$RELEASED" ] \
  || end 0 "пропуск ${MASTER:0:7}: учёт выкладки изменился во время проверки, проверю в следующий раз"

# Попытка записывается до вызова: не записалась (диск) — не вызываем, иначе повтор каждые 2 минуты.
n=$(( ATTEMPTS + 1 ))
if [ "$n" -le "${#BACKOFF_MIN[@]}" ]; then pause=${BACKOFF_MIN[n-1]}; else pause=0; fi
printf '%s %s %s\n' "$MASTER" "$n" "$(( NOW + pause * 60 ))" 2>/dev/null > "$STATE/fail"
f_sha= f_n=
read -r f_sha f_n _ < "$STATE/fail" 2>/dev/null || true
[ "$f_sha $f_n" = "$MASTER $n" ] || alert_once state "СТОП ${MASTER:0:7}: не записать $STATE/fail, ничего не выкладываю"
clear_alert state

rc=0
sudo -n "$DEPLOY" deploy "$MASTER" > "$STATE/last-deploy.log" 2>&1 < /dev/null || rc=$?
case "$rc" in
  0) rm -f -- "$STATE/fail" "$STATE/seen"; STALL_SHA=; end 0 "выложено ${MASTER:0:7} (было ${DEPLOYED:0:7}); журнал: /var/log/tires-deploy.log" ;;
  2) block "$MASTER" "tires-deploy код 2: панель не прошла проверку, код возвращён; журнал: $STATE/last-deploy.log" ;;
  3) block "$MASTER" "tires-deploy код 3: панель не в порядке и возврат не помог, нужен человек; журнал: $STATE/last-deploy.log" ;;
  4) rm -f -- "$STATE/fail" "$STATE/seen"; STALL_SHA=; end 1 "выложено ${MASTER:0:7}, но feed-check нашёл ошибки (код 4), решение за владельцем; журнал: $STATE/last-deploy.log" ;;
  5) rm -f -- "$STATE/fail" "$STATE/seen"; STALL_SHA=; end 1 "выложено ${MASTER:0:7}, но таймер не обновлён (код 5); журнал: $STATE/last-deploy.log" ;;
  *)
    # 1 (не начато: замок, тишина, копия базы, отказ sudo) и любой другой код (git под set -e у tires-deploy
    # выходит со своим кодом). Дошло до выпуска — следующий прогон заблокирует коммит по правилу выпуска.
    why=$(clean "$(grep 'СТОП' "$STATE/last-deploy.log" | tail -1)")
    # пауза от конца попытки (tires-deploy мог ждать тишины 75 минут), а не от начала прогона
    t=$(date +%s)
    [[ "$t" =~ ^[0-9]{9,12}$ ]] && printf '%s %s %s\n' "$MASTER" "$n" "$(( t + pause * 60 ))" 2>/dev/null > "$STATE/fail"
    [ "$n" -le "${#BACKOFF_MIN[@]}" ] \
      || block "$MASTER" "tires-deploy не начал выкладку $n раз подряд (последний код $rc: ${why:-см. журнал}), журнал: $STATE/last-deploy.log"
    end 0 "не начато ${MASTER:0:7} (код $rc, попытка $n): ${why:-см. $STATE/last-deploy.log}; повтор через $pause мин"
    ;;
esac
