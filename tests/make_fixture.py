"""Build synthetic test footage (macOS only: uses the `say` voice).

    python tests/make_fixture.py <out_dir>

Creates:
  talk.mp4       1080p "talking head" stand-in: one voice, a static textured
                 room, a burned-in source timecode, fillers and long pauses.
  two_voices.mp4 the same picture with two different voices (must be rejected).
  two_cams.mp4   a video that cuts between two different rooms (must be rejected).
  uhd.mp4        a 4K file (must be rejected).
  music.wav      a simple synthetic music bed.
  broll.mp4      a short clip to use as b-roll.
"""
from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from clipper.ffmpeg import ffmpeg_exe  # noqa: E402

SCRIPT = (
    "So, um, today I want to show you the three biggest mistakes people make when they start editing videos. "
    "[[slnc 1400]] The first mistake is, uh, cutting too slowly. Nobody has time for that! "
    "[[slnc 1700]] The second mistake is ignoring sound. Great audio matters more than you think. "
    "[[slnc 1100]] Um, and the third mistake is never adding captions. "
    "Eighty percent of people watch without sound. [[slnc 900]] So always add captions."
)
SCRIPT_B = "Wait, can I ask something? Why does that matter so much for a short video? I never really understood that."


def ff(*args: str) -> None:
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def room(path: Path, seed: int, size=(1920, 1080), shift: int = 0, zoom: float = 1.0) -> None:
    """A static, feature-rich picture: shelves, frames, noise."""
    rng = np.random.default_rng(seed)
    w, h = size
    base = np.zeros((h, w, 3), dtype=np.float32)
    top, bottom = rng.integers(40, 120, 3), rng.integers(20, 70, 3)
    base += np.linspace(top, bottom, h)[:, None, :]
    base += rng.normal(0, 6, (h, w, 1))
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img)
    for _ in range(46):
        x, y = int(rng.integers(0, w - 80)), int(rng.integers(0, h - 80))
        ww, hh = int(rng.integers(30, 240)), int(rng.integers(30, 200))
        col = tuple(int(c) for c in rng.integers(30, 230, 3))
        d.rectangle([x, y, x + ww, y + hh], fill=col, outline=(15, 15, 15), width=3)
    # a head-and-shoulders silhouette where a speaker would be
    cx, cy, z = w // 2 + shift, int(h * 0.42), zoom
    d.ellipse([cx - 420 * z, cy + 170 * z, cx + 420 * z, cy + 900 * z], fill=(58, 74, 110))
    d.ellipse([cx - 150 * z, cy - 190 * z, cx + 150 * z, cy + 190 * z], fill=(222, 178, 150), outline=(60, 40, 30), width=4)
    d.ellipse([cx - 75 * z, cy - 50 * z, cx - 35 * z, cy - 15 * z], fill=(30, 30, 30))
    d.ellipse([cx + 35 * z, cy - 50 * z, cx + 75 * z, cy - 15 * z], fill=(30, 30, 30))
    img.save(path)


def say(text: str, voice: str, out_wav: Path) -> None:
    aiff = out_wav.with_suffix(".aiff")
    subprocess.run(["say", "-v", voice, "-r", "175", "-o", str(aiff), text], check=True)
    ff("-i", str(aiff), "-ar", "48000", "-ac", "2", str(out_wav))
    aiff.unlink()


def video(still: Path, audio: Path, out: Path, size: str = "1920:1080") -> None:
    """Still picture + moving 'mouth' + burned-in source timecode, as long as the audio."""
    vf = (
        f"scale={size},"
        "drawbox=x=iw/2-60:y=ih*0.42+70:w=120:h='14+26*abs(sin(t*9))':color=0x802020:t=fill,"
        "drawtext=text='SRC %{pts\\:hms}':fontsize=h/22:fontcolor=white:box=1:boxcolor=black@0.6:x=w*0.05:y=h*0.05,"
        "format=yuv420p"
    )
    ff("-loop", "1", "-framerate", "30", "-i", str(still), "-i", str(audio), "-vf", vf, "-c:v", "libx264",
       "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-shortest", "-r", "30", str(out))


def music(path: Path, seconds: float = 30.0, sr: int = 48000) -> None:
    t = np.arange(int(seconds * sr)) / sr
    beat = 0.5  # 120 bpm
    out = np.zeros_like(t)
    for root, start in zip([110.0, 87.31, 130.81, 98.0] * 8, np.arange(0, seconds, 4 * beat)):
        m = (t >= start) & (t < start + 4 * beat)
        for mult, amp in ((1, 0.30), (1.5, 0.16), (2, 0.14), (2.52, 0.10)):
            out[m] += amp * np.sin(2 * np.pi * root * mult * t[m])
    phase = (t % beat) / beat
    out += 0.7 * np.sin(2 * np.pi * (48 + 60 * np.exp(-phase * 18)) * (t % beat)) * np.exp(-phase * 9)  # kick
    hat = np.random.default_rng(1).standard_normal(len(t)) * np.exp(-((t + beat / 2) % beat) / beat * 30) * 0.12
    out = (out + hat) * 0.5
    data = (np.clip(np.stack([out, out], axis=1), -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(data.tobytes())


def main(out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    room(out / "room_a.png", 3)
    room(out / "room_b.png", 11, shift=-260, zoom=1.35)
    say(SCRIPT, "Samantha", out / "voice_a.wav")
    say(SCRIPT_B, "Daniel", out / "voice_b.wav")
    video(out / "room_a.png", out / "voice_a.wav", out / "talk.mp4")

    # two voices over one picture
    ff("-i", str(out / "voice_a.wav"), "-i", str(out / "voice_b.wav"), "-i", str(out / "voice_a.wav"),
       "-filter_complex", "[0:a]atrim=0:14[a];[2:a]atrim=14:27,asetpts=PTS-STARTPTS[c];[a][1:a][c]concat=n=3:v=0:a=1",
       str(out / "voices_ab.wav"))
    video(out / "room_a.png", out / "voices_ab.wav", out / "two_voices.mp4")

    # two cameras: cut between two rooms every few seconds
    video(out / "room_b.png", out / "voice_a.wav", out / "cam_b.mp4")
    ff("-i", str(out / "talk.mp4"), "-i", str(out / "cam_b.mp4"), "-filter_complex",
       "[0:v]trim=0:6,setpts=PTS-STARTPTS[a];[1:v]trim=6:13,setpts=PTS-STARTPTS[b];"
       "[0:v]trim=13:20,setpts=PTS-STARTPTS[c];[a][b][c]concat=n=3:v=1[v]",
       "-map", "[v]", "-map", "0:a", "-t", "20", "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
       str(out / "two_cams.mp4"))

    ff("-i", str(out / "talk.mp4"), "-t", "3", "-vf", "scale=3840:2160", "-c:v", "libx264", "-preset", "ultrafast",
       "-c:a", "copy", str(out / "uhd.mp4"))
    ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=8", "-pix_fmt", "yuv420p", "-c:v", "libx264",
       "-preset", "veryfast", str(out / "broll.mp4"))
    music(out / "music.wav")
    print("fixture written to", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tests/fixture")
