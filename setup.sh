#!/usr/bin/env bash
# ARGUS Telegram Bot — installer.
# Usage:  curl -L https://raw.githubusercontent.com/kage-kao/ARGUS/main/setup.sh | bash
#
# Скрипт:
#   1) ставит зависимости (python3, ffmpeg, curl, ca-certs),
#   2) интерактивно (через /dev/tty) запрашивает Telegram bot token,
#   3) выкачивает исходники из GitHub,
#   4) ставит pip-зависимости в venv,
#   5) поднимает systemd-юнит и стартует бота.
#
set -Eeuo pipefail

REPO_OWNER="${REPO_OWNER:-kage-kao}"
REPO_NAME="${REPO_NAME:-ARGUS}"
REPO_BRANCH="${REPO_BRANCH:-main}"
APP_DIR="/opt/argus-bot"
APP_USER="argusbot"
SERVICE="argus-bot.service"

c_blue=$'\033[1;36m'; c_green=$'\033[1;32m'; c_yellow=$'\033[1;33m'
c_red=$'\033[1;31m'; c_reset=$'\033[0m'
log()  { echo "${c_blue}[*]${c_reset} $*"; }
ok()   { echo "${c_green}[OK]${c_reset} $*"; }
warn() { echo "${c_yellow}[!]${c_reset} $*"; }
die()  { echo "${c_red}[ERR]${c_reset} $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Запускай от root (sudo)."

# ---------------------------------------------------------------------------
# 1) Зависимости ОС
# ---------------------------------------------------------------------------
log "1/6 Установка системных пакетов…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q \
    python3 python3-venv python3-pip \
    ffmpeg curl ca-certificates tar
ok "пакеты установлены"

# ---------------------------------------------------------------------------
# 2) Чистка предыдущей установки
# ---------------------------------------------------------------------------
log "2/6 Чистка предыдущей установки (если была)…"
systemctl stop "$SERVICE" 2>/dev/null || true
systemctl disable "$SERVICE" 2>/dev/null || true
rm -f "/etc/systemd/system/$SERVICE"
systemctl daemon-reload
# Сохраняем .env с токеном, чтобы не спрашивать заново при переустановке
SAVED_ENV=""
if [ -f "$APP_DIR/.env" ]; then
    SAVED_ENV="$(cat "$APP_DIR/.env")"
fi
rm -rf "$APP_DIR"
id -u "$APP_USER" >/dev/null 2>&1 && userdel "$APP_USER" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 3) Запрос токена бота (интерактивно через /dev/tty)
# ---------------------------------------------------------------------------
log "3/6 Telegram bot token"
TOKEN=""
if [ -n "$SAVED_ENV" ]; then
    TOKEN="$(printf '%s\n' "$SAVED_ENV" | sed -n 's/^BOT_TOKEN=//p' | head -n1)"
fi

if [ -n "${BOT_TOKEN:-}" ]; then
    TOKEN="$BOT_TOKEN"
    ok "использую BOT_TOKEN из окружения"
elif [ -n "$TOKEN" ]; then
    ok "использую сохранённый токен из предыдущей установки"
else
    if [ ! -t 0 ] && [ ! -r /dev/tty ]; then
        die "Не могу прочитать токен: запусти скрипт в интерактивном терминале (или передай BOT_TOKEN=... в env)."
    fi
    echo
    echo "  Получи токен у @BotFather (/newbot) и вставь сюда."
    echo "  Формат:  123456789:ABCdefGhIJKlmNoPQRstuVWXyz"
    echo
    while [ -z "$TOKEN" ]; do
        printf "${c_yellow}>>> Вставь Telegram bot token и нажми Enter: ${c_reset}"
        # Read from controlling tty so curl|bash works
        if [ -r /dev/tty ]; then
            read -r TOKEN </dev/tty || true
        else
            read -r TOKEN || true
        fi
        TOKEN="$(echo -n "$TOKEN" | tr -d '[:space:]')"
        if ! [[ "$TOKEN" =~ ^[0-9]{6,}:[A-Za-z0-9_-]{20,}$ ]]; then
            warn "Похоже не на токен (нужно вида 123:abc...). Попробуй ещё раз."
            TOKEN=""
        fi
    done
    ok "токен принят"
fi

# ---------------------------------------------------------------------------
# 4) Скачивание исходников
# ---------------------------------------------------------------------------
log "4/6 Загрузка исходников из github.com/$REPO_OWNER/$REPO_NAME (ветка $REPO_BRANCH)…"
useradd -r -m -d "$APP_DIR" -s /usr/sbin/nologin "$APP_USER"
TARBALL_URL="https://codeload.github.com/$REPO_OWNER/$REPO_NAME/tar.gz/refs/heads/$REPO_BRANCH"
TMP_TGZ="$(mktemp --suffix=.tgz)"
curl -fsSL "$TARBALL_URL" -o "$TMP_TGZ" || die "не удалось скачать $TARBALL_URL"
tar -xzf "$TMP_TGZ" -C /tmp
rm -f "$TMP_TGZ"
SRC_DIR="/tmp/${REPO_NAME}-${REPO_BRANCH}"
[ -d "$SRC_DIR/argus_bot" ] || die "в архиве нет каталога argus_bot/ — проверь репозиторий"

mkdir -p "$APP_DIR"
cp -r "$SRC_DIR/argus_bot" "$APP_DIR/"
cp "$SRC_DIR/requirements.txt" "$APP_DIR/"
[ -f "$SRC_DIR/README.md" ] && cp "$SRC_DIR/README.md" "$APP_DIR/" || true
mkdir -p "$APP_DIR/work"
rm -rf "$SRC_DIR"

# .env с токеном
cat > "$APP_DIR/.env" <<ENV_EOF
BOT_TOKEN=$TOKEN
ARGUS_DIR=$APP_DIR
ENV_EOF
chmod 600 "$APP_DIR/.env"

# venv + зависимости
log "    создаю venv и ставлю pip-зависимости (это может занять минуту)…"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip --quiet
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" --quiet
ok "зависимости установлены"

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ---------------------------------------------------------------------------
# 5) systemd-юнит
# ---------------------------------------------------------------------------
log "5/6 systemd unit…"
cat > "/etc/systemd/system/$SERVICE" <<UNIT_EOF
[Unit]
Description=ARGUS Telegram Bot (stream recorder)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/python -m argus_bot
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
# Ресурсные лимиты (yt-dlp/ffmpeg долгие, но бот работает асинхронно)
LimitNOFILE=65536
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
UNIT_EOF

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

# ---------------------------------------------------------------------------
# 6) Проверка
# ---------------------------------------------------------------------------
log "6/6 Проверка состояния…"
sleep 2
if systemctl is-active --quiet "$SERVICE"; then
    ok "сервис активен: $SERVICE"
else
    warn "сервис НЕ активен. Логи:"
    journalctl -u "$SERVICE" --no-pager -n 40 || true
    die "запуск не удался"
fi

echo
echo "================================================="
echo "  ARGUS bot установлен и работает."
echo "  Логи:        journalctl -u $SERVICE -f"
echo "  Перезапуск:  systemctl restart $SERVICE"
echo "  Конфиг:      $APP_DIR/.env"
echo "================================================="
echo
echo "Открой Telegram, найди своего бота и пришли /start."
