"""Core data types for the adaptive reframing subsystem.

This module deliberately depends only on the standard library so it can be
imported in any environment (including lightweight test runners) without
pulling in OpenCV, NumPy, MediaPipe or Pydantic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ReframeMode(str, Enum):
    """The eight supported reframing strategies.

    The first four are *focus* strategies (they crop/zoom toward a subject).
    The last four are *preservation* strategies (they keep the full original
    composition and synthesise the missing area). The classifier is biased so
    that, when signals are ambiguous, a preservation mode is chosen.
    """

    FACE_FOCUS = "face_focus"
    ACTIVE_SPEAKER = "active_speaker"
    SMART_CROP = "smart_crop"
    SCENE_AWARE = "scene_aware"
    BLUR_BACKGROUND = "blur_background"
    MIRROR_BACKGROUND = "mirror_background"
    DYNAMIC_CANVAS = "dynamic_canvas"
    NO_CROP = "no_crop"
    # Hybrid: subject-tracked crop to an intermediate aspect (subject enlarged)
    # over a blurred fill of the remaining top/bottom margins. Opt-in / forced.
    CROP_BLUR = "crop_blur"

    @property
    def is_focus(self) -> bool:
        return self in {
            ReframeMode.FACE_FOCUS,
            ReframeMode.ACTIVE_SPEAKER,
            ReframeMode.SMART_CROP,
        }

    @property
    def is_preserving(self) -> bool:
        return self in {
            ReframeMode.BLUR_BACKGROUND,
            ReframeMode.MIRROR_BACKGROUND,
            ReframeMode.DYNAMIC_CANVAS,
            ReframeMode.NO_CROP,
        }


@dataclass(frozen=True)
class BBox:
    """Axis-aligned bounding box in *pixel* coordinates."""

    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def iou(self, other: "BBox") -> float:
        ax2, ay2 = self.x + self.w, self.y + self.h
        bx2, by2 = other.x + other.w, other.y + other.h
        ix1, iy1 = max(self.x, other.x), max(self.y, other.y)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
        inter = iw * ih
        union = self.area + other.area - inter
        return inter / union if union > 0 else 0.0


@dataclass
class FaceObservation:
    """A single detected face at a single sampled timestamp."""

    t: float
    box: BBox
    score: float
    track_id: int = -1
    lip_activity: float = 0.0  # 0..1 normalised mouth-region motion energy


@dataclass(frozen=True)
class SceneSpan:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def contains(self, t: float) -> bool:
        return self.start <= t < self.end


@dataclass
class ClipAnalysis:
    """The full analysis summary for one clip.

    Scalar fields drive the :class:`~adaptive_reframe.classifier.ReframeClassifier`.
    The raw ``faces`` track and ``scenes`` list are consumed by the focus and
    scene-aware strategies during rendering.
    """

    duration: float
    fps: float
    width: int
    height: int

    # --- summary signals (all roughly normalised to 0..1 unless noted) ---
    face_present_ratio: float = 0.0
    typical_face_count: float = 0.0
    dominant_face_area_ratio: float = 0.0
    dominant_face_motion: float = 0.0  # path length of main face centre / diag
    global_motion: float = 0.0  # median optical-flow magnitude / diag
    cut_rate_per_min: float = 0.0
    overlay_score: float = 0.0  # text / graphics overlay prevalence
    has_active_speaker: bool = False

    # --- raw material for strategies ---
    scenes: list[SceneSpan] = field(default_factory=list)
    faces: list[FaceObservation] = field(default_factory=list)
    notes: dict = field(default_factory=dict)

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height if self.height else 0.0

    def faces_in(self, start: float, end: float) -> list[FaceObservation]:
        return [f for f in self.faces if start <= f.t < end]


@dataclass
class ReframeParams:
    """Plain (Pydantic-free) parameter bundle handed to strategies/renderer.

    Built from the validated config so that the rendering layer never needs to
    import Pydantic.
    """

    out_w: int = 1080
    out_h: int = 1920

    # focus / crop trajectory
    max_zoom: float = 1.6
    target_face_frac: float = 0.34  # desired face height as fraction of crop
    min_cutoff: float = 0.6  # One-Euro filter responsiveness floor
    beta: float = 0.02  # One-Euro filter speed coefficient
    safe_margin: float = 0.06  # keep subject this far from crop edge (frac)

    # active speaker switching
    speaker_switch_margin: float = 0.12
    speaker_min_hold_s: float = 0.6

    # preservation modes
    blur_ksize: int = 75
    bg_zoom: float = 1.18  # how much the blurred bg overfills
    canvas_margin: float = 0.05  # inset of content on dynamic canvas
    letterbox_color: tuple[int, int, int] = (8, 8, 8)
    # crop_blur hybrid: width:height of the subject-tracked crop placed over the
    # blur fill. 0.75 (3:4) enlarges the subject while keeping small blur margins.
    combine_crop_aspect: float = 0.75

    # encoding
    crf: int = 18
    preset: str = "medium"
    audio_bitrate: str = "192k"


@dataclass
class ReframeDecision:
    """Result of classification: the chosen mode + why."""

    mode: ReframeMode
    confidence: float
    rationale: list[str] = field(default_factory=list)

    def explain(self) -> str:
        reasons = "; ".join(self.rationale) if self.rationale else "default"
        return f"{self.mode.value} (conf={self.confidence:.2f}): {reasons}"
