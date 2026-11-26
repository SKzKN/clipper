"""Render an EDL to video with one ffmpeg filter graph.

Picture: each *run* of segments (a stretch with no transition and no jump
backwards in the source) is one seeked input, cut with `select`, reframed and
zoomed with `scale`+`crop`. Runs are joined with `concat` (cut) or `xfade`
(transition). B-roll, captions and overlay frames are composited on top.

Sound: mixed in numpy (audio.py) and muxed in, so cuts are sample-accurate
and line up with the frame-quantised picture cuts.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import audio as audio_mod
from . import ffmpeg
from .captions import build_ass, timeline_words
from .edl import TRANSITIONS, Timeline, TSeg, resolve, validate
from .ffmpeg import ClipperError, log
from .overlays import render_overlays
from .templates import font_dirs, load_caption_template


@dataclass
class Run:
    segs: list[TSeg]
    transition: dict | None = None  # how this run is joined to the previous one
    head: int = 0                   # extra frames before the first segment (transition handle)
    tail: int = 0                   # extra frames after the last segment
    ranges: list[tuple[int, int]] = field(default_factory=list)  # absolute source frames [a, b)

    @property
    def frames(self) -> int:
        return sum(b - a for a, b in self.ranges)


def build_runs(tl: Timeline, source_duration: float) -> list[Run]:
    fps = tl.fps
    runs: list[Run] = []
    for seg in tl.segs:
        if runs and not seg.transition_in and seg.src_start >= runs[-1].segs[-1].src_end - 1e-6:
            runs[-1].segs.append(seg)
        else:
            runs.append(Run([seg], seg.transition_in if runs else None))

    def content(run: Run) -> int:
        return sum(int(round(s.duration * fps)) for s in run.segs)

    for prev, run in zip(runs, runs[1:]):
        if not run.transition:
            continue
        half = int(float(run.transition.get("duration", 0.3)) / 2 * fps)
        room_after = int((source_duration - prev.segs[-1].src_end) * fps) - 1
        room_before = int(run.segs[0].src_start * fps)
        half = min(half, room_after, room_before, content(prev) // 2, content(run) // 2)
        if half < 1:
            run.transition = None
        else:
            prev.tail, run.head = half, half
    for run in runs:
        run.ranges = []
        for seg in run.segs:
            a = int(round(seg.src_start * fps))
            run.ranges.append((a, a + int(round(seg.duration * fps))))
        run.ranges[0] = (run.ranges[0][0] - run.head, run.ranges[0][1])
        run.ranges[-1] = (run.ranges[-1][0], run.ranges[-1][1] + run.tail)
    return runs


def _weight(a: float, b: float, ease: float, fps: int, var: str = "t") -> str:
    """0..1 expression for how far a zoom is pushed in at time `var`."""
    eps = 0.5 / fps
    if ease <= 0:
        return f"(gte({var},{a - eps:.4f})*lt({var},{b - eps:.4f}))"
    e = max(1.0 / fps, min(ease, (b - a) / 2))
    p = f"(clip(({var}-{a:.4f})/{e:.4f},0,1)*clip(({b:.4f}-{var})/{e:.4f},0,1))"
    return f"({p}*{p}*(3-2*{p}))"


def _run_chain(run: Run, index: int, edl: dict, zooms: list[dict], fps: int) -> str:
    out_w, out_h = edl["output"]["width"], edl["output"]["height"]
    src_w, src_h = edl["source"]["width"], edl["source"]["height"]
    ax, ay = edl["framing"]["x"], edl["framing"]["y"]
    cover = max(out_w / src_w, out_h / src_h)
    base_w = 2 * -(-int(src_w * cover + 0.999) // 2)
    base_h = 2 * -(-int(src_h * cover + 0.999) // 2)
    x0 = int(round(min(max(ax * base_w - out_w / 2, 0), base_w - out_w)))
    y0 = int(round(min(max(ay * base_h - out_h / 2, 0), base_h - out_h)))

    first = run.ranges[0][0]
    terms = "+".join(f"between(n,{a - first},{b - first - 1})" for a, b in run.ranges)
    chain = [f"[{index}:v]fps={fps}", f"select='{terms}'", f"setpts=N/{fps}/TB",
             f"scale={base_w}:{base_h}:flags=bicubic", f"crop={out_w}:{out_h}:{x0}:{y0}", "setsar=1", "format=yuv420p"]

    run_start = run.segs[0].tl_start - run.head / fps
    run_end = run.segs[-1].tl_end + run.tail / fps
    local = [z for z in zooms if z["tl_end"] > run_start and z["tl_start"] < run_end]
    if local:
        # Zoom with `perspective`: sub-pixel accurate, constant frame size, and only
        # switched on while a zoom is active. The framing point stays put on screen.
        px, py = ax * base_w - x0, ay * base_h - y0
        tvar = f"((in-1)/{fps})"  # `in` counts frames from 1
        zexpr, pxexpr, pyexpr, active = "1", f"{px:.2f}", f"{py:.2f}", []
        for z in local:
            a, b = z["tl_start"] - run_start, z["tl_end"] - run_start
            wgt = _weight(a, b, float(z.get("ease", 0)), fps, tvar)
            zexpr += f"+{float(z['scale']) - 1:.4f}*{wgt}"
            if z.get("focus"):
                pxexpr += f"+{z['focus'][0] * base_w - x0 - px:.2f}*{wgt}"
                pyexpr += f"+{z['focus'][1] * base_h - y0 - py:.2f}*{wgt}"
            active.append(f"gte(t,{a - 0.5 / fps:.4f})*lt(t,{b - 0.5 / fps:.4f})")
        left = f"(clip({pxexpr},0,W)*(1-1/({zexpr})))"
        top = f"(clip({pyexpr},0,H)*(1-1/({zexpr})))"
        right, bottom = f"({left}+W/({zexpr}))", f"({top}+H/({zexpr}))"
        chain.append(
            f"perspective=x0='{left}':y0='{top}':x1='{right}':y1='{top}':x2='{left}':y2='{bottom}'"
            f":x3='{right}':y3='{bottom}':sense=source:eval=frame:interpolation=cubic"
            f":enable='{'+'.join(active)}'"
        )
    return ",".join(chain) + f"[r{index}]"


def _fit_filters(fit: str, out_w: int, out_h: int) -> str:
    if fit == "contain":
        return (f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
                f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black")
    return f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h}"


def render(edl: dict, out_path: str | Path, words: list[dict] | None, cache_dir: str | Path,
           quality: str = "export", window: tuple[float, float] | None = None,
           frame_at: float | None = None) -> dict:
    """Render `edl` to `out_path`.

    quality: "export" (full size) or "preview" (half size, fast encode).
    window:  only render this output range (seconds).
    frame_at: render a single PNG frame at this output time instead of a video.
    """
    problems = validate(edl)
    if problems:
        raise ClipperError("This EDL cannot be rendered:\n- " + "\n- ".join(problems))
    caps = ffmpeg.capabilities()
    started = time.time()
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    fps = int(edl["output"]["fps"])
    out_w, out_h = edl["output"]["width"], edl["output"]["height"]
    full = Timeline(edl)
    if frame_at is not None:
        t = min(max(0.0, float(frame_at)), max(0.0, full.duration - 1 / fps))
        window = (t, t + 1 / fps)
    if window:
        a, b = max(0.0, window[0]), min(full.duration, window[1])
        if b - a < 0.5 / fps:
            raise ClipperError(f"Range {window[0]:.2f}-{window[1]:.2f}s is empty or outside the video "
                               f"(0-{full.duration:.2f}s).")
        a = round(a * fps) / fps
        window = (a, max(round(b * fps) / fps, a + 1 / fps))
        tl = full.sliced(*window)
    else:
        tl = full
    resolved = resolve(edl, full, window)
    warnings = list(resolved["warnings"])
    total_frames = int(round(tl.duration * fps))
    if total_frames < 1:
        raise ClipperError("Nothing to render: the edit is empty.")

    work = Path(tempfile.mkdtemp(prefix="render-", dir=cache_dir))
    try:
        inputs: list[str] = []
        graph: list[str] = []

        # -- picture: runs ------------------------------------------------
        runs = build_runs(tl, edl["source"]["duration"])
        for i, run in enumerate(runs):
            first, last = run.ranges[0][0], run.ranges[-1][1]
            inputs += ["-ss", f"{first / fps:.6f}", "-t", f"{(last - first + 2) / fps:.6f}", "-i", edl["source"]["path"]]
            graph.append(_run_chain(run, i, edl, resolved["zooms"], fps))
        acc, acc_frames = "r0", runs[0].frames
        for i, run in enumerate(runs[1:], 1):
            label = f"j{i}"
            if run.transition:
                d = 2 * run.head
                offset = (acc_frames - d) / fps
                kind = TRANSITIONS[run.transition["type"]]
                graph.append(f"[{acc}][r{i}]xfade=transition={kind}:duration={d / fps:.6f}:offset={offset:.6f}[{label}]")
                acc_frames += run.frames - d
            else:
                graph.append(f"[{acc}][r{i}]concat=n=2:v=1:a=0[{label}]")
                acc_frames += run.frames
            acc = label
        n_inputs = len(runs)

        # -- sound ---------------------------------------------------------
        audio_index = None
        mix_report: dict = {}
        if frame_at is None:
            mix, mix_report = audio_mod.mix_edl(edl, tl, resolved, full.duration, window[0] if window else 0.0)
            audio_mod.write_wav(work / "mix.wav", mix)
            inputs += ["-i", "mix.wav"]
            audio_index = n_inputs
            n_inputs += 1

        # -- b-roll --------------------------------------------------------
        for k, br in enumerate(resolved["broll"]):
            dur = br["tl_end"] - br["tl_start"]
            if br.get("still"):
                inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.4f}", "-i", br["path"]]
            else:
                inputs += ["-ss", f"{br.get('source_in', 0) + br.get('phase', 0):.4f}", "-t", f"{dur + 0.1:.4f}",
                           "-i", br["path"]]
            graph.append(f"[{n_inputs}:v]fps={fps},{_fit_filters(br.get('fit', 'cover'), out_w, out_h)},setsar=1,"
                         f"format=yuv420p,setpts=PTS-STARTPTS+{br['tl_start']:.4f}/TB[b{k}]")
            graph.append(f"[{acc}][b{k}]overlay=enable='between(t,{br['tl_start']:.4f},{br['tl_end'] - 0.5 / fps:.4f})'"
                         f":eof_action=pass[bo{k}]")
            acc = f"bo{k}"
            n_inputs += 1

        # -- captions ------------------------------------------------------
        caption_groups = 0
        cap = edl["captions"]
        if cap.get("enabled") and cap.get("template"):
            if not words:
                warnings.append("Captions are on but there is no transcript, so no captions were drawn.")
            elif not caps["ass"]:
                warnings.append("This ffmpeg build has no libass, so captions were skipped.")
            else:
                tpl = load_caption_template(cap["template"], cap.get("overrides"))
                text, caption_groups = build_ass(timeline_words(words, full), tpl, out_w, out_h, window)
                (work / "captions.ass").write_text(text, encoding="utf-8")
                fonts = [f for d in font_dirs() for f in d.iterdir() if f.suffix.lower() in (".ttf", ".otf", ".ttc")]
                opt = ""
                if fonts:
                    (work / "fonts").mkdir()
                    for f in fonts:
                        try:
                            os.symlink(f, work / "fonts" / f.name)
                        except FileExistsError:
                            pass
                    opt = ":fontsdir=fonts"
                graph.append(f"[{acc}]ass=captions.ass{opt}[cap]")
                acc = "cap"

        # -- motion-graphic overlays --------------------------------------
        rendered = render_overlays(resolved["overlays"], out_w, out_h, fps, cache_dir / "overlays")
        for k, ov in enumerate(rendered):
            os.symlink(ov["folder"], work / f"ov{k}")
            inputs += ["-framerate", str(fps), "-start_number", str(ov["first"]), "-t", f"{ov['count'] / fps:.4f}",
                       "-i", f"ov{k}/%05d.png"]
            graph.append(f"[{n_inputs}:v]format=rgba,setpts=PTS-STARTPTS+{ov['tl_start']:.4f}/TB[o{k}]")
            graph.append(f"[{acc}][o{k}]overlay=x={ov['region']['x']}:y={ov['region']['y']}"
                         f":enable='between(t,{ov['tl_start']:.4f},{ov['tl_end'] - 0.5 / fps:.4f})'"
                         f":eof_action=pass[oo{k}]")
            acc = f"oo{k}"
            n_inputs += 1

        # -- output --------------------------------------------------------
        if quality == "preview" and frame_at is None:
            graph.append(f"[{acc}]scale={out_w // 4 * 2}:{out_h // 4 * 2}:flags=bilinear[vout]")
        else:
            graph.append(f"[{acc}]null[vout]")
        (work / "graph.txt").write_text(";\n".join(graph) + "\n")
        args = [*inputs, "-/filter_complex", "graph.txt", "-map", "[vout]"]
        if frame_at is not None:
            args += ["-frames:v", "1", "-update", "1", str(out_path)]
        else:
            args += ["-map", f"{audio_index}:a"]
            loud = edl["output"].get("loudness")
            if quality == "export" and loud is not None and tl.duration >= 3 and caps["loudnorm"]:
                args += ["-af", f"loudnorm=I={float(loud):.1f}:TP=-1.5:LRA=11"]
            if quality == "preview":
                args += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28"]
            else:
                args += ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
            args += ["-pix_fmt", "yuv420p", "-r", str(fps), "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                     "-t", f"{tl.duration:.6f}", "-movflags", "+faststart", str(out_path)]
        log(f"rendering {out_path.name}: {tl.duration:.1f}s, {len(runs)} run(s), {len(tl.segs)} segment(s)")
        ffmpeg.run(args, cwd=work, what="Render", total=tl.duration if frame_at is None else None)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return {
        "path": str(out_path),
        "duration": round(tl.duration, 3),
        "size": [out_w, out_h] if quality == "export" or frame_at is not None else [out_w // 4 * 2, out_h // 4 * 2],
        "fps": fps,
        "segments": len(tl.segs),
        "runs": len(runs),
        "transitions": sum(1 for r in runs if r.transition),
        "zooms": len(resolved["zooms"]),
        "caption_groups": caption_groups,
        "overlays": len(rendered),
        "broll": len(resolved["broll"]),
        "audio": mix_report,
        "warnings": warnings,
        "render_seconds": round(time.time() - started, 1),
    }
