"""Apply a style spec to an EDL by mapping it onto the closest available
templates and edit operations, and report what could not be matched."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import ops
from .edl import Timeline
from .ffmpeg import ClipperError
from .templates import list_caption_templates, load_caption_template

# Measured transition type -> clipper transition.
TRANSITION_MAP = {"crossfade": "crossfade", "dip_black": "dip_black", "flash_white": "flash_white",
                  "motion": "slide_left"}


def load_style(path: str | Path) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        raise ClipperError(f"Style spec not found: {p}")
    spec = json.loads(p.read_text())
    if "captions" not in spec or "zoom" not in spec:
        raise ClipperError(f"{p.name} does not look like a clipper style spec.")
    return spec


def closest_caption_template(cap: dict) -> tuple[str, float, list[str]]:
    """Pick the caption template with the fewest differences from the measured style."""
    best = None
    for t in list_caption_templates():
        tpl = load_caption_template(t["name"])
        diffs = []
        cost = 0.0
        if tpl["animation"] != cap["animation"]:
            cost += 3.0
            diffs.append(f"animation {tpl['animation']} instead of {cap['animation']}")
        cost += abs(tpl["max_words"] - cap["words_per_group"]) * 0.5
        if bool(tpl["uppercase"]) != bool(cap["font"]["uppercase"]):
            cost += 1.0
        if bool(tpl.get("box")) != bool(cap["font"].get("box")):
            cost += 1.0
        heavy = any(k in tpl["font"].lower() for k in ("black", "impact", "heavy"))
        if heavy != (cap["font"]["weight"] == "black"):
            cost += 0.5
        if best is None or cost < best[1]:
            best = (t["name"], cost, diffs)
    if best is None:
        raise ClipperError("No caption templates are installed.")
    return best


def apply_style(edl: dict, spec: dict, words: list[dict] | None, levels: np.ndarray | None = None,
                music_path: str | None = None) -> dict:
    """Change `edl` to follow `spec`. Returns {"matched", "approximated", "unmatched"}."""
    matched: list[str] = []
    approx: list[str] = []
    unmatched: list[str] = []
    tl = Timeline(edl)

    # -- captions ---------------------------------------------------------
    cap = spec.get("captions") or {}
    if cap.get("present"):
        if not words:
            unmatched.append("Captions: the reference has captions but this project has no transcript yet.")
        else:
            name, _cost, diffs = closest_caption_template(cap)
            tpl = load_caption_template(name)
            wpg = max(1, int(round(cap["words_per_group"])))
            overrides = {
                "position": {"x": min(0.9, max(0.1, cap["position"]["x"])), "y": min(0.92, max(0.08, cap["position"]["y"]))},
                "size": round(min(0.12, max(0.02, cap["size"])), 4),
                "max_words": wpg,
                "max_chars": max(tpl["max_chars"], 7 * wpg) if wpg > tpl["max_words"] else tpl["max_chars"],
                "uppercase": bool(cap["font"]["uppercase"]),
                "color": cap["font"]["fill"],
            }
            if cap["font"].get("outline"):
                overrides["outline_color"] = cap["font"]["outline"]
            if cap["font"].get("box"):
                overrides["box"] = {"color": cap["font"]["box"]["color"], "opacity": (tpl.get("box") or {}).get("opacity", 0.75)}
            elif tpl.get("box"):
                overrides["box"] = None
            if cap.get("highlight_color") and tpl["animation"] == "highlight":
                overrides["highlight_color"] = cap["highlight_color"]
            ops.set_captions(edl, name, True, overrides)
            matched.append(f"Captions: template '{name}' with the reference's position ({overrides['position']['x']:.2f}, "
                           f"{overrides['position']['y']:.2f}), size, colours, case and {wpg} word(s) per caption.")
            for d in diffs:
                approx.append(f"Captions: no template has the reference's animation; using {d}.")
            unmatched.append(f"Caption font: the reference's font family cannot be identified; '{name}' uses "
                             f"{tpl['font']} (reference weight looks {cap['font']['weight']}).")
            for note in cap.get("notes", []):
                unmatched.append(f"Captions: {note}")
    elif cap.get("present") is False:
        edl["captions"]["enabled"] = False
        matched.append("Captions: the reference has none, so captions are off.")
    else:
        unmatched.append(f"Captions: not analysed ({cap.get('note', 'no data')}).")

    # -- zooms and pacing ---------------------------------------------------
    zoom = spec.get("zoom") or {}
    pacing = spec.get("pacing") or {}
    punch_rate = float(zoom.get("punch_ins_per_minute") or 0)
    smooth_rate = float(zoom.get("smooth_zooms_per_minute") or 0)
    kinds = pacing.get("cut_kinds") or {}
    shot_len = float(pacing.get("avg_shot_length") or 0)
    if not words:
        if punch_rate + smooth_rate > 0:
            unmatched.append("Zooms: placing zooms on emphasis needs a transcript.")
    elif punch_rate + smooth_rate >= 0.5:
        use_smooth = smooth_rate > punch_rate
        strength = float((zoom.get("smooth_strength") if use_smooth else zoom.get("punch_strength")) or 1.15)
        strength = min(1.5, max(1.05, strength))
        rate = min(14.0, punch_rate + smooth_rate)
        res = ops.auto_zoom(edl, words, levels, per_minute=rate, scale=strength, ease=0.5 if use_smooth else 0.0)
        line = (f"Zooms: {res['zooms_added']} {'eased' if use_smooth else 'punch-in'} zoom(s) at {strength:.2f}x on "
                f"emphatic sentences (reference: {rate:.1f} per minute at {strength:.2f}x).")
        if res["zooms_added"] < res["wanted"]:
            approx.append(line + f" Wanted {res['wanted']}; there were not enough separate sentences to place more.")
        elif use_smooth:
            approx.append(line + " The reference's slow zooms are approximated with eased zoom-ins.")
        else:
            matched.append(line)
    else:
        edl["zooms"] = [z for z in edl["zooms"] if not z.get("auto")]
        matched.append("Zooms: the reference has none, so automatic zooms were removed.")
    if shot_len:
        changes = 2 * len(edl["zooms"]) + len(tl.cuts())
        ours = tl.duration / (changes + 1)
        text = f"Pacing: the reference changes picture every {shot_len:.1f}s on average; this edit every {ours:.1f}s."
        (matched if ours <= shot_len * 1.4 else approx).append(text)
    if kinds.get("new_view"):
        unmatched.append(f"B-roll: the reference cuts to other footage {kinds['new_view']} time(s). clipper only "
                         "inserts clips you supply (add_broll); nothing was added.")

    # -- transitions ------------------------------------------------------------
    trans = spec.get("transitions") or {}
    rate = float(trans.get("non_cut_per_minute") or 0)
    counts = {k: v for k, v in (trans.get("counts") or {}).items() if k != "cut"}
    ops.clear_items(edl, "transitions")
    if rate >= 0.5 and counts:
        main = max(counts, key=counts.get)
        ours = TRANSITION_MAP.get(main, "crossfade")
        dur = min(1.0, max(0.2, float(trans.get("median_duration") or 0.3)))
        if rate > 6.0:
            approx.append(f"Transitions: the reference measured {rate:.1f} per minute, which is more than plain "
                          "talking-head footage can carry (overlays and b-roll in the reference inflate this "
                          "count); capped at 6 per minute.")
            rate = 6.0
        res = ops.auto_transitions(edl, type=ours, duration=dur, per_minute=rate)
        if res["transitions_added"] == 0:
            unmatched.append(f"Transitions: the reference uses {main} transitions ({rate:.1f} per minute) but this "
                             "edit has no cuts with a real break to put them on.")
        else:
            line = f"Transitions: {res['transitions_added']} '{ours}' transition(s) of {dur:.2f}s on the biggest breaks."
            if main == "motion":
                approx.append(line + " The reference uses a slide, wipe or whip; the exact kind is not identified.")
            elif res["transitions_added"] < res["wanted"]:
                approx.append(line + f" Wanted {res['wanted']}.")
            else:
                matched.append(line)
        others = [k for k in counts if k != main]
        if others:
            approx.append(f"Transitions: the reference also uses {', '.join(others)}; only the most common kind was applied.")
    else:
        matched.append("Transitions: the reference uses plain cuts, and so does this edit.")

    # -- music --------------------------------------------------------------------
    audio = spec.get("audio") or {}
    music = audio.get("music") or {}
    if music.get("present"):
        path = music_path or (edl.get("music") or {}).get("path")
        if path:
            level = float(music.get("level_db_below_speech") or -20.0)
            gain_db = min(-10.0, max(-32.0, level))
            ops.set_music(edl, path, gain_db=gain_db, duck=True, duck_amount_db=-8.0)
            approx.append(f"Music: your track at {gain_db:.0f} dB with ducking under speech (the reference's bed sits "
                          f"about {abs(level):.0f} dB under the voice). The reference's own music is not reused.")
        else:
            unmatched.append("Music: the reference has background music. Supply a track (set_music or --music) "
                             "and it will be levelled and ducked to match.")
    elif music.get("present") is False:
        if edl.get("music"):
            approx.append("Music: the reference has no music; the music already on this edit was left in place.")
        else:
            matched.append("Music: none in the reference, none here.")
    else:
        unmatched.append("Music: could not be analysed.")

    # -- sound effects ----------------------------------------------------------------
    sfx = audio.get("sfx") or {}
    if sfx.get("on_cuts"):
        res = ops.sfx_on_cuts(edl, sound="whoosh", which="all")
        if res["sfx_added"]:
            approx.append(f"Sound effects: a built-in whoosh on {res['sfx_added']} cut(s) and zoom(s). The reference's "
                          "own sounds are not identified or reused; pass your own files to add_sfx_on_cuts.")
        else:
            unmatched.append("Sound effects: the reference has sounds on its cuts, but this edit has no cuts or zooms.")
    elif sfx.get("on_cuts") is False:
        edl["sfx"] = [s for s in edl["sfx"] if not s.get("auto")]
        matched.append("Sound effects: none detected on the reference's cuts.")
    else:
        unmatched.append("Sound effects: too few cuts in the reference to tell.")

    report = {"matched": matched, "approximated": approx, "unmatched": unmatched}
    edl["notes"] = [n for n in edl.get("notes", []) if n.get("kind") != "style"]
    edl["notes"].append({"kind": "style", "reference": (spec.get("source") or {}).get("path"), **report})
    return report
