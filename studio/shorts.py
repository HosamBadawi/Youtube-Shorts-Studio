"""Turn ONE long video into N distinct shorts (no upload).

    python -m studio.shorts <video> --count 3 --model qwen3.6:35b --niche "..."

Transcribes once (large-v3 on GPU per studio.yaml), asks the LLM for ``count``
NON-overlapping segments - each its own topic - then renders each into a finished
vertical short: reframe (your reframe_mode, e.g. crop_blur) + burned captions +
its OWN Arabic title/caption/hashtags drawn from that segment's transcript.

Outputs into ``workspace/shorts/``: one ``.mp4`` and one ``.txt`` (the post text)
per short. Nothing is uploaded.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

from .captions import CaptionStyle, burn_captions
from .config import StudioConfig
from .llm import OllamaClient
from .pipeline import _LANG_NAMES, _offset_words, _probe_duration
from .prepare import _force_utf8_console
from .transcribe import transcribe


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0 if argv else 2

    video = argv[0]
    cfg = StudioConfig.load()
    model = _opt(argv, "--model", "")
    niche = _opt(argv, "--niche", "")
    mode = _opt(argv, "--mode", cfg.reframe_mode if cfg.reframe_mode != "auto"
                else "crop_blur")
    count = int(_opt(argv, "--count", str(cfg.shorts_per_video)))
    if model:
        cfg.ollama_model = model

    from .downloader import download, is_url
    if is_url(video):
        print(f"Downloading source from URL…\n  {video}")
        try:
            video = download(video, str(cfg.download_dir),
                             prefer_mp4=cfg.download_prefer_mp4)
            print(f"  saved: {video}\n")
        except Exception as exc:
            print(f"[FAIL] download failed: {exc}")
            return 1
    elif not Path(video).exists():
        print(f"[FAIL] file not found: {video}")
        return 1

    cfg.ensure_dirs()
    out_dir = cfg.workspace_path / "shorts"
    out_dir.mkdir(parents=True, exist_ok=True)
    sid = uuid.uuid4().hex[:8]

    ollama = OllamaClient(cfg.ollama_url, cfg.ollama_model, cfg.ollama_enabled,
                          timeout=cfg.ollama_timeout, think=cfg.ollama_think)
    if ollama.available():
        print("Ollama model:", ollama.resolve_model())

    # 1. transcribe once -----------------------------------------------------
    duration = _probe_duration(video)
    print(f"Source: {duration:.0f}s. Transcribing (this can take a moment)…")
    tr = transcribe(video, cfg.whisper_model, cfg.whisper_device,
                    cfg.whisper_enabled, cfg.whisper_language)
    if not tr.available:
        print(f"[FAIL] no transcript ({tr.note}); cannot split into topics.")
        return 1

    # 2. semantic selection (sentence-snapped, junk-masked) -------------------
    print(f"Asking {cfg.ollama_model} for up to {count} distinct segments…")
    from .segmenter import select_segments
    picks = select_segments(ollama, tr, duration, count,
                            cfg.min_short_seconds, cfg.max_short_seconds,
                            window=cfg.segment_window_sentences,
                            overlap=cfg.segment_overlap_sentences)
    if not picks:
        print("[FAIL] the model found no strong self-contained segments.")
        return 1
    segments = [(p.start, p.end, p.topic) for p in picks]
    print(f"Got {len(segments)} segment(s). Rendering as '{mode}'…\n")

    language = cfg.metadata_language
    if language.lower() in {"auto", ""}:
        language = _LANG_NAMES.get((tr.language or "").lower(), "")

    style = CaptionStyle(font=cfg.caption_font, fontsize=cfg.caption_fontsize,
                         base_color=cfg.caption_base_color,
                         highlight=cfg.caption_highlight,
                         position=cfg.caption_position,
                         max_words=cfg.caption_max_words)
    from adaptive_reframe import AdaptiveReframePipeline
    reframer = AdaptiveReframePipeline()

    # 3. render each short (resilient: one failure / Ctrl+C keeps the rest) ---
    done = 0
    for i, (start, end, topic) in enumerate(segments, 1):
        base = out_dir / f"{sid}_short{i}"
        seg_mp4 = str(base) + "_seg.mp4"
        raw = str(base) + "_raw.mp4"
        out = str(base) + ".mp4"
        print(f"── Short {i}/{len(segments)}  [{start:.0f}s–{end:.0f}s, "
              f"{end-start:.0f}s]  rendering…", flush=True)
        try:
            _trim(video, (start, end), seg_mp4)
            reframer.reframe(seg_mp4, raw, force_mode=mode)

            words = _offset_words(tr.words, start, end)
            if not (words and burn_captions(raw, words, out, style)):
                Path(raw).replace(out)
            else:
                Path(raw).unlink(missing_ok=True)

            seg_text = " ".join(w.text for w in tr.words if start <= w.start < end)
            meta = (ollama.generate_copy(seg_text or topic, niche, language)
                    if ollama.available() else None)
            print(f"   topic : {topic or '(n/a)'}")
            if meta:
                print(f"   title : {meta.title}")
                print(f"   desc  : {meta.description}")
                print(f"   tags  : {' '.join(meta.hashtags)}")
                _write_caption_file(str(base) + ".txt", meta, topic, start, end)
            print(f"   video : {out}\n", flush=True)
            done += 1
        except KeyboardInterrupt:
            print(f"\n[stopped] interrupted during short {i}. "
                  f"{done} finished short(s) are saved.\n")
            break
        except Exception as exc:
            print(f"   [skipped short {i}: {type(exc).__name__}: {exc}]\n")
        finally:
            Path(seg_mp4).unlink(missing_ok=True)
            Path(raw).unlink(missing_ok=True)

    print(f"Done. {done}/{len(segments)} shorts in {out_dir}\\  (nothing uploaded).")
    return 0


def _write_caption_file(path, meta, topic, start, end) -> None:
    lines = [f"topic: {topic}", f"segment: {start:.0f}s - {end:.0f}s", "",
             meta.title, "", meta.description, "", " ".join(meta.hashtags)]
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def _trim(src: str, span, out: str) -> None:
    start, dur = span[0], max(0.1, span[1] - span[0])
    subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", src, "-t", f"{dur:.2f}",
         "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", out],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _opt(argv: list[str], flag: str, default: str) -> str:
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


if __name__ == "__main__":
    raise SystemExit(main())
