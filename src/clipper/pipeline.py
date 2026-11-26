"""The stages, as plain functions. The CLI, the editor page and the AI editor all call these.

    ingest -> transcribe -> (select) -> build EDL(s) -> edit -> render

Clip mode makes one EDL per selected range; polish mode makes one EDL for the
whole take. From there on the two are the same thing.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from . import ops
from . import render as render_mod
from .checks import FootageRejected, check_footage
from .edl import Timeline, summary
from .ffmpeg import ClipperError, log
from .project import Project, safe_name
from .select import select_clips
from .style_analyze import analyze_style
from .style_apply import apply_style, load_style
from .transcribe import transcribe


def ingest(video: str, project_dir: str | None = None, force: bool = False, voices: bool = True) -> Project:
    """Check the footage and create (or reopen) its project folder.

    Raises FootageRejected unless the footage is single-speaker, single-camera
    and at most 1080p. `force` records the problems and carries on anyway."""
    result = check_footage(video, voices=voices)
    if not result["ok"]:
        if not force:
            raise FootageRejected(result)
        result["forced"] = True
        log("footage check failed but --force was given:\n- " + "\n- ".join(result["reasons"]))
    return Project.create(video, project_dir, checks=result)


def ensure_transcript(project: Project, model: str | None = None, language: str | None = None,
                      redo: bool = False) -> dict:
    if project.has_transcript() and not redo:
        return project.transcript()
    t = transcribe(project.source["path"], model, language)
    if not t["words"]:
        raise ClipperError("No speech was recognised in this video, so there is nothing to cut or caption.")
    project.save_transcript(t)
    return t


def finish_edl(project: Project, edl: dict, fillers: bool = True, pauses: bool = True,
               captions: str | None = "bold-pop", zooms_per_minute: float = 6.0, style: dict | None = None,
               music: str | None = None) -> dict:
    """The standard polish recipe: tighten, caption, zoom, then an optional style."""
    words = project.words()
    report: dict = {}
    if fillers:
        report["fillers"] = ops.remove_fillers(edl, words)
    if pauses:
        report["pauses"] = ops.remove_pauses(edl, project.silences(), words)
    if captions:
        report["captions"] = ops.set_captions(edl, captions)["captions"]["template"]
    if music:
        ops.set_music(edl, music)
        report["music"] = Path(music).name
    if style:
        report["style"] = apply_style(edl, style, words, project.word_levels(), music_path=music)
    elif zooms_per_minute > 0:
        report["zooms"] = ops.auto_zoom(edl, words, project.word_levels(), per_minute=zooms_per_minute)["zooms_added"]
    return report


def use_ai(ai: bool | None) -> bool:
    """None means "when DeepSeek is connected"; True insists on it."""
    from . import deepseek

    if ai and not deepseek.is_ready():
        raise ClipperError("DeepSeek is not connected. Run `clipper deepseek set-key`, or set DEEPSEEK_API_KEY.")
    return deepseek.is_ready() if ai is None else bool(ai)


def finish_with_ai(project: Project, edl: dict, brief: str | None = None, captions: str | None = "bold-pop",
                   style: dict | None = None, music: str | None = None, **recipe) -> dict:
    """Let DeepSeek do the edit. If it cannot (no balance, no network), fall back to the built-in recipe and say so."""
    from . import director

    start = copy.deepcopy(edl)
    try:
        if captions:
            ops.set_captions(edl, captions)
        if music:
            ops.set_music(edl, music)
        report = {"ai": director.direct(project, edl, brief)}
        if style:
            report["style"] = apply_style(edl, style, project.words(), project.word_levels(), music_path=music)
        return report
    except ClipperError as exc:
        log(f"AI editor: {exc} Using the built-in recipe instead.")
        edl.clear()
        edl.update(start)
        report = finish_edl(project, edl, captions=captions, style=style, music=music, **recipe)
        report["note"] = f"{exc} Used the built-in recipe instead."
        return report


def make_polish(project: Project, name: str = "polish", aspect: str = "9:16", ai: bool | None = None,
                brief: str | None = None, **finish) -> tuple[dict, dict]:
    edl = project.new_edl(name, "polish", None, aspect)
    report = finish_with_ai(project, edl, brief, **finish) if use_ai(ai) else finish_edl(project, edl, **finish)
    project.save_edl(edl)
    return edl, report


def make_clips(project: Project, count: int = 3, min_len: float = 20.0, max_len: float = 60.0,
               use_llm: bool | None = None, aspect: str = "9:16", **finish) -> list[dict]:
    words = project.words()
    ai = use_ai(use_llm)
    chosen = select_clips(words, count, min_len, max_len, ai)
    if not chosen["clips"]:
        raise ClipperError(f"No stretch of {min_len:.0f}-{max_len:.0f}s made a usable clip. Try a wider length range.")
    out = []
    for i, clip in enumerate(chosen["clips"], 1):
        end = min(clip["end"], project.source["duration"])
        edl = project.new_edl(f"clip-{i:02d}", "clip", [(clip["start"], end)], aspect)
        edl["title"] = clip.get("title")
        edl["notes"].append({"kind": "selection", **clip})
        report = finish_with_ai(project, edl, None, **finish) if ai else finish_edl(project, edl, **finish)
        project.save_edl(edl)
        out.append({"edl": edl["name"], "source_range": [clip["start"], end], "title": clip.get("title"),
                    "reason": clip.get("reason"), "method": clip.get("method"),
                    "duration": round(Timeline(edl).duration, 2), "report": report})
    if chosen.get("note"):
        out[0]["note"] = chosen["note"]
    return out


def render_edl(project: Project, name: str, quality: str = "export", start: float | None = None,
               end: float | None = None, frame_at: float | None = None, out_path: str | None = None) -> dict:
    edl = project.load_edl(name)
    words = project.words() if project.has_transcript() else None
    window = None
    if start is not None or end is not None:
        window = (start or 0.0, end if end is not None else Timeline(edl).duration)
    if frame_at is not None:
        target = Path(out_path) if out_path else project.dir / "renders" / f"{safe_name(name)}.frame-{frame_at:.2f}.png"
    else:
        target = Path(out_path) if out_path else project.render_path(name, quality)
    return render_mod.render(edl, target, words, project.cache, quality=quality, window=window, frame_at=frame_at)


def style_from_reference(reference: str, out_path: str | None = None, max_seconds: float = 90.0) -> tuple[dict, Path]:
    ref = Path(reference).expanduser().resolve()
    target = Path(out_path).expanduser() if out_path else ref.with_suffix(".style.json")
    spec = analyze_style(ref, max_seconds, sheet_path=target.with_suffix(".frames.jpg"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(spec, indent=2) + "\n")
    return spec, target


def apply_style_to(project: Project, name: str, style_path: str, music: str | None = None) -> dict:
    edl = project.load_edl(name)
    spec = load_style(style_path)
    words = project.words() if project.has_transcript() else None
    levels = project.word_levels() if words else None
    report = apply_style(edl, spec, words, levels, music_path=music)
    project.save_edl(edl)
    return {"edl": summary(edl), "report": report}
