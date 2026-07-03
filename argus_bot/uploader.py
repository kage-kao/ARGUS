"""Upload to Ranoz (ranoz.gg), Tempshare (tempshare.su) and Gofile (gofile.io)."""
from __future__ import annotations
import asyncio
import json
import os
import time
from pathlib import Path

import aiohttp
import aiofiles

from . import config


async def _read_json(r: aiohttp.ClientResponse) -> dict:
    """Безопасно распарсить JSON-ответ.

    Раньше вызывали r.json() напрямую — при пустом/не-JSON ответе (5xx, HTML,
    rate-limit, обрыв) это давало «Expecting value: line 1 column 1 (char 0)».
    Теперь читаем текст и, если это не JSON, кидаем понятную ошибку со статусом
    и куском тела."""
    body = await r.text()
    try:
        return json.loads(body)
    except Exception:
        snippet = (body or "").strip()[:200]
        raise RuntimeError(f"HTTP {r.status}, не-JSON ответ: {snippet!r}")


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
        data = await _read_json(r)
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
            data = await _read_json(r)
    finally:
        fh.close()
    if not data.get("success") or not data.get("url"):
        raise RuntimeError(f"tempshare: bad response: {data}")
    return data["url"]


# ---------------------------------------------------------------------------
# Gofile (анонимно) — авто-выбор быстрой зоны + перебор серверов при сбое
# ---------------------------------------------------------------------------
async def _gofile_fetch_servers(session: aiohttp.ClientSession) -> list[dict]:
    """Список серверов Gofile с зонами: [{'name': 'store1', 'zone': 'eu'}, ...]."""
    async with session.get("https://api.gofile.io/servers",
                           timeout=aiohttp.ClientTimeout(total=30)) as r:
        data = await _read_json(r)
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


# Кэш упорядоченного списка серверов на процесс (TTL).
_GOFILE_CACHE: dict[str, object] = {"servers": None, "ts": 0.0}
_GOFILE_TTL = int(os.environ.get("GOFILE_CACHE_TTL", "600"))
_GOFILE_LOCK = asyncio.Lock()


async def _gofile_server_candidates(session: aiohttp.ClientSession) -> list[str]:
    """Упорядоченный список серверов: сначала самая быстрая зона, затем остальные.

    Порядок используется как список кандидатов — если аплоад на первый сервер
    сорвётся (пустой/не-JSON ответ, 5xx), пробуем следующий."""
    now = time.monotonic()
    cached = _GOFILE_CACHE.get("servers")
    if cached and (now - float(_GOFILE_CACHE.get("ts", 0.0))) < _GOFILE_TTL:
        return list(cached)  # type: ignore

    async with _GOFILE_LOCK:
        now = time.monotonic()
        cached = _GOFILE_CACHE.get("servers")
        if cached and (now - float(_GOFILE_CACHE.get("ts", 0.0))) < _GOFILE_TTL:
            return list(cached)  # type: ignore

        try:
            servers = await _gofile_fetch_servers(session)
        except Exception:
            servers = []

        ordered: list[str] = []
        if servers:
            by_zone: dict[str, list[str]] = {}
            for s in servers:
                name = s.get("name")
                if name:
                    by_zone.setdefault((s.get("zone") or "?").lower(), []).append(name)

            pref = (os.environ.get("GOFILE_ZONE") or "").strip().lower()
            zones = list(by_zone.keys())
            if pref and pref in by_zone:
                zone_order = [pref] + [z for z in zones if z != pref]
            elif len(zones) > 1:
                reps = [by_zone[z][0] for z in zones]
                lat = await asyncio.gather(
                    *(_gofile_latency(session, n) for n in reps))
                zone_order = [z for _, z in sorted(zip(lat, zones), key=lambda x: x[0])]
            else:
                zone_order = zones

            for z in zone_order:
                ordered.extend(by_zone[z])

        if ordered:
            _GOFILE_CACHE["servers"] = ordered
            _GOFILE_CACHE["ts"] = time.monotonic()
        return ordered


async def upload_gofile(session: aiohttp.ClientSession, path: Path, **_) -> str:
    """Anonymous upload to Gofile. Пробует несколько серверов (быстрая зона →
    остальные), устойчиво к пустым/не-JSON ответам. Returns download page URL."""
    candidates = await _gofile_server_candidates(session)

    # Legacy fallback, если /servers недоступен.
    if not candidates:
        try:
            async with session.get("https://api.gofile.io/getServer",
                                   timeout=aiohttp.ClientTimeout(total=30)) as r:
                data = await _read_json(r)
            d = data.get("data")
            srv = d.get("server") if isinstance(d, dict) else d
            if srv:
                candidates = [srv]
        except Exception:
            candidates = []

    if not candidates:
        raise RuntimeError("gofile: не удалось получить список серверов")

    last: object = None
    for srv in candidates[:5]:
        url = f"https://{srv}.gofile.io/contents/uploadfile"
        try:
            form = aiohttp.FormData()
            fh = open(path, "rb")
            try:
                form.add_field("file", fh, filename=path.name,
                               content_type="application/octet-stream")
                async with session.post(
                    url, data=form,
                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=60,
                                                  sock_read=1800),
                ) as r:
                    data = await _read_json(r)
            finally:
                fh.close()
        except Exception as e:  # noqa: BLE001 — пробуем следующий сервер
            last = e
            continue

        if data.get("status") == "ok":
            dd = data.get("data", {}) or {}
            link = dd.get("downloadPage") or dd.get("downloadpage")
            if link:
                return link
        last = RuntimeError(f"gofile {srv}: {str(data)[:200]}")

    # все кандидаты сорвались — сбрасываем кэш, чтобы в следующий раз перечитать
    _GOFILE_CACHE["servers"] = None
    raise RuntimeError(f"gofile: upload failed on {len(candidates[:5])} server(s): {last}")


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
