#!/usr/bin/env bash
# Torcida — one-shot VPS setup (Ubuntu 24.04, Contabo Cloud VPS).
# Run as root:  bash setup_vps.sh app.torcida.app
# Idempotent: safe to re-run. After it finishes, scp the .env and run
# `systemctl restart torcida-web torcida-bot`.
set -euo pipefail

DOMAIN="${1:-app.torcida.app}"
REPO="https://github.com/CrashDiniz/torcida.git"
APP_USER="torcida"
APP_DIR="/home/${APP_USER}/app"

echo "== packages =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3-venv python3-pip ffmpeg ufw curl >/dev/null

if ! command -v node >/dev/null || [[ "$(node -v | cut -c2-3)" -lt 20 ]]; then
  echo "== node 20 =="
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null
  apt-get install -y -qq nodejs >/dev/null
fi

if ! command -v caddy >/dev/null; then
  echo "== caddy =="
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https >/dev/null
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg --yes
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi

echo "== user + repo =="
id -u "${APP_USER}" &>/dev/null || useradd -m -s /bin/bash "${APP_USER}"
if [[ -d "${APP_DIR}/.git" ]]; then
  sudo -u "${APP_USER}" git -C "${APP_DIR}" pull --ff-only
else
  sudo -u "${APP_USER}" git clone "${REPO}" "${APP_DIR}"
fi

echo "== python venv =="
sudo -u "${APP_USER}" bash -c "
  cd '${APP_DIR}'
  [[ -d .venv ]] || python3 -m venv .venv
  .venv/bin/pip install -q -U pip
  .venv/bin/pip install -q -r requirements.txt
  mkdir -p data
"

echo "== onchain deps =="
sudo -u "${APP_USER}" bash -c "cd '${APP_DIR}/onchain' && npm install --no-audit --no-fund --silent"

echo "== systemd units =="
cat > /etc/systemd/system/torcida-web.service <<EOF
[Unit]
Description=Torcida web (uvicorn)
After=network-online.target
[Service]
User=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/uvicorn src.web.app:app --host 127.0.0.1 --port 8090
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/torcida-bot.service <<EOF
[Unit]
Description=Torcida telegram bot
After=network-online.target torcida-web.service
[Service]
User=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python -m src.bot.main
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF

echo "== caddy (${DOMAIN}) =="
cat > /etc/caddy/Caddyfile <<EOF
${DOMAIN} {
    reverse_proxy 127.0.0.1:8090
    encode gzip
}
EOF

echo "== sqlite backup timer (daily, keeps 14) =="
cat > /etc/systemd/system/torcida-backup.service <<EOF
[Unit]
Description=Torcida sqlite backup
[Service]
Type=oneshot
User=${APP_USER}
ExecStart=/bin/bash -c 'mkdir -p /home/${APP_USER}/backups && \
  sqlite3 ${APP_DIR}/data/app.sqlite3 ".backup /home/${APP_USER}/backups/app-\$(date +%%F).sqlite3" && \
  ls -t /home/${APP_USER}/backups/app-*.sqlite3 | tail -n +15 | xargs -r rm'
EOF
cat > /etc/systemd/system/torcida-backup.timer <<EOF
[Unit]
Description=Daily Torcida backup
[Timer]
OnCalendar=daily
Persistent=true
[Install]
WantedBy=timers.target
EOF
apt-get install -y -qq sqlite3 >/dev/null

echo "== firewall =="
ufw allow OpenSSH >/dev/null
ufw allow 80,443/tcp >/dev/null
ufw --force enable >/dev/null

systemctl daemon-reload
systemctl enable --now caddy torcida-backup.timer >/dev/null
systemctl enable torcida-web torcida-bot >/dev/null

echo
echo "======================================================================"
echo "Setup OK. Faltam 2 passos manuais:"
echo "  1) copiar o .env:   scp app/.env root@<IP>:${APP_DIR}/.env"
echo "     (+ a wallet:     scp -r .keys root@<IP>:/home/${APP_USER}/.keys"
echo "      e ajustar WALLET_PATH no .env se necessário)"
echo "     depois:          chown ${APP_USER}:${APP_USER} ${APP_DIR}/.env"
echo "  2) DNS: A record ${DOMAIN} -> IP deste VPS (Spaceship)"
echo "Então:  systemctl restart torcida-web torcida-bot"
echo "⚠️  SÓ ligue o bot aqui DEPOIS de desligar o bot local (TelegramConflict)."
echo "======================================================================"
