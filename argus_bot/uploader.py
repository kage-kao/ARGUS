"""Upload to Ranoz (ranoz.gg), Tempshare (tempshare.su) and Gofile (gofile.io)."""
from __future__ import annotations
import asyncio
import os
import time
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
        # ranoz сейчас отдаёт корректный JSON, но с mimetype text/plain —
        # aiohttp по умолчанию на это ругается, отключаем строгую проверку.
        data = await r.json(content_type=None)
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


async def _gofile_fetch_servers(session: aiohttp.ClientSession) -> list[dict]:
    """Список серверов Gofile с зонами: [{'name': 'store1', 'zone': 'eu'}, ...]."""
    async with session.get("https://api.gofile.io/servers",
                           timeout=aiohttp.ClientTimeout(total=30)) as r:
        data = await r.json(content_type=None)
    return (data.get("data") or {}).get("servers") or []


async def _gofile_latency(session: aiohttp.ClientSession, name: str) -> float:
    """Время отклика конкретного сервера Gofile (сек), inf при ошибке."""
    t0 = time.monotonic()
    try:
        async with session.get(f"https://{name}.gofile.io/",
                               timeout=aiohttp.ClientTimeout(total=6)) as r:
            await r.read()
        return time.monotonic() - t0
    except Exception:
        return float("inf")


# Кэш выбранного сервера на процесс (TTL), чтобы не замерять перед каждой заливкой.
_GOFILE_CACHE: dict[str, object] = {"name": None, "ts": 0.0}
_GOFILE_TTL = int(os.environ.get("GOFILE_CACHE_TTL", "600"))
_GOFILE_LOCK = asyncio.Lock()


async def _gofile_pick_server(session: aiohttp.ClientSession) -> str:
    """Авто-выбор самого быстрого сервера Gofile по зоне.

    Логика:
      1. Если задан env GOFILE_ZONE (eu/na/ap/...), берём сервер из этой зоны.
      2. Иначе замеряем задержку по одному представителю каждой зоны и
         выбираем сервер из самой быстрой зоны.
      3. Результат кэшируется на GOFILE_CACHE_TTL секунд.
    Есть fallback на legacy /getServer, если /servers недоступен.
    """
    now = time.monotonic()
    cached = _GOFILE_CACHE.get("name")
    if cached and (now - float(_GOFILE_CACHE.get("ts", 0.0))) < _GOFILE_TTL:
        return str(cached)

    async with _GOFILE_LOCK:
        # повторная проверка кэша после захвата лока
        now = time.monotonic()
        cached = _GOFILE_CACHE.get("name")
        if cached and (now - float(_GOFILE_CACHE.get("ts", 0.0))) < _GOFILE_TTL:
            return str(cached)

        servers: list[dict] = []
        try:
            servers = await _gofile_fetch_servers(session)
        except Exception:
            servers = []

        chosen: str | None = None
        if servers:
            # Группируем по зонам.
            by_zone: dict[str, list[str]] = {}
            for s in servers:
                name = s.get("name")
                if name:
                    by_zone.setdefault(s.get("zone") or "?", []).append(name)

            pref = (os.environ.get("GOFILE_ZONE") or "").strip().lower()
            if pref and pref in by_zone:
                chosen = by_zone[pref][0]
            elif len(by_zone) == 1:
                chosen = next(iter(by_zone.values()))[0]
            else:
                # Замеряем по одному представителю на зону → берём самую быструю.
                zones = list(by_zone.keys())
                reps = [by_zone[z][0] for z in zones]
                latencies = await asyncio.gather(
                    *(_gofile_latency(session, name) for name in reps)
                )
                best_i = min(range(len(zones)), key=lambda i: latencies[i])
                if latencies[best_i] == float("inf"):
                    chosen = reps[0]  # все недоступны — берём первый
                else:
                    chosen = by_zone[zones[best_i]][0]

        if not chosen:
            # Legacy fallback: /getServer -> {"data": "store1"} | {"data": {"server": ...}}
            async with session.get("https://api.gofile.io/getServer",
                                   timeout=aiohttp.ClientTimeout(total=30)) as r:
                data = await r.json(content_type=None)
            d = data.get("data")
            chosen = d.get("server") if isinstance(d, dict) else d
        if not chosen:
            raise RuntimeError("gofile: no server available")

        _GOFILE_CACHE["name"] = chosen
        _GOFILE_CACHE["ts"] = time.monotonic()
        return chosen


async def upload_gofile(session: aiohttp.ClientSession, path: Path, **_) -> str:
    """Anonymous upload to Gofile. Returns public download page URL."""
    server = await _gofile_pick_server(session)
    endpoints = [
        f"https://{server}.gofile.io/contents/uploadfile",  # current API
        f"https://{server}.gofile.io/uploadFile",            # legacy API
    ]
    last: object = None
    for url in endpoints:
        form = aiohttp.FormData()
        fh = open(path, "rb")
        try:
            form.add_field("file", fh, filename=path.name,
                           content_type="application/octet-stream")
            async with session.post(
                url, data=form,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=1800),
            ) as r:
                data = await r.json(content_type=None)
        except Exception as e:  # noqa: BLE001
            last = e
            continue
        finally:
            fh.close()
        if data.get("status") == "ok":
            dd = data.get("data", {}) or {}
            link = dd.get("downloadPage") or dd.get("downloadpage")
            if link:
                return link
        last = data
    # сбрасываем кэш, чтобы следующая попытка выбрала другой сервер
    _GOFILE_CACHE["name"] = None
    raise RuntimeError(f"gofile: upload failed: {last}")


HOSTERS = {
    "ranoz":     (config.RANOZ_MAX,     upload_ranoz,     "Ranoz"),
    "tempshare": (config.TEMPSHARE_MAX, upload_tempshare, "Tempshare"),
    "gofile":    (config.GOFILE_MAX,    upload_gofile,    "Gofile"),
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
