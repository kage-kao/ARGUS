"""Async wrappers around ffmpeg / ffprobe / yt-dlp.

`record_stream` is the recording entrypoint:
  - Direct live streams (FLV/HLS/RTMP, e.g. BuzzCast pull.* URLs) are recorded
    with **ffmpeg -c copy** (yt-dlp fails on signed Tencent FLV -> "rc=1").
  - Everything else goes through yt-dlp, with an ffmpeg fallback on failure.
"""
from __future__ import annotations
import asyncio
import os
import shlex
import signal
from pathlib import Path
from typing import List

STREAM_UA = "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Mobile Safari/537.36"
STREAM_HEADERS = "Referer: https://www.buzzcast.com/\r\nOrigin: https://www.buzzcast.com\r\n"


class ProcRegistry:
    """Holds references to live subprocesses so an outer cancel can kill them."""

    def __init__(self) -> None:
        self._procs: set[asyncio.subprocess.Process] = set()
        self.cancelled = False

    def add(self, p: asyncio.subprocess.Process) -> None:
        self._procs.add(p)

    def discard(self, p: asyncio.subprocess.Process) -> None:
        self._procs.discard(p)

    def kill_all(self) -> None:
        self.cancelled = True
        for p in list(self._procs):
            try:
                p.send_signal(signal.SIGTERM)
            except (ProcessLookupError, Exception):
                pass


async def _run(cmd: List[str], cwd: Path | None = None,
               registry: "ProcRegistry | None" = None) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    if registry is not None:
        registry.add(proc)
    try:
        out, err = await proc.communicate()
    finally:
        if registry is not None:
            registry.discard(proc)
    return proc.returncode, out, err


def is_direct_stream(url: str) -> bool:
    """Heuristic: a URL we should hand to ffmpeg rather than yt-dlp."""
    base = url.lower().split("?", 1)[0]
    if base.endswith((".flv", ".m3u8", ".ts")):
        return True
    if base.startswith(("rtmp://", "rtmps://", "webrtc://")):
        return True
    return any(host in url.lower() for host in ("pull.buzzcast.com", "pull.facecast", "buzzcast.com/live"))


async def _spawn_logged(cmd: List[str], log_path: Path,
                        registry: "ProcRegistry | None") -> int:
    with open(log_path, "ab") as logf:
        logf.write(f"\n$ {' '.join(shlex.quote(c) for c in cmd)}\n".encode())
        logf.flush()
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=logf, stderr=logf)
        if registry is not None:
            registry.add(proc)
        try:
            rc = await proc.wait()
        finally:
            if registry is not None:
                registry.discard(proc)
    return rc


def _has_data(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 1024


async def _ffmpeg_record(url: str, out_path: Path, log_path: Path,
                         registry: "ProcRegistry | None") -> int:
    """Record a live stream losslessly via ffmpeg -c copy."""
    is_http = url.lower().startswith(("http://", "https://"))
    base = url.lower().split("?", 1)[0]
    is_hls_ts = base.endswith((".m3u8", ".ts"))

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y"]
    if is_http:
        cmd += [
            "-user_agent", STREAM_UA,
            "-headers", STREAM_HEADERS,
            "-rw_timeout", "20000000",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        ]
    cmd += ["-i", url, "-c", "copy"]
    if is_hls_ts:
        # ADTS AAC (HLS/TS) -> MP4 needs this bitstream filter
        cmd += ["-bsf:a", "aac_adtstoasc"]
    cmd += [str(out_path)]
    return await _spawn_logged(cmd, log_path, registry)


async def _ytdlp_record(url: str, out_path: Path, log_path: Path,
                        registry: "ProcRegistry | None") -> int:
    cmd = [
        "yt-dlp", "--no-warnings", "--no-part",
        "--hls-use-mpegts",
        "--user-agent", STREAM_UA,
        "-o", str(out_path),
        url,
    ]
    return await _spawn_logged(cmd, log_path, registry)


async def record_stream(url: str, out_path: Path, log_path: Path,
                        registry: "ProcRegistry | None" = None) -> None:
    """Record `url` to `out_path`. Blocks until the stream ends."""
    direct = is_direct_stream(url)

    if direct:
        rc = await _ffmpeg_record(url, out_path, log_path, registry)
    else:
        rc = await _ytdlp_record(url, out_path, log_path, registry)
        # yt-dlp couldn't handle it (rc=1) → fall back to ffmpeg copy
        if rc != 0 and not _has_data(out_path):
            rc = await _ffmpeg_record(url, out_path, log_path, registry)

    if registry is not None and registry.cancelled:
        raise asyncio.CancelledError()

    # tolerate non-zero rc on graceful stream end as long as we captured data
    if not _has_data(out_path) and rc != 0:
        raise RuntimeError(f"recording failed (rc={rc}) — поток недоступен или приватный")


# Backwards-compat alias.
yt_dlp_record = record_stream


async def probe_duration(path: Path) -> float:
    rc, out, _ = await _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(path),
    ])
    if rc != 0:
        return 0.0
    try:
        return float(out.decode().strip())
    except Exception:
        return 0.0


