"""Runtime configuration loaded from environment / .env."""
import os
from pathlib import Path
from dotenv import load_dotenv

APP_DIR = Path(os.environ.get("ARGUS_DIR", "/opt/argus-bot"))
ENV_FILE = APP_DIR / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# BuzzCast tourist tokens (optional). Leave empty to auto-fetch a fresh token
# through SOCKS5 proxy rotation (recommended — avoids stale 429 tokens).
# Get your own: https://www.buzzcast.com -> DevTools console:
# JSON.stringify({vid: localStorage.getItem('touristToken'), did: localStorage.getItem('_did')})
BUZZCAST_VID = os.environ.get("VID", "")
BUZZCAST_DID = os.environ.get("DID", "378f9de3-0b0a-4e6d-8969-890939d9d5b6")

# SOCKS5 proxy list (rotated on HTTP 429 from BuzzCast).
PROXY_LIST_URL = os.environ.get(
    "PROXY_LIST_URL",
    "https://raw.githubusercontent.com/proxygenerator1/ProxyGenerator/main/MostStable/socks5.txt",
)

WORK_DIR = APP_DIR / "work"
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Hoster limits (bytes)
RANOZ_MAX = 4_990_000_000        # 4.99 GB
TEMPSHARE_MAX = 2_000_000_000    # 2.00 GB

# ezgif segment cap (client-side limit ~200MB → use 199MB for safety)
EZGIF_SEG_MAX = 199 * 1024 * 1024
EZGIF_FORMAT = os.environ.get("EZGIF_FORMAT", "mp4")

# Quality presets for ezgif compression. id -> (tier_p, bitrate_kbps, label)
# tier_p — это ПОТОЛОК по короткой стороне (720/480/360/240). Реальное
# разрешение ezgif подбирается под пропорции исходника в compressor.py,
# поэтому видео НЕ растягивается (никакой «моноширности»).
QUALITY_PRESETS: dict[str, tuple[int, str, str]] = {
    "high":   (720, "1500", "🎬 High — до 720p · 1500 kbps"),
    "medium": (480, "800",  "📺 Medium — до 480p · 800 kbps"),
    "low":    (360, "400",  "📱 Low — до 360p · 400 kbps"),
    "tiny":   (240, "200",  "🪶 Tiny — до 240p · 200 kbps"),
}
DEFAULT_QUALITY = os.environ.get("DEFAULT_QUALITY", "low")

# Allowed Tempshare durations (days). Bot lets user pick.
TEMPSHARE_DURATIONS = (1, 3, 7)
DEFAULT_TS_DURATION = 7

# Concurrency
EZGIF_PARALLEL = int(os.environ.get("EZGIF_PARALLEL", "2"))
UPLOAD_PARALLEL = int(os.environ.get("UPLOAD_PARALLEL", "2"))
