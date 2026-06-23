#!/bin/bash
# ARGUS / stream-recorder — clean install for Debian 12 / 13.
# Записывает стрим через yt-dlp -> заливает на Tempshare и Ranoz.
# Веб-морда: Flask + gunicorn за nginx (порт 80).

set -euo pipefail

# ---- root check ----
if [ "$(id -u)" -ne 0 ]; then
    echo "Запускать от root." >&2
    exit 1
fi

APP_DIR="/opt/stream-recorder"
APP_USER="streamrecorder"
SERVICE="stream-recorder.service"
PORT_APP=5000
PORT_HTTP=80

log()  { printf '\033[1;36m[*]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[OK]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[ERR]\033[0m %s\n' "$*" >&2; exit 1; }

# =====================================================================
log "1/8 Останавливаю и сношу старую установку"
systemctl stop "$SERVICE"   2>/dev/null || true
systemctl disable "$SERVICE" 2>/dev/null || true
rm -f "/etc/systemd/system/$SERVICE"
systemctl daemon-reload
rm -rf "$APP_DIR"
userdel "$APP_USER" 2>/dev/null || true

# =====================================================================
log "2/8 Подключаю backports и ставлю пакеты"
echo "deb http://deb.debian.org/debian bookworm-backports main" \
    > /etc/apt/sources.list.d/backports.list
apt-get update -q

# базовые
apt-get install -y -q \
    python3-flask python3-gunicorn gunicorn \
    ffmpeg curl ca-certificates nginx iproute2

# yt-dlp обязательно из backports — там свежий
apt-get install -y -q -t bookworm-backports yt-dlp

ok "Пакеты установлены"

# =====================================================================
log "3/8 Создаю пользователя и каталоги"
useradd -r -m -d "$APP_DIR" -s /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR/templates" "$APP_DIR/records"

# =====================================================================
log "4/8 Пишу record_and_upload.sh (запись + аплоад)"
cat > "$APP_DIR/record_and_upload.sh" <<'WORKER_EOF'
#!/bin/bash
# Пишет стрим через yt-dlp, заливает на Tempshare и Ranoz, пишет ссылки в links.log.
set -uo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin"

STREAM_URL="${1:?usage: record_and_upload.sh <url>}"
APP_DIR="/opt/stream-recorder"
RECORDS="$APP_DIR/records"
TS="$(date +%F_%H-%M-%S)"
WORK="$RECORDS/work_$$_$TS"
LOG="$RECORDS/links.log"
RAW="$WORK/recording_$TS.mp4"

TEMPSHARE_MAX=2000000000   # ~2 GB
RANOZ_MAX=4990000000       # ~4.99 GB

mkdir -p "$WORK"

inf(){ echo "[$(date +%T)] $*"; }
errf(){ echo "[$(date +%T)] ERROR: $*" >&2; }

# ---------- запись ----------
inf "Recording: $STREAM_URL"
if ! yt-dlp --no-warnings --no-part \
        --user-agent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' \
        -o "$RAW" "$STREAM_URL" 2>"$WORK/ytdlp.log"; then
    errf "yt-dlp failed: $(tail -n3 "$WORK/ytdlp.log" | tr '\n' ' ')"
    rm -rf "$WORK"
    exit 1
fi
[ -s "$RAW" ] || { errf "no output file"; rm -rf "$WORK"; exit 1; }
SIZE=$(stat -c%s "$RAW")
inf "Saved: $RAW ($SIZE bytes)"

# ---------- разбиение ffmpeg copy на сегменты <=max ----------
split_to() {
    local in="$1" max="$2" prefix="$3"
    local dur br segs
    dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$in" 2>/dev/null || echo 0)
    [ "$(echo "$dur > 0" | bc -l 2>/dev/null || echo 0)" = "1" ] || { errf "bad duration"; return 1; }
    br=$(awk -v s="$(stat -c%s "$in")" -v d="$dur" 'BEGIN{print s/d}')
    segs=$(awk -v b="$br" -v t="$max" 'BEGIN{v=int(t/b); if(v<60)v=60; print v}')
    inf "split: dur=${dur}s segtime=${segs}s -> $prefix"
    ffmpeg -nostdin -hide_banner -loglevel error -i "$in" \
        -c copy -f segment -segment_time "$segs" -reset_timestamps 1 "$prefix" 2>/dev/null
}

