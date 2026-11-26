"""The edit operations by name, as the editor page and the AI editor call them.

Each entry takes the EDL and a dict of arguments and changes the EDL in place.
"""
from __future__ import annotations

from . import ops, pipeline
from .project import Project
from .style_apply import load_style


def op_table(proj: Project) -> dict:
    words = lambda: proj.words()
    levels = lambda: proj.word_levels()
    return {
        "cut": lambda d, a: ops.cut(d, a["start"], a["end"]),
        "trim": lambda d, a: ops.trim(d, a.get("start"), a.get("end")),
        "cut_words": lambda d, a: ops.cut_words(d, words(), a["first"], a["last"]),
        "restore_words": lambda d, a: ops.restore_words(d, words(), a["first"], a["last"]),
        "remove_fillers": lambda d, a: ops.remove_fillers(d, words(), a.get("fillers"), a.get("phrases")),
        "remove_pauses": lambda d, a: ops.remove_pauses(d, proj.silences(), words() if proj.has_transcript() else None,
                                                        a.get("max_pause", 0.6), a.get("keep", 0.25)),
        "auto_polish": lambda d, a: pipeline.finish_edl(proj, d, captions=a.get("captions", "bold-pop"),
                                                        zooms_per_minute=a.get("zooms_per_minute", 6.0)),
        "set_captions": lambda d, a: ops.set_captions(d, a.get("template"), a.get("enabled", True), a.get("overrides")),
        "add_zoom": lambda d, a: ops.add_zoom(d, a["start"], a["end"], a.get("scale", 1.15), a.get("ease", 0.0),
                                              a.get("focus"), a.get("anchor", "content")),
        "auto_zoom": lambda d, a: ops.auto_zoom(d, words(), levels(), a.get("per_minute", 6.0), a.get("scale", 1.15),
                                                a.get("ease", 0.0)),
        "set_transition": lambda d, a: ops.set_transition(d, a["at"], a.get("type", "crossfade"), a.get("duration", 0.3)),
        "auto_transitions": lambda d, a: ops.auto_transitions(d, a.get("type", "crossfade"), a.get("duration", 0.3),
                                                              a.get("per_minute", 2.0)),
        "set_music": lambda d, a: ops.set_music(d, a.get("path"), a.get("gain_db", -20.0), a.get("duck", True),
                                                a.get("duck_amount_db", -10.0), a.get("offset", 0.0)),
        "add_sfx": lambda d, a: ops.add_sfx(d, a["at"], a.get("sound", "whoosh"), a.get("gain_db", -8.0)),
        "sfx_on_cuts": lambda d, a: ops.sfx_on_cuts(d, a.get("sound", "whoosh"), a.get("gain_db", -10.0),
                                                    a.get("which", "transitions")),
        "add_overlay": lambda d, a: ops.add_overlay(d, a["template"], a["start"], a.get("duration"), a.get("params"),
                                                    a.get("anchor")),
        "add_broll": lambda d, a: ops.add_broll(d, a["path"], a["start"], a["end"], a.get("source_in", 0.0),
                                                a.get("fit", "cover")),
        "update_item": lambda d, a: ops.update_item(d, a["id"], a["fields"]),
        "remove_item": lambda d, a: ops.remove_item(d, a["id"]),
        "clear_items": lambda d, a: ops.clear_items(d, a["kind"]),
        "set_output": lambda d, a: ops.set_output(d, a.get("aspect"), a.get("fps"), a.get("loudness")),
        "set_framing": lambda d, a: ops.set_framing(d, a["x"], a["y"]),
        "apply_style": lambda d, a: pipeline.apply_style(d, load_style(a["style"]),
                                                         words() if proj.has_transcript() else None,
                                                         levels() if proj.has_transcript() else None, a.get("music")),
    }
