"""Speech to text with word timings (faster-whisper)."""
from __future__ import annotations

import os

from .ffmpeg import ClipperError, log, report_progress
from .media import read_audio

DEFAULT_MODEL = os.environ.get("CLIPPER_WHISPER_MODEL", "small")

# Whisper tends to tidy disfluencies away. Starting it off with a prompt that
# contains them makes it keep "um" and "uh", which filler removal relies on.
VERBATIM_PROMPT = "Um, so, uh, yeah. I, um, I think, uh, hmm, you know, like, er, okay."


def transcribe(path: str, model: str | None = None, language: str | None = None) -> dict:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover
        raise ClipperError("faster-whisper is not installed.") from exc
    model = model or DEFAULT_MODEL
    audio = read_audio(path, 16000, 1)
    if len(audio) < 1600:
        raise ClipperError("The file has no usable audio to transcribe.")
    log(f"transcribing with whisper '{model}' ({len(audio) / 16000:.0f}s of audio; first run downloads the model)")
    try:
        wm = WhisperModel(model, device="cpu", compute_type="int8")
    except Exception as exc:  # noqa: BLE001
        raise ClipperError(f"Could not load whisper model {model!r}: {exc}") from exc
    if language is None:
        try:
            language = wm.detect_language(audio[: 16000 * 30])[0]
        except Exception:  # noqa: BLE001
            language = None
    prompt = VERBATIM_PROMPT if language == "en" else None
    segments, info = wm.transcribe(
        audio, language=language, word_timestamps=True, vad_filter=True,
        condition_on_previous_text=False, initial_prompt=prompt, beam_size=5,
    )
    words, segs = [], []
    total = len(audio) / 16000
    for seg in segments:
        report_progress(seg.end / total)
        segs.append({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip()})
        for w in seg.words or []:
            text = w.word.strip()
            if not text:
                continue
            start, end = float(w.start), float(w.end)
            if end <= start:
                end = start + 0.05
            words.append({"w": text, "start": round(start, 3), "end": round(end, 3), "p": round(float(w.probability), 3)})
    for i in range(1, len(words)):  # keep words in order and non-overlapping
        if words[i]["start"] < words[i - 1]["end"]:
            mid = (words[i]["start"] + words[i - 1]["end"]) / 2
            mid = min(max(mid, words[i - 1]["start"] + 0.02), words[i]["end"] - 0.02)
            words[i - 1]["end"] = words[i]["start"] = round(mid, 3)
    return {"language": info.language, "model": model, "duration": round(len(audio) / 16000, 3),
            "words": words, "segments": segs}
