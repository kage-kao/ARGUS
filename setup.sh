#!/bin/bash

# Останавливаем выполнение скрипта при любой ошибке
set -e

# --- Проверка прав root ---
if [ "$(id -u)" -ne 0 ]; then
  echo "Ошибка: Этот скрипт необходимо запускать с правами root." >&2
  exit 1
fi

echo "🔥 НАЧИНАЮ ПОЛНУЮ ПЕРЕУСТАНОВКУ С НУЛЯ."

# --- ШАГ 1: ПОЛНАЯ ОЧИСТКА ОТ СТАРЫХ УСТАНОВОК ---
echo "⚙️  (1/7) Остановка и полное удаление старого сервиса..."
systemctl stop stream-recorder.service >/dev/null 2>&1 || true
systemctl disable stream-recorder.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/stream-recorder.service
systemctl daemon-reload

echo "⚙️  (2/7) Удаление старой директории приложения..."
rm -rf /opt/stream-recorder

echo "⚙️  (3/7) Удаление старого системного пользователя..."
userdel streamrecorder >/dev/null 2>&1 || true

echo "✅ Система очищена. Начинаю чистую установку."

# --- ШАГ 2: УСТАНОВКА ЗАВИСИМОСТЕЙ ---
echo "⚙️  (4/7) Подключение репозитория backports и установка пакетов..."
echo "deb http://deb.debian.org/debian bookworm-backports main" > /etc/apt/sources.list.d/backports.list
apt-get update
apt-get install -y python3-flask ffmpeg curl -t bookworm-backports yt-dlp

echo "✅ Пакеты установлены."

# --- ШАГ 3: СОЗДАНИЕ СТРУКТУРЫ И ФАЙЛОВ ---
echo "⚙️  (5/7) Создание пользователя, директорий и файлов приложения..."
# Создаем пользователя
useradd -r -m -d /opt/stream-recorder -s /bin/false streamrecorder

# Создаем директории уже внутри домашней папки нового пользователя
APP_DIR="/opt/stream-recorder"
mkdir -p $APP_DIR/templates
mkdir -p $APP_DIR/records

# Создаем скрипт записи.
# Запись -> нарезка на валидные mp4-сегменты <=199МБ (ffmpeg copy, без реэнкода)
# -> сжатие каждого сегмента через ezgif (внешний энкодер, не грузит ваш сервер)
# -> склейка (ffmpeg copy) -> загрузка на Ranoz (<=4.99ГБ, .dat) + Tempshare (<=2ГБ).
cat <<'EOF' > $APP_DIR/record_and_upload.sh
#!/bin/bash
# ARGUS record + compress + upload pipeline
set -uo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin:$PATH"

STREAM_URL="$1"
APP_DIR="${ARGUS_DIR:-/opt/stream-recorder}"
RECORDS_DIR="$APP_DIR/records"
TS="$(date +%Y-%m-%d_%H-%M-%S)"
WORK="$RECORDS_DIR/work_$$_$TS"
LOGFILE="$RECORDS_DIR/links.log"
RAW="$WORK/raw.mp4"

# --- tunables ---
EZGIF_MAX=175000000        # ~175MB, safe under ezgif 200MB upload cap
TS_MAX=2000000000          # Tempshare ~2GB
RZ_MAX=4990000000          # Ranoz ~4.99GB
EZGIF_RES="640x360"        # aggressive compression target
EZGIF_BITRATE="300"        # kbps
EZGIF_FORMAT="mp4"
EZGIF_BASE="https://ezgif.com/video-compressor"

log(){ echo "INFO: $*"; }
err(){ echo "ERROR: $*" >&2; }

mkdir -p "$WORK"

# --- record ---
if [ -n "${ARGUS_RAW_FILE:-}" ]; then
  RAW="$ARGUS_RAW_FILE"
  log "Using existing recording: $RAW"
else
  log "Recording: $STREAM_URL"
  if ! yt-dlp --user-agent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36' --no-warnings -o "$RAW" "$STREAM_URL" 2>"$WORK/ytdlp.log"; then
    err "yt-dlp failed. See $WORK/ytdlp.log"
    exit 1
  fi
  [ -f "$RAW" ] || { err "yt-dlp produced no file"; exit 1; }
  log "Recorded: $RAW ($(stat -c%s "$RAW") bytes)"
