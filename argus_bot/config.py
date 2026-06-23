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
EZGIF_FORMAT = os.environ.get("EZGIF_FORMAT", "mp4")

# Quality presets for ezgif compression. id -> (resolution, bitrate_kbps, label)
QUALITY_PRESETS: dict[str, tuple[str, str, str]] = {
    "high":   ("1280x720", "1500", "🎬 High — 720p · 1500 kbps"),
    "medium": ("854x480",  "800",  "📺 Medium — 480p · 800 kbps"),
    "low":    ("640x360",  "400",  "📱 Low — 360p · 400 kbps"),
    "tiny":   ("426x240",  "200",  "🪶 Tiny — 240p · 200 kbps"),
}
DEFAULT_QUALITY = os.environ.get("DEFAULT_QUALITY", "low")

# Allowed Tempshare durations (days). Bot lets user pick.
TEMPSHARE_DURATIONS = (1, 3, 7)
DEFAULT_TS_DURATION = 7

# Concurrency
EZGIF_PARALLEL = int(os.environ.get("EZGIF_PARALLEL", "2"))
UPLOAD_PARALLEL = int(os.environ.get("UPLOAD_PARALLEL", "2"))
