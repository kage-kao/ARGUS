"""End-to-end pipeline: record stream → split → compress → reassemble → upload.

Each call gets a `ProcRegistry` so /cancel can SIGTERM all live subprocesses.
Job status is persisted to SQLite at every stage transition.
"""
from __future__ import annotations
import asyncio
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Awaitable

import aiohttp

from . import config, ffwrap, compressor, uploader, storage


@dataclass
class JobResult:
    ok: bool
    error: str = ""
    raw_size: int = 0
    final_size: int = 0
    duration_sec: float = 0.0
    links: dict[str, list[str]] = field(default_factory=dict)
    elapsed: float = 0.0
    cancelled: bool = False


ProgressCb = Callable[[str], Awaitable[None]]


async def _maybe(cb: ProgressCb | None, msg: str) -> None:
    if cb is None:
        return
    try:
        await cb(msg)
    except Exception:
        pass


def _human(n: float) -> str:
    f = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024:
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} PB"


async def run_pipeline(job_id: str, stream_url: str, hosters: List[str],
                       quality: str, ts_duration: int,
                       registry: ffwrap.ProcRegistry,
                       progress: ProgressCb | None = None) -> JobResult:
    """Record `stream_url`, compress everything, upload to each hoster.
    `quality` is a key from config.QUALITY_PRESETS.
    `ts_duration` is days for tempshare (1/3/7).
    """
    started = time.time()
    work = config.WORK_DIR / job_id
    work.mkdir(parents=True, exist_ok=True)
    raw = work / "raw.mp4"
    log = work / "ytdlp.log"

    res = JobResult(ok=False)
    try:
        # ---------- Recording ----------
        storage.update(job_id, status="recording", progress="recording")
        await _maybe(progress, "▶️ Запись стрима началась...")
        await ffwrap.record_stream(stream_url, raw, log, registry=registry)

        if registry.cancelled:
            raise asyncio.CancelledError()

        res.raw_size = raw.stat().st_size
        res.duration_sec = await ffwrap.probe_duration(raw)

        # ---------- Splitting ----------
        storage.update(job_id, status="processing",
                       progress=f"recorded {_human(res.raw_size)}")
        await _maybe(progress,
                     f"✅ Запись завершена: {_human(res.raw_size)}, "
                     f"длит. {int(res.duration_sec)}s. Режу на куски ≤199MB...")

        seg_dir = work / "segs"
        seg_dir.mkdir(exist_ok=True)
        segs = await ffwrap.split_copy(raw, config.EZGIF_SEG_MAX,
                                       seg_dir / "seg_%03d.mp4",
                                       registry=registry)
        if registry.cancelled:
            raise asyncio.CancelledError()

        # ---------- Compression ----------
        resolution, bitrate, q_label = config.QUALITY_PRESETS[quality]
        await _maybe(progress,
                     f"📦 Сегментов: {len(segs)}. Сжимаю через ezgif "
                     f"({resolution} · {bitrate}kbps)...")

        async def ez_progress(done: int, total: int):
            storage.update(job_id, progress=f"ezgif {done}/{total}")
            await _maybe(progress, f"🗜 ezgif: {done}/{total} сжато "
                                   f"({resolution} · {bitrate}kbps)")

        cz_dir = work / "cz"
        compressed = await compressor.compress_segments(
            segs, cz_dir, resolution, bitrate, ez_progress
        )
        if registry.cancelled:
            raise asyncio.CancelledError()

        # ---------- Concat ----------
        await _maybe(progress, "🧩 Склеиваю сжатые части в один файл...")
        merged = work / "merged.mp4"
        await ffwrap.concat_copy(compressed, merged, registry=registry)
        res.final_size = merged.stat().st_size
        await _maybe(progress, f"✅ Готовый файл: {_human(res.final_size)}")
        if registry.cancelled:
            raise asyncio.CancelledError()

        # ---------- Upload ----------
        storage.update(job_id, status="uploading", progress="uploading")
        async with aiohttp.ClientSession() as session:
            for h in hosters:
                if h not in uploader.HOSTERS:
                    continue
                limit, fn, label = uploader.HOSTERS[h]
                await _maybe(progress,
                             f"☁️ Загрузка в {label} (лимит {_human(limit)})...")
                parts_dir = work / f"parts_{h}"
                parts_dir.mkdir(exist_ok=True)
                if res.final_size <= limit:
                    parts = [merged]
                else:
                    parts = await ffwrap.split_copy(
                        merged, limit, parts_dir / f"{h}_%03d.mp4",
                        registry=registry,
                    )
                if registry.cancelled:
                    raise asyncio.CancelledError()
                kwargs = {"duration_days": ts_duration} if h == "tempshare" else {}
                links = await uploader.upload_many(session, parts, fn, **kwargs)
                res.links[label] = links
                storage.update(job_id, links=res.links)
                await _maybe(progress, f"✅ {label}: {len(links)} ссыл(ка/ки) готов(а/ы)")

        res.ok = True
        storage.update(job_id, status="done", progress="done", links=res.links)

    except asyncio.CancelledError:
        res.cancelled = True
        res.error = "cancelled by user"
        storage.update(job_id, status="cancelled", error=res.error)
        await _maybe(progress, "🛑 Отменено пользователем.")
    except Exception as e:
        res.ok = False
        res.error = str(e)
        storage.update(job_id, status="failed", error=res.error)
        await _maybe(progress, f"❌ Ошибка: {e}")
    finally:
        res.elapsed = time.time() - started
        # Always nuke the work dir — success, cancel, OR failure (so that a
        # crashed recording does not eat the disk forever). The actual error
        # is already persisted in the DB via storage.update(status='failed').
        # On failure, preserve the tail of ytdlp.log (≤4 KB) in a central
        # /errors/ folder for postmortem — costs almost nothing.
        if not res.ok and not res.cancelled and log.exists():
            try:
                err_dir = config.WORK_DIR.parent / "errors"
                err_dir.mkdir(parents=True, exist_ok=True)
                tail = log.read_bytes()[-4096:]
                (err_dir / f"{job_id}.log").write_bytes(tail)
            except OSError:
                pass
        shutil.rmtree(work, ignore_errors=True)

    return res