fi

# --- split into valid mp4 segments (copy, no re-encode) ---
split_segments(){
  local in="$1" target="$2" prefix="$3"
  local dur bitrate segtime
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$in")
  bitrate=$(awk -v s="$(stat -c%s "$in")" -v d="$dur" 'BEGIN{print s/d}')
  segtime=$(awk -v b="$bitrate" -v t="$target" 'BEGIN{v=int(t/b); if(v<1)v=1; print v}')
  log "Split: dur=${dur}s bitrate=${bitrate}B/s segtime=${segtime}s target=${target}B"
  ffmpeg -nostdin -hide_banner -loglevel error -i "$in" -c copy -f segment \
    -segment_time "$segtime" -reset_timestamps 1 "$prefix" 2>/dev/null
}

# --- compress one segment via ezgif -> stdout: compressed file path ---
ezgif_compress(){
  local seg="$1" out="$2"
  local name size id ext eff compid compext attempt body
  name="$(basename "$seg")"; size=$(stat -c%s "$seg")
  [ "$size" -gt 200000000 ] && { err "$name >200MB, ezgif cap exceeded"; return 1; }

  for attempt in 1 2 3; do
    eff=$(curl -sSL -H "Referer: $EZGIF_BASE" \
      -F "new-image=@$seg" -F "upload=Upload video!" \
      -w "\n%{url_effective}" -o /dev/null "$EZGIF_BASE" 2>/dev/null | tail -1)
    id=$(echo "$eff" | grep -oE 'ezgif-[a-f0-9]+\.[a-z0-9]+' | head -1)
    ext="${id##*.}"; id="${id%.*}"; id="${id#ezgif-}"
    [ -n "$id" ] && break
    sleep 4
  done
  [ -z "$id" ] && { err "ezgif upload failed for $name"; return 1; }
  log "ezgif uploaded $name -> $id.$ext"
  sleep 4

  for attempt in 1 2 3; do
    body=$(curl -sSL -H "Referer: $EZGIF_BASE/ezgif-$id.$ext.html" \
      -F "file=ezgif-$id.$ext" -F "resolution=$EZGIF_RES" -F "bitrate=$EZGIF_BITRATE" \
      -F "format=$EZGIF_FORMAT" -F "video-compressor=Recompress video!" \
      "$EZGIF_BASE/ezgif-$id.$ext" 2>/dev/null)
    compid=$(echo "$body" | grep -oE 'ezgif-[a-f0-9]+\.[a-z0-9]+' | grep -v "^ezgif-$id\." | head -1)
    [ -n "$compid" ] && break
    sleep 6
  done
  [ -z "$compid" ] && { err "ezgif recompress failed for $name"; return 1; }
  compext="${compid##*.}"; compid="${compid%.*}"; compid="${compid#ezgif-}"
  log "ezgif compressed $name -> $compid.$compext"

  curl -sSL -H "Referer: $EZGIF_BASE/ezgif-$id.$ext" \
    -o "$out" "https://ezgif.com/save/ezgif-$compid.$compext" 2>/dev/null
  if [ -s "$out" ] && ffprobe -v error "$out" >/dev/null 2>&1; then
    log "downloaded compressed $name -> $(stat -c%s "$out") bytes"
    return 0
  fi
  err "ezgif download failed for $name"
  return 1
}

# --- Ranoz upload (video blocked -> use .dat) -> stdout: url ---
upload_ranoz(){
  local f="$1" size name j upurl furl
  size=$(stat -c%s "$f"); name="$(basename "${f%.*}").dat"
  j=$(curl -s -X POST https://ranoz.gg/api/v1/files/upload_url \
      -H "Content-Type: application/json" \
      -d "{\"filename\":\"$name\",\"size\":$size}")
  upurl=$(echo "$j" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["upload_url"])' 2>/dev/null)
  furl=$(echo "$j" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["url"])' 2>/dev/null)
  [ -z "$upurl" ] && { err "Ranoz presigned failed"; return 1; }
  curl -s -X PUT "$upurl" --upload-file "$f" -H "Content-Length: $size" -o /dev/null 2>/dev/null
  echo "$furl"
}

