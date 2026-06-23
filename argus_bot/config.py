"""Runtime configuration loaded from environment / .env."""
import os
from pathlib import Path
from dotenv import load_dotenv

APP_DIR = Path(os.environ.get("ARGUS_DIR", "/opt/argus-bot"))
ENV_FILE = APP_DIR / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

WORK_DIR = APP_DIR / "work"
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Hoster limits (bytes)
RANOZ_MAX = 4_990_000_000        # 4.99 GB
TEMPSHARE_MAX = 2_000_000_000    # 2.00 GB

# ezgif segment cap (client-side limit ~200MB → use 199MB for safety)
EZGIF_SEG_MAX = 199 * 1024 * 1024
EZGIF_RES = os.environ.get("EZGIF_RES", "640x360")
EZGIF_BITRATE = os.environ.get("EZGIF_BITRATE", "300")
EZGIF_FORMAT = os.environ.get("EZGIF_FORMAT", "mp4")

# Concurrency
EZGIF_PARALLEL = int(os.environ.get("EZGIF_PARALLEL", "2"))
UPLOAD_PARALLEL = int(os.environ.get("UPLOAD_PARALLEL", "2"))

TEMPSHARE_DURATION_DAYS = int(os.environ.get("TEMPSHARE_DURATION_DAYS", "7"))
