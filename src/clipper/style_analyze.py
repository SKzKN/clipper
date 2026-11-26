"""Analyse a reference short and describe its style as a JSON "style spec".

Everything here is measurement by classical computer vision and signal
processing, so each section carries a confidence and the spec lists what could
not be measured. Nothing is copied from the reference; only numbers are.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from .checks import _relate_views
from .ffmpeg import ClipperError, log
from .media import contact_sheet, grab_frame, iter_frames, probe, read_audio
from .vision import ocr_available, recognize_text

STYLE_VERSION = 1

# Caption font size (fraction of frame height) per unit of measured word-box height,
# calibrated against clipper's own renders. Capitals fill less of the line than mixed case.
SIZE_PER_BOX_HEIGHT_UPPER = 1.33
SIZE_PER_BOX_HEIGHT_MIXED = 1.1


# --------------------------------------------------------------------------
# Picture: shots, transitions, zooms
# --------------------------------------------------------------------------

def _scan_frames(path: str, info: dict, fps: float, seconds: float) -> dict:
    """One pass over the video at analysis size: per-frame change measures."""
    size = (info["width"], info["height"])
    grid = (8, 6)
    prev = None
    diffs, fracs, lumas, smalls = [], [], [], []
    for frame in iter_frames(path, fps, 96, size, gray=True, duration=seconds):
        f = frame.astype(np.float32)
        lumas.append(float(f.mean()))
        smalls.append(frame.copy())
        if prev is None:
            diffs.append(0.0)
            fracs.append(0.0)
        else:
            d = np.abs(f - prev)
            diffs.append(float(d.mean()))
            h, w = d.shape
            bh, bw = h // grid[0], w // grid[1]
            blocks = d[: bh * grid[0], : bw * grid[1]].reshape(grid[0], bh, grid[1], bw).mean(axis=(1, 3))
            fracs.append(float((blocks > 7.0).mean()))
        prev = f
    return {"diff": np.array(diffs), "frac": np.array(fracs), "luma": np.array(lumas), "small": smalls, "fps": fps}


def _block_frac(diff: np.ndarray, threshold: float, grid: tuple[int, int] = (8, 6)) -> float:
    h, w = diff.shape
    bh, bw = h // grid[0], w // grid[1]
    blocks = diff[: bh * grid[0], : bw * grid[1]].reshape(grid[0], bh, grid[1], bw).mean(axis=(1, 3))
    return float((blocks > threshold).mean())


def _find_changes(scan: dict) -> list[dict]:
    """Hard cuts (the picture jumps in one frame) and gradual transitions (it changes over
    several frames: found by comparing frames 0.2 s apart)."""
    d, frac, fps, small = scan["diff"], scan["frac"], scan["fps"], scan["small"]
    n = len(d)
    out = []
    cut_frames = []
    for i in range(1, n):
        if frac[i] < 0.3:
            continue
        neighbours = max(d[max(0, i - 2):i].max(initial=0.0), d[i + 1:i + 3].max(initial=0.0))
        if d[i] > max(4.0, 2.0 * neighbours):
            cut_frames.append(i)
            out.append({"type": "cut", "frame": i, "time": i / fps, "duration": 0.0})

    lag = max(2, int(round(0.2 * fps)))
    busy = np.zeros(n, dtype=bool)
    for i in range(lag, n):
        busy[i] = _block_frac(np.abs(small[i].astype(np.float32) - small[i - lag].astype(np.float32)), 12.0) >= 0.4
    i = lag
    while i < n:
        if not busy[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and busy[j + 1]:
            j += 1
        length = j - i + 1
        is_cut_footprint = any(i - 1 <= c <= j for c in cut_frames) and length <= lag + 3
        frames = max(1, length - lag + 2)
        if not is_cut_footprint and frames / fps <= 1.2:
            lo, hi = max(0, i - lag - 1), min(n - 1, j + 1)
            before, after = small[lo].astype(np.float32), small[hi].astype(np.float32)
            view_change = float(np.abs(after - before).mean())
            mid = small[(lo + hi) // 2].astype(np.float32)
            luma_min = float(scan["luma"][lo:hi + 1].min())
            luma_max = float(scan["luma"][lo:hi + 1].max())
            ends_lo = min(scan["luma"][lo], scan["luma"][hi])
            ends_hi = max(scan["luma"][lo], scan["luma"][hi])
            # Same picture, only dimmer or brighter (a title card's scrim fading in, say) is not a
            # transition: the two ends have to differ in structure, not just in level.
            ba, af = before - before.mean(), after - after.mean()
            corr = float((ba * af).sum() / (np.sqrt((ba ** 2).sum() * (af ** 2).sum()) + 1e-6))
            kind = None
            if luma_min < 0.45 * ends_lo and luma_min < 40:
                kind = "dip_black"
            elif luma_max > 1.4 * ends_hi and luma_max > 190:
                kind = "flash_white"
            elif view_change > 6.0 and corr < 0.9:
                blend_err = float(np.abs(mid - (before + after) / 2).mean()) / max(view_change, 1e-3)
                kind = "crossfade" if blend_err < 0.4 else "motion"  # motion: slide, wipe, whip or zoom
            if kind:
                out.append({"type": kind, "frame": i, "time": (lo + hi) / 2 / fps, "duration": round(frames / fps, 2)})
        i = j + 1
    out.sort(key=lambda c: c["time"])
    return out


def analyze_picture(path: str, info: dict, seconds: float) -> dict:
    fps = float(min(30.0, max(12.0, info["fps"])))
    scan = _scan_frames(path, info, fps, seconds)
    n = len(scan["diff"])
    if n < fps:
        raise ClipperError("The reference video is too short to analyse.")
    dur = n / fps
    changes = _find_changes(scan)
    size = (info["width"], info["height"])

    punch, jump, new_view = [], 0, 0
    for c in changes:
        if c["type"] != "cut":
            continue
        a = grab_frame(path, max(0.0, c["time"] - 2.5 / fps), 480, size)
        b = grab_frame(path, c["time"] + 1.5 / fps, 480, size)
        kind, scale = _relate_views(a, b, overlays=True)
        if kind == "same_view" and scale:
            if scale > 1.04 or scale < 0.96:
                c["cut_kind"] = "punch_in" if scale > 1 else "punch_out"
                punch.append(max(scale, 1 / scale))
            else:
                c["cut_kind"] = "jump"
                jump += 1
            c["scale"] = scale
        else:
            c["cut_kind"] = "new_view"
            new_view += 1

    # Slow zooms inside shots: compare the start and end of each shot.
    bounds = [0.0] + [c["time"] for c in changes] + [dur]
    smooth = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a < 1.0 or len(smooth) + 1 > 40:
            continue
        fa = grab_frame(path, a + 0.15, 480, size)
        fb = grab_frame(path, b - 0.15, 480, size)
        kind, scale = _relate_views(fa, fb, overlays=True)
        if kind == "same_view" and scale and abs(scale - 1) >= 0.03:
            smooth.append({"scale": max(scale, 1 / scale), "seconds": b - a})

    cuts = [c for c in changes if c["type"] == "cut"]
    trans = [c for c in changes if c["type"] != "cut"]
    counts: dict[str, int] = {"cut": len(cuts)}
    for c in trans:
        counts[c["type"]] = counts.get(c["type"], 0) + 1
    total = max(1, len(changes))
    minutes = dur / 60
    punch_ins = max(sum(1 for c in cuts if c.get("cut_kind") == "punch_in"), int(round(len(punch) / 2)))
    return {
        "analysed_seconds": round(dur, 2),
        "pacing": {
            "shot_count": len(changes) + 1,
            "avg_shot_length": round(dur / (len(changes) + 1), 2),
            "cuts_per_minute": round(len(changes) / minutes, 1),
            "cut_kinds": {"punch": len(punch), "jump": jump, "new_view": new_view},
            "note": "Jump cuts on a static background are hard to see in the picture and may be under-counted.",
        },
        "zoom": {
            "punch_ins_per_minute": round(punch_ins / minutes, 1),
            "punch_strength": round(float(np.median(punch)), 3) if punch else None,
            "smooth_zooms_per_minute": round(len(smooth) / minutes, 1),
            "smooth_strength": round(float(np.median([s["scale"] for s in smooth])), 3) if smooth else None,
            "smooth_seconds": round(float(np.median([s["seconds"] for s in smooth])), 2) if smooth else None,
        },
        "transitions": {
            "types": {k: round(v / total, 3) for k, v in sorted(counts.items())},
            "counts": counts,
            "non_cut_per_minute": round(len(trans) / minutes, 1),
            "median_duration": round(float(np.median([c["duration"] for c in trans])), 2) if trans else None,
        },
        "_cut_times": [round(c["time"], 3) for c in changes],
    }


# --------------------------------------------------------------------------
# Captions
# --------------------------------------------------------------------------

def _norm_tokens(text: str) -> list[str]:
    return [t for t in re.sub(r"[^\w' ]+", " ", text.lower()).split() if t]


def _fill_color(crop: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """Estimate the text fill colour of a word crop (BGR) and return (colour, fill mask)."""
    import cv2

    if crop.size == 0 or crop.shape[0] < 6 or crop.shape[1] < 6:
        return None
    pixels = crop.reshape(-1, 3).astype(np.float32)
    k = 3
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 12, 1.0)
    _c, labels, centers = cv2.kmeans(pixels, k, None, criteria, 2, cv2.KMEANS_PP_CENTERS)
    labels = labels.ravel()
    share = np.bincount(labels, minlength=k) / len(labels)
    hsv = cv2.cvtColor(centers.reshape(1, k, 3).astype(np.uint8), cv2.COLOR_BGR2HSV).reshape(k, 3).astype(np.float32)
    # Fill is the bright and/or saturated cluster that covers a real share of the box.
    score = hsv[:, 2] / 255 + 0.6 * hsv[:, 1] / 255
    score[share < 0.12] = -1
    best = int(np.argmax(score))
    return centers[best], (labels == best).reshape(crop.shape[:2])


def _hex(bgr: np.ndarray) -> str:
    b, g, r = [int(round(float(v))) for v in bgr]
    return f"#{min(255, max(0, r)):02X}{min(255, max(0, g)):02X}{min(255, max(0, b)):02X}"


def analyze_captions(path: str, info: dict, seconds: float) -> dict:
    if not ocr_available():
        return {"present": None, "confidence": 0.0,
                "note": "Text recognition needs macOS (Apple Vision); captions were not analysed."}
    import cv2

    size = (info["width"], info["height"])
    fps = 4.0
    if seconds * fps > 360:
        fps = 360 / seconds
    width = 540 if info["width"] <= info["height"] else 960
    from concurrent.futures import ThreadPoolExecutor

    log(f"reading on-screen text ({int(seconds * fps)} frames)")
    kept_frames = [f.copy() for f in iter_frames(path, fps, width, size, gray=False, duration=seconds)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(recognize_text, kept_frames))
    frames_text = [{"t": i / fps, "lines": [ln for ln in lines if ln["confidence"] >= 0.3 and len(ln["text"].strip()) >= 2]}
                   for i, lines in enumerate(results)]
    if not frames_text:
        return {"present": False, "confidence": 0.5}

    # The caption band is the horizontal strip where the text keeps changing.
    bins = 25
    distinct: list[set] = [set() for _ in range(bins)]
    for ft in frames_text:
        for ln in ft["lines"]:
            y = ln["y"] + ln["h"] / 2
            distinct[min(bins - 1, int(y * bins))].add(" ".join(_norm_tokens(ln["text"])))
    counts = np.array([len(s) for s in distinct], dtype=float)
    smooth = np.convolve(counts, [1, 1, 1], mode="same")
    peak = int(np.argmax(smooth))
    if smooth[peak] < 4:
        return {"present": False, "confidence": 0.6,
                "note": "No text that changes over time was found, so the reference seems to have no captions."}
    lo_bin, hi_bin = peak, peak
    while lo_bin > 0 and counts[lo_bin - 1] >= 0.25 * counts[peak]:
        lo_bin -= 1
    while hi_bin < bins - 1 and counts[hi_bin + 1] >= 0.25 * counts[peak]:
        hi_bin += 1
    band = (lo_bin / bins, (hi_bin + 1) / bins)

    states = []  # per frame: caption words in the band
    for ft, frame in zip(frames_text, kept_frames):
        lines = [ln for ln in ft["lines"] if band[0] <= ln["y"] + ln["h"] / 2 <= band[1]]
        lines.sort(key=lambda ln: (round(ln["y"], 2), ln["x"]))
        words = [w for ln in lines for w in ln["words"]]
        states.append({"t": ft["t"], "lines": lines, "words": words,
                       "tokens": _norm_tokens(" ".join(ln["text"] for ln in lines)), "frame": frame})

    # Group consecutive frames that show the same caption (allowing it to grow word by word).
    groups: list[list[dict]] = []
    for st in states:
        if not st["tokens"]:
            continue
        if groups:
            prev = groups[-1][-1]
            a, b = prev["tokens"], st["tokens"]
            gap = st["t"] - prev["t"]
            same = gap <= 1.5 / fps + 1e-6 and (a[: len(b)] == b or b[: len(a)] == a or
                                                len(set(a) & set(b)) >= 0.6 * max(len(a), len(b)))
            if same:
                groups[-1].append(st)
                continue
        groups.append([st])
    if len(groups) < 3:
        return {"present": False, "confidence": 0.5,
                "note": "Too little changing text was found to describe a caption style."}

    words_per_group = [max(len(s["tokens"]) for s in g) for g in groups]
    lines_per_group = [max(len(s["lines"]) for s in g) for g in groups]
    multi = [g for g, n in zip(groups, words_per_group) if n >= 2]
    growing = sum(1 for g in multi if len(g) >= 2 and len(g[0]["tokens"]) < max(len(s["tokens"]) for s in g)
                  and all(len(x["tokens"]) <= len(y["tokens"]) for x, y in zip(g, g[1:])))

    # Colours, stroke weight, highlight.
    fills, heights, stroke_ratios, ring_cols, gap_cols = [], [], [], [], []
    highlight_frames = multi_frames = 0
    highlight_cols, highlight_moves = [], 0
    for g in groups:
        last_idx = None
        for st in g:
            frame = st["frame"]
            fh, fw = frame.shape[:2]
            word_cols = []
            for w in st["words"]:
                x0, y0 = int(w["x"] * fw), int(w["y"] * fh)
                x1, y1 = int((w["x"] + w["w"]) * fw), int((w["y"] + w["h"]) * fh)
                crop = frame[max(0, y0):y1, max(0, x0):x1]
                res = _fill_color(crop)
                if res is None:
                    continue
                col, mask = res
                word_cols.append(col)
                heights.append(w["h"])
                dist = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 3)
                if dist.max() > 0:
                    stroke_ratios.append(2 * float(np.percentile(dist[mask], 92)) / max(1, crop.shape[0]))
                ring = cv2.dilate(mask.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool) & ~mask
                if ring.sum() > 10:
                    ring_cols.append(np.median(crop[ring], axis=0))
            for ln in st["lines"]:
                ws = sorted(ln["words"], key=lambda w: w["x"])
                for wa, wb in zip(ws, ws[1:]):
                    gx0, gx1 = int((wa["x"] + wa["w"]) * fw) + 1, int(wb["x"] * fw) - 1
                    gy0, gy1 = int(ln["y"] * fh), int((ln["y"] + ln["h"]) * fh)
                    if gx1 - gx0 >= 3 and gy1 - gy0 >= 3:
                        gap_cols.append(np.median(frame[gy0:gy1, gx0:gx1].reshape(-1, 3), axis=0))
            if len(word_cols) >= 2:
                multi_frames += 1
                cols = np.array(word_cols)
                med = np.median(cols, axis=0)
                dists = np.linalg.norm(cols - med, axis=1)
                idx = int(np.argmax(dists))
                if dists[idx] > 70 and np.sum(dists > 70) == 1:
                    highlight_frames += 1
                    highlight_cols.append(cols[idx])
                    if last_idx is not None and idx != last_idx:
                        highlight_moves += 1
                    last_idx = idx
                    fills.extend(c for k, c in enumerate(cols) if k != idx)
                else:
                    fills.extend(cols)
            else:
                fills.extend(word_cols)

    letters = "".join(ch for g in groups for ch in " ".join(ln["text"] for ln in g[-1]["lines"]) if ch.isalpha())
    upper_ratio = sum(ch.isupper() for ch in letters) / max(1, len(letters))
    stroke = float(np.median(stroke_ratios)) if stroke_ratios else 0.0
    weight = "black" if stroke >= 0.17 else "bold" if stroke >= 0.1 else "regular"

    fill = np.median(np.array(fills), axis=0) if fills else np.array([255, 255, 255])
    outline = None
    box = None
    if ring_cols:
        ring = np.array(ring_cols)
        ring_med, ring_spread = np.median(ring, axis=0), float(ring.std(axis=0).mean())
        if ring_spread < 40 and np.linalg.norm(ring_med - fill) > 60:
            outline = _hex(ring_med)
        if gap_cols:
            gaps = np.array(gap_cols)
            if float(gaps.std(axis=0).mean()) < 22 and np.linalg.norm(np.median(gaps, axis=0) - ring_med) < 45:
                box = {"color": _hex(np.median(gaps, axis=0))}
                outline = None

    highlight = multi_frames > 0 and highlight_frames >= 0.3 * multi_frames
    reveal = len(multi) > 0 and growing >= 0.4 * len(multi)
    pop = _pop_in(path, info, groups, band, fps)
    extras = []
    if highlight and highlight_moves >= 0.25 * max(1, highlight_frames):
        animation = "highlight"
    elif highlight:
        animation = "highlight"
        extras.append("One word per caption is in a second colour but it does not move with the speech; "
                      "this looks like fixed keyword colouring rather than a karaoke highlight.")
    elif reveal:
        animation = "word_reveal"
    elif pop:
        animation = "pop"
    else:
        animation = "none"
    if pop and animation != "pop":
        extras.append("Captions also scale in (pop) when they appear.")

    # Position comes from each caption once it is complete (word-by-word captions start off-centre).
    full = [max(g, key=lambda st: len(st["tokens"])) for g in groups]
    xs = [(min(ln["x"] for ln in st["lines"]) + max(ln["x"] + ln["w"] for ln in st["lines"])) / 2 for st in full]
    ys = [np.mean([ln["y"] + ln["h"] / 2 for ln in st["lines"]]) for st in full]
    box_h = float(np.median(heights)) if heights else 0.03
    durations = [g[-1]["t"] - g[0]["t"] + 1 / fps for g in groups]
    return {
        "present": True,
        "confidence": round(min(0.95, 0.4 + 0.03 * len(groups)), 2),
        "groups_seen": len(groups),
        "font": {"family": None, "weight": weight, "stroke_ratio": round(stroke, 3), "uppercase": bool(upper_ratio > 0.8),
                 "fill": _hex(fill), "outline": outline, "box": box},
        "text_height": round(box_h, 4),
        "size": round(box_h * (SIZE_PER_BOX_HEIGHT_UPPER if upper_ratio > 0.8 else SIZE_PER_BOX_HEIGHT_MIXED), 4),
        "position": {"x": round(float(np.median(xs)), 3), "y": round(float(np.median(ys)), 3)},
        "words_per_group": round(float(np.median(words_per_group)), 1),
        "max_words_per_group": int(np.percentile(words_per_group, 90)),
        "lines_per_group": int(round(float(np.median(lines_per_group)))),
        "seconds_per_group": round(float(np.median(durations)), 2),
        "animation": animation,
        "highlight_color": _hex(np.median(np.array(highlight_cols), axis=0)) if highlight else None,
        "notes": extras,
    }


def _pop_in(path: str, info: dict, groups: list[list[dict]], band: tuple[float, float], scan_fps: float) -> bool:
    """Do captions scale up when they appear? Looks at a few caption onsets frame by frame."""
    size = (info["width"], info["height"])
    fps = float(min(30.0, info["fps"]))
    width = 540 if info["width"] <= info["height"] else 960
    from concurrent.futures import ThreadPoolExecutor

    onsets = [g[0]["t"] for g in groups[1:] if len(g) >= 2][:6]
    grew = seen = 0
    for t in onsets:
        start = max(0.0, t - 1 / scan_fps - 0.05)
        heights = []
        frames = [f.copy() for f in iter_frames(path, fps, width, size, gray=False, start=start,
                                                duration=1 / scan_fps + 0.35)]
        # Only the caption strip is read here, which is much faster than whole frames.
        y0 = int(max(0.0, band[0] - 0.06) * frames[0].shape[0]) if frames else 0
        y1 = int(min(1.0, band[1] + 0.06) * frames[0].shape[0]) if frames else 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            for lines in pool.map(lambda f: recognize_text(f[y0:y1], with_words=False), frames):
                hs = [ln["h"] for ln in lines if ln["confidence"] >= 0.3]
                heights.append(max(hs) if hs else 0.0)
        # Find where the caption changes: the biggest step in height, then compare with where it settles.
        vis = [h for h in heights if h > 0]
        if len(vis) < 4:
            continue
        settled = float(np.median(vis[-3:]))
        steps = np.abs(np.diff(heights))
        k = int(np.argmax(steps)) + 1 if len(steps) else 0
        first = heights[k] if k < len(heights) and heights[k] > 0 else None
        if first is None or settled <= 0:
            continue
        seen += 1
        if first < 0.9 * settled or max(heights[k:k + 4]) > 1.06 * settled:
            grew += 1
    return seen >= 2 and grew >= 0.5 * seen


# --------------------------------------------------------------------------
# Sound: music bed and effects on cuts
# --------------------------------------------------------------------------

def analyze_audio(path: str, cut_times: list[float], seconds: float) -> dict:
    sr = 22050
    try:
        mono = read_audio(path, sr, 1, duration=seconds)
    except ClipperError:
        return {"music": {"present": None, "confidence": 0.0, "note": "The reference has no audio track."},
                "sfx": {"on_cuts": None, "confidence": 0.0}}
    if len(mono) < sr * 3:
        return {"music": {"present": None, "confidence": 0.0, "note": "Too little audio to analyse."},
                "sfx": {"on_cuts": None, "confidence": 0.0}}
    n_fft, hop = 1024, 512
    window = np.hanning(n_fft).astype(np.float32)
    n_frames = 1 + (len(mono) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
    spec = np.abs(np.fft.rfft(mono[idx] * window, axis=1))
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    frame_rate = sr / hop
    power = spec ** 2
    level = 10 * np.log10(power.sum(axis=1) + 1e-10)

    # Music is judged in the quiet moments between phrases, where speech is not in the way.
    quiet = level < np.percentile(level, 30)
    speech_level = float(np.percentile(level, 90))
    bed_level = float(np.median(level[quiet]))
    gap = speech_level - bed_level

    # 1. Tonal rather than noisy: room tone has a flat spectrum, music has peaks.
    band = (freqs >= 150) & (freqs <= 5000)
    pq = power[quiet][:, band] + 1e-12
    flatness = float(np.median(np.exp(np.log(pq).mean(axis=1)) / pq.mean(axis=1)))

    # 2. A steady beat in the sub-bass (below most voices), between 60 and 180 BPM.
    sub = 10 * np.log10(power[:, (freqs >= 30) & (freqs <= 95)].sum(axis=1) + 1e-10)
    env = np.maximum(sub, np.percentile(sub, 20))
    env = env - env.mean()
    ac = np.correlate(env, env, mode="full")[len(env) - 1:]
    ac = ac / (ac[0] + 1e-9)
    lag_lo, lag_hi = int(frame_rate * 60 / 180), int(frame_rate * 60 / 60)
    beat = float(ac[lag_lo:lag_hi + 1].max()) if len(ac) > lag_hi else 0.0

    score = 0.5 * np.clip((0.3 - flatness) / 0.25, 0, 1) + 0.5 * np.clip((beat - 0.1) / 0.3, 0, 1)
    if gap > 55:  # the gaps are close to silence: there is no bed at all
        score = 0.0
    present = bool(score >= 0.4)
    music = {
        "present": present,
        "confidence": round(float(min(1.0, abs(score - 0.4) / 0.4)), 2),
        "score": round(float(score), 3),
        "evidence": {"quiet_gap_db": round(gap, 1), "spectral_flatness": round(flatness, 3),
                     "beat_strength": round(beat, 3)},
        "level_db_below_speech": round(-gap, 1) if present else None,
    }

    # Effects on cuts. Two kinds of evidence, either one counts as a "burst":
    #  - a whoosh is a swell of noise: the 0.8-4 kHz band turns noise-like (flat) while loud;
    #  - clicks, pops and risers put a spike of energy above 5 kHz.
    # Bursts then have to land on cuts clearly more often than they would by chance.
    mid = power[:, (freqs >= 800) & (freqs <= 4000)] + 1e-12
    mid_flat = np.exp(np.log(mid).mean(axis=1)) / mid.mean(axis=1)
    mid_level = 10 * np.log10(mid.sum(axis=1))
    noisy = (mid_flat > np.percentile(mid_flat, 90)) & (mid_level > np.percentile(mid_level, 40))
    high = 10 * np.log10(power[:, (freqs >= 5000) & (freqs <= 10000)].sum(axis=1) + 1e-10)
    burst = noisy | (high > np.percentile(high, 96))
    half = int(0.12 * frame_rate)
    padded = np.convolve(burst.astype(float), np.ones(2 * half + 1), mode="same") > 0
    baseline = float(padded.mean())
    hits = [bool(padded[min(len(padded) - 1, int(t * frame_rate))]) for t in cut_times if t * frame_rate < len(padded)]
    if len(hits) >= 3:
        frac = float(np.mean(hits))
        ratio = frac / max(baseline, 1e-3)
        on_cuts = bool(frac >= 0.5 and ratio >= 1.8)
        sfx = {"on_cuts": on_cuts, "fraction_of_cuts": round(frac, 2), "chance_level": round(baseline, 2),
               "confidence": round(float(np.clip((ratio - 1.0) / 2.0, 0, 1) * min(1.0, len(hits) / 6)), 2)}
    else:
        sfx = {"on_cuts": None, "confidence": 0.0, "note": "Too few cuts to tell whether sounds land on them."}
    return {"music": music, "sfx": sfx}


# --------------------------------------------------------------------------
# Whole spec
# --------------------------------------------------------------------------

def analyze_style(path: str | Path, max_seconds: float = 90.0, sheet_path: str | Path | None = None) -> dict:
    path = str(Path(path).expanduser().resolve())
    info = probe(path)
    if not info["video_streams"]:
        raise ClipperError("The reference file has no video stream.")
    seconds = min(info["duration"], max_seconds)
    log(f"analysing reference: {Path(path).name} ({seconds:.0f}s of {info['duration']:.0f}s)")
    picture = analyze_picture(path, info, seconds)
    cut_times = picture.pop("_cut_times")
    captions = analyze_captions(path, info, seconds)
    sound = analyze_audio(path, cut_times, seconds)

    limits = [
        "The caption font family cannot be identified; only weight, case and colours are measured.",
        "Music and sound-effect detection is statistical. Treat low-confidence results as a hint.",
    ]
    if picture["pacing"]["cut_kinds"]["new_view"]:
        limits.append("Cuts to other footage (b-roll) are counted but their content is not described.")
    if info["duration"] > seconds:
        limits.append(f"Only the first {seconds:.0f}s of the reference were analysed.")
    spec = {
        "version": STYLE_VERSION,
        "source": {"path": path, "duration": info["duration"], "width": info["width"], "height": info["height"],
                   "fps": info["fps"], "analysed_seconds": picture.pop("analysed_seconds")},
        "captions": captions,
        "pacing": picture["pacing"],
        "zoom": picture["zoom"],
        "transitions": picture["transitions"],
        "audio": sound,
        "limits": limits,
    }
    if sheet_path:
        times = list(np.linspace(0.5, max(0.6, seconds - 0.5), 12))
        try:
            spec["contact_sheet"] = contact_sheet(path, [float(t) for t in times], sheet_path, width=240)
        except ClipperError:
            pass
    return spec
