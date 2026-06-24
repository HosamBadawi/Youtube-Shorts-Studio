"""Computer-vision detectors used during clip analysis.

Each detector is small, single-responsibility, and degrades gracefully:

* :class:`FaceDetector` uses MediaPipe when available, otherwise an OpenCV Haar
  cascade that ships with the ``opencv`` wheel.
* :class:`SceneDetector` uses PySceneDetect when installed, otherwise a
  histogram-difference fallback.

All public methods take ``BGR`` frames (the OpenCV convention).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import cv2
import numpy as np

from .types import BBox, SceneSpan

logger = logging.getLogger(__name__)

try:  # MediaPipe is preferred but optional.
    import mediapipe as mp  # type: ignore

    _MP_AVAILABLE = True
except Exception:  # pragma: no cover - environment dependent
    _MP_AVAILABLE = False


class FaceDetector:
    """Detect faces in a single frame, returning pixel-space :class:`BBox`es."""

    def __init__(self, model_selection: int = 1, min_confidence: float = 0.5):
        self.min_confidence = min_confidence
        self._backend = "none"
        self._mp = None
        self._haar = None
        if _MP_AVAILABLE:
            try:
                self._mp = mp.solutions.face_detection.FaceDetection(
                    model_selection=model_selection,
                    min_detection_confidence=min_confidence,
                )
                self._backend = "mediapipe"
            except Exception as exc:  # pragma: no cover
                logger.warning("MediaPipe init failed (%s); using Haar", exc)
        if self._mp is None:
            path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            self._haar = cv2.CascadeClassifier(path)
            self._backend = "haar"
        logger.info("FaceDetector backend: %s", self._backend)

    @property
    def backend(self) -> str:
        return self._backend

    def detect(self, frame: np.ndarray) -> list[tuple[BBox, float]]:
        h, w = frame.shape[:2]
        if self._mp is not None:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            res = self._mp.process(rgb)
            out: list[tuple[BBox, float]] = []
            if res.detections:
                for d in res.detections:
                    rb = d.location_data.relative_bounding_box
                    bx = max(0.0, rb.xmin * w)
                    by = max(0.0, rb.ymin * h)
                    bw = min(w - bx, rb.width * w)
                    bh = min(h - by, rb.height * h)
                    if bw > 4 and bh > 4:
                        score = float(d.score[0]) if d.score else 1.0
                        out.append((BBox(bx, by, bw, bh), score))
            return out
        # Haar fallback.
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        rects = self._haar.detectMultiScale(gray, 1.1, 5, minSize=(32, 32))
        return [(BBox(float(x), float(y), float(rw), float(rh)), 1.0)
                for (x, y, rw, rh) in rects]

    def close(self) -> None:
        if self._mp is not None:
            self._mp.close()


@dataclass
class _Track:
    track_id: int
    box: BBox
    last_t: float
    prev_mouth: np.ndarray | None = None


class FaceTracker:
    """Greedy nearest-centre tracker assigning stable ids across samples."""

    def __init__(self, max_center_dist_frac: float = 0.12):
        self.max_dist_frac = max_center_dist_frac
        self._tracks: dict[int, _Track] = {}
        self._next_id = 0

    def update(
        self, dets: list[tuple[BBox, float]], t: float, frame_diag: float
    ) -> list[tuple[int, BBox, float]]:
        max_dist = self.max_dist_frac * frame_diag
        assigned: list[tuple[int, BBox, float]] = []
        used = set()
        # Match existing tracks to detections by nearest centre.
        for tid, tr in list(self._tracks.items()):
            best, best_d = None, max_dist
            for i, (box, _score) in enumerate(dets):
                if i in used:
                    continue
                d = ((box.cx - tr.box.cx) ** 2 + (box.cy - tr.box.cy) ** 2) ** 0.5
                if d < best_d:
                    best, best_d = i, d
            if best is not None:
                box, score = dets[best]
                used.add(best)
                tr.box, tr.last_t = box, t
                assigned.append((tid, box, score))
            elif t - tr.last_t > 1.0:  # drop stale tracks
                del self._tracks[tid]
        # New tracks for unmatched detections.
        for i, (box, score) in enumerate(dets):
            if i in used:
                continue
            tid = self._next_id
            self._next_id += 1
            self._tracks[tid] = _Track(tid, box, t)
            assigned.append((tid, box, score))
        return assigned

    def track(self, tid: int) -> _Track | None:
        return self._tracks.get(tid)


def mouth_roi(frame_gray: np.ndarray, box: BBox) -> np.ndarray | None:
    """Extract a normalised lower-central mouth region from a face box."""

    h, w = frame_gray.shape[:2]
    mx = int(box.x + box.w * 0.25)
    my = int(box.y + box.h * 0.62)
    mw = int(box.w * 0.5)
    mh = int(box.h * 0.32)
    mx, my = max(0, mx), max(0, my)
    mw, mh = max(1, min(mw, w - mx)), max(1, min(mh, h - my))
    roi = frame_gray[my : my + mh, mx : mx + mw]
    if roi.size == 0:
        return None
    return cv2.resize(roi, (32, 16), interpolation=cv2.INTER_AREA).astype(
        np.float32
    )


def lip_activity(prev: np.ndarray | None, cur: np.ndarray | None) -> float:
    """Normalised mean absolute difference between two mouth ROIs (0..1)."""

    if prev is None or cur is None or prev.shape != cur.shape:
        return 0.0
    diff = float(np.mean(np.abs(cur - prev))) / 255.0
    return float(min(1.0, diff * 6.0))  # scale; small motions matter


class MotionEstimator:
    """Median dense optical-flow magnitude between consecutive samples."""

    def __init__(self) -> None:
        self._prev: np.ndarray | None = None

    def update(self, frame_gray: np.ndarray, diag: float) -> float | None:
        small = cv2.resize(frame_gray, (160, 90), interpolation=cv2.INTER_AREA)
        if self._prev is None:
            self._prev = small
            return None
        flow = cv2.calcOpticalFlowFarneback(
            self._prev, small, None, 0.5, 2, 15, 3, 5, 1.2, 0
        )
        self._prev = small
        mag = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
        # Normalise by the small-frame diagonal so it is resolution-independent.
        small_diag = (small.shape[0] ** 2 + small.shape[1] ** 2) ** 0.5
        return float(np.median(mag) / small_diag * 100.0)


def overlay_score(frame: np.ndarray) -> float:
    """Heuristic prevalence of text/graphics overlays in a frame (0..1).

    Looks for dense horizontal edge structure concentrated in the bands where
    captions / lower-thirds / titles usually live (top 18% and bottom 28%).
    """

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    edges = cv2.Canny(gray, 80, 180)
    top = edges[: int(h * 0.18)]
    bottom = edges[int(h * 0.72) :]
    band_density = (top.mean() + bottom.mean()) / 2.0 / 255.0
    whole_density = edges.mean() / 255.0
    # Localised banding (band >> whole) is the strong signal for overlays.
    ratio = band_density / (whole_density + 1e-6)
    score = min(1.0, band_density * 4.0) * min(1.0, ratio / 2.0)
    return float(score)


class SceneDetector:
    """Detect scene-cut boundaries; PySceneDetect with a frame-diff fallback."""

    def __init__(self, diff_threshold: float = 0.35, use_pyscenedetect: bool = True):
        self.diff_threshold = diff_threshold
        self.use_psd = use_pyscenedetect

    def detect(self, video_path: str, duration: float, fps: float) -> list[SceneSpan]:
        if self.use_psd:
            spans = self._try_pyscenedetect(video_path)
            if spans is not None:
                return spans
        return self._fallback(video_path, duration, fps)

    def _try_pyscenedetect(self, video_path: str) -> list[SceneSpan] | None:
        try:
            from scenedetect import detect, ContentDetector  # type: ignore
        except Exception:
            return None
        try:
            scenes = detect(video_path, ContentDetector())
            return [
                SceneSpan(s.get_seconds(), e.get_seconds()) for s, e in scenes
            ] or None
        except Exception as exc:  # pragma: no cover
            logger.warning("PySceneDetect failed (%s); falling back", exc)
            return None

    def _fallback(
        self, video_path: str, duration: float, fps: float
    ) -> list[SceneSpan]:
        cap = cv2.VideoCapture(video_path)
        prev_hist = None
        cuts = [0.0]
        idx = 0
        stride = max(1, int(round(fps / 6.0)))  # ~6 samples/sec
        while True:
            ok = cap.grab()
            if not ok:
                break
            if idx % stride == 0:
                ok, frame = cap.retrieve()
                if not ok:
                    break
                small = cv2.resize(frame, (64, 36))
                hist = cv2.calcHist([small], [0, 1, 2], None, [8, 8, 8],
                                    [0, 256, 0, 256, 0, 256])
                cv2.normalize(hist, hist)
                if prev_hist is not None:
                    d = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA)
                    if d > self.diff_threshold:
                        cuts.append(idx / fps)
                prev_hist = hist
            idx += 1
        cap.release()
        cuts.append(duration)
        spans = [SceneSpan(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)]
        return [s for s in spans if s.duration > 0.3] or [SceneSpan(0.0, duration)]
