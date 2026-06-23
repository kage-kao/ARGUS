#!/bin/bash
# ARGUS / stream-recorder — минимальный, без nginx.
# Flask + gunicorn слушают сразу на 0.0.0.0:80 (через CAP_NET_BIND_SERVICE).
# Один процесс, никаких прокси.

set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Запускать от root."; exit 1; }

APP_DIR="/opt/stream-recorder"
APP_USER="streamrecorder"
SERVICE="stream-recorder.service"
PORT=80

c_blue=$'\033[1;36m'; c_green=$'\033[1;32m'; c_yellow=$'\033[1;33m'
c_red=$'\033[1;31m'; c_reset=$'\033[0m'
log()  { echo "${c_blue}[*]${c_reset} $*"; }
ok()   { echo "${c_green}[OK]${c_reset} $*"; }
warn() { echo "${c_yellow}[!]${c_reset} $*"; }
die()  { echo "${c_red}[ERR]${c_reset} $*" >&2; exit 1; }

# =====================================================================
log "1/7 Чистка"
systemctl stop "$SERVICE" 2>/dev/null || true
systemctl disable "$SERVICE" 2>/dev/null || true
rm -f "/etc/systemd/system/$SERVICE"
systemctl daemon-reload
rm -rf "$APP_DIR"
userdel "$APP_USER" 2>/dev/null || true

# Освобождаем порт 80 — гасим всё что на нём висит (apache/nginx/etc)
log "    Освобождаю порт $PORT"
systemctl stop nginx     2>/dev/null || true
systemctl disable nginx  2>/dev/null || true
systemctl stop apache2   2>/dev/null || true
systemctl disable apache2 2>/dev/null || true
systemctl stop httpd     2>/dev/null || true
systemctl stop lighttpd  2>/dev/null || true
systemctl stop caddy     2>/dev/null || true
# Если что-то всё ещё держит 80 — фиксируем и убиваем
if ss -ltnp 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}$"; then
    PIDS=$(ss -ltnp 2>/dev/null | awk -v p=":$PORT" '$4 ~ p {print $0}' \
           | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)
    for pid in $PIDS; do
        warn "    PID $pid держит порт $PORT, убиваю"
        kill -9 "$pid" 2>/dev/null || true
    done
fi

# =====================================================================
log "2/7 Пакеты"
echo "deb http://deb.debian.org/debian bookworm-backports main" \
    > /etc/apt/sources.list.d/backports.list
apt-get update -q
apt-get install -y -q python3-flask python3-gunicorn gunicorn \
                       ffmpeg curl ca-certificates iproute2 procps libcap2-bin
apt-get install -y -q -t bookworm-backports yt-dlp
ok "    установлено"

# =====================================================================
log "3/7 Пользователь и каталоги"
useradd -r -m -d "$APP_DIR" -s /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR/templates" "$APP_DIR/records"

# =====================================================================
log "4/7 record_and_upload.sh"
cat > "$APP_DIR/record_and_upload.sh" <<'WORKER_EOF'
#!/bin/bash
# yt-dlp -> Tempshare + Ranoz. Лимиты: TS ~2GB, Ranoz ~4.99GB, иначе — ffmpeg copy split.
set -uo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin"

URL="${1:?usage: $0 <stream_url>}"
APP_DIR="/opt/stream-recorder"
RECORDS="$APP_DIR/records"
TS="$(date +%F_%H-%M-%S)"
WORK="$RECORDS/work_$$_$TS"
LOG="$RECORDS/links.log"
RAW="$WORK/recording_$TS.mp4"
TS_MAX=2000000000
RZ_MAX=4990000000

mkdir -p "$WORK"
inf(){ echo "[$(date +%T)] $*"; }
errf(){ echo "[$(date +%T)] ERROR: $*" >&2; }

inf "rec: $URL"
if ! yt-dlp --no-warnings --no-part \
        --user-agent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' \
        -o "$RAW" "$URL" 2>"$WORK/ytdlp.log"; then
    errf "yt-dlp: $(tail -n3 "$WORK/ytdlp.log" | tr '\n' ' ')"; rm -rf "$WORK"; exit 1