# ---------- Tempshare ----------
upload_tempshare() {
    local f="$1"
    curl -sS --max-time 600 -X POST \
        -F "file=@$f" -F "duration=7" \
        https://api.tempshare.su/upload 2>/dev/null \
    | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("url",""))
except: pass'
}

# ---------- Ranoz (presigned PUT, расширение .dat) ----------
upload_ranoz() {
    local f="$1" size name j upurl furl
    size=$(stat -c%s "$f")
    name="$(basename "${f%.*}").dat"
    j=$(curl -sS --max-time 60 -X POST https://ranoz.gg/api/v1/files/upload_url \
        -H "Content-Type: application/json" \
        -d "{\"filename\":\"$name\",\"size\":$size}")
    upurl=$(echo "$j" | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["data"]["upload_url"])
except: pass')
    furl=$(echo "$j" | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["data"]["url"])
except: pass')
    [ -z "$upurl" ] && { errf "ranoz: no presigned url"; return 1; }
    curl -sS --max-time 1800 -X PUT "$upurl" \
        --upload-file "$f" -H "Content-Length: $size" -o /dev/null
    echo "$furl"
}

upload_chunked() {
    local f="$1" max="$2" tag="$3" upfn="$4"
    local size pdir p u
    size=$(stat -c%s "$f")
    if [ "$size" -le "$max" ]; then
        u=$($upfn "$f"); [ -n "$u" ] && echo "$u"
        return
    fi
    pdir="$WORK/${tag}_parts"; mkdir -p "$pdir"
    split_to "$f" "$max" "$pdir/${tag}_%03d.mp4" || return 1
    for p in "$pdir"/${tag}_*.mp4; do
        [ -e "$p" ] || continue
        u=$($upfn "$p"); [ -n "$u" ] && echo "$u"
    done
}

# ---------- запуск аплоадов ----------
{
    echo ""
    echo "$(date '+%d.%m.%Y %H:%M:%S') | $(basename "$RAW") | $SIZE bytes"
} >> "$LOG"

inf "Uploading to Tempshare..."
while read -r u; do
    [ -n "$u" ] || continue
    inf "tempshare: $u"
    echo "  Tempshare: <a href='$u' target='_blank'>$u</a>" >> "$LOG"
done < <(upload_chunked "$RAW" "$TEMPSHARE_MAX" "ts" upload_tempshare)

inf "Uploading to Ranoz..."
while read -r u; do
    [ -n "$u" ] || continue
    inf "ranoz: $u"
    echo "  Ranoz: <a href='$u' target='_blank'>$u</a>" >> "$LOG"
done < <(upload_chunked "$RAW" "$RANOZ_MAX" "rz" upload_ranoz)

# чистим временное, но оставляем сам raw на сутки на всякий случай
rm -rf "$WORK"
inf "Done."
WORKER_EOF
chmod +x "$APP_DIR/record_and_upload.sh"

# =====================================================================
log "5/8 Пишу Flask-приложение"
cat > "$APP_DIR/app.py" <<'APP_EOF'
from flask import Flask, request, render_template, redirect, url_for
import subprocess, os, sys, shlex

APP_DIR    = "/opt/stream-recorder"
LOG_FILE   = os.path.join(APP_DIR, "records", "links.log")
SCRIPT     = os.path.join(APP_DIR, "record_and_upload.sh")
WORKER_LOG = os.path.join(APP_DIR, "records", "worker.log")

app = Flask(__name__)


@app.route("/health")
def health():
    return "ok", 200


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        url = (request.form.get("url") or "").strip()
        if url:
            print(f"[web] new recording: {url}", file=sys.stderr, flush=True)
            cmd = f"nohup {shlex.quote(SCRIPT)} {shlex.quote(url)} >> {shlex.quote(WORKER_LOG)} 2>&1 &"
            subprocess.Popen(cmd, shell=True, executable="/bin/bash")
        return redirect(url_for("index"))

    links = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            links = [ln.rstrip() for ln in f if ln.strip()]
        links.reverse()
    return render_template("index.html", links=links)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
APP_EOF

