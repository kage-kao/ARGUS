"""Disk-space janitor for WORK_DIR.

Three layers of protection against leftover work/ directories:

1. **Pipeline finally-block** (in pipeline.py) — every job removes its own
   work dir on completion regardless of outcome (success / failed / cancelled).
2. **Startup sweep** — `cleanup_orphans()` is called once on bot start, AFTER
   `storage.mark_stale_interrupted()`. Any subdirectory of WORK_DIR that does
   not match an *active* job id is deleted. This handles:
     - hard crashes / OOM / kill -9 / power loss
     - systemctl restart while a job was running
     - manual mess in the work folder
3. **Background loop** — `cleanup_loop()` reruns the sweep every 15 minutes
   so anything that slips through (e.g. a job that died between finally and
   storage update) is reaped within one interval.

Active = status in storage.ACTIVE_STATUSES (pending/recording/processing/uploading).
"""
from __future__ import annotations
import asyncio
import logging
import shutil
import time
from pathlib import Path

from . import config, storage

log = logging.getLogger("argus.cleanup")

CLEANUP_INTERVAL = 15 * 60  # seconds — periodic sweep cadence


def _dir_size(p: Path) -> int:
    total = 0
    try:
        for x in p.rglob("*"):
            if x.is_file():
                try:
                    total += x.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _human(n: float) -> str:
    f = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024:
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} PB"


def cleanup_orphans(work_dir: Path | None = None,
                    min_age_sec: int = 0) -> tuple[int, int]:
    """Remove every entry in WORK_DIR not belonging to an active job.

    Args:
        work_dir: override WORK_DIR (tests).
        min_age_sec: only remove items older than this (mtime). 0 = no filter.
            Used by the background loop to avoid racing fresh jobs that
            haven't yet been persisted to the DB.

    Returns:
        (entries_removed, bytes_freed)
    """
    work_dir = work_dir or config.WORK_DIR
    if not work_dir.exists():
        return 0, 0

    active_ids = {j.id for j in storage.list_all_active()}
    now = time.time()
    removed = 0
    freed = 0

    try:
        entries = list(work_dir.iterdir())
    except OSError as e:
        log.warning("cleanup: cannot list %s: %s", work_dir, e)
        return 0, 0

    for entry in entries:
        try:
            if min_age_sec and (now - entry.stat().st_mtime) < min_age_sec:
                continue
        except OSError:
            continue

        if entry.is_dir():
            if entry.name in active_ids:
                continue
            size = _dir_size(entry)
            shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                removed += 1
                freed += size
                log.info("cleanup: removed orphan dir %s (%s)",
                         entry.name, _human(size))
        else:
            # loose files at the root of work/ are never legit
            try:
                size = entry.stat().st_size
                entry.unlink()
                removed += 1
                freed += size
            except OSError:
                pass

    if removed:
        log.warning("cleanup: removed %d entrie(s), freed %s",
                    removed, _human(freed))
    return removed, freed


async def cleanup_loop(interval: int = CLEANUP_INTERVAL) -> None:
    """Background task — periodic orphan sweep."""
    log.info("cleanup loop started (interval=%ds)", interval)
    # first iteration after one interval, startup sweep already ran
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            log.info("cleanup loop cancelled")
            raise
        try:
            # Use a small grace period to avoid racing a job that has just
            # created its work dir but hasn't yet INSERTed into the DB.
            await asyncio.to_thread(cleanup_orphans, None, 60)
        except Exception:  # noqa: BLE001
            log.exception("cleanup_loop iteration failed")
