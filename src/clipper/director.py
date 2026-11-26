"""The AI editor: DeepSeek reads the transcript and does the editing.

Two jobs:

* ``choose_clips`` - pick the shorts out of a long recording (JSON Output).
* ``direct`` - edit one EDL by calling the edit tools, either from a standing
  brief ("make this a finished short") or from what the user typed.

The model only ever sees text: numbered words with their times, and a summary
of the edit. It refers to footage by word number, which stays valid while the
edit changes underneath it; clipper turns word numbers into times.
"""
from __future__ import annotations

import json

from . import audio as audio_mod
from . import deepseek, ops
from . import templates as templates_mod
from .edl import ASPECTS, TRANSITIONS, Timeline, resolve
from .ffmpeg import ClipperError, log
from .project import Project

MAX_ROUNDS = 14

DEFAULT_BRIEF = (
    "Turn this raw take into a finished short video. Remove the filler words and shorten the long pauses. Cut false "
    "starts, repeated takes and tangents, keeping the best version of each thought. Put punch-in zooms on the lines "
    "that carry the most emphasis. If the opening states what the video is about, add a short title card. Add one to "
    "three keyword callouts on the phrases a viewer should remember."
)

SYSTEM = (
    "You are the editor inside clipper, a tool that edits talking-head video: one person speaking to one camera. "
    "You edit by calling tools. You cannot see or hear the footage; you work from the transcript.\n\n"
    "The transcript is listed one sentence per line as `[first-last] start-end  text`: the numbers of its first and "
    "last word, and where it currently sits in the output video in seconds. A line marked CUT is not in the video "
    "now; a line marked PART has some words cut. Tools take word numbers, never seconds. Word numbers never change, "
    "whatever you cut.\n\n"
    "How to work:\n"
    "- Do what the request asks and nothing else. When the request is a general one to finish the video, use your "
    "judgement as an editor would.\n"
    "- Cut whole phrases or sentences so the speech still flows; do not cut single words out of the middle of a "
    "sentence except fillers. Never cut so much that the argument stops making sense.\n"
    "- A zoom should cover one sentence or one clause. A callout repeats two to four words the speaker is saying at "
    "that moment. A title is at most six words.\n"
    "- Tool results tell you what happened. If a tool reports a problem, fix the call or move on; do not repeat it "
    "unchanged.\n"
    "- When you are finished, stop calling tools and answer with two or three plain sentences saying what you "
    "changed, for the person who asked. No lists, no markdown."
)


# ---- what the model reads ------------------------------------------------------------

def _kept(tl: Timeline, word: dict) -> tuple[float, float] | None:
    """Where a word sits on the output timeline, or None if (most of) it is cut. Same rule as the editor page."""
    ranges = tl.map_range(word["start"], word["end"])
    if sum(b - a for a, b in ranges) < 0.5 * max(word["end"] - word["start"], 1e-3):
        return None
    return ranges[0][0], ranges[-1][1]


def _word_span(tl: Timeline, words: list[dict], first: int, last: int) -> tuple[float, float]:
    """Where words first..last sit on the output timeline right now."""
    if not (0 <= first <= last < len(words)):
        raise ClipperError(f"Word numbers run from 0 to {len(words) - 1}; got {first}-{last}.")
    spans = [span for i in range(first, last + 1) if (span := _kept(tl, words[i]))]
    if not spans:
        raise ClipperError(f"Words {first}-{last} are cut, so nothing can be placed on them. Restore them first, or pick words that are kept.")
    return min(a for a, _ in spans), max(b for _, b in spans)


def listing(doc: dict, words: list[dict]) -> str:
    """The transcript as the model reads it, limited to the stretch of the source this edit draws on."""
    tl = Timeline(doc)
    lo, hi = min(s.src_start for s in tl.segs), max(s.src_end for s in tl.segs)
    lines = []
    for sent in ops.sentences(words):
        if words[sent[-1]]["end"] < lo - 30 or words[sent[0]]["start"] > hi + 30:
            continue
        kept = [i for i in sent if _kept(tl, words[i])]
        text = " ".join(words[i]["w"] for i in sent)
        if not kept:
            lines.append(f"[{sent[0]}-{sent[-1]}] CUT  {text}")
            continue
        a, b = _word_span(tl, words, kept[0], kept[-1])
        mark = "" if len(kept) == len(sent) else " PART"
        lines.append(f"[{sent[0]}-{sent[-1]}] {a:.1f}-{b:.1f}s{mark}  {text}")
    return "\n".join(lines)


