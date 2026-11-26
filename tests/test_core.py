"""Fast tests of the EDL maths and edit operations. No media needed."""
import pytest

from clipper import ops
from clipper.captions import build_ass, group_words, timeline_words
from clipper.edl import Timeline, new_edl, resolve, validate
from clipper.ffmpeg import ClipperError
from clipper.render import build_runs
from clipper.select import select_heuristic
from clipper.templates import list_caption_templates, list_overlay_templates, load_caption_template

SOURCE = {"path": "/nonexistent/talk.mp4", "duration": 60.0, "width": 1920, "height": 1080, "fps": 30.0}


def words_every(step=0.5, n=100, fillers=()):
    out = []
    for i in range(n):
        text = "um" if i in fillers else f"word{i}"
        out.append({"w": text, "start": round(i * step, 3), "end": round(i * step + 0.3, 3)})
    return out


def make(segments=None, aspect="9:16"):
    return new_edl("t", "polish", SOURCE, segments, aspect)


def test_new_edl_sizes():
    assert (make()["output"]["width"], make()["output"]["height"]) == (1080, 1920)
    assert (make(aspect="source")["output"]["width"], make(aspect="source")["output"]["height"]) == (1920, 1080)
    with pytest.raises(ClipperError):
        make(aspect="7:3")


def test_timeline_maps_both_ways():
    tl = Timeline(make([(10, 20), (30, 35)]))
    assert tl.duration == pytest.approx(15)
    assert tl.to_timeline(12) == pytest.approx(2)
    assert tl.to_timeline(31) == pytest.approx(11)
    assert tl.to_timeline(25) is None
    assert tl.to_timeline(25, snap="next") == pytest.approx(10)
    assert tl.to_timeline(25, snap="prev") == pytest.approx(10)
    assert tl.to_source(11) == pytest.approx(31)
    assert tl.map_range(18, 32) == [(pytest.approx(8), pytest.approx(12))]
    assert tl.source_ranges(8, 12) == [(pytest.approx(18), pytest.approx(20)), (pytest.approx(30), pytest.approx(32))]


def test_segments_snap_to_frames():
    tl = Timeline(make([(0.011, 1.021), (2.0, 3.0)]))
    for seg in tl.segs:
        assert (seg.src_start * 30) == pytest.approx(round(seg.src_start * 30))
        assert (seg.tl_end * 30) == pytest.approx(round(seg.tl_end * 30))


def test_cut_and_trim():
    edl = make([(0, 60)])
    assert ops.cut(edl, 10, 20)["removed"] == pytest.approx(10)
    assert [(s["start"], s["end"]) for s in edl["segments"]] == [(0, 10), (20, 60)]
    ops.trim(edl, 5, 15)
    assert [(s["start"], s["end"]) for s in edl["segments"]] == [(5, 10), (20, 25)]
    with pytest.raises(ClipperError):
        ops.cut(edl, 0, 10)  # would remove everything
    with pytest.raises(ClipperError):
        ops.cut(edl, 5, 99)


def test_remove_fillers_keeps_breathing_room():
    words = words_every(fillers={3, 10})
    edl = make([(0, 50)])
    res = ops.remove_fillers(edl, words)
    assert res["removed_count"] == 2
    tl = Timeline(edl)
    for i in (3, 10):
        mid = (words[i]["start"] + words[i]["end"]) / 2
        assert tl.to_timeline(mid) is None
    for i in (2, 4, 9, 11):  # neighbours survive whole
        assert tl.map_range(words[i]["start"], words[i]["end"])[0][1] - tl.map_range(
            words[i]["start"], words[i]["end"])[0][0] == pytest.approx(0.3, abs=0.04)


def test_remove_pauses_respects_words():
    edl = make([(0, 60)])
    silences = [(5.0, 8.0), (20.0, 20.4), (30.0, 33.0)]
    quiet_word = [{"w": "psst", "start": 31.0, "end": 31.6}]
    res = ops.remove_pauses(edl, silences, quiet_word, max_pause=0.6, keep=0.2)
    assert res["pauses_shortened"] == 1  # 20.0-20.4 is short; 30-33 holds a quiet word
    assert res["removed_seconds"] == pytest.approx(2.8, abs=0.05)


