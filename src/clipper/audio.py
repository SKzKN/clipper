"""Audio analysis and the final mix (speech + ducked music + SFX)."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from .edl import Timeline
from .ffmpeg import ClipperError
from .media import read_audio

MIX_SR = 48000


def db(x: float | np.ndarray) -> float | np.ndarray:
    return 20 * np.log10(np.maximum(x, 1e-9))


def gain(db_value: float) -> float:
    return float(10 ** (db_value / 20))


def frame_rms(mono: np.ndarray, sr: int, hop: float = 0.01, win: float = 0.02) -> np.ndarray:
    """RMS in dBFS, one value every `hop` seconds."""
    hop_n, win_n = int(sr * hop), int(sr * win)
    if len(mono) < win_n:
        return np.full(1, -90.0, dtype=np.float32)
    sq = np.concatenate([[0.0], np.cumsum(mono.astype(np.float64) ** 2)])
    starts = np.arange(0, len(mono) - win_n + 1, hop_n)
    return db(np.sqrt((sq[starts + win_n] - sq[starts]) / win_n)).astype(np.float32)


def speech_threshold(levels: np.ndarray) -> float:
    """A dB level between the noise floor and normal speech."""
    floor, speech = float(np.percentile(levels, 10)), float(np.percentile(levels, 95))
    return float(min(max(floor + 6, floor + 0.3 * (speech - floor)), speech - 12))


def find_silences(mono: np.ndarray, sr: int, min_len: float = 0.25) -> list[tuple[float, float]]:
    """Stretches quieter than the speech threshold, at least `min_len` long."""
    hop = 0.01
    levels = frame_rms(mono, sr, hop)
    quiet = levels < speech_threshold(levels)
    out, start = [], None
    for i, q in enumerate(quiet):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if (i - start) * hop >= min_len:
                out.append((round(start * hop, 3), round(i * hop + 0.01, 3)))
            start = None
    if start is not None and (len(quiet) - start) * hop >= min_len:
        out.append((round(start * hop, 3), round(len(mono) / sr, 3)))
    return out


def word_levels(mono: np.ndarray, sr: int, words: list[dict]) -> np.ndarray:
    """Peak short-term level (dB) of each word."""
    levels = frame_rms(mono, sr, 0.01, 0.03)
    out = np.empty(len(words), dtype=np.float32)
    for i, w in enumerate(words):
        a, b = int(w["start"] * 100), max(int(w["end"] * 100), int(w["start"] * 100) + 1)
        chunk = levels[a:b]
        out[i] = float(chunk.max()) if len(chunk) else -90.0
    return out


# --------------------------------------------------------------------------
# Built-in sound effects (synthesised, so there is nothing to license)
# --------------------------------------------------------------------------

def _env(n: int, attack: float, sr: int) -> np.ndarray:
    t = np.arange(n) / sr
    return np.minimum(1.0, t / max(attack, 1e-4))


def synth_sfx(name: str, sr: int = MIX_SR) -> tuple[np.ndarray, float]:
    """Return (stereo samples, sync offset in seconds). The sync offset is the
    moment inside the sound that should land on the cut."""
    rng = np.random.default_rng(7)
    if name == "whoosh":
        n = int(0.42 * sr)
        t = np.arange(n) / sr
        noise = rng.standard_normal(n)
        spec = np.fft.rfft(noise)
        freqs = np.fft.rfftfreq(n, 1 / sr)
        spec *= np.exp(-0.5 * ((np.log2(np.maximum(freqs, 1) / 1800)) / 1.1) ** 2)
        body = np.fft.irfft(spec, n)
        peak = 0.24
        env = np.exp(-0.5 * ((t - peak) / 0.085) ** 2)
        mono = body / np.abs(body).max() * env
        pan = np.clip(t / t[-1], 0, 1)
        return np.stack([mono * np.cos(pan * np.pi / 2), mono * np.sin(pan * np.pi / 2)], axis=1).astype(np.float32), peak
    if name == "pop":
        n = int(0.09 * sr)
        t = np.arange(n) / sr
        freq = 520 * np.exp(-t * 14)
        mono = np.sin(2 * np.pi * np.cumsum(freq) / sr) * np.exp(-t * 55) * _env(n, 0.002, sr)
        return np.stack([mono, mono], axis=1).astype(np.float32), 0.0
    if name == "click":
        n = int(0.03 * sr)
        t = np.arange(n) / sr
        mono = (rng.standard_normal(n) * 0.5 + np.sin(2 * np.pi * 2400 * t)) * np.exp(-t * 320)
        mono /= np.abs(mono).max()
        return np.stack([mono, mono], axis=1).astype(np.float32), 0.0
    if name == "thud":
        n = int(0.35 * sr)
        t = np.arange(n) / sr
        freq = 95 * np.exp(-t * 6) + 38
        mono = np.sin(2 * np.pi * np.cumsum(freq) / sr) * np.exp(-t * 11) * _env(n, 0.003, sr)
        return np.stack([mono, mono], axis=1).astype(np.float32), 0.0
    raise ClipperError(f"Unknown built-in sound {name!r}. Built-ins: {', '.join(BUILTIN_SFX)}; or give a file path.")


BUILTIN_SFX = ("whoosh", "pop", "click", "thud")


def load_sfx(sound: str) -> tuple[np.ndarray, float]:
    if sound in BUILTIN_SFX:
        return synth_sfx(sound)
    if not Path(sound).exists():
        raise ClipperError(f"Sound effect not found: {sound}. Built-ins: {', '.join(BUILTIN_SFX)}.")
    return read_audio(sound, MIX_SR, 2), 0.0


# --------------------------------------------------------------------------
# Mix
# --------------------------------------------------------------------------

def assemble_speech(source: str, tl: Timeline, sr: int = MIX_SR) -> np.ndarray:
    """Source audio cut to the timeline, with 4 ms fades at every join so cuts do not click."""
    total = int(round(tl.duration * sr))
    out = np.zeros((total, 2), dtype=np.float32)
    if not tl.segs:
        return out
    lo = min(s.src_start for s in tl.segs)
    hi = max(s.src_end for s in tl.segs)
    src = read_audio(source, sr, 2, start=lo, duration=hi - lo + 0.1)
    fade_n = int(0.004 * sr)
    ramp = np.linspace(0.0, 1.0, fade_n, dtype=np.float32)[:, None]
    for seg in tl.segs:
        a = int(round((seg.src_start - lo) * sr))
        n = int(round(seg.tl_end * sr)) - int(round(seg.tl_start * sr))
        piece = src[a:a + n]
        if len(piece) == 0:
            continue
        piece = piece.copy()
        if len(piece) > 2 * fade_n:
            piece[:fade_n] *= ramp
            piece[-fade_n:] *= ramp[::-1]
        dst = int(round(seg.tl_start * sr))
        out[dst:dst + len(piece)] = piece[: total - dst]
    return out


def duck_envelope(speech: np.ndarray, sr: int, amount_db: float, attack: float, release: float) -> np.ndarray:
    """Per-sample gain for the music: down by `amount_db` while speech is present.

    Speech is detected from the level of the speech track itself, the dip
    starts slightly early (80 ms look-ahead) and gaps under 0.3 s between
    words are bridged so the music does not pump.
    """
    hop = 0.01
    mono = speech.mean(axis=1)
    levels = frame_rms(mono, sr, hop)
    active = levels > speech_threshold(levels)
    bridge = int(0.3 / hop)
    idx = np.flatnonzero(active)
    for i, j in zip(idx, idx[1:]):
        if 1 < j - i <= bridge:
            active[i:j] = True
    look = int(0.08 / hop)
    if look and len(active) > look:
        active = np.concatenate([active[look:], np.zeros(look, dtype=bool)]) | active
    target = np.where(active, -abs(amount_db), 0.0)
    a_coef = np.exp(-hop / max(attack, 1e-3))
    r_coef = np.exp(-hop / max(release, 1e-3))
    smooth = np.empty_like(target)
    level = 0.0
    for i, tgt in enumerate(target):
        coef = a_coef if tgt < level else r_coef
        level = coef * level + (1 - coef) * tgt
        smooth[i] = level
    samples = np.interp(np.arange(len(mono)) / sr, np.arange(len(smooth)) * hop, smooth)
    return (10 ** (samples / 20)).astype(np.float32)


def music_bed(music: dict, speech: np.ndarray, sr: int, offset_extra: float = 0.0,
              full_duration: float | None = None) -> np.ndarray:
    n = len(speech)
    track = read_audio(music["path"], sr, 2)
    if len(track) == 0:
        raise ClipperError(f"No audio could be read from the music file {music['path']}.")
    start = int((float(music.get("offset", 0.0)) + offset_extra) * sr) % len(track)
    reps = int(np.ceil((start + n) / len(track)))
    bed = np.tile(track, (reps, 1))[start:start + n].copy()
    bed *= gain(float(music.get("gain_db", -20.0)))
    # Fades are placed on the full timeline so a preview window shows the right part of them.
    total = full_duration if full_duration is not None else n / sr
    t = offset_extra + np.arange(n) / sr
    fade_in, fade_out = float(music.get("fade_in", 0.5)), float(music.get("fade_out", 1.5))
    env = np.ones(n, dtype=np.float32)
    if fade_in > 0:
        env *= np.clip(t / fade_in, 0, 1)
    if fade_out > 0:
        env *= np.clip((total - t) / fade_out, 0, 1)
    bed *= env[:, None]
    duck = music.get("duck") or {}
    if duck.get("enabled", True):
        bed *= duck_envelope(speech, sr, float(duck.get("amount_db", -10.0)), float(duck.get("attack", 0.08)),
                             float(duck.get("release", 0.5)))[:, None]
    return bed


def limit(mix: np.ndarray, sr: int, ceiling: float = 0.89) -> np.ndarray:
    """Transparent peak limiter: only touches the signal where it would exceed the ceiling."""
    peak = np.abs(mix).max(axis=1)
    if peak.max() <= ceiling:
        return mix
    block = int(0.002 * sr)
    pad = (-len(peak)) % block
    blocks = np.pad(peak, (0, pad)).reshape(-1, block).max(axis=1)
    need = np.minimum(1.0, ceiling / np.maximum(blocks, 1e-9))
    spread = 4
    padded = np.pad(need, spread, mode="edge")
    need = np.min(np.stack([padded[i:i + len(need)] for i in range(2 * spread + 1)]), axis=0)
    kernel = np.ones(spread) / spread
    need = np.minimum(need, np.convolve(np.pad(need, spread, mode="edge"), kernel, mode="same")[spread:-spread])
    g = np.interp(np.arange(len(peak)), np.arange(len(need)) * block + block / 2, need)
    return (mix * g[:, None]).astype(np.float32)


def mix_edl(edl: dict, tl: Timeline, resolved: dict, full_duration: float, window_start: float = 0.0,
            sr: int = MIX_SR) -> tuple[np.ndarray, dict]:
    """Render the whole soundtrack for a (possibly windowed) timeline."""
    speech = assemble_speech(edl["source"]["path"], tl, sr)
    mix = speech.copy()
    report = {"speech_peak_db": round(float(db(np.abs(speech).max())), 1) if len(speech) else None}
    if edl.get("music"):
        bed = music_bed(edl["music"], speech, sr, offset_extra=window_start, full_duration=full_duration)
        mix += bed
        report["music"] = Path(edl["music"]["path"]).name
    placed = 0
    for s in resolved["sfx"]:
        samples, sync = load_sfx(s["sound"])
        start = int(round((s["tl_at"] - sync) * sr))
        samples = samples * gain(float(s.get("gain_db", -8.0)))
        if start < 0:
            samples, start = samples[-start:], 0
        end = min(len(mix), start + len(samples))
        if end > start:
            mix[start:end] += samples[: end - start]
            placed += 1
    report["sfx_placed"] = placed
    return limit(mix, sr), report


def write_wav(path: str | Path, samples: np.ndarray, sr: int = MIX_SR) -> None:
    data = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as f:
        f.setnchannels(data.shape[1] if data.ndim > 1 else 1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(data.tobytes())
