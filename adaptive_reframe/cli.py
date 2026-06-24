"""Command-line interface for the adaptive reframing subsystem.

Examples
--------
Reframe a single clip with automatic mode selection::

    python -m adaptive_reframe.cli reframe input.mp4 -o output.mp4

Force a specific mode and use a config file::

    python -m adaptive_reframe.cli reframe in.mp4 -o out.mp4 \
        --mode blur_background --config config.yaml

Just analyse / classify without rendering (prints the decision + signals)::

    python -m adaptive_reframe.cli analyze input.mp4
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .analyzer import AnalyzerSettings, ClipAnalyzer
from .classifier import ReframeClassifier
from .config import ReframeConfig
from .pipeline import AdaptiveReframePipeline
from .types import ReframeMode


def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING - min(verbosity, 2) * 10
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _analyzer_settings(cfg: ReframeConfig) -> AnalyzerSettings:
    a = cfg.analysis
    return AnalyzerSettings(
        sample_fps=a.sample_fps,
        face_min_confidence=a.face_min_confidence,
        mediapipe_model=a.mediapipe_model,
        use_pyscenedetect=a.use_pyscenedetect,
        scene_diff_threshold=a.scene_diff_threshold,
    )


def cmd_reframe(args: argparse.Namespace) -> int:
    cfg = ReframeConfig.load(args.config)
    pipeline = AdaptiveReframePipeline(
        params=cfg.to_params(),
        thresholds=cfg.to_thresholds(),
        analyzer_settings=_analyzer_settings(cfg),
    )
    force = args.mode or cfg.force_mode
    out = args.output or str(Path(args.input).with_suffix("")) + "_vertical.mp4"
    result = pipeline.reframe(args.input, out, force_mode=force)
    print(result.summary())
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    cfg = ReframeConfig.load(args.config)
    analyzer = ClipAnalyzer(_analyzer_settings(cfg))
    analysis = analyzer.analyze(args.input)
    decision = ReframeClassifier(cfg.to_thresholds()).classify(analysis)
    payload = {
        "decision": {
            "mode": decision.mode.value,
            "confidence": round(decision.confidence, 3),
            "rationale": decision.rationale,
        },
        "signals": {
            "duration_s": round(analysis.duration, 2),
            "aspect_ratio": round(analysis.aspect_ratio, 3),
            "face_present_ratio": round(analysis.face_present_ratio, 3),
            "typical_face_count": round(analysis.typical_face_count, 2),
            "dominant_face_area_ratio": round(analysis.dominant_face_area_ratio, 4),
            "dominant_face_motion": round(analysis.dominant_face_motion, 3),
            "global_motion": round(analysis.global_motion, 3),
            "cut_rate_per_min": round(analysis.cut_rate_per_min, 2),
            "overlay_score": round(analysis.overlay_score, 3),
            "has_active_speaker": analysis.has_active_speaker,
            "scenes": len(analysis.scenes),
        },
        "notes": analysis.notes,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adaptive_reframe",
        description="Adaptive vertical reframing for short-form video.",
    )
    parser.add_argument("-v", "--verbose", action="count", default=1,
                        help="-v info, -vv debug")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("reframe", help="Reframe a clip to vertical.")
    pr.add_argument("input")
    pr.add_argument("-o", "--output", default=None)
    pr.add_argument(
        "--mode", default=None, choices=[m.value for m in ReframeMode],
        help="Force a specific reframing mode (skips auto-classification).",
    )
    pr.set_defaults(func=cmd_reframe)

    pa = sub.add_parser("analyze", help="Analyse + classify only (no render).")
    pa.add_argument("input")
    pa.set_defaults(func=cmd_analyze)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except Exception as exc:  # pragma: no cover
        logging.getLogger("adaptive_reframe").error("Failed: %s", exc,
                                                     exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
