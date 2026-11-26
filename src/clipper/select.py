"""Built-in clip selection: the fallback `select` stage for headless runs.

When an agent is connected over MCP it does the choosing itself (it reads the
transcript and calls create_clip). This module is for runs with no agent:

* heuristic (default) - scores windows of whole sentences, no network needed;
* DeepSeek (`use_llm=True`, or by default once a key is set) - asks the model
  to choose, and falls back to the heuristic if the API is unavailable.
"""
from __future__ import annotations

import json
import os
import re

from .ffmpeg import ClipperError, log
from .ops import EMPHASIS_WORDS, norm_word, sentences

HOOK_OPENERS = ("how", "why", "what", "the", "here's", "heres", "this", "most", "if", "stop", "never", "imagine",
                "nobody", "everyone", "you", "there", "one", "three", "five")
WEAK_OPENERS = ("and", "but", "so", "or", "because", "also", "then", "it", "that", "they", "he", "she", "which",
                "well", "yeah", "um", "uh")


def sentence_list(words: list[dict]) -> list[dict]:
    out = []
    for idxs in sentences(words, max_gap=1.0):
        text = " ".join(words[i]["w"].strip() for i in idxs)
        out.append({"start": words[idxs[0]]["start"], "end": words[idxs[-1]]["end"], "text": text,
                    "n_words": len(idxs)})
    return out


def _score(window: list[dict]) -> tuple[float, str]:
    first, last = window[0], window[-1]
    toks = [norm_word(t) for t in first["text"].split()]
    score, why = 0.0, []
    if toks and toks[0] in HOOK_OPENERS:
        score += 2.0
        why.append("strong opening")
    if toks and toks[0] in WEAK_OPENERS:
        score -= 2.5
        why.append("opens mid-thought")
    if first["text"].rstrip().endswith("?"):
        score += 1.5
        why.append("opens with a question")
    if any(re.search(r"\d", t) for t in toks):
        score += 1.0
        why.append("number in the hook")
    all_toks = [norm_word(t) for s in window for t in s["text"].split()]
    emph = sum(t in EMPHASIS_WORDS for t in all_toks)
    score += min(3.0, emph * 0.4)
    if re.search(r"[.!?]['\")]?$", last["text"].strip()):
        score += 1.5
    else:
        score -= 2.0
        why.append("ends mid-sentence")
    dur = last["end"] - first["start"]
    spoken = sum(s["end"] - s["start"] for s in window)
    density = len(all_toks) / max(dur, 1e-3)
    score += min(2.0, density / 2.0)
    score -= max(0.0, (dur - spoken) / dur - 0.25) * 4  # long silences inside
    if emph:
        why.append(f"{emph} emphatic word(s)")
    return score, ", ".join(why) or "complete thought"


def select_heuristic(words: list[dict], count: int = 3, min_len: float = 20.0, max_len: float = 60.0) -> list[dict]:
    sents = sentence_list(words)
    cands = []
    for i in range(len(sents)):
        for j in range(i, len(sents)):
            dur = sents[j]["end"] - sents[i]["start"]
            if dur > max_len:
                break
            if dur >= min_len:
                score, why = _score(sents[i:j + 1])
                cands.append({"start": sents[i]["start"], "end": sents[j]["end"], "score": round(score, 2),
                              "reason": why, "title": sents[i]["text"][:70]})
    if not cands and sents:
        # The whole thing is shorter than min_len (or one long run-on): offer it as a single clip.
        total = sents[-1]["end"] - sents[0]["start"]
        if total <= max_len:
            cands.append({"start": sents[0]["start"], "end": sents[-1]["end"], "score": 0.0,
                          "reason": "the video is shorter than the minimum clip length",
                          "title": sents[0]["text"][:70]})
    cands.sort(key=lambda c: -c["score"])
    chosen: list[dict] = []
    for c in cands:
        if len(chosen) >= count:
            break
        if all(c["start"] >= o["end"] or c["end"] <= o["start"] for o in chosen):
            chosen.append(c)
    chosen.sort(key=lambda c: c["start"])
    for c in chosen:
        c["start"], c["end"] = round(max(0.0, c["start"] - 0.15), 3), round(c["end"] + 0.25, 3)
        c["method"] = "heuristic"
    return chosen


def select_clips(words: list[dict], count: int = 3, min_len: float = 20.0, max_len: float = 60.0,
                 use_llm: bool | None = None) -> dict:
    """Propose clips. `use_llm`: True asks DeepSeek, False never does, None asks it when a key is set."""
    from . import deepseek, director

    note = None
    if use_llm or (use_llm is None and deepseek.is_ready()):
        try:
            clips = director.choose_clips(words, count, min_len, max_len)
            if clips:
                return {"clips": clips, "method": "deepseek", "note": None}
            note = "DeepSeek returned no usable clips; used the built-in heuristic instead."
        except ClipperError as exc:
            note = f"{exc} Used the built-in heuristic instead."
        log(note)
    return {"clips": select_heuristic(words, count, min_len, max_len), "method": "heuristic", "note": note}
