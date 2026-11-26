"""Edit operations. Each one changes an EDL dict in place and returns a short
report of what it did. Times passed in are *output timeline seconds* unless a
parameter says otherwise; items that should follow the footage are stored
source-anchored (see edl.py)."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from . import audio as audio_mod
from .edl import ASPECTS, MIN_SEGMENT, TRANSITIONS, Timeline, next_id, output_size, renumber
from .ffmpeg import ClipperError

DEFAULT_FILLERS = ("um", "umm", "uh", "uhh", "uhm", "er", "erm", "ah", "hmm", "mm", "mhm", "eh")

EMPHASIS_WORDS = {
    "never", "always", "every", "nobody", "everyone", "best", "worst", "biggest", "huge", "massive", "secret",
    "mistake", "mistakes", "truth", "actually", "literally", "exactly", "only", "most", "important", "crazy",
    "insane", "incredible", "free", "stop", "must", "need", "problem", "key", "simple", "easy", "hard", "wrong",
    "first", "number", "million", "billion", "percent", "double", "triple", "zero", "nothing", "everything",
}


def norm_word(text: str) -> str:
    return re.sub(r"[^\w']+", "", text.lower())


# --------------------------------------------------------------------------
# Structure: trimming and cutting
# --------------------------------------------------------------------------

def _subtract(segments: list[dict], a: float, b: float) -> list[dict]:
    """Remove source range [a, b) from every segment."""
    out = []
    for seg in segments:
        if b <= seg["start"] or a >= seg["end"]:
            out.append(seg)
            continue
        if a - seg["start"] >= MIN_SEGMENT:
            out.append({**seg, "end": round(a, 3)})
        if seg["end"] - b >= MIN_SEGMENT:
            tail = {**seg, "start": round(b, 3)}
            if a - seg["start"] >= MIN_SEGMENT:
                tail.pop("transition_in", None)  # the new join is a plain cut
            out.append(tail)
    return out


def cut_source_ranges(edl: dict, ranges: list[tuple[float, float]]) -> float:
    before = Timeline(edl).duration
    segs = edl["segments"]
    for a, b in ranges:
        if b > a:
            segs = _subtract(segs, a, b)
    if not segs:
        raise ClipperError("That cut would remove the whole video. Nothing was changed.")
    edl["segments"] = segs
    renumber(edl)
    return round(before - Timeline(edl).duration, 3)


def set_segments(edl: dict, ranges: list[tuple[float, float]]) -> dict:
    """Replace the segment list with source ranges, in playback order."""
    dur = edl["source"]["duration"]
    clean = []
    for a, b in ranges:
        a, b = max(0.0, float(a)), min(dur, float(b))
        if b - a < MIN_SEGMENT:
            raise ClipperError(f"Segment {a:.2f}-{b:.2f}s is empty or outside the source (0-{dur:.2f}s).")
        clean.append({"id": "", "start": round(a, 3), "end": round(b, 3)})
    if not clean:
        raise ClipperError("At least one segment is required.")
    edl["segments"] = clean
    renumber(edl)
    return {"segments": len(clean), "duration": round(Timeline(edl).duration, 2)}


def trim(edl: dict, start: float | None = None, end: float | None = None) -> dict:
    """Keep only output range [start, end]."""
    tl = Timeline(edl)
    a = 0.0 if start is None else float(start)
    b = tl.duration if end is None else float(end)
    if not (0 <= a < b <= tl.duration + 1e-3):
        raise ClipperError(f"Trim range {a:.2f}-{b:.2f}s is not inside the video (0-{tl.duration:.2f}s).")
    kept = tl.source_ranges(a, b)
    by_start = {round(s.src_start, 3): s for s in tl.segs}
    segs = []
    for sa, sb in kept:
        seg = {"id": "", "start": round(sa, 3), "end": round(sb, 3)}
        original = by_start.get(round(sa, 3))
        if original and original.transition_in and segs:
            seg["transition_in"] = original.transition_in
        segs.append(seg)
    edl["segments"] = segs
    renumber(edl)
    return {"duration": round(Timeline(edl).duration, 2), "removed": round(tl.duration - (b - a), 2)}


def cut(edl: dict, start: float, end: float) -> dict:
    """Remove output range [start, end]."""
    tl = Timeline(edl)
    if not (0 <= start < end <= tl.duration + 1e-3):
        raise ClipperError(f"Cut range {start:.2f}-{end:.2f}s is not inside the video (0-{tl.duration:.2f}s).")
    removed = cut_source_ranges(edl, tl.source_ranges(start, end))
    return {"removed": removed, "duration": round(Timeline(edl).duration, 2)}


def _word_span(words: list[dict], first: int, last: int) -> tuple[int, int]:
    if not (0 <= first < len(words) and 0 <= last < len(words)):
        raise ClipperError(f"Word index out of range (the transcript has {len(words)} words).")
    return min(first, last), max(first, last)


def cut_words(edl: dict, words: list[dict], first: int, last: int, keep_gap: float = 0.1) -> dict:
    """Cut the transcript words first..last (indices), with the same breathing room as filler removal."""
    i, j = _word_span(words, first, last)
    a, b = words[i]["start"], words[j]["end"]
    prev_end = words[i - 1]["end"] if i > 0 else a
    next_start = words[j + 1]["start"] if j + 1 < len(words) else b
    lo = min(a, max(prev_end, a - 0.6))
    hi = max(b, min(next_start, b + 0.6))
    if hi - lo > keep_gap + 0.05:
        lo, hi = lo + keep_gap / 2, hi - keep_gap / 2
    else:
        lo, hi = a, b
    removed = cut_source_ranges(edl, [(lo, hi)])
    return {"removed_seconds": removed, "words": j - i + 1, "duration": round(Timeline(edl).duration, 2)}


def restore_source_range(edl: dict, a: float, b: float) -> dict:
    """Bring source range a..b back into the edit (the opposite of a cut)."""
    segs = edl["segments"]
    if any(y["start"] < x["end"] - 1e-6 for x, y in zip(segs, segs[1:])):
        raise ClipperError("This edit's segments were reordered, so footage cannot be restored by range. "
                           "Use set_segments instead.")
    a, b = max(0.0, a), min(edl["source"]["duration"], b)
    if b - a < 0.02:
        raise ClipperError("Nothing to restore.")
    before = Timeline(edl).duration
    items = [dict(seg) for seg in segs] + [{"id": "", "start": round(a, 3), "end": round(b, 3)}]
    items.sort(key=lambda x: x["start"])
    merged = [items[0]]
    for seg in items[1:]:
        if seg["start"] <= merged[-1]["end"] + 0.04:
            merged[-1]["end"] = max(merged[-1]["end"], seg["end"])
        else:
            merged.append(seg)
    edl["segments"] = merged
    renumber(edl)
    return {"restored_seconds": round(Timeline(edl).duration - before, 3), "duration": round(Timeline(edl).duration, 2)}


def restore_words(edl: dict, words: list[dict], first: int, last: int) -> dict:
    i, j = _word_span(words, first, last)
    a = words[i]["start"] - 0.06
    b = words[j]["end"] + 0.06
    if i > 0:
        a = max(a, (words[i - 1]["end"] + words[i]["start"]) / 2)
    if j + 1 < len(words):
        b = min(b, (words[j]["end"] + words[j + 1]["start"]) / 2)
    return restore_source_range(edl, a, b)


def update_item(edl: dict, item_id: str, fields: dict) -> dict:
    """Change properties of an existing zoom, overlay, b-roll clip or sound."""
    allowed = {"zooms": ("scale", "ease"), "overlays": ("params", "duration"), "sfx": ("gain_db", "sound"),
               "broll": ("fit", "source_in")}
    for kind, keys in allowed.items():
        for item in edl[kind]:
            if item["id"] != item_id:
                continue
            bad = sorted(set(fields) - set(keys))
            if bad:
                raise ClipperError(f"{kind[:-1] if kind != 'sfx' else 'sfx'} items only take: {', '.join(keys)} (got {bad}).")
            if "scale" in fields and not 1.0 < float(fields["scale"]) <= 2.0:
                raise ClipperError("Zoom scale must be above 1.0 and at most 2.0.")
            if "duration" in fields and float(fields["duration"]) < 0.2:
                raise ClipperError("Duration must be at least 0.2s.")
            if "params" in fields:
                from .templates import load_overlay_template

                tpl = load_overlay_template(item["template"])
                unknown = sorted(set(fields["params"]) - set(tpl["params"]))
                if unknown:
                    raise ClipperError(f"Template {tpl['id']} has no parameter(s) {unknown}.")
            item.update(fields)
            return {"updated": item}
    raise ClipperError(f"No item with id {item_id!r}.")


def _kept_words(edl: dict, words: list[dict]) -> list[tuple[int, dict]]:
    """Words whose midpoint is still in the edit, with their transcript index."""
    tl = Timeline(edl)
    return [(i, w) for i, w in enumerate(words) if tl.to_timeline((w["start"] + w["end"]) / 2) is not None]


def remove_fillers(edl: dict, words: list[dict], fillers: list[str] | None = None,
                   phrases: list[str] | None = None, keep_gap: float = 0.1) -> dict:
    """Cut filler words ("um", "uh") and optional filler phrases ("you know").

    The cut runs from the end of the previous word to the start of the next
    one, leaving `keep_gap` seconds of air so the join breathes naturally.
    """
    filler_set = {norm_word(f) for f in (fillers or DEFAULT_FILLERS)}
    phrase_list = [[norm_word(p) for p in phrase.split()] for phrase in (phrases or [])]
    tokens = [norm_word(w["w"]) for w in words]
    hit = [False] * len(words)
    for i, tok in enumerate(tokens):
        if tok in filler_set:
            hit[i] = True
    for phrase in phrase_list:
        n = len(phrase)
        for i in range(len(tokens) - n + 1):
            if tokens[i:i + n] == phrase:
                for j in range(i, i + n):
                    hit[j] = True
    kept_idx = {i for i, _ in _kept_words(edl, words)}
    ranges, removed_words = [], []
    i = 0
    while i < len(words):
        if not hit[i] or i not in kept_idx:
            i += 1
            continue
        j = i
        while j + 1 < len(words) and hit[j + 1]:
            j += 1
        a, b = words[i]["start"], words[j]["end"]
        prev_end = words[i - 1]["end"] if i > 0 else a
        next_start = words[j + 1]["start"] if j + 1 < len(words) else b
        lo = min(a, max(prev_end, a - 0.6))
        hi = max(b, min(next_start, b + 0.6))
        if hi - lo > keep_gap + 0.05:
            lo, hi = lo + keep_gap / 2, hi - keep_gap / 2
        else:
            lo, hi = a, b
        ranges.append((lo, hi))
        removed_words.append(" ".join(w["w"].strip() for w in words[i:j + 1]))
        i = j + 1
    removed = cut_source_ranges(edl, ranges) if ranges else 0.0
    return {"removed_count": len(ranges), "removed_seconds": removed, "removed": removed_words[:40],
            "duration": round(Timeline(edl).duration, 2)}


def remove_pauses(edl: dict, silences: list[tuple[float, float]], words: list[dict] | None = None,
                  max_pause: float = 0.6, keep: float = 0.25) -> dict:
    """Shorten every silence longer than `max_pause` down to `keep` seconds.

    `silences` are source ranges from audio.find_silences. A silence is left
    alone if the transcript puts the middle of a word inside the part that
    would be cut (very quiet speech)."""
    if keep >= max_pause:
        raise ClipperError("keep must be shorter than max_pause.")
    mids = np.array([(w["start"] + w["end"]) / 2 for w in (words or [])])
    ranges = []
    for a, b in silences:
        if b - a <= max_pause:
            continue
        lo, hi = a + keep / 2, b - keep / 2
        if len(mids) and np.any((mids > lo) & (mids < hi)):
            continue
        ranges.append((lo, hi))
    tl = Timeline(edl)
    ranges = [r for r in ranges if tl.map_range(*r)]
    removed = cut_source_ranges(edl, ranges) if ranges else 0.0
    return {"pauses_shortened": len(ranges), "removed_seconds": removed, "duration": round(Timeline(edl).duration, 2)}


# --------------------------------------------------------------------------
# Output and framing
# --------------------------------------------------------------------------

def set_output(edl: dict, aspect: str | None = None, fps: int | None = None, loudness: float | None = None) -> dict:
    if aspect:
        w, h = output_size(edl["source"]["width"], edl["source"]["height"], aspect)
        edl["output"].update({"aspect": aspect, "width": w, "height": h})
    if fps:
        if not 12 <= int(fps) <= 60:
            raise ClipperError("fps must be between 12 and 60.")
        edl["output"]["fps"] = int(fps)
    if loudness is not None:
        edl["output"]["loudness"] = float(loudness)
    return {"output": edl["output"], "aspects": ["source", *ASPECTS]}


def set_framing(edl: dict, x: float, y: float) -> dict:
    """Point in the source frame (0-1) kept centred when cropping and zooming."""
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise ClipperError("Framing x and y are fractions of the source frame, from 0 to 1.")
    edl["framing"] = {"x": round(x, 4), "y": round(y, 4)}
    return {"framing": edl["framing"]}


# --------------------------------------------------------------------------
# Captions
# --------------------------------------------------------------------------

def set_captions(edl: dict, template: str | None, enabled: bool = True, overrides: dict | None = None) -> dict:
    from .templates import load_caption_template

    if template:
        load_caption_template(template)  # raises with the list of templates if unknown
        edl["captions"]["template"] = template
    if enabled and not edl["captions"]["template"]:
        raise ClipperError("Choose a caption template first.")
    edl["captions"]["enabled"] = bool(enabled)
    if overrides is not None:
        edl["captions"]["overrides"] = overrides
    return {"captions": edl["captions"]}


# --------------------------------------------------------------------------
# Zooms
# --------------------------------------------------------------------------

def _anchor_range(edl: dict, start: float, end: float, anchor: str) -> tuple[float, float]:
    """Convert an output range to what gets stored for the given anchor."""
    tl = Timeline(edl)
    if not (0 <= start < end <= tl.duration + 1e-3):
        raise ClipperError(f"Range {start:.2f}-{end:.2f}s is not inside the video (0-{tl.duration:.2f}s).")
    if anchor == "timeline":
        return round(start, 3), round(end, 3)
    if anchor != "content":
        raise ClipperError("anchor must be 'content' (follows the footage) or 'timeline' (fixed output time).")
    return round(tl.to_source(start), 3), round(tl.to_source(end - 1e-4), 3)


def _stored_anchor(anchor: str) -> str:
    return "source" if anchor == "content" else "timeline"


def add_zoom(edl: dict, start: float, end: float, scale: float = 1.15, ease: float = 0.0,
             focus: tuple[float, float] | None = None, anchor: str = "content") -> dict:
    """Punch in between `start` and `end`. `ease` 0 is a hard punch-in cut;
    above 0 the zoom ramps in and out over that many seconds."""
    if not 1.0 < scale <= 2.0:
        raise ClipperError("Zoom scale must be above 1.0 and at most 2.0 (1.15 is a typical punch-in).")
    a, b = _anchor_range(edl, start, end, anchor)
    item = {"id": next_id(edl, "zooms"), "start": a, "end": b, "anchor": _stored_anchor(anchor),
            "scale": round(float(scale), 3), "ease": round(max(0.0, float(ease)), 3)}
    if focus:
        item["focus"] = [round(float(focus[0]), 4), round(float(focus[1]), 4)]
    edl["zooms"].append(item)
    return {"added": item}


def sentences(words: list[dict], max_gap: float = 0.7) -> list[list[int]]:
    """Group word indices into sentences by punctuation and pauses."""
    out, cur = [], []
    for i, w in enumerate(words):
        if cur and w["start"] - words[cur[-1]]["end"] > max_gap:
            out.append(cur)
            cur = []
        cur.append(i)
        if re.search(r"[.!?]['\")]?$", w["w"].strip()):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def emphasis_scores(words: list[dict], levels: np.ndarray | None) -> list[dict]:
    """Score each sentence for how much it deserves a punch-in."""
    out = []
    median = float(np.median(levels)) if levels is not None and len(levels) else 0.0
    for idxs in sentences(words):
        toks = [norm_word(words[i]["w"]) for i in idxs]
        text = " ".join(words[i]["w"].strip() for i in idxs)
        score = 0.0
        score += 1.2 * sum(t in EMPHASIS_WORDS for t in toks)
        score += 1.0 * sum(bool(re.search(r"\d", t)) for t in toks)
        if text.endswith("!"):
            score += 1.5
        if text.endswith("?"):
            score += 1.0
        if levels is not None and len(levels):
            score += max(0.0, float(np.max(levels[idxs])) - median) * 0.35
        if 3 <= len(idxs) <= 12:
            score += 0.5
        out.append({"start": words[idxs[0]]["start"], "end": words[idxs[-1]]["end"], "score": round(score, 2),
                    "text": text, "words": idxs})
    return out


def auto_zoom(edl: dict, words: list[dict], levels: np.ndarray | None = None, per_minute: float = 6.0,
              scale: float = 1.15, ease: float = 0.0, min_len: float = 1.2, max_len: float = 6.0,
              min_gap: float = 1.5, replace: bool = True) -> dict:
    """Place punch-in zooms on the most emphatic sentences."""
    if replace:
        edl["zooms"] = [z for z in edl["zooms"] if not z.get("auto")]
    tl = Timeline(edl)
    target = max(0, int(round(per_minute * tl.duration / 60)))
    # Work in output time, so sentences are judged as the viewer will hear them.
    tw, idx = [], []
    for i, w in enumerate(words):
        ranges = tl.map_range(w["start"], w["end"])
        if sum(b - a for a, b in ranges) >= 0.5 * max(w["end"] - w["start"], 1e-3):
            tw.append({"w": w["w"], "start": ranges[0][0], "end": ranges[-1][1]})
            idx.append(i)
    tw_levels = levels[idx] if levels is not None and len(levels) == len(words) else None
    cands = []
    for s in emphasis_scores(tw, tw_levels):
        if len(s["words"]) < 3:
            continue
        a = s["start"]
        b = min(max(s["end"], a + min_len), a + max_len, tl.duration)
        if b - a >= min_len * 0.8:
            cands.append({**s, "tl": (a, b)})
    cands.sort(key=lambda c: -c["score"])
    chosen: list[dict] = []
    for c in cands:
        if len(chosen) >= target:
            break
        if all(c["tl"][0] >= o["tl"][1] + min_gap or c["tl"][1] <= o["tl"][0] - min_gap for o in chosen):
            chosen.append(c)
    chosen.sort(key=lambda c: c["tl"][0])
    for c in chosen:
        a, b = c["tl"]
        edl["zooms"].append({"id": next_id(edl, "zooms"), "start": round(tl.to_source(a), 3),
                             "end": round(tl.to_source(b - 1e-4), 3), "anchor": "source", "scale": round(scale, 3),
                             "ease": round(ease, 3), "auto": True, "on": c["text"][:60]})
    return {"zooms_added": len(chosen), "wanted": target,
            "at": [{"start": round(c["tl"][0], 2), "end": round(c["tl"][1], 2), "on": c["text"][:60]} for c in chosen]}


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------

def set_transition(edl: dict, at: float, type: str = "crossfade", duration: float = 0.3) -> dict:
    """Put a transition on the cut nearest to output time `at` ('cut' removes it)."""
    if type != "cut" and type not in TRANSITIONS:
        raise ClipperError(f"Unknown transition {type!r}. Available: cut, {', '.join(TRANSITIONS)}.")
    tl = Timeline(edl)
    cuts = tl.cuts()
    if not cuts:
        raise ClipperError("This edit has no cuts yet, so there is nowhere to put a transition. "
                           "Transitions sit on the join between two segments.")
    nearest = min(cuts, key=lambda c: abs(c["time"] - at))
    if abs(nearest["time"] - at) > 1.0:
        times = ", ".join(f"{c['time']:.2f}" for c in cuts[:30])
        raise ClipperError(f"No cut within 1s of {at:.2f}s. Cuts are at: {times}.")
    seg = edl["segments"][nearest["index"]]
    if type == "cut":
        seg.pop("transition_in", None)
    else:
        seg["transition_in"] = {"type": type, "duration": round(min(max(float(duration), 0.1), 1.5), 3)}
    return {"cut_at": round(nearest["time"], 3), "transition": type}


def auto_transitions(edl: dict, type: str = "crossfade", duration: float = 0.3, per_minute: float = 2.0,
                     min_removed: float = 0.5) -> dict:
    """Put a transition on the cuts that removed the most footage (the biggest
    breaks in thought), up to `per_minute`."""
    if type not in TRANSITIONS:
        raise ClipperError(f"Unknown transition {type!r}. Available: {', '.join(TRANSITIONS)}.")
    tl = Timeline(edl)
    cuts = [c for c in tl.cuts() if abs(c["removed"]) >= min_removed]
    cuts.sort(key=lambda c: -abs(c["removed"]))
    want = int(round(per_minute * tl.duration / 60))
    chosen = cuts[:want]
    for c in chosen:
        edl["segments"][c["index"]]["transition_in"] = {"type": type, "duration": round(duration, 3)}
    return {"transitions_added": len(chosen), "wanted": want, "eligible_cuts": len(cuts),
            "at": sorted(round(c["time"], 2) for c in chosen)}


# --------------------------------------------------------------------------
# Music and SFX
# --------------------------------------------------------------------------

def set_music(edl: dict, path: str | None, gain_db: float = -20.0, duck: bool = True, duck_amount_db: float = -10.0,
              offset: float = 0.0, fade_in: float = 0.5, fade_out: float = 1.5) -> dict:
    """Background music under the whole video. Pass path=None to remove it."""
    if path is None:
        edl["music"] = None
        return {"music": None}
    p = Path(path).expanduser()
    if not p.exists():
        raise ClipperError(f"Music file not found: {p}")
    edl["music"] = {
        "path": str(p.resolve()), "gain_db": float(gain_db), "offset": max(0.0, float(offset)),
        "fade_in": max(0.0, float(fade_in)), "fade_out": max(0.0, float(fade_out)),
        "duck": {"enabled": bool(duck), "amount_db": -abs(float(duck_amount_db)), "attack": 0.08, "release": 0.5},
    }
    return {"music": edl["music"]}


def add_sfx(edl: dict, at: float, sound: str = "whoosh", gain_db: float = -8.0, anchor: str = "content") -> dict:
    if sound not in audio_mod.BUILTIN_SFX and not Path(sound).expanduser().exists():
        raise ClipperError(f"Sound {sound!r} is not a built-in ({', '.join(audio_mod.BUILTIN_SFX)}) or an existing file.")
    tl = Timeline(edl)
    if not 0 <= at <= tl.duration:
        raise ClipperError(f"Time {at:.2f}s is not inside the video (0-{tl.duration:.2f}s).")
    if sound not in audio_mod.BUILTIN_SFX:
        sound = str(Path(sound).expanduser().resolve())
    stored = round(tl.to_source(at), 3) if anchor == "content" else round(at, 3)
    item = {"id": next_id(edl, "sfx"), "sound": sound, "at": stored, "anchor": _stored_anchor(anchor),
            "gain_db": float(gain_db)}
    edl["sfx"].append(item)
    return {"added": item}


def sfx_on_cuts(edl: dict, sound: str = "whoosh", gain_db: float = -10.0, which: str = "transitions",
                min_gap: float = 1.0, replace: bool = True) -> dict:
    """Add a sound on cuts. `which`: 'transitions' (cuts that have a transition),
    'zooms' (every punch-in), 'cuts' (every cut) or 'all'."""
    if which not in ("transitions", "zooms", "cuts", "all"):
        raise ClipperError("which must be one of: transitions, zooms, cuts, all.")
    if sound not in audio_mod.BUILTIN_SFX and not Path(sound).expanduser().exists():
        raise ClipperError(f"Sound {sound!r} is not a built-in ({', '.join(audio_mod.BUILTIN_SFX)}) or an existing file.")
    if replace:
        edl["sfx"] = [s for s in edl["sfx"] if not s.get("auto")]
    tl = Timeline(edl)
    times: list[float] = []
    if which in ("transitions", "cuts", "all"):
        for c in tl.cuts():
            if which == "transitions" and c["transition"] == "cut":
                continue
            times.append(c["time"])
    if which in ("zooms", "all"):
        from .edl import resolve
        times += [z["tl_start"] for z in resolve(edl, tl)["zooms"]]
    times.sort()
    placed: list[float] = []
    for t in times:
        if t < 0.3 or (placed and t - placed[-1] < min_gap):
            continue
        placed.append(t)
        edl["sfx"].append({"id": next_id(edl, "sfx"), "sound": sound, "at": round(tl.to_source(t), 3),
                           "anchor": "source", "gain_db": float(gain_db), "auto": True})
    return {"sfx_added": len(placed), "at": [round(t, 2) for t in placed]}


# --------------------------------------------------------------------------
# Overlays and b-roll
# --------------------------------------------------------------------------

def add_overlay(edl: dict, template: str, start: float, duration: float | None = None, params: dict | None = None,
                anchor: str | None = None) -> dict:
    """Add a motion-graphic overlay (title card, lower third, keyword callout)."""
    from .templates import load_overlay_template

    tpl = load_overlay_template(template)
    params = dict(params or {})
    unknown = sorted(set(params) - set(tpl["params"]))
    if unknown:
        raise ClipperError(f"Template {tpl['id']} has no parameter(s) {unknown}. It takes: {sorted(tpl['params'])}.")
    missing = [k for k, spec in tpl["params"].items() if spec.get("required") and not str(params.get(k, "")).strip()]
    if missing:
        raise ClipperError(f"Template {tpl['id']} needs: {missing}.")
    duration = float(duration or tpl.get("default_duration", 3.0))
    if anchor is None:
        anchor = "timeline" if tpl["kind"] == "title_card" else "content"
    tl = Timeline(edl)
    if not 0 <= start < tl.duration:
        raise ClipperError(f"Start {start:.2f}s is not inside the video (0-{tl.duration:.2f}s).")
    duration = min(duration, tl.duration - start)
    stored = round(tl.to_source(start), 3) if anchor == "content" else round(start, 3)
    item = {"id": next_id(edl, "overlays"), "template": tpl["id"], "start": stored, "duration": round(duration, 3),
            "anchor": _stored_anchor(anchor), "params": params}
    edl["overlays"].append(item)
    return {"added": item}


def add_broll(edl: dict, path: str, start: float, end: float, source_in: float = 0.0, fit: str = "cover",
              anchor: str = "content") -> dict:
    """Cover the speaker with a user-supplied clip from `start` to `end`. The
    speaker's audio keeps playing; the clip's own audio is not used."""
    from .media import probe

    p = Path(path).expanduser()
    if not p.exists():
        raise ClipperError(f"B-roll file not found: {p}")
    if fit not in ("cover", "contain"):
        raise ClipperError("fit must be 'cover' or 'contain'.")
    info = probe(p)
    if not info["video_streams"]:
        raise ClipperError(f"{p.name} has no video stream.")
    a, b = _anchor_range(edl, start, end, anchor)
    need = end - start
    still = info["duration"] < 0.2
    if not still and info["duration"] - source_in < need - 0.05:
        raise ClipperError(f"{p.name} is {info['duration']:.1f}s long; from {source_in:.1f}s it cannot cover "
                           f"{need:.1f}s. Shorten the range or pick an earlier source_in.")
    item = {"id": next_id(edl, "broll"), "path": str(p.resolve()), "start": a, "end": b,
            "anchor": _stored_anchor(anchor), "source_in": round(max(0.0, float(source_in)), 3), "fit": fit,
            "still": still}
    edl["broll"].append(item)
    return {"added": item}


def remove_item(edl: dict, item_id: str) -> dict:
    for kind in ("zooms", "overlays", "broll", "sfx"):
        for item in edl[kind]:
            if item["id"] == item_id:
                edl[kind].remove(item)
                return {"removed": item_id, "kind": kind}
    raise ClipperError(f"No item with id {item_id!r}. Use get_edl to list ids.")


def clear_items(edl: dict, kind: str) -> dict:
    if kind == "transitions":
        n = sum(1 for s in edl["segments"] if s.pop("transition_in", None))
        return {"cleared": n, "kind": kind}
    if kind not in ("zooms", "overlays", "broll", "sfx"):
        raise ClipperError("kind must be one of: zooms, overlays, broll, sfx, transitions.")
    n = len(edl[kind])
    edl[kind] = []
    return {"cleared": n, "kind": kind}
