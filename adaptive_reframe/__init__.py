"""Adaptive vertical (9:16) reframing subsystem.

Public API::

    from adaptive_reframe import AdaptiveReframePipeline, ReframeMode

    pipe = AdaptiveReframePipeline()
    result = pipe.reframe("clip.mp4", "clip_vertical.mp4")
    print(result.decision.explain())

The submodules ``types``, ``geometry`` and ``classifier`` are stdlib-only and
import cleanly without OpenCV / NumPy / Pydantic.
"""

from __future__ import annotations

from .types import (
    BBox,
    ClipAnalysis,
    FaceObservation,
    ReframeDecision,
    ReframeMode,
    ReframeParams,
    SceneSpan,
)

__all__ = [
    "AdaptiveReframePipeline",
    "ReframeResult",
    "ReframeMode",
    "ReframeParams",
    "ReframeDecision",
    "ClipAnalysis",
    "FaceObservation",
    "SceneSpan",
    "BBox",
]

__version__ = "0.1.0"


def __getattr__(name: str):
    # Lazily import the heavy pipeline (pulls in cv2/numpy) only when asked, so
    # ``import adaptive_reframe`` stays light for callers that just need types.
    if name in {"AdaptiveReframePipeline", "ReframeResult"}:
        from .pipeline import AdaptiveReframePipeline, ReframeResult

        return {"AdaptiveReframePipeline": AdaptiveReframePipeline,
                "ReframeResult": ReframeResult}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
