"""Footage checks: clipper only takes single-speaker, single-camera video up to 1080p.

`check_footage` returns {"ok", "reasons", "warnings", "details"}. Each reason
is a sentence that can be shown to the user as-is.
"""
from __future__ import annotations

import os
import urllib.request
from pathlib import Path

import numpy as np

from .audio import find_silences
from .ffmpeg import ClipperError, log
from .media import fmt_time, grab_frame, iter_frames, probe, read_audio
from .vision import detect_faces, face_backend

SPEAKER_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
    "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
)

# Two voice clusters whose average embeddings are less similar than this count as two people.
VOICE_SIMILARITY_LIMIT = 0.45
VOICE_MIN_SHARE = 0.10


class FootageRejected(ClipperError):
    def __init__(self, result: dict):
        self.result = result
        super().__init__("This footage can't be used:\n- " + "\n- ".join(result["reasons"]))


# --------------------------------------------------------------------------
# Cameras
# --------------------------------------------------------------------------

def find_shot_changes(path: str, info: dict, fps: float | None = None, start: float | None = None,
                      duration: float | None = None) -> list[dict]:
    """Hard cuts, each classified as 'same_view' (jump cut or punch-in on the
    same camera set-up) or 'new_view' (a different shot)."""
    import cv2

    if fps is None:
        fps = 4.0 if info["duration"] <= 1200 else 2.0
    size = (info["width"], info["height"])
    prev = prev2 = None
    diffs: list[float] = []
    frames_small: list[np.ndarray] = []
    for frame in iter_frames(path, fps, 96, size, gray=True, start=start, duration=duration):
        f = frame.astype(np.float32)
        frames_small.append(f)
        diffs.append(float(np.abs(f - prev).mean()) if prev is not None else 0.0)
        prev2, prev = prev, f
    if len(diffs) < 4:
        return []
    d = np.array(diffs)
    floor = float(np.median(d))
    cuts = []
    for i in range(1, len(d)):
        if d[i] < max(10.0, 5 * floor + 4):
            continue
        before = frames_small[max(0, i - 2)]
        after = frames_small[min(len(d) - 1, i + 1)]
        settled = float(np.abs(frames_small[min(len(d) - 1, i + 1)] - frames_small[i]).mean())
        if settled > 0.6 * d[i]:
            continue  # still changing fast: motion, not a cut
        view_change = float(np.abs(after - before).mean())
        if cuts and i - cuts[-1]["index"] <= 1:
            continue
        cuts.append({"index": i, "time": round((start or 0) + i / fps, 2), "strength": round(float(d[i]), 1),
                     "view_change": round(view_change, 1)})
    for cut in cuts:
        t = cut["time"]
        a = grab_frame(path, max(0.0, t - 1.5 / fps), 480, size)
        b = grab_frame(path, t + 1.0 / fps, 480, size)
        cut["kind"], cut["scale"] = _relate_views(a, b)
    return cuts


def _relate_views(a: np.ndarray | None, b: np.ndarray | None, overlays: bool = False) -> tuple[str, float | None]:
    """Is frame b the same camera set-up as frame a (possibly zoomed or shifted)?

    With `overlays=True` (finished videos), captions and graphics that stay put
    while the footage underneath is punched in are allowed for: if the best fit
    is "nothing moved", the points that disagree get a second fit of their own."""
    import cv2

    if a is None or b is None:
        return "new_view", None
    ga, gb = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
    orb = cv2.ORB_create(1500)
    ka, da = orb.detectAndCompute(ga, None)
    kb, db_ = orb.detectAndCompute(gb, None)
    if da is None or db_ is None or len(ka) < 12 or len(kb) < 12:
        # Featureless frames: fall back to plain picture similarity.
        diff = float(np.abs(ga.astype(np.float32) - gb.astype(np.float32)).mean())
        return ("same_view", 1.0) if diff < 12 else ("new_view", None)
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db_)
    if len(matches) < 12:
        return "new_view", None
    pa = np.float32([ka[m.queryIdx].pt for m in matches])
    pb = np.float32([kb[m.trainIdx].pt for m in matches])
    model, inliers = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=4.0)
    if model is None or inliers is None:
        return "new_view", None
    n_in = int(inliers.sum())
    scale = float(np.hypot(model[0, 0], model[1, 0]))
    if n_in < 20 or not 0.5 < scale < 2.0:
        return "new_view", None
    if overlays and abs(scale - 1) < 0.02:
        rest = inliers.ravel() == 0
        if rest.sum() >= 12:
            m2, in2 = cv2.estimateAffinePartial2D(pa[rest], pb[rest], method=cv2.RANSAC, ransacReprojThreshold=4.0)
            if m2 is not None and in2 is not None:
                s2 = float(np.hypot(m2[0, 0], m2[1, 0]))
                if int(in2.sum()) >= max(12, 0.3 * rest.sum()) and 0.5 < s2 < 2.0 and abs(s2 - 1) >= 0.04:
                    model, scale = m2, s2
    # Matching points are not enough (a logo or the same person can match across
    # cameras): after lining the frames up, the whole picture has to agree.
    h, w = gb.shape
    warped = cv2.warpAffine(ga, model, (w, h), borderMode=cv2.BORDER_REPLICATE)
    blur_a, blur_b = cv2.GaussianBlur(warped, (9, 9), 0), cv2.GaussianBlur(gb, (9, 9), 0)
    residual = float(np.abs(blur_a.astype(np.float32) - blur_b.astype(np.float32)).mean())
    if residual <= 16.0:
        return "same_view", round(scale, 3)
    return "new_view", None