def test_content_anchored_items_follow_cuts():
    edl = make([(0, 60)])
    ops.add_zoom(edl, 30, 33, 1.2)                 # content anchored -> source 30..33
    ops.add_sfx(edl, 40, "pop")
    edl["overlays"].append({"id": "o1", "template": "title_card/plain-top", "start": 0.0, "duration": 2.0,
                            "anchor": "timeline", "params": {"title": "x"}})
    ops.cut(edl, 10, 20)
    res = resolve(edl)
    assert res["zooms"][0]["tl_start"] == pytest.approx(20)
    assert res["sfx"][0]["tl_at"] == pytest.approx(30)
    assert res["overlays"][0]["tl_start"] == pytest.approx(0)  # timeline anchored: stays put
    ops.cut(edl, 19, 25)                           # removes the zoom's footage entirely
    res = resolve(edl)
    assert res["zooms"] == [] and any("zoom" in w for w in res["warnings"])


def test_zoom_split_by_a_cut_stays_one_range():
    edl = make([(0, 60)])
    ops.add_zoom(edl, 10, 20, 1.2)
    ops.cut(edl, 12, 14)
    z = resolve(edl)["zooms"]
    assert len(z) == 1 and z[0]["tl_end"] - z[0]["tl_start"] == pytest.approx(8, abs=0.05)


def test_window_reports_phase():
    edl = make([(0, 60)])
    ops.add_zoom(edl, 10, 20, 1.2, anchor="timeline")
    res = resolve(edl, window=(15, 30))
    assert res["zooms"][0]["tl_start"] == 0 and res["zooms"][0]["tl_end"] == pytest.approx(5)
    assert res["zooms"][0]["phase"] == pytest.approx(5)


def test_transitions_and_runs():
    edl = make([(0, 10), (12, 20), (25, 30), (2, 4)])
    ops.set_transition(edl, 10.0, "crossfade", 0.4)
    tl = Timeline(edl)
    runs = build_runs(tl, SOURCE["duration"])
    # run 2 starts at the transition; the last segment jumps backwards so it is its own run
    assert [len(r.segs) for r in runs] == [1, 2, 1]
    assert runs[1].transition["type"] == "crossfade" and runs[1].head == 6 and runs[0].tail == 6
    total = runs[0].frames
    for r in runs[1:]:
        total += r.frames - (2 * r.head if r.transition else 0)
    assert total == round(tl.duration * 30)
    with pytest.raises(ClipperError):
        ops.set_transition(edl, 10.0, "sparkle")
    with pytest.raises(ClipperError):
        ops.set_transition(make([(0, 60)]), 10.0, "crossfade")  # no cuts


def test_auto_zoom_and_sfx_on_cuts():
    words = words_every(n=110)
    for i in range(9, 110, 10):
        words[i]["w"] += "."
    words[20]["w"] = "never"
    edl = make([(0, 55)])
    res = ops.auto_zoom(edl, words, None, per_minute=4)
    assert 1 <= res["zooms_added"] <= 4
    starts = sorted(z["tl_start"] for z in resolve(edl)["zooms"])
    assert all(b - a >= 1.5 for a, b in zip(starts, starts[1:]))
    assert ops.sfx_on_cuts(edl, which="zooms")["sfx_added"] >= 1
    assert all(s["sound"] == "whoosh" for s in edl["sfx"])


def test_validate_reports_missing_files():
    edl = make([(0, 10)])
    edl["music"] = {"path": "/nonexistent/music.mp3"}
    problems = validate(edl)
    assert any("Source video is missing" in p for p in problems)
    assert any("Music file is missing" in p for p in problems)


def test_caption_grouping_and_ass():
    tpl = load_caption_template("highlight")
    words = [{"w": w, "start": i * 0.4, "end": i * 0.4 + 0.3} for i, w in
             enumerate("So today I want to show you. Three {big} mistakes".split())]
    groups = group_words(words, tpl)
    assert all(len(g) <= tpl["max_words"] for g in groups)
    assert groups[1][-1]["text"] == "YOU"            # sentence end closes a group, punctuation stripped
    text, n = build_ass(words, tpl, 1080, 1920)
    assert n == len(groups) and "PlayResY: 1920" in text
    assert "{BIG}" not in text and "(BIG)" in text   # braces would be read as ASS tags
    assert text.count("Dialogue:") == len(words)     # highlight: one event per word


