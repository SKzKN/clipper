"""MCP server: every stage and every edit operation as a tool.

The connected agent does the planning (which clips, which words to punch in
on, where a title goes). The server only measures, edits the EDL and renders.

Run with `clipper mcp` (stdio).
"""
from __future__ import annotations

import functools
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import anyio

from . import audio as audio_mod
from . import ops, pipeline
from . import templates as templates_mod
from .checks import check_footage as run_check
from .edl import ASPECTS, TRANSITIONS, Timeline, resolve, summary
from .ffmpeg import ClipperError
from .media import contact_sheet, fmt_time
from .project import Project, safe_name
from .select import select_clips as run_select

try:  # mcp 2.x
    from mcp.server.mcpserver import Image, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer
    from mcp.server.fastmcp import Image
    from mcp.server.fastmcp.exceptions import ToolError

INSTRUCTIONS = """\
clipper edits single-speaker, single-camera talking-head video (1080p max).

Two modes share one EDL format and one renderer:
- polish: one raw take in, one finished video out.
- clip: one long video in, several shorts out (one EDL per short).

Typical order: ingest -> transcribe -> get_transcript -> create_edl -> edit tools -> render_preview
(look at the returned frames) -> export.

Clip mode: read the transcript, choose self-contained ranges yourself and call create_edl once per
clip with mode="clip" and the source range. select_clips is only a non-LLM fallback.

Times: every edit tool takes seconds on the EDL's current output timeline, the same times
get_transcript(edl=...) reports. Cutting changes later times, so re-read the transcript or get_edl
after remove_fillers, remove_pauses, cut or trim, and do those structural edits first.
Items added with anchor="content" follow the footage when it is cut later; anchor="timeline"
stays at a fixed output time.

`project` is the project folder returned by ingest, or the path of the video it was made from.
"""

server = MCPServer("clipper", instructions=INSTRUCTIONS)

_jobs: dict[str, dict] = {}