async def probe_dimensions(path: Path) -> tuple[int, int]:
    """Вернуть (width, height) первого видеопотока. (0, 0) если не удалось."""
    rc, out, _ = await _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path),
    ])
    if rc != 0:
        return 0, 0
    try:
        w, h = out.decode().strip().split(",")[:2]
        return int(w), int(h)
    except Exception:
        return 0, 0


async def split_copy(src: Path, max_bytes: int, out_pattern: Path,
                     registry: "ProcRegistry | None" = None) -> List[Path]:
    """Split via ffmpeg -c copy into segments roughly <= max_bytes."""
    size = src.stat().st_size
    if size <= max_bytes:
        return [src]

    duration = await probe_duration(src)
    if duration <= 0:
        segtime = 600
    else:
        bps = size / duration
        segtime = max(60, int(max_bytes / bps))

    out_pattern.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-c", "copy", "-f", "segment",
        "-segment_time", str(segtime),
        "-reset_timestamps", "1",
        str(out_pattern),
    ]
    rc, _, err = await _run(cmd, registry=registry)
    if rc != 0:
        raise RuntimeError(f"ffmpeg segment failed: {err.decode(errors='ignore')[:400]}")

    parent = out_pattern.parent
    stem_prefix = out_pattern.stem.split("%")[0]
    parts = sorted(
        p for p in parent.iterdir()
        if p.is_file() and p.name.startswith(stem_prefix) and p.suffix == out_pattern.suffix
    )
    if not parts:
        raise RuntimeError("ffmpeg produced no segments")
    return parts


async def concat_copy(parts: List[Path], out_path: Path,
                      registry: "ProcRegistry | None" = None) -> None:
    """Lossless concat with ffmpeg concat demuxer (re-encode fallback)."""
    if len(parts) == 1:
        if parts[0].resolve() != out_path.resolve():
            os.replace(parts[0], out_path)
        return

    list_file = out_path.with_suffix(out_path.suffix + ".list.txt")
    with open(list_file, "w") as f:
        for p in parts:
            f.write(f"file '{p.as_posix()}'\n")

    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy", str(out_path),
    ]
    rc, _, err = await _run(cmd, registry=registry)
    if rc != 0:
        with open(list_file, "w") as f:
            for p in parts:
                f.write(f"file '{p.as_posix()}'\n")
        cmd2 = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c:v", "libx264", "-c:a", "aac", str(out_path),
        ]
        rc2, _, err2 = await _run(cmd2, registry=registry)
        list_file.unlink(missing_ok=True)
        if rc2 != 0:
            raise RuntimeError(f"ffmpeg concat failed: {err2.decode(errors='ignore')[:400]}")
        return
    list_file.unlink(missing_ok=True)
