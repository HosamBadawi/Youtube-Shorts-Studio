"""Configuration models (Pydantic) and ``config.yaml`` loading.

Only the CLI imports this module, so the rest of the package (pipeline,
strategies, renderer) never depends on Pydantic and can run in minimal
environments. :meth:`ReframeConfig.to_params` / :meth:`to_thresholds` translate
the validated config into the plain dataclasses used downstream.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .classifier import ClassifierThresholds
from .types import ReframeParams


class OutputConfig(BaseModel):
    width: int = 1080
    height: int = 1920
    crf: int = Field(18, ge=0, le=51)
    preset: str = "medium"
    audio_bitrate: str = "192k"


class AnalysisConfig(BaseModel):
    sample_fps: float = Field(5.0, gt=0)
    face_min_confidence: float = Field(0.5, ge=0, le=1)
    mediapipe_model: int = Field(1, ge=0, le=1)  # 0 short-range, 1 full-range
    use_pyscenedetect: bool = True
    scene_diff_threshold: float = Field(0.35, gt=0, le=1)


class FocusConfig(BaseModel):
    max_zoom: float = Field(1.6, ge=1.0)
    target_face_frac: float = Field(0.34, gt=0, lt=1)
    min_cutoff: float = 0.6
    beta: float = 0.02
    safe_margin: float = Field(0.06, ge=0, lt=0.4)
    speaker_switch_margin: float = 0.12
    speaker_min_hold_s: float = 0.6


class PreserveConfig(BaseModel):
    blur_ksize: int = 75
    bg_zoom: float = Field(1.18, ge=1.0)
    canvas_margin: float = Field(0.05, ge=0, lt=0.3)
    letterbox_color: tuple[int, int, int] = (8, 8, 8)


class ClassifierConfig(BaseModel):
    vertical_aspect_max: float = 0.62
    high_cut_rate_per_min: float = 14.0
    high_global_motion: float = 0.16
    high_overlay_score: float = 0.42
    multi_subject_count: float = 1.6
    preserve_pressure_threshold: float = 1.0
    min_face_present_ratio: float = 0.55
    max_face_motion_for_static: float = 0.18
    min_face_area_for_focus: float = 0.012
    overlay_force_nocrop: float = 0.55
    scene_aware_min_scenes: int = 3
    scene_aware_max_motion: float = 0.16


class ReframeConfig(BaseModel):
    """Top-level reframing configuration."""

    output: OutputConfig = OutputConfig()
    analysis: AnalysisConfig = AnalysisConfig()
    focus: FocusConfig = FocusConfig()
    preserve: PreserveConfig = PreserveConfig()
    classifier: ClassifierConfig = ClassifierConfig()
    force_mode: str | None = None  # override auto-selection ("face_focus", ...)

    @classmethod
    def load(cls, path: str | Path | None) -> "ReframeConfig":
        if path is None:
            return cls()
        data: dict[str, Any] = yaml.safe_load(Path(path).read_text()) or {}
        # Allow nesting the reframe config under a "reframe:" key in a larger file.
        if "reframe" in data and isinstance(data["reframe"], dict):
            data = data["reframe"]
        return cls.model_validate(data)

    def to_params(self) -> ReframeParams:
        return ReframeParams(
            out_w=self.output.width,
            out_h=self.output.height,
            max_zoom=self.focus.max_zoom,
            target_face_frac=self.focus.target_face_frac,
            min_cutoff=self.focus.min_cutoff,
            beta=self.focus.beta,
            safe_margin=self.focus.safe_margin,
            speaker_switch_margin=self.focus.speaker_switch_margin,
            speaker_min_hold_s=self.focus.speaker_min_hold_s,
            blur_ksize=self.preserve.blur_ksize,
            bg_zoom=self.preserve.bg_zoom,
            canvas_margin=self.preserve.canvas_margin,
            letterbox_color=tuple(self.preserve.letterbox_color),  # type: ignore[arg-type]
            crf=self.output.crf,
            preset=self.output.preset,
            audio_bitrate=self.output.audio_bitrate,
        )

    def to_thresholds(self) -> ClassifierThresholds:
        c = self.classifier
        return ClassifierThresholds(
            vertical_aspect_max=c.vertical_aspect_max,
            high_cut_rate_per_min=c.high_cut_rate_per_min,
            high_global_motion=c.high_global_motion,
            high_overlay_score=c.high_overlay_score,
            multi_subject_count=c.multi_subject_count,
            preserve_pressure_threshold=c.preserve_pressure_threshold,
            min_face_present_ratio=c.min_face_present_ratio,
            max_face_motion_for_static=c.max_face_motion_for_static,
            min_face_area_for_focus=c.min_face_area_for_focus,
            overlay_force_nocrop=c.overlay_force_nocrop,
            scene_aware_min_scenes=c.scene_aware_min_scenes,
            scene_aware_max_motion=c.scene_aware_max_motion,
        )