def tool(fn):
    """Register a blocking function as an MCP tool, run off the event loop.
    ClipperError messages are written for the user, so they pass through as the tool error."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
        except ClipperError as exc:
            raise ToolError(str(exc)) from None

    server.tool()(wrapper)
    return fn


def _edit(project: str, edl: str, change) -> dict:
    """Load an EDL, apply `change`, save it, and return the result with the new state."""
    proj = Project.open(project)
    doc = proj.load_edl(edl)
    result = change(proj, doc)
    proj.save_edl(doc)
    tl = Timeline(doc)
    return {**result, "edl": doc["name"], "timeline_duration": round(tl.duration, 2),
            "warnings": resolve(doc, tl)["warnings"]}


def _jpeg(path: str | Path, max_width: int = 720) -> Image:
    import cv2

    img = cv2.imread(str(path))
    if img is None:
        raise ClipperError(f"Could not read image {path}.")
    h, w = img.shape[:2]
    if w > max_width:
        img = cv2.resize(img, (max_width, int(h * max_width / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return Image(data=buf.tobytes(), format="jpeg")


# ==========================================================================
# Stages
# ==========================================================================

@tool
def check_footage(video: str) -> dict:
    """Check whether a video can be used: single speaker, one camera, at most 1080p.
    Returns ok, the reasons it was rejected (ready to show the user) and warnings. Changes nothing."""
    return run_check(video)


@tool
def ingest(video: str, project_dir: str | None = None, force: bool = False) -> dict:
    """Stage 1. Check the footage and create its project folder (next to the video unless project_dir is given).
    Fails with a clear message if the footage is not single-speaker, single-camera, 1080p max.
    Only pass force=true when the user has seen the reasons and wants to continue anyway."""
    proj = pipeline.ingest(video, project_dir, force=force)
    return {"project": str(proj.dir), "source": proj.source, "checks": proj.meta.get("checks"),
            "has_transcript": proj.has_transcript(), "edls": proj.list_edls()}


@tool
def transcribe(project: str, model: str | None = None, language: str | None = None, redo: bool = False) -> dict:
    """Stage 2. Transcribe the source with word timings (cached in the project).
    model: whisper size (tiny, base, small, medium, large-v3); default small. language: e.g. "en"; default auto."""
    proj = Project.open(project)
    t = pipeline.ensure_transcript(proj, model, language, redo)
    return {"language": t["language"], "model": t["model"], "words": len(t["words"]),
            "duration": t["duration"], "text": " ".join(w["w"] for w in t["words"])[:6000]}


@tool
def get_transcript(project: str, edl: str | None = None, start: float | None = None, end: float | None = None,
                   detail: str = "sentences") -> dict:
    """Read the transcript with timings.
    Without edl: source times, the whole video (use this to choose clips).
    With edl: only what is still in that edit, in output-timeline seconds (use these times for edit tools).
    detail: "sentences" (default) or "words" (each word with its index, for precise placement).
    start/end limit the range (in the same time base)."""
    proj = Project.open(project)
    words = proj.words()
    rows = []
    if edl:
        tl = Timeline(proj.load_edl(edl))
        for i, w in enumerate(words):
            ranges = tl.map_range(w["start"], w["end"])
            if sum(b - a for a, b in ranges) >= 0.5 * max(w["end"] - w["start"], 1e-3):
                rows.append({"i": i, "w": w["w"], "start": round(ranges[0][0], 2), "end": round(ranges[-1][1], 2)})
        time_base = f"timeline of edl '{edl}'"
    else:
        rows = [{"i": i, "w": w["w"], "start": w["start"], "end": w["end"]} for i, w in enumerate(words)]
        time_base = "source"
    if start is not None:
        rows = [r for r in rows if r["end"] >= start]
    if end is not None:
        rows = [r for r in rows if r["start"] <= end]
    if detail == "words":
        return {"time_base": time_base, "words": rows[:4000], "truncated": len(rows) > 4000}
    sents = []
    for idxs in ops.sentences(rows):
        sents.append({"start": rows[idxs[0]]["start"], "end": rows[idxs[-1]]["end"],
                      "first_word": rows[idxs[0]]["i"], "text": " ".join(rows[i]["w"] for i in idxs)})
    return {"time_base": time_base, "sentences": sents}


@tool
def correct_transcript(project: str, corrections: list[dict]) -> dict:
    """Fix misheard words so captions read correctly. corrections: [{"index": word index, "text": "new text"}].
    An empty text removes the word from captions. Word timings are kept."""
    proj = Project.open(project)
    t = proj.transcript()
    changed = []
    for c in corrections:
        i = int(c["index"])
        if not 0 <= i < len(t["words"]):
            raise ClipperError(f"Word index {i} does not exist (the transcript has {len(t['words'])} words).")
        changed.append({"index": i, "was": t["words"][i]["w"], "now": c["text"]})
        t["words"][i]["w"] = str(c["text"])
    proj.save_transcript(t)
    return {"changed": changed}


@tool
def select_clips(project: str, count: int = 3, min_len: float = 20.0, max_len: float = 60.0,
                 use_llm: bool = False) -> dict:
    """Fallback clip selection for runs without an agent: proposes source ranges for shorts.
    If you are an agent, prefer reading get_transcript and choosing ranges yourself.
    Returns proposals only; call create_edl for the ones to keep."""
    proj = Project.open(project)
    return run_select(proj.words(), count, min_len, max_len, use_llm)


@tool
def create_edl(project: str, name: str, mode: str = "polish", ranges: list[list[float]] | None = None,
               aspect: str = "9:16", title: str | None = None) -> dict:
    """Create an EDL (one output video).
    mode "polish": the whole take (ranges optional). mode "clip": pass ranges=[[start, end], ...] in SOURCE
    seconds; several ranges are played back to back in the order given.
    aspect: "9:16", "16:9", "1:1", "4:5" or "source". Replaces an EDL of the same name."""
    proj = Project.open(project)
    if mode == "clip" and not ranges:
        raise ClipperError("Clip mode needs ranges: the source time range(s) of the clip.")
    doc = proj.new_edl(name, mode, [(float(a), float(b)) for a, b in ranges] if ranges else None, aspect)
    if ranges:
        ops.set_segments(doc, [(float(a), float(b)) for a, b in ranges])
    if title:
        doc["title"] = title
    proj.save_edl(doc)
    return summary(doc)


@tool
def auto_polish(project: str, edl: str, fillers: bool = True, pauses: bool = True,
                captions: str | None = "bold-pop", zooms_per_minute: float = 6.0) -> dict:
    """Apply the standard recipe to an EDL in one call: remove fillers, shorten pauses, turn on captions,
    add punch-in zooms on emphasis. A quick starting point; refine with the individual tools after."""
    def change(proj, doc):
        return {"report": pipeline.finish_edl(proj, doc, fillers=fillers, pauses=pauses, captions=captions,
                                              zooms_per_minute=zooms_per_minute)}
    return _edit(project, edl, change)


@tool
def list_edls(project: str) -> dict:
    """List the EDLs in a project with a one-line summary each."""
    proj = Project.open(project)
    return {"project": str(proj.dir), "has_transcript": proj.has_transcript(),
            "edls": [summary(proj.load_edl(n)) for n in proj.list_edls()]}


@tool
def get_edl(project: str, edl: str) -> dict:
    """The full state of an EDL: summary, where every cut is, and every item with its id and output time."""
    proj = Project.open(project)
    doc = proj.load_edl(edl)
    tl = Timeline(doc)
    res = resolve(doc, tl)
    place = lambda items: [{k: v for k, v in it.items() if k not in ("phase",)} | {
        "tl_start": round(it["tl_start"], 2), "tl_end": round(it["tl_end"], 2)} for it in items]
    return {
        "summary": summary(doc),
        "cuts": [{**c, "time": round(c["time"], 2)} for c in tl.cuts()][:300],
        "segments": [{"id": s.id, "source": [round(s.src_start, 2), round(s.src_end, 2)],
                      "timeline": [round(s.tl_start, 2), round(s.tl_end, 2)]} for s in tl.segs][:300],
        "captions": doc["captions"],
        "zooms": place(res["zooms"]),
        "overlays": place(res["overlays"]),
        "broll": place(res["broll"]),
        "sfx": [{**s, "tl_at": round(s["tl_at"], 2)} for s in res["sfx"]],
        "music": doc.get("music"),
        "framing": doc["framing"],
        "notes": doc.get("notes", []),
    }


@tool
def duplicate_edl(project: str, edl: str, new_name: str) -> dict:
    """Copy an EDL under a new name (to try a variation without losing the original)."""
    proj = Project.open(project)
    doc = proj.load_edl(edl)
    doc["name"] = safe_name(new_name)
    proj.save_edl(doc)
    return summary(doc)


# ==========================================================================
# Edit operations
# ==========================================================================

@tool
def trim(project: str, edl: str, start: float | None = None, end: float | None = None) -> dict:
    """Keep only the output range start..end (seconds); drop everything before and after."""
    return _edit(project, edl, lambda p, d: ops.trim(d, start, end))


@tool
def cut(project: str, edl: str, start: float, end: float) -> dict:
    """Remove the output range start..end (seconds) and close the gap."""
    return _edit(project, edl, lambda p, d: ops.cut(d, start, end))


@tool
def set_segments(project: str, edl: str, ranges: list[list[float]]) -> dict:
    """Replace the whole segment list with SOURCE time ranges [[start, end], ...], played in the order
    given (use this to reorder, e.g. to put a hook first). Removes existing transitions."""
    return _edit(project, edl, lambda p, d: ops.set_segments(d, [(float(a), float(b)) for a, b in ranges]))


@tool
def remove_fillers(project: str, edl: str, fillers: list[str] | None = None, phrases: list[str] | None = None) -> dict:
    """Cut filler words. Default list: um, uh, er, ah, hmm and variants. `fillers` replaces that list;
    `phrases` adds multi-word fillers such as "you know" or "I mean" (use with care, they can be meaningful)."""
    return _edit(project, edl, lambda p, d: ops.remove_fillers(d, p.words(), fillers, phrases))


@tool
def remove_pauses(project: str, edl: str, max_pause: float = 0.6, keep: float = 0.25) -> dict:
    """Shorten every silence longer than max_pause seconds down to `keep` seconds."""
    return _edit(project, edl, lambda p, d: ops.remove_pauses(
        d, p.silences(), p.words() if p.has_transcript() else None, max_pause, keep))


@tool
def set_caption_template(project: str, edl: str, template: str | None = None, enabled: bool = True,
                         overrides: dict | None = None) -> dict:
    """Choose the caption template (see list_templates) or turn captions off with enabled=false.
    overrides change single template fields for this EDL, e.g. {"position": {"x": 0.5, "y": 0.8},
    "size": 0.05, "max_words": 2, "color": "#FFFFFF", "highlight_color": "#00E5FF", "uppercase": true}."""
    return _edit(project, edl, lambda p, d: ops.set_captions(d, template, enabled, overrides))


@tool
def add_zoom(project: str, edl: str, start: float, end: float, scale: float = 1.15, ease: float = 0.0,
             focus_x: float | None = None, focus_y: float | None = None, anchor: str = "content") -> dict:
    """Punch in from start to end (output seconds). scale 1.15 is a typical punch-in.
    ease 0 = instant punch-in cut; ease > 0 = ramp in and out over that many seconds.
    focus_x/focus_y (0-1 of the source frame) zoom towards a point other than the speaker's face."""
    focus = (focus_x, focus_y) if focus_x is not None and focus_y is not None else None
    return _edit(project, edl, lambda p, d: ops.add_zoom(d, start, end, scale, ease, focus, anchor))


