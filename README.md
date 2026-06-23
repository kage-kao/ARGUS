# ARGUS — Telegram бот для записи стримов.

ARGUS принимает ссылку на стрим, пишет его до самого конца через `yt-dlp`,
режет на куски ≤199 МБ, **сжимает каждый через ezgif.com/video-compressor**
(вся тяжёлая работа сжатия на стороне ezgif), склеивает обратно
(`ffmpeg -c copy`, без потерь) и заливает результат в выбранный
файлообменник.

Бот построен на **aiogram 3** (async), всё пайплайн-`I/O` асинхронное —
несколько задач одного юзера могут идти параллельно (по умолчанию до 3).

## Установка (Debian/Ubuntu/etc, под root)

```bash
curl -L https://raw.githubusercontent.com/kage-kao/ARGUS/main/setup.sh | bash
```

Скрипт:
1. Ставит `python3`, `ffmpeg`, `curl`, `ca-certificates`.
2. **Останавливается и просит ввести Telegram bot token** прямо в терминале
   (получить можно у [@BotFather](https://t.me/BotFather)).
3. Скачивает исходники из репо, ставит pip-зависимости в venv.
4. Создаёт systemd-юнит `argus-bot.service` и запускает бота.

После установки — открой бота в Telegram и пришли `/start`.

### Альтернатива: токен через env

```bash
BOT_TOKEN=123:abc... bash -c "$(curl -L https://raw.githubusercontent.com/kage-kao/ARGUS/main/setup.sh)"
```

## Использование

1. `/start` — приветствие.
2. Пришли боту ссылку на стрим (`m3u8` / `flv` / любой URL для `yt-dlp`).
3. Выбери файлообменник: **Ranoz** (4.99 ГБ), **Tempshare** (2 ГБ) или **оба сразу**.
4. Выбери **качество сжатия**: 🎬 High (720p · 1500 kbps), 📺 Medium
   (480p · 800 kbps), 📱 Low (360p · 400 kbps), 🪶 Tiny (240p · 200 kbps).
5. Если выбран Tempshare — выбери срок хранения: **1 / 3 / 7 дней**.
6. Бот пишет стрим **до конца** (это могут быть часы). По завершении пришлёт
   ссылки. Файлы больше лимита автоматически режутся и ты получаешь несколько
   ссылок.

Параллельных задач у одного пользователя сколько угодно — ограничения нет.
Состояние всех задач персистится в SQLite (`/opt/argus-bot/argus.db`),
переживает рестарт бота. Активные джобы, прерванные рестартом, помечаются
статусом `interrupted` (yt-dlp/ffmpeg-процессы потеряны и записать продолжение
невозможно — нужно запускать заново).

## Команды

| Команда | Что делает |
|---------|------------|
| `/start` | приветствие |
| `/status` | список твоих активных задач с ID и прогрессом |
| `/cancel <job_id>` | отменить задачу: SIGTERM yt-dlp/ffmpeg + cancel asyncio-task |

## Файлообменники

| Сервис | Лимит файла | Хранение | Особенность |
|--------|------------|----------|-------------|
| **Ranoz** (`ranoz.gg`) | 4.99 ГБ | бессрочно | Видео-расширения блокируются → грузим как `.dat`. После скачивания переименуй в `.mp4`. |
| **Tempshare** (`tempshare.su`) | 2 ГБ | 7 дней | Прямые ссылки, всё прозрачно. |

Лимиты обходятся автоматическим демукс-сплитом (`ffmpeg -c copy`) — без
реэнкода и без потерь качества.

## Параметры (env / `.env`)

| Переменная | Дефолт | Что значит |
|------------|--------|------------|
| `EZGIF_FORMAT` | `mp4` | формат выхода ezgif |
| `DEFAULT_QUALITY` | `low` | пресет по умолчанию (`high`/`medium`/`low`/`tiny`) |
| `EZGIF_PARALLEL` | `2` | параллельных компрессий ezgif |
| `UPLOAD_PARALLEL` | `2` | параллельных загрузок на хостер |

Качество выбирается каждый раз через инлайн-кнопки. Срок хранения Tempshare
(1/3/7 дней) — тоже.

Изменить — отредактируй `/opt/argus-bot/.env`, потом
`systemctl restart argus-bot`.

## Логи и управление

```bash
journalctl -u argus-bot -f       # живые логи
systemctl restart argus-bot      # перезапуск
systemctl stop argus-bot         # остановить
cat /opt/argus-bot/.env          # конфиг (содержит токен — не публикуй)
```

## Архитектура

```
argus_bot/
├── __main__.py     # python -m argus_bot
├── bot.py          # aiogram handlers + UI (URL → hoster → quality → duration)
├── pipeline.py     # main flow: record → split → compress → concat → upload
├── ffwrap.py       # async-обёртки yt-dlp / ffmpeg / ffprobe + ProcRegistry для cancel
├── compressor.py   # клиент ezgif.com/video-compressor
├── uploader.py     # Ranoz + Tempshare
├── storage.py      # SQLite-хранилище задач (статусы / ссылки / прогресс)
└── config.py       # env-конфиг + пресеты качества
```

## Удаление

```bash
systemctl disable --now argus-bot
rm -f /etc/systemd/system/argus-bot.service
systemctl daemon-reload
rm -rf /opt/argus-bot
userdel argusbot 2>/dev/null
```
