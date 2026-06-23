"""Compress mp4 segments via ezgif.com/video-compressor (no API key)."""
from __future__ import annotations
import asyncio
import re
from pathlib import Path

import aiohttp
import aiofiles
from bs4 import BeautifulSoup

from . import config

EZGIF_BASE = "https://ezgif.com"
UPLOAD_URL = f"{EZGIF_BASE}/video-compressor"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 ARGUS-Bot/2.0"

_FILE_ID_RE = re.compile(r"ezgif-[a-f0-9]{16}")


async def _upload(session: aiohttp.ClientSession, path: Path) -> tuple[str, str]:
    """Upload a video to ezgif. Returns (file_id_with_ext, result_page_url)."""
    form = aiohttp.FormData()
    fh = open(path, "rb")
    try:
        form.add_field("new-image", fh, filename=path.name, content_type="video/mp4")
        form.add_field("upload", "Upload video!")
        headers = {"Referer": UPLOAD_URL, "User-Agent": UA}
        async with session.post(UPLOAD_URL, data=form, headers=headers,
                                allow_redirects=True,
                                timeout=aiohttp.ClientTimeout(total=900)) as r:
            text = await r.text()
            result_url = str(r.url)
    finally:
        fh.close()
    m = _FILE_ID_RE.search(result_url) or _FILE_ID_RE.search(text)
    if not m:
        raise RuntimeError("ezgif: upload did not return a file id")
    file_id = m.group(0)
    soup = BeautifulSoup(text, "html.parser")
    # find the hidden 'file' input which contains 'ezgif-xxx.mp4'
    fname = file_id + ".mp4"
    inp = soup.find("input", {"name": "file"})
    if inp and inp.get("value"):
        fname = inp["value"]
    return fname, result_url


async def _recompress(session: aiohttp.ClientSession, file_field: str,
                      result_url: str) -> str:
    """POST recompress params; return the compressed file_id (with extension)."""
    form_post = aiohttp.FormData()
    form_post.add_field("file", file_field)
    form_post.add_field("resolution", config.EZGIF_RES)
    form_post.add_field("bitrate", config.EZGIF_BITRATE)
    form_post.add_field("format", config.EZGIF_FORMAT)
    form_post.add_field("video-compressor", "Recompress video!")
    headers = {"Referer": result_url, "User-Agent": UA}
    # action URL is the result page without .html
    action = result_url.rstrip("/")
    if action.endswith(".html"):
        action = action[:-5]
    async with session.post(action, data=form_post, headers=headers,
                            allow_redirects=True,
                            timeout=aiohttp.ClientTimeout(total=1800)) as r:
        text = await r.text()
    # The result page contains a link to /save/ezgif-NEWID.<ext>
    soup = BeautifulSoup(text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/save/" in href:
            # strip query, get last segment
            tail = href.split("/save/")[-1].split("?")[0]
            if tail:
                return tail
    raise RuntimeError("ezgif: cannot find compressed save link")


async def _download_save(session: aiohttp.ClientSession, save_name: str,
                         out_path: Path, referer: str) -> None:
    url = f"{EZGIF_BASE}/save/{save_name}"
    headers = {"Referer": referer, "User-Agent": UA}
    async with session.get(url, headers=headers,
                           timeout=aiohttp.ClientTimeout(total=1800)) as r:
        r.raise_for_status()
        async with aiofiles.open(out_path, "wb") as f:
            async for chunk in r.content.iter_chunked(1024 * 1024):
                await f.write(chunk)


async def compress_one(session: aiohttp.ClientSession, src: Path,
                       dst: Path) -> Path:
    """Upload → recompress → download. Falls back to copying src on failure."""
    try:
        file_field, result_url = await _upload(session, src)
        save_name = await _recompress(session, file_field, result_url)
        await _download_save(session, save_name, dst, result_url)
        if not dst.exists() or dst.stat().st_size == 0:
            raise RuntimeError("downloaded compressed file is empty")
        return dst
    except Exception as e:
        # Fallback: keep original (don't break the pipeline)
        if src.resolve() != dst.resolve():
            import shutil
            shutil.copy2(src, dst)
        raise RuntimeError(f"ezgif compress failed for {src.name}: {e}") from e


async def compress_segments(segments: list[Path], out_dir: Path,
                            on_progress=None) -> list[Path]:
    """Compress each segment via ezgif in parallel (bounded). Returns new paths.
    If a segment fails, the original is kept (so pipeline still completes).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(config.EZGIF_PARALLEL)
    results: list[Path] = [None] * len(segments)  # type: ignore

    async with aiohttp.ClientSession() as session:
        async def worker(i: int, p: Path):
            async with sem:
                target = out_dir / f"cz_{i:03d}.{config.EZGIF_FORMAT}"
                try:
                    await compress_one(session, p, target)
                    results[i] = target
                except Exception:
                    # fallback to original
                    import shutil
                    fb = out_dir / f"cz_{i:03d}{p.suffix}"
                    shutil.copy2(p, fb)
                    results[i] = fb
                if on_progress:
                    try:
                        await on_progress(i + 1, len(segments))
                    except Exception:
                        pass

        await asyncio.gather(*(worker(i, p) for i, p in enumerate(segments)))
    return results