# --------------------------------------------------------------------------
# People on screen
# --------------------------------------------------------------------------

def sample_faces(path: str, info: dict, samples: int = 24) -> dict:
    size = (info["width"], info["height"])
    dur = info["duration"]
    times = np.linspace(dur * 0.04, dur * 0.96, samples) if dur > 2 else [dur / 2]
    with_face = multi = 0
    centers, heights = [], []
    for t in times:
        frame = grab_frame(path, float(t), 640, size)
        if frame is None:
            continue
        faces = [f for f in detect_faces(frame) if f["h"] >= 0.08]
        if not faces:
            continue
        with_face += 1
        faces.sort(key=lambda f: -f["h"])
        main = faces[0]
        centers.append((main["x"] + main["w"] / 2, main["y"] + main["h"] / 2))
        heights.append(main["h"])
        if sum(1 for f in faces if f["h"] >= 0.45 * main["h"]) >= 2:
            multi += 1
    out = {"frames_sampled": len(times), "frames_with_face": with_face, "frames_with_two_faces": multi,
           "backend": face_backend()}
    if centers:
        c = np.median(np.array(centers), axis=0)
        out["face_center"] = [round(float(c[0]), 4), round(float(c[1]), 4)]
        out["face_height"] = round(float(np.median(heights)), 4)
    return out


# --------------------------------------------------------------------------
# Voices
# --------------------------------------------------------------------------

def speaker_model_path() -> Path | None:
    """Local path of the speaker-embedding model, downloading it on first use."""
    env = os.environ.get("CLIPPER_SPEAKER_MODEL")
    if env:
        return Path(env) if Path(env).exists() else None
    cache = Path(os.environ.get("CLIPPER_CACHE", Path.home() / ".cache" / "clipper")) / "models"
    target = cache / "speaker-campplus-en-16k.onnx"
    if target.exists() and target.stat().st_size > 1_000_000:
        return target
    if os.environ.get("CLIPPER_OFFLINE"):
        return None
    try:
        import ssl

        import certifi

        cache.mkdir(parents=True, exist_ok=True)
        log("downloading the speaker model for the voice check (about 28 MB, one time)")
        ctx = ssl.create_default_context(cafile=certifi.where())
        tmp = target.with_suffix(".part")
        with urllib.request.urlopen(SPEAKER_MODEL_URL, context=ctx, timeout=60) as resp, open(tmp, "wb") as f:
            while chunk := resp.read(1 << 20):
                f.write(chunk)
        tmp.replace(target)
        return target
    except Exception as exc:  # noqa: BLE001
        log(f"could not download the speaker model: {exc}")
        return None


def _speech_windows(mono: np.ndarray, sr: int, win: float = 3.0, hop: float = 1.5, limit: int = 120) -> list[tuple[float, float]]:
    silences = find_silences(mono, sr, min_len=0.35)
    total = len(mono) / sr
    speech, t = [], 0.0
    for a, b in silences + [(total, total)]:
        if a - t >= 1.2:
            speech.append((t, a))
        t = b
    windows = []
    for a, b in speech:
        if b - a <= win:
            windows.append((a, b))
            continue
        s = a
        while s + win <= b + 1e-6:
            windows.append((s, s + win))
            s += hop
    if len(windows) > limit:
        idx = np.linspace(0, len(windows) - 1, limit).round().astype(int)
        windows = [windows[i] for i in idx]
    return windows


def _two_clusters(emb: np.ndarray) -> np.ndarray:
    """Average-linkage agglomerative clustering down to two groups (cosine)."""
    n = len(emb)
    sim = emb @ emb.T
    clusters = [[i] for i in range(n)]
    while len(clusters) > 2:
        best, pair = -2.0, (0, 1)
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                s = sim[np.ix_(clusters[i], clusters[j])].mean()
                if s > best:
                    best, pair = s, (i, j)
        i, j = pair
        clusters[i] += clusters.pop(j)
    labels = np.zeros(n, dtype=int)
    labels[clusters[1]] = 1
    return labels


