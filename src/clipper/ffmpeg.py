"""Locate and run ffmpeg.

Order: $CLIPPER_FFMPEG, then the binary bundled with imageio-ffmpeg (known to
have libass, xfade and libx264), then whatever `ffmpeg` is on PATH.
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


class ClipperError(Exception):
    """An error meant to be shown to the user as-is."""


@functools.lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    env = os.environ.get("CLIPPER_FFMPEG")
    if env:
        if not Path(env).exists():
            raise ClipperError(f"CLIPPER_FFMPEG points to {env}, which does not exist.")
        return env
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise ClipperError(
        "ffmpeg not found. Install the imageio-ffmpeg package or set CLIPPER_FFMPEG "
        "to an ffmpeg binary built with libass."
    )


@functools.lru_cache(maxsize=1)
def capabilities() -> dict:
    exe = ffmpeg_exe()
    version = subprocess.run([exe, "-hide_banner", "-version"], capture_output=True, text=True).stdout
    filters = subprocess.run([exe, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    encoders = subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    have = lambda name: any(f" {name} " in line for line in filters.splitlines())
    return {
        "path": exe,
        "version": version.splitlines()[0] if version else "unknown",
        "ass": have("ass"),
        "xfade": have("xfade"),
        "loudnorm": have("loudnorm"),
        "libx264": "libx264" in encoders,
    }


_local = threading.local()


def set_sinks(log_sink=None, progress_sink=None) -> None:
    """Send this thread's log lines and progress (0..1) to callbacks as well (used by the UI's jobs)."""
    _local.log, _local.progress = log_sink, progress_sink


def log(msg: str) -> None:
    """All diagnostics go to stderr: stdout carries the command's JSON result."""
    print(msg, file=sys.stderr, flush=True)
    sink = getattr(_local, "log", None)
    if sink:
        sink(msg)


def report_progress(fraction: float) -> None:
    sink = getattr(_local, "progress", None)
    if sink:
        sink(min(1.0, max(0.0, float(fraction))))


def run(args: list[str], cwd: str | Path | None = None, what: str = "ffmpeg", total: float | None = None) -> None:
    """Run ffmpeg. With `total` (output seconds) progress is reported to this thread's progress sink."""
    base = [ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    if total and getattr(_local, "progress", None):
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen([*base, "-progress", "pipe:1", "-nostats", *args], cwd=cwd,
                                    stdout=subprocess.PIPE, stderr=err, text=True)
            for line in proc.stdout:
                if line.startswith("out_time_us="):
                    try:
                        report_progress(int(line.split("=", 1)[1]) / 1e6 / total)
                    except ValueError:
                        pass
            proc.wait()
            err.seek(0)
            stderr = err.read().decode(errors="replace")
        code = proc.returncode
    else:
        proc = subprocess.run([*base, *args], cwd=cwd, capture_output=True, text=True)
        code, stderr = proc.returncode, proc.stderr
    if code != 0:
        tail = "\n".join(stderr.strip().splitlines()[-12:])
        raise ClipperError(f"{what} failed:\n{tail}")


def run_capture(args: list[str], cwd: str | Path | None = None) -> bytes:
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error", *args]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.decode(errors="replace").strip().splitlines()[-12:])
        raise ClipperError(f"ffmpeg failed:\n{tail}")
    return proc.stdout


def probe_stderr(path: str | Path) -> str:
    """`ffmpeg -i` banner text for a file (ffprobe is not bundled)."""
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path)], capture_output=True, text=True
    )
    return proc.stderr