@tool
def auto_zoom(project: str, edl: str, per_minute: float = 6.0, scale: float = 1.15, ease: float = 0.0) -> dict:
    """Place punch-in zooms automatically on the most emphatic sentences (loud, numbers, strong words).
    Replaces earlier automatic zooms; zooms added by hand are kept."""
    return _edit(project, edl, lambda p, d: ops.auto_zoom(d, p.words(), p.word_levels(), per_minute, scale, ease))


@tool
def set_transition(project: str, edl: str, at: float, type: str = "crossfade", duration: float = 0.3) -> dict:
    """Put a transition on the cut nearest to output time `at` (see get_edl for cut times).
    type: cut (removes it), crossfade, dip_black, flash_white, slide_left/right/up/down, wipe_left/right,
    zoom_in, blur."""
    return _edit(project, edl, lambda p, d: ops.set_transition(d, at, type, duration))


@tool
def set_music(project: str, edl: str, path: str | None, gain_db: float = -20.0, duck: bool = True,
              duck_amount_db: float = -10.0, offset: float = 0.0, fade_in: float = 0.5, fade_out: float = 1.5) -> dict:
    """Add a background music track under the whole video (path=null removes it). The track loops if it
    is shorter than the video. With duck=true it drops by duck_amount_db whenever the speaker talks.
    offset: where in the track to start (seconds)."""
    return _edit(project, edl, lambda p, d: ops.set_music(d, path, gain_db, duck, duck_amount_db, offset,
                                                         fade_in, fade_out))