def edit_summary(doc: dict) -> str:
    tl = Timeline(doc)
    res = resolve(doc, tl)
    cap = doc["captions"]
    parts = [f"Output: {tl.duration:.1f}s of {doc['source']['duration']:.1f}s source, shape {doc['output']['aspect']}, "
             f"{len(tl.cuts())} cuts.",
             "Captions: " + (f"on, template {cap['template']}" if cap.get("enabled") and cap.get("template") else "off") + "."]
    for kind, label in (("zooms", "Zooms"), ("overlays", "Graphics"), ("broll", "B-roll")):
        items = res[kind]
        if items:
            parts.append(f"{label}: " + "; ".join(
                f"{it['id']} at {it['tl_start']:.1f}-{it['tl_end']:.1f}s"
                + (f" {it['template']} {json.dumps(it.get('params', {}), ensure_ascii=False)}" if kind == "overlays" else "")
                for it in items) + ".")
    if res["sfx"]:
        parts.append(f"Sound effects: {len(res['sfx'])}.")
    if doc.get("music"):
        parts.append("Music: on.")
    return " ".join(parts)


# ---- the tools the model can call ------------------------------------------------------

def _tool(name: str, description: str, properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties or {}, "required": required or []}}}


_WORD = lambda what: {"type": "integer", "description": f"Number of the {what} word."}


def tool_specs() -> list[dict]:
    captions = [t["name"] for t in templates_mod.list_caption_templates()]
    kinds = sorted({t["kind"] for t in templates_mod.list_overlay_templates()})
    return [
        _tool("remove_fillers", "Cut every filler word (um, uh and the like) that is still in the video."),
        _tool("shorten_pauses", "Shorten silences longer than max_pause seconds down to keep seconds.",
              {"max_pause": {"type": "number", "description": "Default 0.6."}, "keep": {"type": "number", "description": "Default 0.25."}}),
        _tool("cut_words", "Remove words first..last from the video (a false start, a repeated take, a tangent).",
              {"first": _WORD("first"), "last": _WORD("last"), "reason": {"type": "string", "description": "A few words on why."}},
              ["first", "last"]),
        _tool("restore_words", "Put cut words first..last back into the video.", {"first": _WORD("first"), "last": _WORD("last")},
              ["first", "last"]),
        _tool("zoom", "Punch in on the speaker while words first..last are spoken.",
              {"first": _WORD("first"), "last": _WORD("last"),
               "scale": {"type": "number", "description": "How far to zoom, 1.05 to 1.5. Default 1.15."}}, ["first", "last"]),
        _tool("auto_zoom", "Place punch-in zooms automatically on the most emphatic sentences.",
              {"per_minute": {"type": "number", "description": "How many per minute, 1 to 12."}}, ["per_minute"]),
        _tool("graphic", "Add a motion graphic. title_card: a title, usually at the very start. lower_third: the "
              "speaker's name and role. keyword_callout: a short phrase shown while it is said.",
              {"kind": {"type": "string", "enum": kinds}, "text": {"type": "string", "description": "The main text."},
               "secondary": {"type": "string", "description": "Second line: subtitle or role. Optional."},
               "at_word": {"type": "integer", "description": "Word it appears on. Leave out to put it at the start of the video."},
               "seconds": {"type": "number", "description": "How long it stays. Optional."}}, ["kind", "text"]),
        _tool("captions", "Turn captions on with a template, or off.",
              {"template": {"type": "string", "enum": captions + ["none"]},
               "max_words": {"type": "integer", "description": "Words shown at a time, 1 to 8. Optional."}}, ["template"]),
        _tool("shape", "Set the shape of the output video.", {"aspect": {"type": "string", "enum": [*ASPECTS, "source"]}}, ["aspect"]),
        _tool("transitions", "Put a transition on the biggest breaks between kept parts.",
              {"type": {"type": "string", "enum": list(TRANSITIONS)}, "per_minute": {"type": "number", "description": "Default 2."}},
              ["type"]),
        _tool("sounds", "Put a sound effect on cuts, zooms or transitions.",
              {"sound": {"type": "string", "enum": list(audio_mod.BUILTIN_SFX)},
               "on": {"type": "string", "enum": ["transitions", "zooms", "cuts", "all"]}}, ["sound", "on"]),
        _tool("remove", "Remove one zoom, graphic, b-roll or sound by its id (ids are in the edit summary).",
              {"id": {"type": "string"}}, ["id"]),
        _tool("clear", "Remove every item of one kind.",
              {"kind": {"type": "string", "enum": ["zooms", "overlays", "sfx", "transitions"]}}, ["kind"]),
        _tool("read_edit", "Read the transcript and the edit as they are now, after your changes."),
    ]