# =====================================================================
log "6/8 Пишу шаблон"
cat > "$APP_DIR/templates/index.html" <<'HTML_EOF'
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>ARGUS — запись стримов</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: ui-monospace, Menlo, Consolas, monospace;
         background:#0f1115; color:#d8dee9; max-width:880px;
         margin:40px auto; padding:0 20px; }
  h1 { color:#88c0d0; margin:0 0 24px; }
  h2 { color:#a3be8c; margin-top:32px; }
  form { display:flex; gap:8px; margin-bottom:24px; }
  input[type=url] { flex:1; padding:12px; background:#1f2430;
                    border:1px solid #2e3440; color:#eceff4;
                    border-radius:6px; font:inherit; }
  input[type=submit] { padding:12px 22px; border:0; cursor:pointer;
                       background:#a3be8c; color:#0f1115;
                       border-radius:6px; font-weight:bold; }
  input[type=submit]:hover { background:#b9d39c; }
  ul { list-style:none; padding:0; }
  li { background:#1f2430; border:1px solid #2e3440;
       padding:12px 14px; margin-bottom:8px; border-radius:6px;
       word-break:break-all; }
  a { color:#88c0d0; }
  .empty { color:#6c7280; }
</style>
</head>
<body>
  <h1>ARGUS — запись стримов</h1>
  <form method="post">
    <input type="url" name="url" placeholder="https://... (.m3u8 / .flv / любой источник для yt-dlp)" required>
    <input type="submit" value="Записать">
  </form>
  <h2>Последние записи</h2>
  {% if links %}
    <ul>{% for l in links %}<li>{{ l|safe }}</li>{% endfor %}</ul>
  {% else %}
    <p class="empty">Пока ничего не записано.</p>
  {% endif %}
</body>
</html>
HTML_EOF

# =====================================================================
log "7/8 Права, systemd unit, nginx, файрвол"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# systemd unit. /usr/bin/gunicorn уже точно есть (пакет gunicorn установлен).
cat > "/etc/systemd/system/$SERVICE" <<UNIT
[Unit]
Description=Stream Recorder (Flask + gunicorn)
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/gunicorn --workers 2 --bind 127.0.0.1:$PORT_APP app:app
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

# nginx reverse proxy: 80 -> 127.0.0.1:5000
cat > /etc/nginx/sites-available/stream-recorder <<NGINX
server {
    listen $PORT_HTTP default_server;
    listen [::]:$PORT_HTTP default_server;
    server_name _;

    client_max_body_size 50m;

    location / {
        proxy_pass http://127.0.0.1:$PORT_APP;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;
    }
}
NGINX
rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-enabled/stream-recorder
ln -s /etc/nginx/sites-available/stream-recorder /etc/nginx/sites-enabled/stream-recorder
nginx -t

# Файрвол — открыть порты если ufw активен
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "$PORT_HTTP"/tcp     >/dev/null || true
    ufw allow "$PORT_APP"/tcp      >/dev/null || true
    ok "ufw: открыт $PORT_HTTP/tcp и $PORT_APP/tcp"
fi

# =====================================================================
log "8/8 Запуск"
systemctl daemon-reload
systemctl enable --now "$SERVICE"
systemctl restart nginx
sleep 2

if ! systemctl is-active --quiet "$SERVICE"; then
    warn "stream-recorder не запустился. Логи:"
    journalctl -u "$SERVICE" --no-pager -n 30
    die  "не удалось поднять сервис"
fi

# health-check
if ! curl -fsS --max-time 5 "http://127.0.0.1:$PORT_APP/health" >/dev/null; then
    warn "gunicorn не отвечает на /health (внутренний 127.0.0.1:$PORT_APP)"
fi
if ! curl -fsS --max-time 5 "http://127.0.0.1:$PORT_HTTP/health" >/dev/null; then
    warn "nginx не отвечает на :$PORT_HTTP/health"
fi

# IP-адреса
PRIVATE_IP=$(hostname -I | awk '{print $1}')
PUBLIC_IP=$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null \
            || curl -fsS --max-time 5 https://ifconfig.me 2>/dev/null \
            || echo "")

echo
ok "Установка завершена."
echo
echo "  Внутренний IP : http://$PRIVATE_IP/    (или :$PORT_APP)"
[ -n "$PUBLIC_IP" ] && echo "  Внешний  IP   : http://$PUBLIC_IP/     (или :$PORT_APP)"
echo
echo "  Логи сервиса : journalctl -u $SERVICE -f"
echo "  Логи воркера : tail -f $APP_DIR/records/worker.log"
echo
if [ -n "$PUBLIC_IP" ] && [ "$PUBLIC_IP" != "$PRIVATE_IP" ]; then
    warn "Заходи в браузере по ВНЕШНЕМУ IP ($PUBLIC_IP), а не по $PRIVATE_IP — он приватный."
fi
