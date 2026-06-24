"""High-level reframing pipeline.

This is the single entry point the larger Shorts pipeline should call once it has
cut a clip. It analyses the clip, classifies the best reframing mode (or honours
a forced/override mode), renders the vertical output, and returns a small result
object describing what happened.

It depends only on plain dataclasses (no Pydantic), so it can be embedded
anywhere.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import cv2

from .analyzer import AnalyzerSettings, ClipAnalyzer
from .classifier import ClassifierThresholds, ReframeClassifier
from .renderer import Renderer
from .strategies import RenderContext, build_strategy
from .types import ClipAnalysis, ReframeDecision, ReframeMode, ReframeParams

logger = logging.getLogger(__name__)


@dataclass
class ReframeResult:
    input_path: str
    output_path: str
    decision: ReframeDecision
    analysis: ClipAnalysis
    elapsed_s: float

    def summary(self) -> str:
        return (
            f"{Path(self.input_path).name} -> {Path(self.output_path).name} | "
            f"{self.decision.explain()} | {self.elapsed_s:.1f}s"
        )


class AdaptiveReframePipeline:
    """Analyse -> classify -> render, with an optional forced mode override."""

    def __init__(
        self,
        params: ReframeParams | None = None,
        thresholds: ClassifierThresholds | None = None,
        analyzer_settings: AnalyzerSettings | None = None,
    ) -> None:
        self.params = params or ReframeParams()
        self.classifier = ReframeClassifier(thresholds)
        self.analyzer = ClipAnalyzer(analyzer_settings)
        self.renderer = Renderer(self.params)

    def reframe(
        self,
        input_path: str,
        output_path: str,
        force_mode: ReframeMode | str | None = None,
    ) -> ReframeResult:
        start = time.perf_counter()
        in_path = str(input_path)
        if not Path(in_path).exists():
            raise FileNotFoundError(in_path)

        analysis = self.analyzer.analyze(in_path)

        if force_mode is not None:
            mode = ReframeMode(force_mode)
            decision = ReframeDecision(mode, 1.0, ["forced by caller/config"])
        else:
            decision = self.classifier.classify(analysis)
        logger.info("Decision: %s", decision.explain())

        ctx = self._context(in_path, analysis)
        strategy = build_strategy(decision.mode, ctx)
        self.renderer.render(in_path, strategy, str(output_path))

        elapsed = time.perf_counter() - start
        result = ReframeResult(in_path, str(output_path), decision, analysis,
                               elapsed)
        logger.info("Done: %s", result.summary())
        return result

    def _context(self, video_path: str, analysis: ClipAnalysis) -> RenderContext:
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or analysis.fps or 30.0
        cap.release()
        return RenderContext(
            analysis=analysis,
            params=self.params,
            src_w=analysis.width,
            src_h=analysis.height,
            fps=fps,
            duration=analysis.duration,
            video_path=video_path,
        )