fi
[ -s "$RAW" ] || { errf "no file"; rm -rf "$WORK"; exit 1; }
SIZE=$(stat -c%s "$RAW")
inf "saved: $RAW ($SIZE)"

split_to(){
    local in="$1" max="$2" prefix="$3" dur br segs
    dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$in" 2>/dev/null || echo 0)
    br=$(awk -v s="$(stat -c%s "$in")" -v d="$dur" 'BEGIN{if(d<=0){print 0;exit}print s/d}')
    segs=$(awk -v b="$br" -v t="$max" 'BEGIN{if(b<=0){print 600;exit}v=int(t/b);if(v<60)v=60;print v}')
    inf "split segtime=${segs}s -> $prefix"
    ffmpeg -nostdin -hide_banner -loglevel error -i "$in" \
        -c copy -f segment -segment_time "$segs" -reset_timestamps 1 "$prefix" 2>/dev/null
}
upload_tempshare(){
    curl -sS --max-time 1200 -X POST -F "file=@$1" -F "duration=7" \
         https://api.tempshare.su/upload 2>/dev/null \
    | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("url",""))
except: pass'
}
upload_ranoz(){
    local f="$1" sz nm j up fu
    sz=$(stat -c%s "$f"); nm="$(basename "${f%.*}").dat"
    j=$(curl -sS --max-time 60 -X POST https://ranoz.gg/api/v1/files/upload_url \
        -H "Content-Type: application/json" -d "{\"filename\":\"$nm\",\"size\":$sz}")
    up=$(echo "$j" | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["data"]["upload_url"])
except: pass')
    fu=$(echo "$j" | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["data"]["url"])
except: pass')
    [ -z "$up" ] && { errf "ranoz: presign fail"; return 1; }
    curl -sS --max-time 1800 -X PUT "$up" --upload-file "$f" \
         -H "Content-Length: $sz" -o /dev/null
    echo "$fu"
}
upload_chunked(){
    local f="$1" max="$2" tag="$3" fn="$4" sz=$(stat -c%s "$1") p u
    if [ "$sz" -le "$max" ]; then u=$($fn "$f"); [ -n "$u" ] && echo "$u"; return; fi
    local pdir="$WORK/${tag}_parts"; mkdir -p "$pdir"
    split_to "$f" "$max" "$pdir/${tag}_%03d.mp4" || return 1
    for p in "$pdir"/${tag}_*.mp4; do
        [ -e "$p" ] || continue
        u=$($fn "$p"); [ -n "$u" ] && echo "$u"
    done
}

{ echo ""; echo "$(date '+%d.%m.%Y %H:%M:%S') | $(basename "$RAW") | $SIZE bytes"; } >> "$LOG"

inf "-> tempshare"
while read -r u; do
    [ -n "$u" ] || continue
    inf "ts: $u"; echo "  Tempshare: <a href='$u' target='_blank'>$u</a>" >> "$LOG"
done < <(upload_chunked "$RAW" "$TS_MAX" "ts" upload_tempshare)

inf "-> ranoz"
while read -r u; do
    [ -n "$u" ] || continue
    inf "rz: $u"; echo "  Ranoz: <a href='$u' target='_blank'>$u</a>" >> "$LOG"
done < <(upload_chunked "$RAW" "$RZ_MAX" "rz" upload_ranoz)

rm -rf "$WORK"
inf "done"
WORKER_EOF
chmod +x "$APP_DIR/record_and_upload.sh"

# =====================================================================
log "5/7 Flask app"
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
            print(f"[web] new: {url}", file=sys.stderr, flush=True)
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
    app.run(host="0.0.0.0", port=80)
APP_EOF

cat > "$APP_DIR/templates/index.html" <<'HTML_EOF'
<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>ARGUS</title>
<style>
:root{color-scheme:dark}
body{font-family:ui-monospace,Menlo,Consolas,monospace;background:#0f1115;color:#d8dee9;
     max-width:880px;margin:40px auto;padding:0 20px}