def _run_tool(project: Project, doc: dict, words: list[dict], name: str, a: dict) -> str:
    tl = Timeline(doc)
    if name == "remove_fillers":
        r = ops.remove_fillers(doc, words)
        return f"Cut {r['removed_count']} fillers, {r['removed_seconds']:.1f}s."
    if name == "shorten_pauses":
        r = ops.remove_pauses(doc, project.silences(), words, float(a.get("max_pause", 0.6)), float(a.get("keep", 0.25)))
        return f"Shortened {r['pauses_shortened']} pauses, {r['removed_seconds']:.1f}s removed."
    if name == "cut_words":
        before = tl.duration
        ops.cut_words(doc, words, int(a["first"]), int(a["last"]))
        return f"Cut words {a['first']}-{a['last']}. The video is now {Timeline(doc).duration:.1f}s (was {before:.1f}s)."
    if name == "restore_words":
        ops.restore_words(doc, words, int(a["first"]), int(a["last"]))
        return f"Restored words {a['first']}-{a['last']}. The video is now {Timeline(doc).duration:.1f}s."
    if name == "zoom":
        start, end = _word_span(tl, words, int(a["first"]), int(a["last"]))
        end = max(end, start + 0.6)
        scale = min(1.5, max(1.05, float(a.get("scale", 1.15))))
        item = ops.add_zoom(doc, start, min(end, tl.duration), scale)["added"]
        return f"Zoom {item['id']} added at {start:.1f}-{end:.1f}s, {scale}x."
    if name == "auto_zoom":
        r = ops.auto_zoom(doc, words, project.word_levels(), per_minute=min(12.0, max(0.0, float(a["per_minute"]))))
        return f"{r['zooms_added']} zooms placed."
    if name == "graphic":
        tpl = next((t for t in templates_mod.list_overlay_templates() if t["kind"] == a["kind"]), None)
        if not tpl:
            raise ClipperError(f"No template of kind {a['kind']!r} is installed.")
        text_keys = [k for k, spec in tpl["params"].items() if spec.get("type") == "text"]
        params = {text_keys[0]: str(a["text"]).strip()}
        if a.get("secondary") and len(text_keys) > 1:
            params[text_keys[1]] = str(a["secondary"]).strip()
        start = 0.0 if a.get("at_word") is None else _word_span(tl, words, int(a["at_word"]), int(a["at_word"]))[0]
        seconds = float(a["seconds"]) if a.get("seconds") else None
        item = ops.add_overlay(doc, tpl["id"], min(start, max(0.0, tl.duration - 0.3)), seconds, params)["added"]
        return f"Graphic {item['id']} ({tpl['id']}) added at {start:.1f}s for {item['duration']:.1f}s."
    if name == "captions":
        if a["template"] == "none":
            ops.set_captions(doc, doc["captions"].get("template"), False, doc["captions"].get("overrides"))
            return "Captions are off."
        overrides = {"max_words": min(8, max(1, int(a["max_words"])))} if a.get("max_words") else None
        ops.set_captions(doc, a["template"], True, overrides)
        return f"Captions are on with the {a['template']} template."
    if name == "shape":
        ops.set_output(doc, a["aspect"])
        return f"The output is now {doc['output']['width']}x{doc['output']['height']}."
    if name == "transitions":
        r = ops.auto_transitions(doc, a["type"], 0.3, float(a.get("per_minute", 2.0)))
        return f"{r['transitions_added']} transitions placed." if r["transitions_added"] else "No cut removed enough footage to take a transition."
    if name == "sounds":
        r = ops.sfx_on_cuts(doc, a["sound"], -10.0, a["on"])
        return f"{r['sfx_added']} sounds placed." if r["sfx_added"] else "Nothing to put sounds on yet."
    if name == "remove":
        ops.remove_item(doc, a["id"])
        return f"Removed {a['id']}."
    if name == "clear":
        ops.clear_items(doc, a["kind"])
        return f"Removed all {a['kind']}."
    if name == "read_edit":
        return edit_summary(doc) + "\n\n" + listing(doc, words)
    raise ClipperError(f"There is no tool called {name!r}.")


def _describe(name: str, a: dict) -> str:
    """A short line about one step, for the person watching."""
    if name in ("cut_words", "restore_words", "zoom"):
        verb = {"cut_words": "Cut", "restore_words": "Restored", "zoom": "Zoomed on"}[name]
        return f"{verb} words {a.get('first')}-{a.get('last')}" + (f": {a['reason']}" if a.get("reason") else "")
    if name == "graphic":
        return f"Added a {str(a.get('kind', 'graphic')).replace('_', ' ')}: {a.get('text', '')}"
    labels = {"remove_fillers": "Removed fillers", "shorten_pauses": "Shortened pauses", "auto_zoom": "Placed zooms",
              "captions": f"Captions: {a.get('template')}", "shape": f"Shape: {a.get('aspect')}",
              "transitions": f"Transitions: {a.get('type')}", "sounds": f"Sounds: {a.get('sound')}",
              "remove": f"Removed {a.get('id')}", "clear": f"Removed all {a.get('kind')}", "read_edit": "Read the edit"}
    return labels.get(name, name)


