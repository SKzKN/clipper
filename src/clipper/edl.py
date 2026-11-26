"""The edit decision list (EDL) and its timeline maths.

One EDL describes one output video. Clip mode writes several EDLs for one
source; polish mode writes one. Both go through the same renderer.

Time bases
----------
`segments` are ranges of *source* time, played back to back in list order.
Every other timed item carries an `anchor`:

* ``"source"``   - times are source seconds. The item sticks to the footage,
  so it keeps its place when fillers or pauses are cut later.
* ``"timeline"`` - times are output seconds (a title card at 0, say).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .ffmpeg import ClipperError

EDL_VERSION = 1

ASPECTS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350)}

# name -> ffmpeg xfade transition. "cut" is the absence of a transition.
TRANSITIONS = {
    "crossfade": "fade",
    "dip_black": "fadeblack",
    "flash_white": "fadewhite",
    "slide_left": "slideleft",
    "slide_right": "slideright",
    "slide_up": "slideup",
    "slide_down": "slidedown",
    "wipe_left": "wipeleft",
    "wipe_right": "wiperight",
    "zoom_in": "zoomin",
    "blur": "hblur",
}

ITEM_LISTS = ("zooms", "overlays", "broll", "sfx")
_PREFIX = {"zooms": "z", "overlays": "o", "broll": "b", "sfx": "x"}

MIN_SEGMENT = 0.1  # seconds; shorter slivers are dropped


def output_size(src_w: int, src_h: int, aspect: str) -> tuple[int, int]:
    if aspect == "source":
        scale = min(1.0, 1080 / min(src_w, src_h))
        return int(src_w * scale) // 2 * 2, int(src_h * scale) // 2 * 2
    if aspect not in ASPECTS:
        raise ClipperError(f"Unknown aspect {aspect!r}. Use one of: source, {', '.join(ASPECTS)}.")
    return ASPECTS[aspect]


def output_fps(src_fps: float) -> int:
    return int(min(60, max(24, round(src_fps or 30))))


def new_edl(name: str, mode: str, source: dict, segments: list[tuple[float, float]] | None = None,
            aspect: str = "9:16", framing: tuple[float, float] = (0.5, 0.42)) -> dict:
    if mode not in ("polish", "clip"):
        raise ClipperError("mode must be 'polish' or 'clip'.")
    width, height = output_size(source["width"], source["height"], aspect)
    if segments is None:
        segments = [(0.0, source["duration"])]
    edl = {
        "version": EDL_VERSION,
        "name": name,
        "mode": mode,
        "source": {k: source[k] for k in ("path", "duration", "width", "height", "fps")},
        "output": {"aspect": aspect, "width": width, "height": height, "fps": output_fps(source["fps"]),
                   "loudness": -14.0},
        "framing": {"x": round(framing[0], 4), "y": round(framing[1], 4)},
        "segments": [{"id": "", "start": round(a, 3), "end": round(b, 3)} for a, b in segments],
        "captions": {"enabled": False, "template": None, "overrides": {}},
        "zooms": [],
        "overlays": [],
        "broll": [],
        "music": None,
        "sfx": [],
        "notes": [],
    }
    renumber(edl)
    return edl


def renumber(edl: dict) -> None:
    for i, seg in enumerate(edl["segments"], 1):
        seg["id"] = f"s{i:03d}"


def next_id(edl: dict, kind: str) -> str:
    prefix = _PREFIX[kind]
    used = {item["id"] for item in edl[kind]}
    n = len(edl[kind]) + 1
    while f"{prefix}{n}" in used:
        n += 1
    return f"{prefix}{n}"


def load(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise ClipperError(f"EDL not found: {path}")
    edl = json.loads(path.read_text())
    if edl.get("version") != EDL_VERSION:
        raise ClipperError(f"{path.name} is EDL version {edl.get('version')}; this build reads version {EDL_VERSION}.")
    for key in ITEM_LISTS:
        edl.setdefault(key, [])
    edl.setdefault("notes", [])
    edl.setdefault("music", None)
    edl.setdefault("captions", {"enabled": False, "template": None, "overrides": {}})
    return edl


def save(edl: dict, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(edl, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


# --------------------------------------------------------------------------
# Timeline
# --------------------------------------------------------------------------

@dataclass
class TSeg:
    index: int
    id: str
    src_start: float  # frame-quantised source seconds
    src_end: float
    tl_start: float
    tl_end: float
    transition_in: dict | None = None

    @property
    def duration(self) -> float:
        return self.tl_end - self.tl_start


class Timeline:
    """Maps between source time and output time for one EDL.

    Segment edges are snapped to the output frame grid, so picture and sound
    are cut at exactly the same instants and cannot drift apart.
    """

    def __init__(self, edl: dict):
        self.fps = int(edl["output"]["fps"])
        self.segs: list[TSeg] = []
        t = 0
        for i, seg in enumerate(edl["segments"]):
            a = int(round(seg["start"] * self.fps))
            b = int(round(seg["end"] * self.fps))
            if b <= a:
                continue
            self.segs.append(TSeg(i, seg.get("id", ""), a / self.fps, b / self.fps, t / self.fps,
                                  (t + b - a) / self.fps, seg.get("transition_in")))
            t += b - a
        self.duration = t / self.fps
        self.offset = 0.0  # timeline seconds trimmed off the front by sliced()

    # -- conversions -----------------------------------------------------
    def to_timeline(self, src_t: float, snap: str | None = None) -> float | None:
        """Source time -> output time. With `snap`, a time that falls in a
        removed range moves to the next ('next') or previous ('prev') kept frame."""
        eps = 0.5 / self.fps
        for seg in self.segs:
            if seg.src_start - eps <= src_t < seg.src_end - eps:
                return seg.tl_start + max(0.0, src_t - seg.src_start)
        if snap == "next":
            later = [s for s in self.segs if s.src_start >= src_t]
            if later:
                return min(later, key=lambda s: s.src_start).tl_start
        if snap == "prev":
            earlier = [s for s in self.segs if s.src_end <= src_t + eps]
            if earlier:
                return max(earlier, key=lambda s: s.src_end).tl_end
        return None

    def to_source(self, tl_t: float) -> float:
        if not self.segs:
            return 0.0
        tl_t = min(max(tl_t, 0.0), self.duration)
        for seg in self.segs:
            if tl_t < seg.tl_end:
                return seg.src_start + (tl_t - seg.tl_start)
        return self.segs[-1].src_end

    def map_range(self, src_a: float, src_b: float) -> list[tuple[float, float]]:
        """Source range -> output ranges, touching pieces merged."""
        out: list[list[float]] = []
        for seg in self.segs:
            a, b = max(src_a, seg.src_start), min(src_b, seg.src_end)
            if b - a <= 1e-6:
                continue
            ta, tb = seg.tl_start + (a - seg.src_start), seg.tl_start + (b - seg.src_start)
            if out and abs(out[-1][1] - ta) < 1e-6:
                out[-1][1] = tb
            else:
                out.append([ta, tb])
        return [(a, b) for a, b in out]

    def source_ranges(self, tl_a: float, tl_b: float) -> list[tuple[float, float]]:
        """Output range -> the source ranges it shows."""
        out = []
        for seg in self.segs:
            a, b = max(tl_a, seg.tl_start), min(tl_b, seg.tl_end)
            if b - a > 1e-6:
                out.append((seg.src_start + (a - seg.tl_start), seg.src_start + (b - seg.tl_start)))
        return out

    # -- structure -------------------------------------------------------
    def cuts(self) -> list[dict]:
        """Every join between two segments, in output order."""
        out = []
        for prev, seg in zip(self.segs, self.segs[1:]):
            out.append({
                "time": seg.tl_start,
                "segment": seg.id,
                "index": seg.index,
                "removed": round(seg.src_start - prev.src_end, 3),
                "transition": (seg.transition_in or {}).get("type", "cut"),
            })
        return out

    def sliced(self, a: float, b: float) -> "Timeline":
        """A copy limited to output range [a, b), re-based to start at 0."""
        a = round(max(0.0, a) * self.fps) / self.fps
        b = round(min(self.duration, b) * self.fps) / self.fps
        new = Timeline.__new__(Timeline)
        new.fps, new.segs, new.offset = self.fps, [], self.offset + a
        for seg in self.segs:
            ta, tb = max(a, seg.tl_start), min(b, seg.tl_end)
            if tb - ta <= 1e-6:
                continue
            new.segs.append(TSeg(seg.index, seg.id, seg.src_start + (ta - seg.tl_start),
                                 seg.src_start + (tb - seg.tl_start), ta - a, tb - a,
                                 seg.transition_in if ta == seg.tl_start and ta > a else None))
        new.duration = max(0.0, b - a) if new.segs else 0.0
        return new


# --------------------------------------------------------------------------
# Resolving anchored items onto the timeline
# --------------------------------------------------------------------------

def item_range(tl: Timeline, item: dict, start_key: str = "start", end_key: str = "end") -> list[tuple[float, float]]:
    a, b = float(item[start_key]), float(item[end_key])
    if item.get("anchor", "timeline") == "source":
        return tl.map_range(a, b)
    a, b = max(0.0, a), min(tl.duration, b)
    return [(a, b)] if b - a > 1e-6 else []


def item_point(tl: Timeline, item: dict, key: str = "at") -> float | None:
    t = float(item[key])
    if item.get("anchor", "timeline") == "source":
        return tl.to_timeline(t, snap="next")
    return t if 0 <= t <= tl.duration else None


def _window(items: list[dict], window: tuple[float, float], point: str | None = None) -> list[dict]:
    """Shift resolved items into a preview window and drop what falls outside.
    `phase` records how far into an item the window starts."""
    a, b = window
    out = []
    for item in items:
        if point:
            if a <= item[point] < b:
                out.append({**item, point: item[point] - a})
            continue
        start, end = max(a, item["tl_start"]), min(b, item["tl_end"])
        if end - start > 1e-6:
            out.append({**item, "tl_start": start - a, "tl_end": end - a, "phase": start - item["tl_start"]})
    return out


def resolve(edl: dict, tl: Timeline | None = None, window: tuple[float, float] | None = None) -> dict:
    """Place every item on the output timeline. Items whose footage was cut
    away are dropped and listed under `warnings`. `tl` is the full timeline;
    `window` limits the result to an output range, re-based to start at 0."""
    tl = tl or Timeline(edl)
    if window:
        full = resolve(edl, tl)
        return {"zooms": _window(full["zooms"], window), "overlays": _window(full["overlays"], window),
                "broll": _window(full["broll"], window), "sfx": _window(full["sfx"], window, point="tl_at"),
                "warnings": full["warnings"]}
    warnings: list[str] = []
    zooms, overlays, broll, sfx = [], [], [], []

    for z in edl["zooms"]:
        ranges = item_range(tl, z)
        if not ranges:
            warnings.append(f"zoom {z['id']} is outside the kept footage and will not show.")
        for a, b in ranges:
            zooms.append({**z, "tl_start": a, "tl_end": b})

    for o in edl["overlays"]:
        span = {**o, "end": float(o["start"]) + float(o["duration"])}
        ranges = item_range(tl, span)
        if not ranges:
            warnings.append(f"overlay {o['id']} is outside the kept footage and will not show.")
            continue
        a = ranges[0][0]
        total = sum(b2 - a2 for a2, b2 in ranges) if o.get("anchor") == "source" else ranges[0][1] - a
        overlays.append({**o, "tl_start": a, "tl_end": min(tl.duration, a + total)})

    for br in edl["broll"]:
        ranges = item_range(tl, br)
        if not ranges:
            warnings.append(f"b-roll {br['id']} is outside the kept footage and will not show.")
            continue
        a = ranges[0][0]
        total = sum(b2 - a2 for a2, b2 in ranges) if br.get("anchor") == "source" else ranges[0][1] - a
        broll.append({**br, "tl_start": a, "tl_end": min(tl.duration, a + total)})

    for s in edl["sfx"]:
        t = item_point(tl, s)
        if t is None:
            warnings.append(f"sfx {s['id']} is outside the kept footage and will not play.")
            continue
        sfx.append({**s, "tl_at": t})

    return {"zooms": zooms, "overlays": overlays, "broll": broll, "sfx": sfx, "warnings": warnings}


def validate(edl: dict) -> list[str]:
    """Problems that would stop a render."""
    problems: list[str] = []
    src = edl["source"]
    if not edl["segments"]:
        problems.append("The EDL has no segments, so there is nothing to render.")
    for seg in edl["segments"]:
        if seg["end"] <= seg["start"]:
            problems.append(f"Segment {seg['id']} ends before it starts.")
        if seg["start"] < -1e-3 or seg["end"] > src["duration"] + 0.05:
            problems.append(f"Segment {seg['id']} ({seg['start']}-{seg['end']}s) is outside the source "
                            f"(0-{src['duration']}s).")
        tr = seg.get("transition_in")
        if tr and tr.get("type") not in TRANSITIONS:
            problems.append(f"Segment {seg['id']} has unknown transition {tr.get('type')!r}.")
    if not Path(src["path"]).exists():
        problems.append(f"Source video is missing: {src['path']}")
    if edl.get("music") and not Path(edl["music"]["path"]).exists():
        problems.append(f"Music file is missing: {edl['music']['path']}")
    for br in edl["broll"]:
        if not Path(br["path"]).exists():
            problems.append(f"B-roll file is missing: {br['path']}")
    return problems


def summary(edl: dict) -> dict:
    tl = Timeline(edl)
    res = resolve(edl, tl)
    return {
        "name": edl["name"],
        "mode": edl["mode"],
        "duration": round(tl.duration, 2),
        "source_duration": edl["source"]["duration"],
        "output": edl["output"],
        "segments": len(tl.segs),
        "cuts": len(tl.cuts()),
        "transitions": sum(1 for c in tl.cuts() if c["transition"] != "cut"),
        "captions": edl["captions"]["template"] if edl["captions"]["enabled"] else None,
        "zooms": len(edl["zooms"]),
        "overlays": [f"{o['id']}:{o['template']}" for o in edl["overlays"]],
        "broll": len(edl["broll"]),
        "music": Path(edl["music"]["path"]).name if edl.get("music") else None,
        "sfx": len(edl["sfx"]),
        "warnings": res["warnings"] + validate(edl),
    }
