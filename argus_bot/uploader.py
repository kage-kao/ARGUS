"""Upload to Ranoz (ranoz.gg) and Tempshare (tempshare.su)."""
from __future__ import annotations
import asyncio
from pathlib import Path

import aiohttp
import aiofiles

from . import config


async def upload_ranoz(session: aiohttp.ClientSession, path: Path) -> str:
    """Two-step upload to Ranoz. Returns public file URL.
    Files are uploaded with .dat extension (ranoz blocks video extensions)."""
    size = path.stat().st_size
    name = path.stem + ".dat"
    # 1) presigned URL
    async with session.post(
        "https://ranoz.gg/api/v1/files/upload_url",
        json={"filename": name, "size": size},
        timeout=aiohttp.ClientTimeout(total=60),
    ) as r:
        data = await r.json()
    upload_url = data.get("data", {}).get("upload_url")
    file_url = data.get("data", {}).get("url")
    if not upload_url or not file_url:
        raise RuntimeError(f"ranoz: bad presign response: {data}")

    # 2) PUT body
    async def file_sender():
        async with aiofiles.open(path, "rb") as f:
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk

    headers = {"Content-Length": str(size), "Content-Type": "application/octet-stream"}
    async with session.put(upload_url, data=file_sender(), headers=headers,
                           timeout=aiohttp.ClientTimeout(total=None, sock_connect=60,
                                                         sock_read=1800)) as r:
        if r.status >= 400:
            body = await r.text()
            raise RuntimeError(f"ranoz: PUT {r.status} — {body[:200]}")
    return file_url


async def upload_tempshare(session: aiohttp.ClientSession, path: Path,
                           duration_days: int = 7) -> str:
    """Upload to Tempshare. Returns public URL."""
    form = aiohttp.FormData()
    fh = open(path, "rb")
    try:
        form.add_field("file", fh, filename=path.name,
                       content_type="application/octet-stream")
        form.add_field("duration", str(duration_days))
        async with session.post(
            "https://api.tempshare.su/upload",
            data=form,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=1800),
        ) as r:
            data = await r.json(content_type=None)
    finally:
        fh.close()
    if not data.get("success") or not data.get("url"):
        raise RuntimeError(f"tempshare: bad response: {data}")
    return data["url"]


HOSTERS = {
    "ranoz":     (config.RANOZ_MAX,     upload_ranoz,     "Ranoz"),
    "tempshare": (config.TEMPSHARE_MAX, upload_tempshare, "Tempshare"),
}


async def upload_many(session: aiohttp.ClientSession, paths: list[Path],
                      uploader_fn, **kwargs) -> list[str]:
    sem = asyncio.Semaphore(config.UPLOAD_PARALLEL)
    results: list[str] = [None] * len(paths)  # type: ignore

    async def worker(i: int, p: Path):
        async with sem:
            results[i] = await uploader_fn(session, p, **kwargs)

    await asyncio.gather(*(worker(i, p) for i, p in enumerate(paths)))
    return results