# --- Tempshare upload -> stdout: url ---
upload_tempshare(){
  local f="$1" url
  url=$(curl -s -X POST -F "file=@$f" -F "duration=7" https://api.tempshare.su/upload \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["url"])' 2>/dev/null)
  [ -z "$url" ] && { err "Tempshare failed"; return 1; }
  echo "$url"
}

# --- split a file into <=maxbyte valid mp4 parts (copy) and upload each ---
upload_split(){
  local f="$1" max="$2" dest="$3" prefix="$4" upfn="$5"
  local pdir="$WORK/$dest"; mkdir -p "$pdir"
  split_segments "$f" "$max" "$pdir/$prefix"
  local p outurl
  for p in "$pdir"/${prefix}*; do
    [ -e "$p" ] || continue
    outurl=$($upfn "$p")
    [ -n "$outurl" ] && echo "$outurl"
  done
}

# --- compress all raw segments ---
mkdir -p "$WORK/seg" "$WORK/comp"
split_segments "$RAW" "$EZGIF_MAX" "$WORK/seg/s_%03d.mp4"
segs=("$WORK"/seg/s_*.mp4)
log "Raw segments: ${#segs[@]}"

COMBINED="$WORK/list.txt"; : > "$COMBINED"
ok=0
for s in "${segs[@]}"; do
  [ -e "$s" ] || continue
  c="$WORK/comp/$(basename "$s")"
  if ezgif_compress "$s" "$c"; then
    echo "file '$c'" >> "$COMBINED"; ok=$((ok+1))
  else
    err "using raw $s (ezgif failed)"
    echo "file '$s'" >> "$COMBINED"; ok=$((ok+1))
  fi
done
[ "$ok" -eq 0 ] && { err "no segments produced"; exit 1; }

# --- concat compressed segments (copy; fallback re-encode) ---
FINAL="$WORK/final.mp4"
if ! ffmpeg -nostdin -hide_banner -loglevel error -f concat -safe 0 -i "$COMBINED" -c copy "$FINAL" 2>/dev/null; then
  log "concat copy failed, re-encoding"
  ffmpeg -nostdin -hide_banner -loglevel error -f concat -safe 0 -i "$COMBINED" -c:v libx264 -c:a aac "$FINAL" 2>/dev/null
fi
[ -f "$FINAL" ] || { err "concat produced no file"; exit 1; }
FSIZE=$(stat -c%s "$FINAL")
log "Final: $FINAL ($FSIZE bytes) from $ok segments"

# --- upload to both file hosters (split by their limits) ---
echo "" >> "$LOGFILE"
echo "$(date '+%d.%m.%Y %H:%M:%S') | $(basename "$FINAL")" >> "$LOGFILE"

ranoz_urls=(); tempshare_urls=()
if [ "$FSIZE" -le "$RZ_MAX" ]; then
  u=$(upload_ranoz "$FINAL"); [ -n "$u" ] && ranoz_urls+=("$u")
else
  while read -r u; do [ -n "$u" ] && ranoz_urls+=("$u"); done < <(upload_split "$FINAL" "$RZ_MAX" "rz" "rz_%03d.mp4" upload_ranoz)
fi
if [ "$FSIZE" -le "$TS_MAX" ]; then
  u=$(upload_tempshare "$FINAL"); [ -n "$u" ] && tempshare_urls+=("$u")
else
  while read -r u; do [ -n "$u" ] && tempshare_urls+=("$u"); done < <(upload_split "$FINAL" "$TS_MAX" "ts" "ts_%03d.mp4" upload_tempshare)
fi

for u in "${ranoz_urls[@]}"; do
  echo "  Ranoz: <a href='$u' target='_blank'>$u</a>" >> "$LOGFILE"
  log "Ranoz: $u"
done
for u in "${tempshare_urls[@]}"; do
  echo "  Tempshare: <a href='$u' target='_blank'>$u</a>" >> "$LOGFILE"
  log "Tempshare: $u"
done

# cleanup workdir (keep records dir)
rm -rf "$WORK"
log "Done."
EOF

# Создаем Flask-приложение
cat <<'EOF' > $APP_DIR/app.py
from flask import Flask, request, render_template, redirect, url_for
import subprocess, os, sys

app = Flask(__name__)
APP_DIR = "/opt/stream-recorder"
LOG_FILE = os.path.join(APP_DIR, "records", "links.log")
SCRIPT_PATH = os.path.join(APP_DIR, "record_and_upload.sh")

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        stream_url = request.form.get('url')
        if stream_url:
            print(f"Received request to record URL: {stream_url}", file=sys.stderr)
            command = f"nohup {SCRIPT_PATH} '{stream_url}' &"
            subprocess.Popen(command, shell=True)
            return redirect(url_for('index'))
    recent_links = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r') as f:
            recent_links = [line.strip() for line in f.readlines()]
            recent_links.reverse()
    return render_template('index.html', links=recent_links)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
EOF

# Создаем HTML-шаблон (без изменений)
cat <<'EOF' > $APP_DIR/templates/index.html
<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><title>Запись стримов</title><style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;max-width:800px;margin:40px auto;padding:20px;background-color:#f8f9fa;color:#333}h1,h2{color:#0056b3}.container{background-color:#fff;padding:30px;border-radius:8px;box-shadow:0 4px 8px rgba(0,0,0,.1)}form{display:flex;gap:10px;margin-bottom:30px}input[type=url]{flex-grow:1;padding:12px;border:1px solid #ccc;border-radius:4px;font-size:16px}input[type=submit]{padding:12px 20px;border:none;background-color:#28a745;color:#fff;border-radius:4px;font-size:16px;cursor:pointer;transition:background-color .2s}input[type=submit]:hover{background-color:#218838}.recent-links{list-style:none;padding:0}.recent-links li{background-color:#e9ecef;border:1px solid #dee2e6;padding:15px;margin-bottom:10px;border-radius:4px;word-wrap:break-word}.recent-links a{color:#0056b3;text-decoration:none}.recent-links a:hover{text-decoration:underline}</style></head><body><div class="container"><h1>Сервис записи стримов</h1><form action="/" method="post"><input type="url" name="url" placeholder="Вставьте ссылку на .flv или .m3u8 стрим" required><input type="submit" value="Начать запись"></form><h2>Недавние записи</h2>{% if links %}<ul class="recent-links">{% for link in links %}<li>{{ link|safe }}</li>{% endfor %}</ul>{% else %}<p>Здесь будут отображаться ссылки на скачивание записанных стримов.</p>{% endif %}</div></body></html>
EOF

echo "✅ Файлы созданы."

# --- ШАГ 4: НАСТРОЙКА ПРАВ И СЕРВИСА ---
echo "⚙️  (6/7) Настройка прав доступа и создание systemd сервиса..."
# Выставляем права. Теперь это домашняя директория пользователя, проблем быть не должно.
chown -R streamrecorder:streamrecorder $APP_DIR
chmod +x $APP_DIR/record_and_upload.sh

# Создаем сервис
cat <<EOF > /etc/systemd/system/stream-recorder.service
[Unit]
Description=Stream Recorder Service
After=network.target

[Service]
User=streamrecorder
Group=streamrecorder
WorkingDirectory=$APP_DIR
# Важно: запускаем python через /usr/bin/env для надежности
ExecStart=/usr/bin/python3 $APP_DIR/app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo "✅ Права и сервис настроены."

# --- ШАГ 5: ЗАПУСК ---
echo "⚙️  (7/7) Запуск и проверка сервиса..."
systemctl daemon-reload
systemctl enable stream-recorder.service
systemctl start stream-recorder.service
sleep 2 # Даем сервису время на запуск

IP_ADDRESS=$(hostname -I | awk '{print $1}')
echo ""
if systemctl is-active --quiet stream-recorder.service; then
    echo "🎉🎉🎉 ВСЁ! УСТАНОВКА ЗАВЕРШЕНА! 🎉🎉🎉"
    echo ""
    echo "Сервис работает. Откройте в браузере: http://$IP_ADDRESS:5000"
    echo ""
    echo "‼️ ВАЖНО: Если что-то не так, смотрите логи командой:"
    echo "   journalctl -u stream-recorder -f"
else
    echo "❌❌❌ ОШИБКА: Сервис не смог запуститься. Смотрите причину командой:"
    echo "   journalctl -u stream-recorder --no-pager"
fi