@tool
def add_sfx(project: str, edl: str, at: float, sound: str = "whoosh", gain_db: float = -8.0,
            anchor: str = "content") -> dict:
    """Play a sound effect at output time `at`. sound: built-in whoosh, pop, click or thud, or a file path."""
    return _edit(project, edl, lambda p, d: ops.add_sfx(d, at, sound, gain_db, anchor))


@tool
def add_sfx_on_cuts(project: str, edl: str, sound: str = "whoosh", gain_db: float = -10.0,
                    which: str = "transitions", min_gap: float = 1.0) -> dict:
    """Add a sound on cuts. which: "transitions" (cuts with a transition), "zooms" (every punch-in),
    "cuts" (every cut) or "all". Replaces earlier automatic sounds."""
    return _edit(project, edl, lambda p, d: ops.sfx_on_cuts(d, sound, gain_db, which, min_gap))


@tool
def add_overlay(project: str, edl: str, template: str, start: float, duration: float | None = None,
                params: dict | None = None, anchor: str | None = None) -> dict:
    """Add a motion-graphic overlay from a template: title card, lower third or keyword callout.
    template: an id from list_templates, e.g. "title_card/plain-top". params: the template's text and
    colours, e.g. {"title": "3 mistakes"}. anchor defaults to "timeline" for title cards and "content"
    for the others."""
    return _edit(project, edl, lambda p, d: ops.add_overlay(d, template, start, duration, params, anchor))


@tool
def add_broll(project: str, edl: str, path: str, start: float, end: float, source_in: float = 0.0,
              fit: str = "cover", anchor: str = "content") -> dict:
    """Cover the speaker with a user-supplied clip or image from start to end (output seconds); the speaker's
    audio continues underneath. source_in: where to start inside the clip. fit: cover or contain.
    clipper does not generate or search for b-roll."""
    return _edit(project, edl, lambda p, d: ops.add_broll(d, path, start, end, source_in, fit, anchor))


@tool
def remove_item(project: str, edl: str, item_id: str) -> dict:
    """Remove one zoom, overlay, b-roll clip or sound by its id (ids are listed by get_edl)."""
    return _edit(project, edl, lambda p, d: ops.remove_item(d, item_id))


@tool
def clear_items(project: str, edl: str, kind: str) -> dict:
    """Remove all items of one kind: zooms, overlays, broll, sfx or transitions."""
    return _edit(project, edl, lambda p, d: ops.clear_items(d, kind))


@tool
def set_output(project: str, edl: str, aspect: str | None = None, fps: int | None = None,
               loudness: float | None = None) -> dict:
    """Change the output format. aspect: 9:16, 16:9, 1:1, 4:5 or source. loudness: target LUFS for the
    export (default -14)."""
    return _edit(project, edl, lambda p, d: ops.set_output(d, aspect, fps, loudness))


