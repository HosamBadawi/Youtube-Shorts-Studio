"""Unit tests for the reframing classifier decision logic (no CV deps)."""

from __future__ import annotations

from adaptive_reframe.classifier import ReframeClassifier
from adaptive_reframe.types import ClipAnalysis, ReframeMode, SceneSpan


def _analysis(**kw) -> ClipAnalysis:
    base = dict(duration=30.0, fps=30.0, width=1920, height=1080)
    base.update(kw)
    return ClipAnalysis(**base)


def test_already_vertical_is_no_crop():
    a = _analysis(width=1080, height=1920)
    d = ReframeClassifier().classify(a)
    assert d.mode == ReframeMode.NO_CROP


def test_static_talking_head_is_face_focus():
    a = _analysis(
        face_present_ratio=0.95,
        typical_face_count=1.0,
        dominant_face_area_ratio=0.06,
        dominant_face_motion=0.05,
        global_motion=0.03,
        cut_rate_per_min=0.5,
        overlay_score=0.05,
    )
    d = ReframeClassifier().classify(a)
    assert d.mode == ReframeMode.FACE_FOCUS


def test_two_speakers_with_talker_is_active_speaker():
    a = _analysis(
        face_present_ratio=0.95,
        typical_face_count=2.0,
        dominant_face_area_ratio=0.05,
        dominant_face_motion=0.06,
        global_motion=0.03,
        cut_rate_per_min=0.5,
        overlay_score=0.05,
        has_active_speaker=True,
    )
    d = ReframeClassifier().classify(a)
    assert d.mode == ReframeMode.ACTIVE_SPEAKER


def test_heavily_edited_overlays_preserves_full_frame():
    a = _analysis(
        face_present_ratio=0.3,
        typical_face_count=1.0,
        global_motion=0.1,
        cut_rate_per_min=20.0,
        overlay_score=0.7,  # lots of on-screen text
    )
    d = ReframeClassifier().classify(a)
    assert d.mode.is_preserving
    assert d.mode == ReframeMode.NO_CROP  # legibility wins


def test_high_motion_action_preserves_composition():
    a = _analysis(
        face_present_ratio=0.2,
        typical_face_count=0.0,
        global_motion=0.4,  # fast camera / action
        cut_rate_per_min=8.0,
        overlay_score=0.1,
    )
    d = ReframeClassifier().classify(a)
    assert d.mode.is_preserving


def test_multiple_subjects_no_speaker_preserves():
    a = _analysis(
        face_present_ratio=0.9,
        typical_face_count=3.0,
        dominant_face_area_ratio=0.02,
        global_motion=0.05,
        cut_rate_per_min=2.0,
        overlay_score=0.1,
        has_active_speaker=False,
    )
    d = ReframeClassifier().classify(a)
    assert d.mode.is_preserving


def test_calm_multi_scene_is_scene_aware():
    scenes = [SceneSpan(i * 5.0, (i + 1) * 5.0) for i in range(4)]
    a = _analysis(
        face_present_ratio=0.3,
        typical_face_count=2.0,
        global_motion=0.05,
        cut_rate_per_min=20.0,  # many cuts -> preservation pressure
        overlay_score=0.2,
        scenes=scenes,
    )
    d = ReframeClassifier().classify(a)
    assert d.mode == ReframeMode.SCENE_AWARE


def test_no_stable_face_falls_back_to_smart_crop():
    a = _analysis(
        face_present_ratio=0.2,  # face rarely present
        typical_face_count=0.0,
        dominant_face_area_ratio=0.02,
        global_motion=0.05,
        cut_rate_per_min=1.0,
        overlay_score=0.1,
    )
    d = ReframeClassifier().classify(a)
    # Low pressure + unstable face -> smart crop (a focus mode).
    assert d.mode == ReframeMode.SMART_CROP


def test_decision_is_explainable():
    a = _analysis(width=1080, height=1920)
    d = ReframeClassifier().classify(a)
    assert d.rationale and isinstance(d.explain(), str)
