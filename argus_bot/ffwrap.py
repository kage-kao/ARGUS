"""Thin async wrappers around ffmpeg / ffprobe / yt-dlp."""
import asyncio
import os
import shlex
from pathlib import Path
from typing import List


async def _run(cmd: List[str], cwd: Path | None = None) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    out, err = await proc.communicate()
    return proc.returncode, out, err


async def yt_dlp_record(url: str, out_path: Path, log_path: Path) -> None:
    """Record stream to mp4. Blocks until stream ends."""
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-part",
        "--user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "-o",
        str(out_path),
        url,
    ]
    with open(log_path, "ab") as logf:
        logf.write(f"\n$ {' '.join(shlex.quote(c) for c in cmd)}\n".encode())
        logf.flush()
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=logf, stderr=logf
        )
        rc = await proc.wait()
    if rc != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"yt-dlp failed (rc={rc})")


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


async def split_copy(src: Path, max_bytes: int, out_pattern: Path) -> List[Path]:
    """Split via ffmpeg -c copy into segments roughly <= max_bytes.

    out_pattern uses ffmpeg's segment %03d placeholder, e.g. '/tmp/seg_%03d.mp4'.
    """
    size = src.stat().st_size
    if size <= max_bytes:
        return [src]

    duration = await probe_duration(src)
    if duration <= 0:
        # fallback: arbitrary 600s segments
        segtime = 600
    else:
        bps = size / duration  # bytes per second
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
    rc, _, err = await _run(cmd)
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


async def concat_copy(parts: List[Path], out_path: Path) -> None:
    """Lossless concat with ffmpeg concat demuxer."""
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
    rc, _, err = await _run(cmd)
    list_file.unlink(missing_ok=True)
    if rc != 0:
        # Re-encode fallback (last resort) — rare
        cmd2 = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c:v", "libx264", "-c:a", "aac", str(out_path),
        ]
        with open(list_file, "w") as f:
            for p in parts:
                f.write(f"file '{p.as_posix()}'\n")
        rc2, _, err2 = await _run(cmd2)
        list_file.unlink(missing_ok=True)
        if rc2 != 0:
            raise RuntimeError(f"ffmpeg concat failed: {err2.decode(errors='ignore')[:400]}")
