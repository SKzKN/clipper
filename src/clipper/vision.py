"""Face detection and on-screen text recognition.

On macOS both use Apple's Vision framework (no model downloads). Elsewhere,
faces fall back to OpenCV's Haar cascade and text recognition is unavailable.
All boxes are fractions of the frame with the origin at the top left.
"""
from __future__ import annotations

import functools
import sys

import numpy as np


@functools.lru_cache(maxsize=1)
def _vision():
    if sys.platform != "darwin":
        return None
    try:
        import Foundation
        import Vision

        return Vision, Foundation
    except Exception:  # noqa: BLE001
        return None


def ocr_available() -> bool:
    return _vision() is not None


def face_backend() -> str:
    return "apple-vision" if _vision() else "opencv-haar"


def _handler(frame_bgr: np.ndarray):
    import cv2

    Vision, Foundation = _vision()
    ok, buf = cv2.imencode(".png", frame_bgr)
    raw = buf.tobytes()
    data = Foundation.NSData.dataWithBytes_length_(raw, len(raw))
    return Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)


def _box(rect) -> dict:
    x, y, w, h = rect.origin.x, rect.origin.y, rect.size.width, rect.size.height
    return {"x": float(x), "y": float(1 - y - h), "w": float(w), "h": float(h)}


def detect_faces(frame_bgr: np.ndarray) -> list[dict]:
    if _vision():
        Vision, _ = _vision()
        req = Vision.VNDetectFaceRectanglesRequest.alloc().init()
        ok, _err = _handler(frame_bgr).performRequests_error_([req], None)
        if not ok:
            return []
        return [{**_box(o.boundingBox()), "confidence": float(o.confidence())} for o in (req.results() or [])]
    import cv2

    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    found = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(int(h * 0.08), int(h * 0.08)))
    return [{"x": x / w, "y": y / h, "w": fw / w, "h": fh / h, "confidence": 0.5} for x, y, fw, fh in found]


def recognize_text(frame_bgr: np.ndarray, with_words: bool = True) -> list[dict]:
    """Text lines in the frame: text, confidence, box and (optionally) per-word boxes."""
    if not _vision():
        return []
    Vision, Foundation = _vision()
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(False)
    ok, _err = _handler(frame_bgr).performRequests_error_([req], None)
    if not ok:
        return []
    out = []
    for obs in req.results() or []:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        cand = cands[0]
        text = str(cand.string())
        line = {"text": text, "confidence": float(cand.confidence()), **_box(obs.boundingBox()), "words": []}
        if with_words:
            pos = 0
            for word in text.split():
                idx = text.find(word, pos)
                pos = idx + len(word)
                try:
                    rect_obs, _e = cand.boundingBoxForRange_error_(Foundation.NSMakeRange(idx, len(word)), None)
                except Exception:  # noqa: BLE001
                    rect_obs = None
                if rect_obs is not None:
                    line["words"].append({"text": word, **_box(rect_obs.boundingBox())})
        out.append(line)
    return out