def count_voices(path: str) -> dict:
    """Estimate whether the speech comes from one voice or more."""
    try:
        import sherpa_onnx
    except ImportError:
        return {"checked": False, "why": "sherpa-onnx is not installed (pip install 'clipper[voices]')"}
    model = speaker_model_path()
    if model is None:
        return {"checked": False, "why": "the speaker model is not available (offline?)"}
    sr = 16000
    mono = read_audio(path, sr, 1)
    windows = _speech_windows(mono, sr)
    if len(windows) < 6:
        return {"checked": False, "why": "not enough continuous speech to compare voices"}
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=2))
    embs = []
    for a, b in windows:
        stream = extractor.create_stream()
        stream.accept_waveform(sr, mono[int(a * sr):int(b * sr)])
        stream.input_finished()
        e = np.array(extractor.compute(stream), dtype=np.float32)
        embs.append(e / (np.linalg.norm(e) + 1e-9))
    emb = np.stack(embs)
    labels = _two_clusters(emb)
    c0, c1 = emb[labels == 0].mean(axis=0), emb[labels == 1].mean(axis=0)
    similarity = float(c0 @ c1 / (np.linalg.norm(c0) * np.linalg.norm(c1) + 1e-9))
    minor = int(labels.sum() if labels.sum() <= len(labels) / 2 else len(labels) - labels.sum())
    share = minor / len(labels)
    minor_label = 1 if labels.sum() <= len(labels) / 2 else 0
    first = next((windows[i][0] for i in range(len(windows)) if labels[i] == minor_label), None)
    two = similarity < VOICE_SIMILARITY_LIMIT and share >= VOICE_MIN_SHARE and minor >= 2
    return {"checked": True, "voices": 2 if two else 1, "cluster_similarity": round(similarity, 3),
            "second_voice_share": round(share, 3) if two else 0.0,
            "second_voice_first_heard": round(first, 1) if two and first is not None else None,
            "windows": len(windows)}


# --------------------------------------------------------------------------
# All checks
# --------------------------------------------------------------------------

def check_footage(path: str | Path, voices: bool = True) -> dict:
    path = str(Path(path).expanduser())
    info = probe(path)
    reasons: list[str] = []
    warnings: list[str] = []
    details: dict = {"probe": info}

    if not info["video_streams"]:
        reasons.append("The file has no video stream.")
    if not info["audio_streams"]:
        reasons.append("The file has no audio track. clipper cuts on the speaker's voice, so it needs one.")
    if info["video_streams"] and info["duration"] < 3:
        reasons.append(f"The video is only {info['duration']:.1f}s long; there is nothing to edit.")
    w, h = info["width"], info["height"]
    if info["video_streams"] and (min(w, h) > 1080 or max(w, h) > 1920):
        reasons.append(f"The video is {w}x{h}. clipper handles up to 1080p (1920x1080 or 1080x1920); "
                       "export a 1080p copy and use that.")
    if reasons:
        return {"ok": False, "reasons": reasons, "warnings": warnings, "details": details}

    log("checking footage: camera cuts")
    cuts = find_shot_changes(path, info)
    new_views = [c for c in cuts if c["kind"] == "new_view"]
    details["hard_cuts"] = len(cuts)
    details["cuts_to_other_shot"] = [c["time"] for c in new_views][:50]
    if new_views:
        times = ", ".join(fmt_time(c["time"]) for c in new_views[:4])
        more = f" and {len(new_views) - 4} more" if len(new_views) > 4 else ""
        reasons.append(f"The picture cuts to a different shot at {times}{more}. That means more than one camera, "
                       "or cutaways already edited in. clipper needs one continuous camera angle.")
    elif cuts:
        warnings.append(f"Found {len(cuts)} jump cut(s) on the same camera angle; the video seems to be edited already.")

    log("checking footage: people on screen")
    faces = sample_faces(path, info)
    details["faces"] = faces
    if "face_center" in faces:
        details["face_center"] = faces["face_center"]
    if faces["frames_with_face"] == 0:
        warnings.append("No face was found, so framing will be centred. Check the crop in a preview.")
    elif faces["frames_with_two_faces"] >= 3 and faces["frames_with_two_faces"] > 0.35 * faces["frames_with_face"]:
        reasons.append(f"Two or more people are on screen in {faces['frames_with_two_faces']} of "
                       f"{faces['frames_with_face']} sampled frames. clipper works on single-speaker footage.")

    if voices:
        log("checking footage: voices")
        v = count_voices(path)
        details["voices"] = v
        if not v["checked"]:
            warnings.append(f"The voice check was skipped: {v['why']}.")
        elif v["voices"] > 1:
            reasons.append(f"There seems to be more than one speaker: about {v['second_voice_share'] * 100:.0f}% of the "
                           f"speech is in a second voice (first heard near {fmt_time(v['second_voice_first_heard'])}). "
                           "clipper works on single-speaker footage.")
    return {"ok": not reasons, "reasons": reasons, "warnings": warnings, "details": details}
