"""Adaptive reframing classifier.

Maps a :class:`~adaptive_reframe.types.ClipAnalysis` to a
:class:`~adaptive_reframe.types.ReframeDecision`. The single most important rule,
stated in the brief, is encoded explicitly here:

    *Preserving the original editing and visual information ALWAYS takes
    priority over forcing a face-centred crop.*

So the classifier first computes a "preservation pressure" from signals that
indicate the source is already edited / busy (cuts, camera motion, overlays,
multiple subjects). If that pressure clears a threshold, a preservation mode is
chosen and focus modes are never considered. Only for genuinely static,
single-subject footage do we crop toward a face.

Pure module: depends only on the standard library + :mod:`.types`, so it is
trivially unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import ClipAnalysis, ReframeDecision, ReframeMode


@dataclass
class ClassifierThresholds:
    """All tunable decision thresholds (mirrored in ``config.yaml``)."""

    # A source whose aspect is already close to vertical is never cropped.
    vertical_aspect_max: float = 0.62  # ~ <= 10:16

    # Preservation-pressure component thresholds.
    high_cut_rate_per_min: float = 14.0
    high_global_motion: float = 0.16
    high_overlay_score: float = 0.42
    multi_subject_count: float = 1.6

    # Pressure above this => force a preservation mode.
    preserve_pressure_threshold: float = 1.0

    # Focus-mode requirements (only consulted when pressure is low).
    min_face_present_ratio: float = 0.55
    max_face_motion_for_static: float = 0.18
    min_face_area_for_focus: float = 0.012

    # Within preservation, prefer No-Crop (legible) when overlays/text dominate.
    overlay_force_nocrop: float = 0.55

    # Scene-aware is chosen when there are several scenes but each is calm.
    scene_aware_min_scenes: int = 3
    scene_aware_max_motion: float = 0.16


def _preservation_pressure(
    a: ClipAnalysis, th: ClassifierThresholds
) -> tuple[float, list[str]]:
    """Accumulate evidence that the clip is already edited / visually busy.

    Returns ``(pressure, reasons)`` where each contributing signal adds roughly
    1.0 (scaled by how far past its threshold it is, capped) so a single very
    strong signal, or two moderate ones, can force preservation.
    """

    pressure = 0.0
    reasons: list[str] = []

    if a.cut_rate_per_min >= th.high_cut_rate_per_min:
        c = min(2.0, a.cut_rate_per_min / th.high_cut_rate_per_min)
        pressure += c
        reasons.append(f"high cut rate ({a.cut_rate_per_min:.1f}/min)")

    if a.global_motion >= th.high_global_motion:
        c = min(2.0, a.global_motion / th.high_global_motion)
        pressure += c
        reasons.append(f"camera/scene motion ({a.global_motion:.2f})")

    if a.overlay_score >= th.high_overlay_score:
        c = min(2.0, a.overlay_score / th.high_overlay_score)
        pressure += c
        reasons.append(f"text/graphics overlays ({a.overlay_score:.2f})")

    if a.typical_face_count >= th.multi_subject_count and not a.has_active_speaker:
        pressure += 1.0
        reasons.append(
            f"multiple subjects with no clear speaker "
            f"({a.typical_face_count:.1f})"
        )

    return pressure, reasons


def _pick_preservation_mode(
    a: ClipAnalysis, th: ClassifierThresholds, reasons: list[str]
) -> ReframeMode:
    """Choose among the four preservation modes."""

    # Heavy on-screen text/graphics => keep everything readable: letterbox.
    if a.overlay_score >= th.overlay_force_nocrop:
        reasons.append("overlays heavy -> keep full frame legible")
        return ReframeMode.NO_CROP

    # Several calm scenes => let each scene be reframed independently.
    if (
        len(a.scenes) >= th.scene_aware_min_scenes
        and a.global_motion <= th.scene_aware_max_motion
    ):
        reasons.append("multiple calm scenes -> per-scene reframing")
        return ReframeMode.SCENE_AWARE

    # A present, reasonably static subject reads well on a blurred bg.
    if a.face_present_ratio >= 0.4:
        reasons.append("subject present -> blurred background expansion")
        return ReframeMode.BLUR_BACKGROUND

    # Wide establishing / scenic shots: mirror extension feels less muddy than
    # blur when there is no central subject; dynamic canvas for colourful B-roll.
    if a.dominant_face_area_ratio < 0.005 and a.global_motion < 0.25:
        reasons.append("scenic/no subject -> dynamic canvas")
        return ReframeMode.DYNAMIC_CANVAS

    reasons.append("busy footage -> mirror background extension")
    return ReframeMode.MIRROR_BACKGROUND


def _pick_focus_mode(
    a: ClipAnalysis, th: ClassifierThresholds, reasons: list[str]
) -> ReframeMode:
    """Choose among the focus modes for static, single-subject footage."""

    face_ok = (
        a.face_present_ratio >= th.min_face_present_ratio
        and a.dominant_face_area_ratio >= th.min_face_area_for_focus
        and a.dominant_face_motion <= th.max_face_motion_for_static
    )

    if not face_ok:
        reasons.append("no stable face -> saliency-based smart crop")
        return ReframeMode.SMART_CROP

    if a.has_active_speaker and a.typical_face_count >= th.multi_subject_count:
        reasons.append("multiple faces + clear talker -> active speaker")
        return ReframeMode.ACTIVE_SPEAKER

    reasons.append("single stable face -> face focus")
    return ReframeMode.FACE_FOCUS


class ReframeClassifier:
    """Stateless classifier (pure function wrapped for DI / testability)."""

    def __init__(self, thresholds: ClassifierThresholds | None = None) -> None:
        self.th = thresholds or ClassifierThresholds()

    def classify(self, a: ClipAnalysis) -> ReframeDecision:
        th = self.th
        reasons: list[str] = []

        # Rule 0: already (near) vertical -> never crop, just fit.
        if 0 < a.aspect_ratio <= th.vertical_aspect_max:
            reasons.append(f"source already vertical (ar={a.aspect_ratio:.2f})")
            return ReframeDecision(ReframeMode.NO_CROP, 0.95, reasons)

        # Rule 1: preservation pressure dominates everything.
        pressure, p_reasons = _preservation_pressure(a, th)
        if pressure >= th.preserve_pressure_threshold:
            reasons.extend(p_reasons)
            mode = _pick_preservation_mode(a, th, reasons)
            conf = min(0.97, 0.55 + 0.2 * pressure)
            return ReframeDecision(mode, conf, reasons)

        # Rule 2: low pressure -> safe to focus on a subject.
        mode = _pick_focus_mode(a, th, reasons)
        # Confidence drops as we approach the preservation threshold.
        conf = max(0.5, 0.9 - 0.3 * (pressure / th.preserve_pressure_threshold))
        if mode == ReframeMode.SMART_CROP:
            conf = min(conf, 0.7)
        return ReframeDecision(mode, conf, reasons)