h1{color:#88c0d0;margin:0 0 24px} h2{color:#a3be8c;margin-top:32px}
form{display:flex;gap:8px;margin-bottom:24px}
input[type=url]{flex:1;padding:12px;background:#1f2430;border:1px solid #2e3440;
     color:#eceff4;border-radius:6px;font:inherit}
input[type=submit]{padding:12px 22px;border:0;cursor:pointer;background:#a3be8c;
     color:#0f1115;border-radius:6px;font-weight:bold}
input[type=submit]:hover{background:#b9d39c}
ul{list-style:none;padding:0}
li{background:#1f2430;border:1px solid #2e3440;padding:12px 14px;margin-bottom:8px;
   border-radius:6px;word-break:break-all}
a{color:#88c0d0}
.empty{color:#6c7280}
</style></head><body>
<h1>ARGUS — запись стримов</h1>
<form method="post">
  <input type="url" name="url" placeholder="https://... (.m3u8 / .flv / любой источник для yt-dlp)" required>
  <input type="submit" value="Записать">
</form>
<h2>Последние записи</h2>
{% if links %}<ul>{% for l in links %}<li>{{ l|safe }}</li>{% endfor %}</ul>
{% else %}<p class="empty">Пока ничего не записано.</p>{% endif %}
</body></html>
HTML_EOF

# =====================================================================
log "6/7 systemd unit (gunicorn на :80 через CAP_NET_BIND_SERVICE)"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

GUNICORN_BIN="$(command -v gunicorn || true)"
[ -x "$GUNICORN_BIN" ] || die "gunicorn not found after install"

cat > "/etc/systemd/system/$SERVICE" <<UNIT
[Unit]
Description=Stream Recorder (Flask + gunicorn)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$GUNICORN_BIN --workers 2 --bind 0.0.0.0:$PORT --access-logfile - --error-logfile - app:app
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

# =====================================================================
log "7/7 Запуск + диагностика"
systemctl daemon-reload
systemctl enable --now "$SERVICE"

# Ждём до 10 секунд пока gunicorn реально начнёт слушать
for i in 1 2 3 4 5 6 7 8 9 10; do
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}$"; then
        break
    fi
    sleep 1
done

# Локальная проверка
LOCAL_OK=0
if curl -fsS --max-time 5 "http://127.0.0.1/health" >/dev/null 2>&1; then
    LOCAL_OK=1
fi

PRIV_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
PUB_IP=$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null \
         || curl -fsS --max-time 5 https://ifconfig.me 2>/dev/null || echo "")

echo
echo "=================== РЕЗУЛЬТАТ ==================="
systemctl is-active --quiet "$SERVICE" \
    && ok "сервис: active" \
    || warn "сервис НЕ active"

echo
echo "Слушающие сокеты на :$PORT:"
ss -ltnp 2>/dev/null | awk -v p=":$PORT" 'NR==1 || $4 ~ p'

echo
if [ "$LOCAL_OK" = "1" ]; then
    ok "локальный curl http://127.0.0.1/health отвечает"
else
    warn "локальный curl http://127.0.0.1/health НЕ отвечает"
    echo "------ последние логи сервиса ------"
    journalctl -u "$SERVICE" --no-pager -n 30
    echo "-------------------------------------"
fi

echo
[ -n "$PRIV_IP" ] && echo "Приватный IP: http://$PRIV_IP/"
if [ -n "$PUB_IP" ]; then
    echo "Внешний  IP:  http://$PUB_IP/"
    echo
    if curl -fsS --max-time 8 "http://$PUB_IP/health" >/dev/null 2>&1; then
        ok "ВНЕШНИЙ curl http://$PUB_IP/health отвечает — открывай в браузере!"
    else
        warn "ВНЕШНИЙ curl http://$PUB_IP/health НЕ отвечает"
        echo
        echo "Это значит локально работает, но снаружи режется. Проверь:"
        echo "  1) Файрвол хостера (веб-панель Hetzner/Aeza/Selectel/OVH/etc)"
        echo "     — порт $PORT/tcp должен быть открыт во входящих правилах."
        echo "  2) iptables/nftables на сервере:"
        echo "       iptables -L -n | head"
        echo "       nft list ruleset 2>/dev/null | head"
        echo "  3) Если есть ufw — он отключён или allow $PORT/tcp:"
        echo "       ufw status; ufw allow $PORT/tcp"
    fi
fi
echo "================================================="
echo
echo "Логи:    journalctl -u $SERVICE -f"
echo "Воркер:  tail -f $APP_DIR/records/worker.log"