# ---- the two jobs -------------------------------------------------------------------------

def direct(project: Project, doc: dict, instruction: str | None = None) -> dict:
    """Let DeepSeek edit ``doc`` in place. Returns what it said and did. Raises ClipperError if it cannot run."""
    if not project.has_transcript():
        raise ClipperError("Transcribe the video first: the AI editor works from the transcript.")
    words = project.words()
    request = (instruction or "").strip() or DEFAULT_BRIEF
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Request: {request}\n\nThe edit now: {edit_summary(doc)}\n\n"
                                    f"<transcript>\n{listing(doc, words)}\n</transcript>"},
    ]
    tools, steps, model_name = tool_specs(), [], deepseek.model()
    for round_no in range(MAX_ROUNDS):
        log(f"AI editor: thinking (step {round_no + 1})")
        message = deepseek.chat(messages, tools=tools)
        model_name = message.pop("_model", model_name)
        calls = message.get("tool_calls") or []
        messages.append(message)   # with its reasoning_content, which DeepSeek wants passed back when tools are in use
        if not calls:
            summary = (message.get("content") or "").strip() or "Done."
            _note(doc, request, summary, steps, model_name)
            return {"summary": summary, "steps": steps, "model": model_name}
        for call in calls:
            name, args = call["function"]["name"], {}
            try:
                parsed = json.loads(call["function"].get("arguments") or "{}")
                if not isinstance(parsed, dict):
                    raise ValueError("the arguments must be a JSON object")
                args = parsed
                result, ok = _run_tool(project, doc, words, name, args), True
            except KeyError as exc:
                result, ok = f"Problem: the argument {exc} is missing.", False
            except (ClipperError, TypeError, ValueError) as exc:
                result, ok = f"Problem: {exc}", False
            if name != "read_edit":
                steps.append({"tool": name, "did": _describe(name, args), "ok": ok, "result": result})
                log("AI editor: " + (steps[-1]["did"] if ok else result))
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    summary = "Stopped after the most steps allowed in one go. What was done so far is kept."
    _note(doc, request, summary, steps, model_name)
    return {"summary": summary, "steps": steps, "model": model_name}


def _note(doc: dict, request: str, summary: str, steps: list[dict], model_name: str) -> None:
    notes = [n for n in doc.setdefault("notes", []) if n.get("kind") != "ai"]
    notes.append({"kind": "ai", "request": request, "summary": summary, "model": model_name,
                  "steps": [s["did"] for s in steps if s["ok"]]})
    doc["notes"] = notes


def choose_clips(words: list[dict], count: int, min_len: float, max_len: float) -> list[dict]:
    """Ask DeepSeek for the best stand-alone clips. Returns source ranges with a title and a reason."""
    from .select import sentence_list

    sents = sentence_list(words)
    lines = "\n".join(f"[{i}] {s['start']:.1f}-{s['end']:.1f}s  {s['text']}" for i, s in enumerate(sents))
    prompt = (
        f"Below is the transcript of one long talking-head video, one numbered sentence per line with its start and end "
        f"time in seconds. Choose the {count} best stand-alone short clips for vertical social video.\n\n"
        f"Each clip is a run of consecutive sentences lasting between {min_len:.0f} and {max_len:.0f} seconds. It has to "
        "make sense to someone who has seen nothing else: it opens on a line that hooks without earlier context, and it "
        f"ends on a finished thought. Clips must not overlap. If fewer than {count} parts are good enough, return fewer.\n\n"
        "Answer in json, in exactly this form:\n"
        '{"clips": [{"first_sentence": 4, "last_sentence": 9, "title": "A short title a viewer would see", '
        '"reason": "One sentence on why it works"}]}\n\n'
        f"<transcript>\n{lines}\n</transcript>"
    )
    answer = deepseek.chat_json([{"role": "user", "content": prompt}])
    clips = []
    for c in answer.get("clips", []):
        try:
            i, j = int(c["first_sentence"]), int(c["last_sentence"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= i <= j < len(sents)):
            continue
        start, end = max(0.0, sents[i]["start"] - 0.15), sents[j]["end"] + 0.25
        if not (min_len * 0.6 <= end - start <= max_len * 1.4):   # the model miscounted the length badly
            continue
        clips.append({"start": round(start, 3), "end": round(end, 3), "title": str(c.get("title", "Clip"))[:120],
                      "reason": str(c.get("reason", ""))[:300], "method": f"deepseek:{deepseek.model()}"})
    clips.sort(key=lambda c: c["start"])
    return [c for k, c in enumerate(clips) if k == 0 or c["start"] >= clips[k - 1]["end"] - 0.5][:count]
