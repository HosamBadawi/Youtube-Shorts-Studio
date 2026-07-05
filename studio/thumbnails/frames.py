"""Candidate frame selection: find the best face moments in a clip.

Reuses the face detector from :mod:`adaptive_reframe` (MediaPipe BlazeFace
with an OpenCV Haar fallback — untouched engine, imported only). Each sampled
frame is scored by face size x sharpness x brightness, with a small bonus for
an upper-center face — the composition thumbnails want.
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

_MAX_SAMPLES = 240


def extract_frame(video_path: str, t: float):
    """One BGR frame at ``t`` seconds, or None."""
    try:
        import cv2  # type: ignore
    except Exception:
        return None
    cap = cv2.VideoCapture(video_path)
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000.0)
        ok, frame = cap.read()
        return frame if ok else None
    finally:
        cap.release()


def select_candidates(video_path: str, t0: float, t1: float, k: int = 5,
                      sample_fps: float = 2.0) -> list[tuple[float, object, float]]:
    """Return up to ``k`` ``(t, frame_bgr, score)`` candidates from
    ``video_path`` within ``[t0, t1]``, best score first, temporally spread."""
    try:
        import cv2  # type: ignore
    except Exception:
        logger.warning("OpenCV missing — no thumbnail candidates")
        return []
    try:
        from adaptive_reframe.detectors import FaceDetector
        detector = FaceDetector()
    except Exception:
        detector = None

    span = max(0.5, t1 - t0)
    stride = max(1.0 / sample_fps, span / _MAX_SAMPLES)
    scored: list[tuple[float, object, float]] = []

    cap = cv2.VideoCapture(video_path)
    try:
        t = t0
        while t < t1:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if ok and frame is not None:
                scored.append((t, frame, _score(cv2, detector, frame)))
            t += stride
    finally:
        cap.release()
        if detector is not None:
            try:
                detector.close()
            except Exception:
                pass

    if not scored:
        return []
    if all(s[2] <= 0.002 for s in scored):
        logger.info("no faces found in clip — falling back to sharpest frames")

    # best-first, but enforce temporal spacing so the 5 picks aren't twins
    min_spacing = span / (k * 2)
    picked: list[tuple[float, object, float]] = []
    for cand in sorted(scored, key=lambda x: -x[2]):
        if all(abs(cand[0] - p[0]) >= min_spacing for p in picked):
            picked.append(cand)
        if len(picked) >= k:
            break
    return picked


def _score(cv2, detector, frame) -> float:
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    face_box = None
    conf = 0.0
    if detector is not None:
        try:
            faces = detector.detect(frame)
            if faces:
                face_box, conf = max(faces, key=lambda f: f[0].area)
        except Exception:
            face_box = None

    if face_box is None:
        # faceless fallback: rank purely by (dampened) whole-frame sharpness
        var = cv2.Laplacian(gray, cv2.CV_64F).var()
        return 0.001 * math.tanh(var / 300.0)

    x, y = int(max(0, face_box.x)), int(max(0, face_box.y))
    fw, fh = int(face_box.w), int(face_box.h)
    crop = gray[y:y + fh, x:x + fw]
    if crop.size == 0:
        return 0.0

    area = (fw * fh) / float(w * h)
    sharp = math.tanh(cv2.Laplacian(crop, cv2.CV_64F).var() / 300.0)
    mean = float(crop.mean())
    bright = 1.0 if 40.0 <= mean <= 215.0 else 0.4
    # upper-center bonus: faces near x-center, upper half compose best
    cx = (x + fw / 2) / w
    cy = (y + fh / 2) / h
    center = 1.0 + 0.15 * (1.0 - min(1.0, abs(cx - 0.5) * 2)) \
        + 0.1 * (1.0 - min(1.0, abs(cy - 0.38) * 2))
    return area * sharp * bright * center * max(0.5, conf)
