"""Compress mp4 segments via ezgif.com/video-compressor (no API key)."""
from __future__ import annotations
import asyncio
import logging
import re
import shutil
from pathlib import Path

import aiohttp
import aiofiles
from bs4 import BeautifulSoup

from . import config

log = logging.getLogger("argus.compress")

EZGIF_BASE = "https://ezgif.com"
UPLOAD_URL = f"{EZGIF_BASE}/video-compressor"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 ARGUS-Bot/2.0"

_FILE_ID_RE = re.compile(r"ezgif-[a-f0-9]{16}")


async def _upload(session: aiohttp.ClientSession,
                  path: Path) -> tuple[str, str, str]:
    """Загрузить видео на ezgif. Возвращает (file_field, file_id, result_url).
    `file_field` — то, что нужно отдать как form-поле `file` на шаге recompress
    (например, 'ezgif-449eed3c0db00cfd.mp4').
    `file_id` — только идентификатор без расширения (для отличения от
    сжатого результата позже).
    """
    form = aiohttp.FormData()
    fh = open(path, "rb")
    try:
        form.add_field("new-image", fh, filename=path.name,
                       content_type="video/mp4")
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
    inp = soup.find("input", {"name": "file"})
    file_field = (inp.get("value") if inp and inp.get("value")
                  else f"{file_id}.mp4")
    return file_field, file_id, result_url


async def _recompress(session: aiohttp.ClientSession, *,
                      file_field: str, original_id: str,
                      result_url: str, resolution: str, bitrate: str) -> str:
    """POST параметры сжатия, вернуть имя файла для /save/ (например
    'ezgif-4a4c70b2811c0c40.mp4').

    ВАЖНО: страница результата содержит ДВЕ /save/ ссылки — на оригинал
    (тот же id что мы загрузили) и на сжатый файл (новый id). Раньше код
    брал первую попавшуюся → скачивал назад оригинал → пользователь
    получал "После сжатия = Запись". Теперь явно исключаем `original_id`.
    """
    form = aiohttp.FormData()
    form.add_field("file", file_field)
    form.add_field("resolution", resolution)
    form.add_field("bitrate", bitrate)
    form.add_field("format", config.EZGIF_FORMAT)
    form.add_field("video-compressor", "Recompress video!")
    headers = {"Referer": result_url, "User-Agent": UA}
    action = result_url.rstrip("/")
    if action.endswith(".html"):
        action = action[:-5]
    async with session.post(action, data=form, headers=headers,
                            allow_redirects=True,
                            timeout=aiohttp.ClientTimeout(total=1800)) as r:
        text = await r.text()

    soup = BeautifulSoup(text, "html.parser")
    candidates: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/save/" not in href:
            continue
        tail = href.split("/save/")[-1].split("?")[0].strip("/")
        if not tail:
            continue
        candidates.append(tail)

    if not candidates:
        # fallback — посмотреть на <source src=...> внутри <video>
        for s in soup.find_all("source"):
            src = s.get("src", "")
            m = _FILE_ID_RE.search(src)
            if m and m.group(0) != original_id:
                ext = src.rsplit(".", 1)[-1] or "mp4"
                return f"{m.group(0)}.{ext}"
        raise RuntimeError("ezgif: cannot find compressed save link on result page")

    # ищем ссылку с НЕ-original id
    for tail in candidates:
        m = _FILE_ID_RE.search(tail)
        if m and m.group(0) != original_id:
            return tail

    # если все ссылки указывают на тот же id — компрессия не отдала новый файл
    raise RuntimeError(
        f"ezgif: only original /save/ links present ({candidates[:2]}) — "
        f"recompress, видимо, не дал результата"
    )


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
                       dst: Path, resolution: str, bitrate: str) -> Path:
    """Upload → recompress → download. На неудаче кидает RuntimeError —
    caller (compress_segments) сделает fallback на оригинал."""
    file_field, original_id, result_url = await _upload(session, src)
    save_name = await _recompress(
        session,
        file_field=file_field, original_id=original_id,
        result_url=result_url, resolution=resolution, bitrate=bitrate,
    )
    await _download_save(session, save_name, dst, result_url)
    if not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError("downloaded compressed file is empty")
    return dst


async def compress_segments(segments: list[Path], out_dir: Path,
                            resolution: str, bitrate: str,
                            on_progress=None) -> list[Path]:
    """Сжать каждый сегмент параллельно (ограничено EZGIF_PARALLEL).
    Если сегмент не сжался — оставляем оригинал, чтобы не терять весь стрим.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(max(1, config.EZGIF_PARALLEL))
    results: list[Path] = [None] * len(segments)  # type: ignore
    failures = 0
    done = {"n": 0}
    lock = asyncio.Lock()

    async with aiohttp.ClientSession() as session:
        async def worker(i: int, p: Path):
            nonlocal failures
            async with sem:
                target = out_dir / f"cz_{i:03d}.{config.EZGIF_FORMAT}"
                try:
                    await compress_one(session, p, target, resolution, bitrate)
                    results[i] = target
                except Exception as e:  # noqa: BLE001
                    log.warning("ezgif compress segment %d failed: %s — "
                                "keeping original", i, e)
                    fb = out_dir / f"cz_{i:03d}{p.suffix or '.mp4'}"
                    shutil.copy2(p, fb)
                    results[i] = fb
                    async with lock:
                        failures += 1
                async with lock:
                    done["n"] += 1
                    done_n = done["n"]
                if on_progress:
                    try:
                        await on_progress(done_n, len(segments))
                    except Exception:  # noqa: BLE001
                        pass

        await asyncio.gather(*(worker(i, p) for i, p in enumerate(segments)))

    if failures:
        log.warning("compress_segments: %d/%d сегментов не сжалось "
                    "(использован оригинал)", failures, len(segments))
    return results
