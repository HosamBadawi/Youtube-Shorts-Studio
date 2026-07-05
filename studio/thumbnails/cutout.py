"""Subject cutout via rembg — the presenter's REAL pixels, never a generated
face. Model preference: birefnet-portrait (best hair edges) -> isnet-general
-> u2net. CPU by default so the 8 GB GPU stays free for Ollama/Whisper; one
thumbnail costs seconds either way. Missing rembg just means "no cutout" and
the composer degrades gracefully.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_MODELS = ("birefnet-portrait", "isnet-general-use", "u2net")
_session = None
_session_gpu = None


def _get_session(use_gpu: bool):
    global _session, _session_gpu
    if _session is not None and _session_gpu == use_gpu:
        return _session
    try:
        from rembg import new_session  # type: ignore
    except Exception:
        return None
    providers = (["CUDAExecutionProvider", "CPUExecutionProvider"]
                 if use_gpu else ["CPUExecutionProvider"])
    for model in _MODELS:
        try:
            logger.info("loading matting model %s (first run downloads it)",
                        model)
            _session = new_session(model, providers=providers)
            _session_gpu = use_gpu
            return _session
        except Exception:
            logger.warning("matting model %s unavailable, trying next", model)
    return None


def cut_subject(frame_bgr, use_gpu: bool = False):
    """RGBA PIL image of the subject, or None (no rembg / bad matte)."""
    try:
        from PIL import Image
        from rembg import remove  # type: ignore
    except Exception:
        logger.info("rembg not installed — composing without cutout")
        return None
    session = _get_session(use_gpu)
    if session is None:
        return None
    try:
        rgb = Image.fromarray(frame_bgr[:, :, ::-1])  # BGR -> RGB
        out = remove(rgb, session=session)
        if out.mode != "RGBA":
            out = out.convert("RGBA")
    except Exception:
        logger.warning("matting failed", exc_info=True)
        return None

    box = out.getbbox()
    if not box:
        return None
    out = out.crop(box)
    # Reject bad mattes: a subject under 5% of the frame is usually noise.
    alpha = out.getchannel("A")
    opaque = sum(1 for v in alpha.getdata() if v > 40)
    if opaque < 0.05 * frame_bgr.shape[0] * frame_bgr.shape[1]:
        logger.info("cutout rejected (opaque area too small)")
        return None
    return out
