"""Render the SAME picked segment with different reframing techniques so you can
compare them side by side. Nothing is uploaded.

    python -m studio.sample_modes <video> --model qwen3.6:35b [--niche "..."]
                                  [--modes blur_background,face_focus,crop_blur]

For each mode it writes a captioned vertical .mp4 plus a single .png preview
frame into ``workspace/samples/``. The transcription, segment pick and Arabic
metadata are computed once and shared across all modes.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

from .captions import CaptionStyle, burn_captions
from .config import StudioConfig
from .llm import OllamaClient
from .pipeline import (_enforce_bounds, _LANG_NAMES, _offset_words,
                       _probe_duration)
from .prepare import _force_utf8_console
from .transcribe import transcribe

DEFAULT_MODES = ["blur_background", "face_focus", "crop_blur"]
_LABELS = {
    "blur_background": "WITH blanking (blur background, full scene)",
    "face_focus": "WITHOUT blanking (crop fills the frame)",
    "crop_blur": "COMBINED (subject-tracked crop + blur margins)",
}


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0 if argv else 2

    video = argv[0]
    model = _opt(argv, "--model", "")
    niche = _opt(argv, "--niche", "")
    modes = _opt(argv, "--modes", ",".join(DEFAULT_MODES)).split(",")
    modes = [m.strip() for m in modes if m.strip()]

    if not Path(video).exists():
        print(f"[FAIL] file not found: {video}")
        return 1

    cfg = StudioConfig.load()
    if model:
        cfg.ollama_model = model
    cfg.ensure_dirs()
    samples_dir = cfg.workspace_path / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    sid = uuid.uuid4().hex[:8]

    ollama = OllamaClient(cfg.ollama_url, cfg.ollama_model, cfg.ollama_enabled,
                          timeout=cfg.ollama_timeout, think=cfg.ollama_think)
    if ollama.available():
        print("Ollama model:", ollama.resolve_model())

    # --- shared prep: transcribe -> pick segment -> trim -> metadata --------
    duration = _probe_duration(video)
    print(f"Source: {duration:.0f}s. Transcribing…")
    tr = transcribe(video, cfg.whisper_model, cfg.whisper_device,
                    cfg.whisper_enabled, cfg.whisper_language)

    seg = (0.0, min(cfg.target_short_seconds, duration))
    if duration > cfg.keep_whole_if_under_seconds and tr.available:
        print("Selecting highlight…")
        picked = ollama.pick_segment(tr.timestamped(), duration,
                                     cfg.target_short_seconds)
        if picked:
            seg = picked
    seg = _enforce_bounds(seg, cfg.min_short_seconds, cfg.max_short_seconds,
                          duration)
    print(f"Segment: {seg[0]:.0f}s -> {seg[1]:.0f}s ({seg[1]-seg[0]:.0f}s)")

    seg_path = str(samples_dir / f"{sid}_segment.mp4")
    _trim(video, seg, seg_path)
    words = _offset_words(tr.words, seg[0], seg[1])

    language = cfg.metadata_language
    if language.lower() in {"auto", ""}:
        language = _LANG_NAMES.get((tr.language or "").lower(), "")
    meta = ollama.generate_metadata(tr.text, None, niche, language) if \
        ollama.available() else None

    print("\n=== POST TEXT" + (f" ({language})" if language else "") + " ===")
    if meta:
        print("title   :", meta.title)
        print("caption :", meta.caption)
        print("hashtags:", " ".join(meta.hashtags))
    else:
        print("(Ollama unavailable)")

    # --- render each mode ---------------------------------------------------
    style = CaptionStyle(font=cfg.caption_font, fontsize=cfg.caption_fontsize,
                         base_color=cfg.caption_base_color,
                         highlight=cfg.caption_highlight,
                         position=cfg.caption_position,
                         max_words=cfg.caption_max_words)
    from adaptive_reframe import AdaptiveReframePipeline
    reframer = AdaptiveReframePipeline()

    print("\n=== SAMPLES ===")
    for mode in modes:
        raw = str(samples_dir / f"{sid}_{mode}_raw.mp4")
        out = str(samples_dir / f"{sid}_{mode}.mp4")
        png = str(samples_dir / f"{sid}_{mode}.png")
        try:
            reframer.reframe(seg_path, raw, force_mode=mode)
        except Exception as exc:
            print(f"  [FAIL] {mode}: {exc}")
            continue
        if not (words and burn_captions(raw, words, out, style)):
            Path(raw).replace(out)
        else:
            Path(raw).unlink(missing_ok=True)
        _frame(out, png, t=min(8.0, (seg[1] - seg[0]) / 2))
        print(f"  [{mode:15}] {_LABELS.get(mode, '')}")
        print(f"      video: {out}")
        print(f"      frame: {png}")

    Path(seg_path).unlink(missing_ok=True)
    print("\nDone. Open the .mp4 files (or .png frames) to compare.\n")
    return 0


def _trim(src: str, span: tuple[float, float], out: str) -> None:
    start, dur = span[0], max(0.1, span[1] - span[0])
    subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", src, "-t", f"{dur:.2f}",
         "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", out],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _frame(video: str, png: str, t: float) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}",
         "-i", video, "-frames:v", "1", png],
        check=False)


def _opt(argv: list[str], flag: str, default: str) -> str:
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


if __name__ == "__main__":
    raise SystemExit(main())