@tool
def set_framing(project: str, edl: str, x: float, y: float) -> dict:
    """Set the point of the source frame (0-1) that the crop and zooms keep centred. It is set to the
    speaker's face at ingest; change it if the crop looks off in a preview."""
    return _edit(project, edl, lambda p, d: ops.set_framing(d, x, y))


@tool
def list_templates() -> dict:
    """Everything that can be chosen by name: caption templates, overlay templates (with their parameters),
    transitions, built-in sounds and aspect ratios."""
    return {
        "captions": templates_mod.list_caption_templates(),
        "overlays": templates_mod.list_overlay_templates(),
        "transitions": ["cut", *TRANSITIONS],
        "sounds": list(audio_mod.BUILTIN_SFX),
        "aspects": ["source", *ASPECTS],
        "template_folders": [str(d) for d in templates_mod.template_dirs()],
    }


# ==========================================================================
# Reference style
# ==========================================================================

@tool
def analyze_style(reference: str, out_path: str | None = None, max_seconds: float = 90.0) -> list:
    """Analyse a reference short and write a style spec JSON (next to the reference unless out_path is
    given): caption look and animation, words per caption, average shot length, zoom frequency and
    strength, transition types, music and sound-effect presence. Also returns a sheet of frames."""
    spec, path = pipeline.style_from_reference(reference, out_path, max_seconds)
    out: list[Any] = [json.dumps({"style_spec": str(path), **spec}, indent=1)]
    if spec.get("contact_sheet"):
        out.append(_jpeg(spec["contact_sheet"], 1440))
    return out


@tool
def apply_style(project: str, edl: str, style: str, music: str | None = None) -> dict:
    """Apply a style spec to an EDL by mapping it onto the closest caption template, zooms, transitions,
    music level and sounds. The report lists what was matched, what was approximated and what could not
    be matched. music: a track to use if the reference has music."""
    return pipeline.apply_style_to(Project.open(project), edl, style, music)


# ==========================================================================
# Render
# ==========================================================================

def _sheet(video: str, duration: float, out: Path, n: int = 8) -> Image:
    times = [duration * (i + 0.5) / n for i in range(n)]
    contact_sheet(video, times, out, width=270, columns=4)
    return _jpeg(out, 1080)


@tool
def render_preview(project: str, edl: str, start: float | None = None, end: float | None = None) -> list:
    """Render a fast half-size preview (optionally only start..end of the output) and return its path
    plus a sheet of frames from it, so the result can be checked without playing the file."""
    proj = Project.open(project)
    result = pipeline.render_edl(proj, edl, "preview", start, end)
    sheet = _sheet(result["path"], result["duration"], proj.dir / "renders" / f"{safe_name(edl)}.preview-sheet.jpg")
    return [json.dumps(result, indent=1), sheet]


@tool
def render_frame(project: str, edl: str, at: float) -> list:
    """Render one full-size frame of the finished video at output time `at` and return it as an image.
    The quickest way to check captions, an overlay or the crop."""
    proj = Project.open(project)
    result = pipeline.render_edl(proj, edl, "export", frame_at=at)
    return [json.dumps({"path": result["path"], "at": at, "warnings": result["warnings"]}), _jpeg(result["path"])]


@tool
def export(project: str, edl: str, out_path: str | None = None, wait: bool = True) -> dict:
    """Render the final full-quality video. With wait=false it returns a job id at once; poll job_status."""
    proj = Project.open(project)
    if wait:
        return pipeline.render_edl(proj, edl, "export", out_path=out_path)
    job_id = uuid.uuid4().hex[:8]
    _jobs[job_id] = {"state": "running", "edl": edl, "started": time.time()}

    def work():
        try:
            _jobs[job_id].update(state="done", result=pipeline.render_edl(proj, edl, "export", out_path=out_path))
        except Exception as exc:  # noqa: BLE001
            _jobs[job_id].update(state="failed", error=str(exc))

    threading.Thread(target=work, daemon=True).start()
    return {"job_id": job_id, "state": "running"}


@tool
def job_status(job_id: str) -> dict:
    """State of an export started with wait=false: running, done (with the result) or failed (with the error)."""
    job = _jobs.get(job_id)
    if not job:
        raise ClipperError(f"No export job with id {job_id!r} in this server session.")
    return {**job, "elapsed": fmt_time(time.time() - job["started"])}


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
