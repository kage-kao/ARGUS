"""SQLite-хранилище задач. Активные подпроцессы и asyncio.Task'и держатся в RAM,
но статусы и метаданные персистятся, чтобы переживать рестарт бота."""
from __future__ import annotations
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import config

DB_PATH = config.APP_DIR / "argus.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    url         TEXT NOT NULL,
    hosters     TEXT NOT NULL,        -- JSON list
    quality     TEXT NOT NULL,        -- preset id
    ts_duration INTEGER NOT NULL,     -- tempshare duration days (0 if N/A)
    status      TEXT NOT NULL,        -- pending/recording/processing/uploading/done/failed/cancelled/interrupted
    progress    TEXT DEFAULT '',
    error       TEXT DEFAULT '',
    links       TEXT DEFAULT '',      -- JSON dict {label: [url, ...]}
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_status ON jobs(user_id, status);
"""

ACTIVE_STATUSES = ("pending", "recording", "processing", "uploading")


@dataclass
class Job:
    id: str
    user_id: int
    chat_id: int
    url: str
    hosters: list[str]
    quality: str
    ts_duration: int
    status: str = "pending"
    progress: str = ""
    error: str = ""
    links: dict[str, list[str]] = field(default_factory=dict)
    created_at: int = 0
    updated_at: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            url=row["url"],
            hosters=json.loads(row["hosters"]),
            quality=row["quality"],
            ts_duration=row["ts_duration"],
            status=row["status"],
            progress=row["progress"] or "",
            error=row["error"] or "",
            links=json.loads(row["links"]) if row["links"] else {},
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)


def insert(job: Job) -> None:
    now = int(time.time())
    job.created_at = job.created_at or now
    job.updated_at = now
    with _conn() as c:
        c.execute(
            """INSERT INTO jobs(id,user_id,chat_id,url,hosters,quality,
                                ts_duration,status,progress,error,links,
                                created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job.id, job.user_id, job.chat_id, job.url,
             json.dumps(job.hosters), job.quality, job.ts_duration,
             job.status, job.progress, job.error, json.dumps(job.links),
             job.created_at, job.updated_at),
        )


def update(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = int(time.time())
    if "links" in fields and not isinstance(fields["links"], str):
        fields["links"] = json.dumps(fields["links"])
    if "hosters" in fields and not isinstance(fields["hosters"], str):
        fields["hosters"] = json.dumps(fields["hosters"])
    cols = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE jobs SET {cols} WHERE id=?",
                  (*fields.values(), job_id))


def get(job_id: str) -> Job | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return Job.from_row(r) if r else None


def list_user_active(user_id: int) -> list[Job]:
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM jobs WHERE user_id=? AND status IN ({placeholders}) "
            f"ORDER BY created_at DESC",
            (user_id, *ACTIVE_STATUSES),
        ).fetchall()
    return [Job.from_row(r) for r in rows]


def list_all_active() -> list[Job]:
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM jobs WHERE status IN ({placeholders})",
            ACTIVE_STATUSES,
        ).fetchall()
    return [Job.from_row(r) for r in rows]


def mark_stale_interrupted() -> int:
    """Called at startup: any 'active' jobs from a previous process are marked
    as interrupted (we cannot resume them — yt-dlp/ffmpeg children are gone)."""
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    with _conn() as c:
        cur = c.execute(
            f"UPDATE jobs SET status='interrupted', "
            f"error='process restarted, recording lost', updated_at=? "
            f"WHERE status IN ({placeholders})",
            (int(time.time()), *ACTIVE_STATUSES),
        )
        return cur.rowcount or 0
