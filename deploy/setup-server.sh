#!/usr/bin/env bash
# Разовая настройка автовыкладки на tires-server (deploy/AUTODEPLOY.md, шаги 2–4). Запускает владелец от root:
#   bash setup-server.sh <ссылка-на-архив-раннера> <токен-регистрации-раннера>
# Ссылку на архив и токен регистрации даёт страница GitHub → Settings → Actions → Runners → New self-hosted runner
# (Linux x64; строки «curl -o actions-runner-...tar.gz -L <ссылка>» и «./config.sh ... --token <токен>»).
# Скрипт можно запускать повторно: готовые шаги пропускаются. Ничего не выкладывает и автовыкладку не включает.
set -euo pipefail
RUNNER_URL=${1:?нужна ссылка на архив раннера}
RUNNER_TOKEN=${2:?нужен токен регистрации раннера}
REPO_SSH=git@github.com:pasha9230/tires-avito-sync.git
REPO_WEB=https://github.com/pasha9230/tires-avito-sync
HERE=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || { echo "запускать от root"; exit 1; }
for f in tires-deploy.sh sudoers-tires-deploy; do [ -f "$HERE/$f" ] || { echo "рядом нет $f"; exit 1; }; done

echo "1. пользователь раннера tires-deploy"
id tires-deploy >/dev/null 2>&1 || useradd -m -s /bin/bash tires-deploy
if id -nG tires-deploy | tr ' ' '\n' | grep -qx tires; then echo "tires-deploy не должен быть в группе tires"; exit 1; fi

echo "2. скрипт выкладки и sudo"
install -o root -g root -m 0755 "$HERE/tires-deploy.sh" /usr/local/sbin/tires-deploy
visudo -cf "$HERE/sudoers-tires-deploy"
install -o root -g root -m 0440 "$HERE/sudoers-tires-deploy" /etc/sudoers.d/tires-deploy
install -d -o root -g root -m 0700 /etc/tires-deploy

echo "3. ключ только на чтение и копия репозитория"
if [ ! -f /root/.ssh/tires-deploy-ro ]; then
  install -d -m 0700 /root/.ssh
  ssh-keygen -q -t ed25519 -f /root/.ssh/tires-deploy-ro -N '' -C tires-deploy-ro
fi
grep -q '^github.com ' /root/.ssh/known_hosts 2>/dev/null || ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts 2>/dev/null
if [ ! -d /opt/tires-sync/repo/.git ]; then
  echo
  echo ">>> Добавьте этот ключ в $REPO_WEB/settings/keys/new (название tires-server, БЕЗ «Allow write access»):"
  cat /root/.ssh/tires-deploy-ro.pub
  read -r -p ">>> Добавили? Нажмите Enter. " _
  git clone -q -c core.sshCommand="ssh -i /root/.ssh/tires-deploy-ro -o IdentitiesOnly=yes" "$REPO_SSH" /opt/tires-sync/repo
fi
git -C /opt/tires-sync/repo fetch -q origin master && echo "   копия репозитория в порядке"

echo "4. раннер GitHub"
if [ ! -f /home/tires-deploy/actions-runner/.runner ]; then
  runuser -u tires-deploy -- bash -c "mkdir -p ~/actions-runner && cd ~/actions-runner \
    && curl -fsSL -o runner.tar.gz '$RUNNER_URL' && tar xzf runner.tar.gz && rm runner.tar.gz \
    && ./config.sh --unattended --url '$REPO_WEB' --token '$RUNNER_TOKEN' --name tires-server --labels tires-prod --work _work"
  (cd /home/tires-deploy/actions-runner && ./svc.sh install tires-deploy && ./svc.sh start)
fi
echo
echo "Готово. Включение после защиты master в GitHub: touch /etc/tires-deploy/enabled"
/usr/local/sbin/tires-deploy status || true
