"""AI thumbnails: best-face frame pick -> real-pixel cutout -> composed
1080x1920 JPEG with a typeset Arabic headline.

Per short, everything lives under ``cfg.thumbs_dir / job_id``:
``cand_N.jpg`` (candidate frames for the picker UI), ``manifest.json`` and
``thumb.jpg`` (the final composition). :func:`rebuild_thumbnail` recomposes
from a chosen candidate / new headline / different template without
re-running detection.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["generate_thumbnail", "rebuild_thumbnail", "list_candidates"]


def _job_dir(cfg, job_id: str) -> Path:
    return cfg.thumbs_dir / Path(job_id).name


def generate_thumbnail(cfg, job_id: str, video_path: str,
                       span: tuple[float, float], headline: str,
                       template: str | None = None) -> str | None:
    """Full pipeline for one short. Returns the thumb.jpg path or None."""
    try:
        from . import compose, cutout, frames
    except Exception:  # pragma: no cover - PIL missing
        logger.warning("thumbnail deps unavailable")
        return None
    template = template or getattr(cfg, "thumb_template", "auto")
    t0, t1 = float(span[0]), float(span[1])

    cands = frames.select_candidates(video_path, t0, t1, k=5)
    if not cands:
        logger.warning("no candidate frames for %s", job_id)
        return None

    out_dir = _job_dir(cfg, job_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"video_path": str(video_path), "span": [t0, t1],
                "headline": headline, "template": template,
                "chosen_t": cands[0][0], "candidates": []}
    for i, (t, frame, score) in enumerate(cands, 1):
        name = f"cand_{i}.jpg"
        _save_preview(frame, out_dir / name)
        manifest["candidates"].append(
            {"name": name, "t": round(t, 2), "score": round(score * 1000, 1)})

    path = _compose(cfg, out_dir, cands[0][1], headline, template)
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


def rebuild_thumbnail(cfg, job_id: str, headline: str | None = None,
                      frame_t: float | None = None,
                      template: str | None = None) -> str | None:
    """Recompose from stored state, with any of the three knobs overridden."""
    try:
        from . import frames
    except Exception:  # pragma: no cover
        return None
    out_dir = _job_dir(cfg, job_id)
    try:
        manifest = json.loads((out_dir / "manifest.json")
                              .read_text(encoding="utf-8"))
    except Exception:
        logger.warning("no thumbnail manifest for %s", job_id)
        return None

    if headline is not None:
        manifest["headline"] = headline
    if template:
        manifest["template"] = template
    if frame_t is not None:
        manifest["chosen_t"] = float(frame_t)

    frame = frames.extract_frame(manifest["video_path"],
                                 float(manifest["chosen_t"]))
    if frame is None:  # source moved/deleted: fall back to nearest candidate
        cand = min(manifest.get("candidates", []),
                   key=lambda c: abs(c["t"] - float(manifest["chosen_t"])),
                   default=None)
        if not cand:
            return None
        try:
            import cv2  # type: ignore
            frame = cv2.imread(str(out_dir / cand["name"]))
        except Exception:
            return None
    if frame is None:
        return None

    path = _compose(cfg, out_dir, frame, manifest.get("headline", ""),
                    manifest.get("template", "auto"))
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


def list_candidates(cfg, job_id: str) -> list[dict]:
    try:
        manifest = json.loads((_job_dir(cfg, job_id) / "manifest.json")
                              .read_text(encoding="utf-8"))
        return list(manifest.get("candidates", []))
    except Exception:
        return []


def _compose(cfg, out_dir: Path, frame_bgr, headline: str,
             template: str) -> str | None:
    from . import compose, cutout
    subject = cutout.cut_subject(frame_bgr,
                                 use_gpu=getattr(cfg, "thumb_use_gpu", False))
    out = str(out_dir / "thumb.jpg")
    try:
        return compose.make_thumbnail(frame_bgr, subject, headline,
                                      template, out)
    except Exception:
        logger.exception("thumbnail composition failed")
        return None


def _save_preview(frame_bgr, path: Path, max_w: int = 720) -> None:
    """Candidate frame preview JPEG for the picker UI."""
    try:
        import cv2  # type: ignore
        h, w = frame_bgr.shape[:2]
        if w > max_w:
            frame_bgr = cv2.resize(frame_bgr, (max_w, int(h * max_w / w)))
        cv2.imwrite(str(path), frame_bgr,
                    [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    except Exception:
        logger.warning("could not save candidate preview %s", path)
