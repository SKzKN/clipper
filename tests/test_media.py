"""End-to-end tests on synthetic footage. Slow (they transcribe and render).

Build the footage first (macOS only, it uses the `say` voice):

    python tests/make_fixture.py tests/fixture
"""
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from clipper import ops, pipeline
from clipper.checks import FootageRejected, check_footage
from clipper.edl import Timeline
from clipper.media import grab_frame, probe, read_audio
from clipper.style_analyze import analyze_style
from clipper.style_apply import apply_style

FIX = Path(__file__).parent / "fixture"
pytestmark = pytest.mark.skipif(not (FIX / "talk.mp4").exists(), reason="run tests/make_fixture.py first")


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    work = tmp_path_factory.mktemp("proj")
    video = work / "talk.mp4"
    shutil.copy(FIX / "talk.mp4", video)
    proj = pipeline.ingest(str(video))
    pipeline.ensure_transcript(proj, model="base")
    return proj


@pytest.mark.parametrize("name,phrase", [
    ("uhd.mp4", "up to 1080p"),
    ("two_cams.mp4", "more than one camera"),
    ("two_voices.mp4", "more than one speaker"),
])
def test_other_footage_is_rejected_with_a_reason(name, phrase):
    result = check_footage(FIX / name)
    assert not result["ok"]
    assert any(phrase in r for r in result["reasons"]), result["reasons"]
    with pytest.raises(FootageRejected):
        pipeline.ingest(str(FIX / name), project_dir="/tmp/should-not-be-created")
    assert not Path("/tmp/should-not-be-created").exists()


def test_good_footage_passes(project):
    checks = project.meta["checks"]
    assert checks["ok"] and checks["details"]["voices"]["voices"] == 1


def test_transcript_has_fillers_and_timings(project):
    words = project.words()
    text = " ".join(w["w"].lower() for w in words)
    assert "mistake" in text and "um" in text
    assert all(a["end"] <= b["start"] + 1e-6 for a, b in zip(words, words[1:]))


def test_polish_renders_in_sync(project):
    edl, report = pipeline.make_polish(project, "polish", "9:16", captions="bold-pop", zooms_per_minute=6,
                                       music=str(FIX / "music.wav"))
    assert report["fillers"]["removed_count"] >= 2 and report["pauses"]["pauses_shortened"] >= 2
    ops.sfx_on_cuts(edl, which="all")
    ops.add_overlay(edl, "title_card/plain-top", 0.0, 2.0, {"title": "Three mistakes"})
    ops.add_broll(edl, str(FIX / "broll.mp4"), 4.0, 6.0)
    project.save_edl(edl)
    result = pipeline.render_edl(project, "polish", "preview")
    info = probe(result["path"])
    tl = Timeline(edl)
    assert info["duration"] == pytest.approx(tl.duration, abs=0.08)
    assert (info["width"], info["height"]) == (540, 960)
    assert result["caption_groups"] > 5 and result["overlays"] == 1 and result["broll"] == 1
    # Sound and picture were cut at the same places: the rendered speech lines up with the source speech.
    out = read_audio(result["path"], 16000, 1)
    src = read_audio(project.source["path"], 16000, 1)
    seg = max(tl.segs, key=lambda s: s.duration)
    a = out[int(seg.tl_start * 16000): int(seg.tl_end * 16000)]
    b = src[int(seg.src_start * 16000): int(seg.src_start * 16000) + len(a)]
    n = min(len(a), len(b))
    assert np.corrcoef(a[:n], b[:n])[0, 1] > 0.5  # music and effects sit on top, speech still dominates


