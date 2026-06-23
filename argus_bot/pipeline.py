"""End-to-end pipeline: record stream → split → compress → reassemble → upload."""
from __future__ import annotations
import asyncio
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Awaitable

import aiohttp

from . import config, ffwrap, compressor, uploader


@dataclass
class JobResult:
    ok: bool
    error: str = ""
    raw_size: int = 0
    final_size: int = 0
    duration_sec: float = 0.0
    links: dict[str, list[str]] = field(default_factory=dict)
    elapsed: float = 0.0


ProgressCb = Callable[[str], Awaitable[None]]


async def _maybe(cb: ProgressCb | None, msg: str) -> None:
    if cb is None:
        return
    try:
        await cb(msg)
    except Exception:
        pass


def _human(n: int) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


async def run_pipeline(stream_url: str, hosters: List[str],
                       progress: ProgressCb | None = None) -> JobResult:
    """Record `stream_url`, compress everything, upload to each hoster in `hosters`.

    `hosters`: subset of {'ranoz','tempshare'}.
    """
    started = time.time()
    job_id = f"job_{int(started)}_{abs(hash(stream_url)) % 10000:04d}"
    work = config.WORK_DIR / job_id
    work.mkdir(parents=True, exist_ok=True)
    raw = work / "raw.mp4"
    log = work / "ytdlp.log"

    res = JobResult(ok=False)
    try:
        await _maybe(progress, "▶️ Запись стрима началась (yt-dlp)...")
        await ffwrap.yt_dlp_record(stream_url, raw, log)
        res.raw_size = raw.stat().st_size
        res.duration_sec = await ffwrap.probe_duration(raw)
        await _maybe(progress,
                     f"✅ Запись завершена: {_human(res.raw_size)}, "
                     f"длит. {int(res.duration_sec)}s. Режу на куски ≤199MB...")

        # Split into ezgif-friendly segments (<=199MB each)
        seg_dir = work / "segs"
        seg_dir.mkdir(exist_ok=True)
        segs = await ffwrap.split_copy(raw, config.EZGIF_SEG_MAX,
                                       seg_dir / "seg_%03d.mp4")
        await _maybe(progress, f"📦 Сегментов: {len(segs)}. Сжимаю через ezgif...")

        async def ez_progress(done: int, total: int):
            await _maybe(progress, f"🗜 ezgif: {done}/{total} сжато")

        cz_dir = work / "cz"
        compressed = await compressor.compress_segments(segs, cz_dir, ez_progress)

        await _maybe(progress, "🧩 Склеиваю сжатые части в один файл...")
        merged = work / "merged.mp4"
        await ffwrap.concat_copy(compressed, merged)
        res.final_size = merged.stat().st_size
        await _maybe(progress, f"✅ Готовый файл: {_human(res.final_size)}")

        # Per-hoster: split to fit limit, upload all parts
        async with aiohttp.ClientSession() as session:
            for h in hosters:
                if h not in uploader.HOSTERS:
                    continue
                limit, fn, label = uploader.HOSTERS[h]
                await _maybe(progress, f"☁️ Загрузка в {label} (лимит {_human(limit)})...")
                parts_dir = work / f"parts_{h}"
                parts_dir.mkdir(exist_ok=True)
                if res.final_size <= limit:
                    parts = [merged]
                else:
                    parts = await ffwrap.split_copy(
                        merged, limit, parts_dir / f"{h}_%03d.mp4"
                    )
                links = await uploader.upload_many(session, parts, fn)
                res.links[label] = links
                await _maybe(progress, f"✅ {label}: {len(links)} ссыл(ка/ки) готов(а/ы)")

        res.ok = True
    except Exception as e:
        res.ok = False
        res.error = str(e)
        await _maybe(progress, f"❌ Ошибка: {e}")
    finally:
        res.elapsed = time.time() - started
        # Cleanup work dir (keep on error for debug)
        if res.ok:
            shutil.rmtree(work, ignore_errors=True)

    return res
