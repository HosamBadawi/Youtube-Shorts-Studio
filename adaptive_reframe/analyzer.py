"""Clip analyzer.

Performs a single sampled pass over the clip, runs the detectors, and produces
a :class:`~adaptive_reframe.types.ClipAnalysis` with both the scalar signals the
classifier needs and the raw face track the focus strategies consume.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from statistics import median

import cv2
import numpy as np

from .detectors import (
    FaceDetector,
    FaceTracker,
    MotionEstimator,
    SceneDetector,
    lip_activity,
    mouth_roi,
    overlay_score,
)
from .types import ClipAnalysis, FaceObservation

logger = logging.getLogger(__name__)


@dataclass
class AnalyzerSettings:
    sample_fps: float = 5.0
    face_min_confidence: float = 0.5
    mediapipe_model: int = 1
    use_pyscenedetect: bool = True
    scene_diff_threshold: float = 0.35


class ClipAnalyzer:
    """Analyse one clip file and return a :class:`ClipAnalysis`."""

    def __init__(self, settings: AnalyzerSettings | None = None) -> None:
        self.s = settings or AnalyzerSettings()
        self.face_detector = FaceDetector(
            model_selection=self.s.mediapipe_model,
            min_confidence=self.s.face_min_confidence,
        )
        self.scene_detector = SceneDetector(
            diff_threshold=self.s.scene_diff_threshold,
            use_pyscenedetect=self.s.use_pyscenedetect,
        )

    def analyze(self, video_path: str) -> ClipAnalysis:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise FileNotFoundError(f"Cannot open video: {video_path}")
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration = n_frames / fps if n_frames else 0.0
        diag = (w * w + h * h) ** 0.5

        stride = max(1, int(round(fps / self.s.sample_fps)))
        tracker = FaceTracker()
        motion = MotionEstimator()

        faces: list[FaceObservation] = []
        per_frame_face_counts: list[int] = []
        per_frame_overlay: list[float] = []
        motion_samples: list[float] = []
        largest_face_area_ratio: list[float] = []
        dominant_centers: list[tuple[float, float]] = []

        idx = 0
        sampled = 0
        while True:
            ok = cap.grab()
            if not ok:
                break
            if idx % stride != 0:
                idx += 1
                continue
            ok, frame = cap.retrieve()
            if not ok:
                break
            t = idx / fps
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            dets = self.face_detector.detect(frame)
            tracked = tracker.update(dets, t, diag)
            per_frame_face_counts.append(len(tracked))

            frame_obs: list[FaceObservation] = []
            for tid, box, score in tracked:
                roi = mouth_roi(gray, box)
                tr = tracker.track(tid)
                la = lip_activity(tr.prev_mouth if tr else None, roi)
                if tr is not None:
                    tr.prev_mouth = roi
                obs = FaceObservation(t=t, box=box, score=score, track_id=tid,
                                      lip_activity=la)
                frame_obs.append(obs)
                faces.append(obs)

            if frame_obs:
                biggest = max(frame_obs, key=lambda o: o.box.area)
                largest_face_area_ratio.append(biggest.box.area / (w * h))
                dominant_centers.append((biggest.box.cx, biggest.box.cy))

            m = motion.update(gray, diag)
            if m is not None:
                motion_samples.append(m)
            per_frame_overlay.append(overlay_score(frame))

            sampled += 1
            idx += 1

        cap.release()
        self.face_detector.close()

        scenes = self.scene_detector.detect(video_path, duration, fps)
        analysis = self._summarize(
            duration, fps, w, h, sampled, faces, per_frame_face_counts,
            per_frame_overlay, motion_samples, largest_face_area_ratio,
            dominant_centers, scenes, diag,
        )
        logger.info(
            "Analysis: faces=%.2f cuts=%.1f/min motion=%.2f overlay=%.2f "
            "speaker=%s scenes=%d",
            analysis.face_present_ratio, analysis.cut_rate_per_min,
            analysis.global_motion, analysis.overlay_score,
            analysis.has_active_speaker, len(analysis.scenes),
        )
        return analysis

    def _summarize(
        self, duration, fps, w, h, sampled, faces, face_counts, overlays,
        motion_samples, area_ratios, centers, scenes, diag,
    ) -> ClipAnalysis:
        sampled = max(1, sampled)
        frames_with_face = sum(1 for c in face_counts if c > 0)
        face_present_ratio = frames_with_face / sampled
        typical_face_count = median(face_counts) if face_counts else 0.0
        dom_area = median(area_ratios) if area_ratios else 0.0

        # Dominant-face motion: total path length of the main-face centre,
        # normalised by the diagonal and time (so slow drift != fast tracking).
        dom_motion = 0.0
        if len(centers) >= 2:
            path = sum(
                ((centers[i][0] - centers[i - 1][0]) ** 2
                 + (centers[i][1] - centers[i - 1][1]) ** 2) ** 0.5
                for i in range(1, len(centers))
            )
            dom_motion = path / diag / max(1, len(centers) - 1) * 30.0

        global_motion = median(motion_samples) if motion_samples else 0.0
        overlay = median(overlays) if overlays else 0.0
        cut_rate = (len(scenes) / duration * 60.0) if duration > 0 else 0.0

        has_speaker = self._detect_active_speaker(faces, typical_face_count)

        return ClipAnalysis(
            duration=duration, fps=fps, width=w, height=h,
            face_present_ratio=face_present_ratio,
            typical_face_count=float(typical_face_count),
            dominant_face_area_ratio=float(dom_area),
            dominant_face_motion=float(dom_motion),
            global_motion=float(global_motion),
            cut_rate_per_min=float(cut_rate),
            overlay_score=float(overlay),
            has_active_speaker=has_speaker,
            scenes=scenes,
            faces=faces,
            notes={"sampled_frames": sampled,
                   "face_backend": self.face_detector.backend},
        )

    @staticmethod
    def _detect_active_speaker(
        faces: list[FaceObservation], typical_face_count: float
    ) -> bool:
        """A clear speaker exists when one track shows sustained lip motion
        well above the others (only meaningful with multiple faces)."""

        if typical_face_count < 1.6:
            return False
        by_track: dict[int, list[float]] = {}
        for f in faces:
            by_track.setdefault(f.track_id, []).append(f.lip_activity)
        if len(by_track) < 2:
            return False
        means = sorted(
            (sum(v) / len(v) for v in by_track.values()), reverse=True
        )
        top, second = means[0], means[1]
        return top > 0.05 and top > second * 1.8
