"""Probing and decoding helpers (frames and audio as numpy arrays)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterator

import numpy as np

from .ffmpeg import ClipperError, ffmpeg_exe, probe_stderr, run_capture


def probe(path: str | Path) -> dict:
    """Return duration, display size (rotation applied), fps and stream counts."""
    path = Path(path)
    if not path.exists():
        raise ClipperError(f"File not found: {path}")
    import av

    try:
        container = av.open(str(path))
    except Exception as exc:  # noqa: BLE001
        raise ClipperError(f"Could not open {path.name} as a media file ({exc}).") from exc
    with container:
        videos = [s for s in container.streams.video if not _is_cover_art(s)]
        audios = list(container.streams.audio)
        duration = float(container.duration / av.time_base) if container.duration else 0.0
        info: dict = {
            "path": str(path.resolve()),
            "duration": round(duration, 3),
            "video_streams": len(videos),
            "audio_streams": len(audios),
        }
        if videos:
            v = videos[0]
            width, height = v.codec_context.width, v.codec_context.height
            rate = v.average_rate or v.guessed_rate
            fps = float(rate) if rate else 30.0
            if not duration and v.duration and v.time_base:
                info["duration"] = round(float(v.duration * v.time_base), 3)
            info.update({"codec": v.codec_context.name, "fps": round(fps, 3)})
        else:
            width = height = 0
        if audios:
            a = audios[0]
            info.update({"audio_codec": a.codec_context.name, "sample_rate": a.codec_context.sample_rate})
    rotation = 0
    if videos:
        m = re.search(r"rotation of (-?\d+(?:\.\d+)?) degrees", probe_stderr(path))
        if m:
            rotation = int(round(float(m.group(1)))) % 360
        if rotation in (90, 270):
            width, height = height, width
    info.update({"width": width, "height": height, "rotation": rotation})
    return info


def _is_cover_art(stream) -> bool:
    try:
        return bool(stream.disposition.value & 0x0400)  # AV_DISPOSITION_ATTACHED_PIC
    except Exception:  # noqa: BLE001
        return False


def read_audio(
    path: str | Path, sr: int = 16000, channels: int = 1, start: float | None = None, duration: float | None = None
) -> np.ndarray:
    """Decode audio to float32. Shape (n,) for mono, (n, channels) otherwise."""
    args: list[str] = []
    if start is not None and start > 0:
        args += ["-ss", f"{start:.6f}"]
    if duration is not None:
        args += ["-t", f"{duration:.6f}"]
    args += ["-i", str(path), "-vn", "-map", "0:a:0", "-ac", str(channels), "-ar", str(sr), "-f", "f32le", "pipe:1"]
    raw = run_capture(args)
    data = np.frombuffer(raw, dtype=np.float32)
    if channels > 1:
        data = data[: len(data) // channels * channels].reshape(-1, channels)
    return data.copy()


def scaled_size(src_w: int, src_h: int, width: int) -> tuple[int, int]:
    width = max(2, int(width) // 2 * 2)
    height = max(2, int(round(src_h * width / src_w)) // 2 * 2)
    return width, height


def iter_frames(
    path: str | Path,
    fps: float,
    width: int,
    src_size: tuple[int, int],
    gray: bool = False,
    start: float | None = None,
    duration: float | None = None,
) -> Iterator[np.ndarray]:
    """Yield frames resampled to `fps` and scaled to `width` (BGR or gray)."""
    w, h = scaled_size(src_size[0], src_size[1], width)
    pix = "gray" if gray else "bgr24"
    bpp = 1 if gray else 3
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error"]
    if start is not None and start > 0:
        cmd += ["-ss", f"{start:.6f}"]
    if duration is not None:
        cmd += ["-t", f"{duration:.6f}"]
    cmd += ["-i", str(path), "-an", "-vf", f"fps={fps},scale={w}:{h}", "-pix_fmt", pix, "-f", "rawvideo", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = w * h * bpp
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            frame = np.frombuffer(buf, dtype=np.uint8)
            yield frame.reshape((h, w) if gray else (h, w, 3))
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


def grab_frame(path: str | Path, t: float, width: int, src_size: tuple[int, int]) -> np.ndarray | None:
    """One BGR frame at time t, or None if nothing could be decoded there."""
    w, h = scaled_size(src_size[0], src_size[1], width)
    args = ["-ss", f"{max(0.0, t):.3f}", "-i", str(path), "-an", "-frames:v", "1", "-vf", f"scale={w}:{h}",
            "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
    try:
        raw = run_capture(args)
    except ClipperError:
        return None
    if len(raw) < w * h * 3:
        return None
    return np.frombuffer(raw[: w * h * 3], dtype=np.uint8).reshape(h, w, 3).copy()


def fmt_time(t: float) -> str:
    t = max(0.0, float(t))
    m, s = divmod(t, 60)
    h, m = divmod(int(m), 60)
    return f"{h:d}:{m:02d}:{s:05.2f}" if h else f"{m:d}:{s:05.2f}"


def contact_sheet(path: str | Path, times: list[float], out_png: str | Path, width: int = 270,
                  columns: int = 6) -> str:
    """Save a labelled grid of frames taken at `times` (seconds)."""
    import cv2

    info = probe(path)
    size = (info["width"], info["height"])
    frames = []
    for t in times:
        frame = grab_frame(path, t, width, size)
        if frame is None:
            continue
        cv2.rectangle(frame, (0, 0), (92, 22), (0, 0, 0), -1)
        cv2.putText(frame, f"{t:.2f}s", (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
        frames.append(frame)
    if not frames:
        raise ClipperError("No frames could be read for the contact sheet.")
    h, w = frames[0].shape[:2]
    columns = min(columns, len(frames))
    rows = -(-len(frames) // columns)
    sheet = np.zeros((rows * h, columns * w, 3), dtype=np.uint8)
    for i, frame in enumerate(frames):
        r, c = divmod(i, columns)
        sheet[r * h:(r + 1) * h, c * w:(c + 1) * w] = frame[:h, :w]
    cv2.imwrite(str(out_png), sheet)
    return str(out_png)