def test_broll_and_zoom_show_in_frames(project):
    edl = project.new_edl("frames", "polish", [(0.0, 12.0)], "9:16")
    ops.add_zoom(edl, 2.0, 4.0, 1.4)
    ops.add_broll(edl, str(FIX / "broll.mp4"), 6.0, 8.0)
    project.save_edl(edl)
    import cv2

    def frame(t):
        return cv2.imread(pipeline.render_edl(project, "frames", frame_at=t)["path"]).astype(np.float32)

    plain, zoomed, broll = frame(1.0), frame(3.0), frame(7.0)
    assert plain.shape == (1920, 1080, 3)
    assert np.abs(plain - zoomed).mean() > 5     # the punch-in changed the picture
    assert np.abs(plain - broll).mean() > 40     # the b-roll replaced it
    # The punch-in is frame-exact: on at 2.0s, still on in the last frame before 4.0s, off at 4.0s.
    f = 1 / 30
    assert np.abs(frame(2.0 - f) - plain).mean() < 3 and np.abs(frame(4.0) - plain).mean() < 3
    assert np.abs(frame(2.0) - zoomed).mean() < 3 and np.abs(frame(4.0 - f) - zoomed).mean() < 3
    # A full render agrees with single frames (same zoom timing when not starting at the zoom).
    video = pipeline.render_edl(project, "frames", "export", start=1.5, end=4.5)["path"]
    from clipper.media import grab_frame

    def at(t):
        return grab_frame(video, t - 1.5 - 0.004, 1080, (1080, 1920)).astype(np.float32)  # just before that frame

    assert np.abs(at(2.0 - f) - plain).mean() < 6 and np.abs(at(2.0) - zoomed).mean() < 6
    assert np.abs(at(4.0 - f) - zoomed).mean() < 6 and np.abs(at(4.0) - plain).mean() < 6


def test_clip_mode_makes_several_edls(project):
    clips = pipeline.make_clips(project, count=2, min_len=5, max_len=14, captions="highlight")
    assert len(clips) == 2
    assert clips[0]["source_range"][1] <= clips[1]["source_range"][0] + 0.5
    assert set(project.list_edls()) >= {"clip-01", "clip-02"}
    result = pipeline.render_edl(project, "clip-01", "preview")
    assert result["duration"] == pytest.approx(clips[0]["duration"], abs=0.05)


def test_style_round_trip(project, tmp_path):
    """Render with a known style, analyse the render, and check the numbers come back."""
    edl = project.new_edl("ref", "polish", None, "9:16")
    words = project.words()
    ops.remove_fillers(edl, words)
    ops.remove_pauses(edl, project.silences(), words)
    ops.set_captions(edl, "highlight")
    ops.auto_zoom(edl, words, project.word_levels(), per_minute=8, scale=1.2)
    ops.set_music(edl, str(FIX / "music.wav"), gain_db=-18)
    ops.sfx_on_cuts(edl, which="zooms")
    project.save_edl(edl)
    ref = pipeline.render_edl(project, "ref", "export")["path"]

    spec = analyze_style(ref)
    cap = spec["captions"]
    assert cap["present"] and cap["animation"] == "highlight" and cap["font"]["uppercase"]
    assert cap["position"]["y"] == pytest.approx(0.72, abs=0.03)
    assert cap["size"] == pytest.approx(0.044, abs=0.008)
    assert 2 <= cap["words_per_group"] <= 4
    assert spec["zoom"]["punch_strength"] == pytest.approx(1.2, abs=0.04)
    assert spec["zoom"]["punch_ins_per_minute"] > 1
    assert spec["audio"]["music"]["present"] is True
    (tmp_path / "s.json").write_text(json.dumps(spec))

    target = project.new_edl("styled", "polish", None, "9:16")
    report = apply_style(target, spec, words, project.word_levels())
    assert target["captions"]["template"] == "highlight"
    assert target["zooms"] and target["zooms"][0]["scale"] == pytest.approx(1.2, abs=0.04)
    assert any("font family" in u for u in report["unmatched"])
    assert any("Music" in u for u in report["unmatched"])  # no track supplied -> reported, not invented


def test_plain_speech_has_no_music(project):
    assert analyze_style(project.source["path"])["audio"]["music"]["present"] is False
