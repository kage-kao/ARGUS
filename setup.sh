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

# Создаем скрипт записи. Обратите внимание: все `echo` теперь будут видны в системном логе.
cat <<'EOF' > $APP_DIR/record_and_upload.sh
#!/bin/bash
STREAM_URL="$1"
RECORDS_DIR="/opt/stream-recorder/records"
FILENAME="$RECORDS_DIR/stream_$(date +%Y-%m-%d_%H-%M-%S).mp4"
LOGFILE="$RECORDS_DIR/links.log"

echo "INFO: Script started for URL: $STREAM_URL"

/usr/bin/yt-dlp --user-agent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36' --no-warnings -o "$FILENAME" "$STREAM_URL"

if [ -f "$FILENAME" ]; then
    echo "INFO: Recording finished successfully. Uploading..."
    UPLOAD_URL=$(curl -H "Max-Days: 7" --upload-file "$FILENAME" "https://wgetz.com/$(basename "$FILENAME")")
    echo "INFO: Upload complete. URL: $UPLOAD_URL"
    echo "$(date '+%d.%m.%Y %H:%M:%S') | <a href='${UPLOAD_URL}' target='_blank'>${UPLOAD_URL}</a>" >> "$LOGFILE"
    rm "$FILENAME"
else
    echo "ERROR: Recording failed. yt-dlp did not create a file. Check previous log entries for errors from yt-dlp."
fi
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