def test_caption_words_follow_the_edit():
    edl = make([(0, 2), (4, 6)])
    words = [{"w": "a", "start": 0.5, "end": 0.9}, {"w": "gone", "start": 2.5, "end": 3.0},
             {"w": "b", "start": 4.5, "end": 4.9}]
    out = timeline_words(words, Timeline(edl))
    assert [w["w"] for w in out] == ["a", "b"] and out[1]["start"] == pytest.approx(2.5)


def test_select_heuristic_prefers_complete_thoughts():
    text = ("And so that was that. Here is the biggest mistake people make with video. They never cut the pauses. "
            "It makes every second count. That is the whole secret. But anyway moving on to")
    words, t = [], 0.0
    for w in text.split():
        words.append({"w": w, "start": t, "end": t + 0.35})
        t += 0.5
    clips = select_heuristic(words, count=1, min_len=6, max_len=14)
    assert len(clips) == 1
    assert clips[0]["title"].startswith("Here is the biggest mistake")


def test_builtin_templates_present():
    assert {"bold-pop", "highlight", "clean"} <= {t["name"] for t in list_caption_templates()}
    kinds = {}
    for t in list_overlay_templates():
        kinds.setdefault(t["kind"], []).append(t["id"])
    assert all(len(kinds.get(k, [])) >= 2 for k in ("title_card", "lower_third", "keyword_callout"))


def test_add_overlay_validates_params():
    edl = make([(0, 30)])
    with pytest.raises(ClipperError):
        ops.add_overlay(edl, "title_card/plain-top", 0)                       # title is required
    with pytest.raises(ClipperError):
        ops.add_overlay(edl, "title_card/plain-top", 0, params={"title": "x", "nope": 1})
    item = ops.add_overlay(edl, "keyword_callout/plain-pill", 5, params={"text": "hi"})["added"]
    assert item["anchor"] == "source" and item["duration"] == pytest.approx(1.8)
    assert ops.add_overlay(edl, "title_card/plain-top", 0, params={"title": "x"})["added"]["anchor"] == "timeline"


def test_dated_offer_shows_only_inside_its_dates():
    from datetime import date

    from clipper import plans

    offer = plans.PROMO
    starts, ends = date.fromisoformat(offer["starts"]), date.fromisoformat(offer["ends"])
    assert plans.active_promo(date.fromordinal(starts.toordinal() - 1)) is None
    assert plans.active_promo(date.fromordinal(ends.toordinal() + 1)) is None
    live = plans.active_promo(ends)
    assert live["days_left"] == 0 and str(offer["percent"]) in live["headline"] and live["ends_label"].startswith(str(ends.day))

    during = {p["id"]: p for p in plans.payload(starts)["plans"]}
    after = {p["id"]: p for p in plans.payload(date.fromordinal(ends.toordinal() + 1))["plans"]}
    for pid, p in during.items():
        if pid in offer["plans"]:
            assert p["promo_monthly"] == pytest.approx(p["monthly"] * (100 - offer["percent"]) / 100, abs=0.006)
            assert p["promo_yearly"] == pytest.approx(p["yearly"] * (100 - offer["percent"]) / 100, abs=0.006)
        else:                                            # Free and Business are never discounted
            assert p["promo_monthly"] is None and p["promo_yearly"] is None
        assert after[pid]["promo_monthly"] is None       # and nothing is, once the offer has ended
    assert plans.payload(date.fromordinal(ends.toordinal() + 1))["promo"] is None


def test_windowed_captions_match_the_full_render():
    """A partial render (preview range, single frame) must show the same caption groups as a full one."""
    tpl = load_caption_template("highlight")
    words = [{"w": w, "start": i * 0.4, "end": i * 0.4 + 0.3} for i, w in enumerate("nobody has time for that today".split())]
    full, _ = build_ass(words, tpl, 1080, 1920)
    part, n = build_ass(words, tpl, 1080, 1920, window=(0.5, 0.6))
    assert n == 1 and part.count("Dialogue:") == 1
    line = [ln for ln in part.splitlines() if ln.startswith("Dialogue:")][0]
    assert "NOBODY" in line and "HAS" in line and "FOR" in line      # the whole group, not just the word in range
    assert line.split(",")[1] == "0:00:00.00"                        # re-based to the window start
    assert line.split("Cap,,0,0,0,,")[1] in full                     # identical text and tags to the full render
