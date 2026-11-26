"""Turn transcript words into an animated ASS subtitle file."""
from __future__ import annotations

import re

from .edl import Timeline


def timeline_words(words: list[dict], tl: Timeline, window: tuple[float, float] | None = None) -> list[dict]:
    """Words that survive the edit, with output-timeline times."""
    out = []
    for w in words:
        ranges = tl.map_range(w["start"], w["end"])
        kept = sum(b - a for a, b in ranges)
        if kept < 0.5 * max(w["end"] - w["start"], 1e-3):
            continue
        a, b = ranges[0][0], ranges[-1][1]
        if window:
            if b <= window[0] or a >= window[1]:
                continue
            a, b = a - window[0], b - window[0]
        out.append({"w": w["w"].strip(), "start": a, "end": b})
    out.sort(key=lambda w: w["start"])
    return out


def clean_text(text: str, tpl: dict) -> str:
    text = text.strip()
    if tpl["strip_punctuation"]:
        text = re.sub(r"[.,;:…\"“”]+", "", text)
    if tpl["uppercase"]:
        text = text.upper()
    return text.replace("{", "(").replace("}", ")").replace("\\", "/")


def group_words(words: list[dict], tpl: dict) -> list[list[dict]]:
    groups: list[list[dict]] = []
    cur: list[dict] = []
    chars = 0
    for w in words:
        text = clean_text(w["w"], tpl)
        if not text:
            continue
        item = {**w, "text": text}
        new_group = False
        if cur:
            if len(cur) >= tpl["max_words"]:
                new_group = True
            elif chars + 1 + len(text) > tpl["max_chars"]:
                new_group = True
            elif w["start"] - cur[-1]["end"] > tpl["max_gap"]:
                new_group = True
            elif re.search(r"[.!?]$", cur[-1]["w"]):
                new_group = True
        if new_group:
            groups.append(cur)
            cur, chars = [], 0
        cur.append(item)
        chars += len(text) + (1 if chars else 0)
    if cur:
        groups.append(cur)
    return groups


def caption_groups(words: list[dict], tpl: dict) -> list[dict]:
    """Caption groups with on-screen times: [{start, end, words: [{text, start, end}]}].
    The renderer and the UI's live preview both use this, so they agree on timing."""
    groups = group_words(words, tpl)
    out = []
    for gi, group in enumerate(groups):
        start = group[0]["start"]
        end = group[-1]["end"] + tpl["linger"]
        if gi + 1 < len(groups):
            end = min(end, groups[gi + 1][0]["start"])
        end = max(end, start + 0.05)
        out.append({"start": start, "end": end,
                    "words": [{"text": w["text"], "start": w["start"], "end": w["end"]} for w in group]})
    return out


def _ass_color(hex_color: str, alpha: float = 0.0) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"&H{int(round(alpha * 255)):02X}{b:02X}{g:02X}{r:02X}"


def _inline_color(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def _ts(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def build_ass(words: list[dict], tpl: dict, width: int, height: int,
              window: tuple[float, float] | None = None) -> tuple[str, int]:
    """Return (ASS file text, number of caption groups).

    `words` cover the whole timeline. With `window`, only that output range is
    written, re-based to start at 0: captions are grouped on the whole timeline
    first, so a partial render shows exactly what the full render shows."""
    size = max(8, round(tpl["size"] * height))
    outline = round(tpl["outline"] * height, 2)
    shadow = round(tpl["shadow"] * height, 2)
    box = tpl.get("box")
    if box:
        border_style = 3
        outline_col = _ass_color(box.get("color", "#000000"), 1 - float(box.get("opacity", 0.6)))
        back_col = outline_col
        outline = max(outline, round(0.006 * height, 2))
        shadow = 0
    else:
        border_style = 1
        outline_col = _ass_color(tpl["outline_color"])
        back_col = _ass_color(tpl["shadow_color"], 0.35)
    style = ",".join(str(v) for v in [
        "Cap", tpl["font"], size, _ass_color(tpl["color"]), _ass_color(tpl["color"]), outline_col, back_col,
        -1 if tpl["bold"] else 0, -1 if tpl["italic"] else 0, 0, 0, 100, 100,
        round(tpl["letter_spacing"] * size, 2), 0, border_style, outline, shadow, 5, 0, 0, 0, 1,
    ])
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding\n"
        f"Style: {style}\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    x, y = round(tpl["position"]["x"] * width), round(tpl["position"]["y"] * height)
    pos = f"\\an5\\pos({x},{y})"
    anim = tpl["animation"]
    groups = caption_groups(words, tpl)
    lines = []
    w0, w1 = window if window else (0.0, float("inf"))

    def emit(start: float, end: float, body: str) -> None:
        start, end = max(start, w0), min(end, w1)
        if end - start >= 0.01:
            lines.append(f"Dialogue: 0,{_ts(start - w0)},{_ts(end - w0)},Cap,,0,0,0,,{body}")

    shown = 0
    for g in groups:
        g_start, g_end = g["start"], g["end"]
        if g_end <= w0 or g_start >= w1:
            continue
        shown += 1
        group = g["words"]
        texts = [w["text"] for w in group]
        if anim in ("none", "pop", "fade"):
            tags = pos
            if g_start < w0:
                pass  # already on screen when the window opens: no entrance animation
            elif anim == "pop":
                tags += "\\fscx72\\fscy72\\t(0,70,\\fscx106\\fscy106)\\t(70,130,\\fscx100\\fscy100)"
            elif anim == "fade":
                tags += "\\fad(90,70)"
            emit(g_start, g_end, f"{{{tags}}}{' '.join(texts)}")
            continue
        for wi, w in enumerate(group):
            start = w["start"] if wi else g_start
            end = group[wi + 1]["start"] if wi + 1 < len(group) else g_end
            if anim == "highlight":
                parts = [f"{{\\c{_inline_color(tpl['highlight_color'])}}}{t}{{\\c{_inline_color(tpl['color'])}}}"
                         if i == wi else t for i, t in enumerate(texts)]
                body = " ".join(parts)
            else:  # word_reveal: later words are laid out but invisible, so the line does not shift
                shown = " ".join(texts[: wi + 1])
                hidden = " ".join(texts[wi + 1:])
                body = shown + (f"{{\\alpha&HFF&}} {hidden}" if hidden else "")
            emit(start, end, f"{{{pos}}}{body}")
    return head + "\n".join(lines) + "\n", shown
