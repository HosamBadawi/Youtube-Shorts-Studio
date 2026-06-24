"""Ingest -> process -> publish orchestration.

``process_job`` runs the heavy, AI-assisted preparation (transcribe -> pick the
best segment -> trim -> reframe to 9:16 -> draft metadata) and leaves the job in
``ready`` for review. ``publish_job`` then pushes the reviewed video to the
chosen platforms. Both are designed to run on a background worker thread and to
record their progress on the :class:`Job` so the phone UI can poll it.

ffmpeg / ffprobe must be on PATH (same requirement as the reframe renderer).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from .captions import CaptionStyle, burn_captions
from .config import StudioConfig
from .jobs import (STATUS_DONE, STATUS_ERROR, STATUS_PROCESSING,
                   STATUS_PUBLISHING, STATUS_READY, Job, JobStore)
from .llm import OllamaClient
from .metadata import VideoMeta
from .transcribe import transcribe

logger = logging.getLogger(__name__)

# Whisper language codes -> human names for the metadata prompt.
_LANG_NAMES = {
    "ar": "Arabic", "en": "English", "fr": "French", "es": "Spanish",
    "de": "German", "tr": "Turkish", "ur": "Urdu", "fa": "Persian",
    "hi": "Hindi", "id": "Indonesian", "ru": "Russian", "pt": "Portuguese",
}


class StudioPipeline:
    def __init__(self, cfg: StudioConfig, store: JobStore) -> None:
        self.cfg = cfg
        self.store = store
        self.ollama = OllamaClient(cfg.ollama_url, cfg.ollama_model,
                                   cfg.ollama_enabled, timeout=cfg.ollama_timeout,
                                   think=cfg.ollama_think)

    # ======================================================================
    # PREPARE  (transcribe -> segment -> reframe -> metadata)
    # ======================================================================
    def process_job(self, job: Job, *, auto_metadata: bool = True,
                    per_platform: bool = False, niche: str = "") -> Job:
        try:
            job.status = STATUS_PROCESSING
            job.stage = "probing"
            self.store.update(job)

            # Lock in the Ollama model now (auto-pick the best installed one).
            if self.cfg.ollama_enabled:
                self.ollama.resolve_model()

            src = job.source_path
            job.duration = _probe_duration(src)

            # 1. Transcribe ---------------------------------------------------
            job.stage = "transcribing"
            self.store.update(job)
            tr = transcribe(src, self.cfg.whisper_model, self.cfg.whisper_device,
                            self.cfg.whisper_enabled, self.cfg.whisper_language)
            job.transcript = tr.text
            if tr.note:
                logger.info("transcription: %s", tr.note)

            # 2. Pick the best segment (only for longer sources) -------------
            work_path = src
            if job.duration > self.cfg.keep_whole_if_under_seconds and tr.available:
                job.stage = "selecting highlight"
                self.store.update(job)
                span = self.ollama.pick_segment(
                    tr.timestamped(), job.duration, self.cfg.target_short_seconds)
                if span is None:  # LLM failed -> sensible default window
                    span = (0.0, min(self.cfg.target_short_seconds, job.duration))
                span = _enforce_bounds(span, self.cfg.min_short_seconds,
                                       self.cfg.max_short_seconds, job.duration)
                job.segment = span
                work_path = self._trim(src, job.id, span)

            # 3. Reframe to vertical 9:16 ------------------------------------
            job.stage = "reframing to 9:16"
            self.store.update(job)
            out_path = str(self.cfg.rendered_dir / f"{job.id}.mp4")
            raw_path = str(self.cfg.rendered_dir / f"{job.id}_raw.mp4")
            self._reframe(work_path, raw_path)

            # 3b. Burn TikTok-style captions from the words in this segment ---
            self._apply_captions(job, tr.words, raw_path, out_path)
            job.output_path = out_path

            # 4. Draft metadata (unless the user already supplied it) --------
            if auto_metadata and not job.meta.is_complete():
                job.stage = "writing captions"
                self.store.update(job)
                language = self._metadata_language(tr.language)
                job.meta = self._draft_metadata(job.transcript, per_platform,
                                                niche, language)

            job.stage = ""
            job.status = STATUS_READY
            self.store.update(job)
            return job
        except Exception as exc:  # pragma: no cover - depends on cv2/ffmpeg
            logger.exception("process_job failed")
            job.status = STATUS_ERROR
            job.error = f"{type(exc).__name__}: {exc}"
            job.stage = ""
            self.store.update(job)
            return job

    def _draft_metadata(self, transcript: str, per_platform: bool,
                        niche: str, language: str = "") -> VideoMeta:
        if not self.ollama.available():
            return VideoMeta(title="", caption="", source="manual", hashtags=[])
        if per_platform:
            return self.ollama.generate_per_platform(
                transcript, list(self.cfg.enabled_platforms), niche, language)
        return (self.ollama.generate_metadata(transcript, None, niche, language)
                or VideoMeta())

    def _metadata_language(self, transcript_lang: str) -> str:
        """Resolve the post-text language: config override, else the detected
        spoken language mapped to a human name (empty -> model matches transcript)."""
        cfg_lang = (self.cfg.metadata_language or "auto").strip()
        if cfg_lang.lower() not in {"auto", ""}:
            return cfg_lang
        return _LANG_NAMES.get((transcript_lang or "").lower(), "")

    # ======================================================================
    # PUBLISH
    # ======================================================================
    def publish_job(self, job: Job, platforms: list[str]) -> Job:
        from .publishers import get_publisher

        job.status = STATUS_PUBLISHING
        job.stage = "publishing"
        self.store.update(job)

        any_ok = False
        for platform in platforms:
            job.stage = f"publishing to {platform}"
            self.store.update(job)
            try:
                pub = get_publisher(platform, self.cfg)
                result = pub.publish(job.output_path, job.meta)
            except Exception as exc:  # pragma: no cover
                from .publishers.base import PublishResult
                result = PublishResult.failure(platform, str(exc))
            job.results[platform] = result.to_dict()
            any_ok = any_ok or result.ok
            self.store.update(job)

        if any_ok:
            self.store.mark_published_today(job.id)
        job.status = STATUS_DONE
        job.stage = ""
        self.store.update(job)
        return job

    # ======================================================================
    # helpers
    # ======================================================================
    def _trim(self, src: str, job_id: str, span: tuple[float, float]) -> str:
        start, end = span
        duration = max(0.1, end - start)
        out = str(self.cfg.incoming_dir / f"{job_id}_seg.mp4")
        # -ss before -i for a fast seek; -t (duration) keeps the output starting
        # at 0 so caption word offsets (= word.start - start) stay correct.
        cmd = [
            "ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", src,
            "-t", f"{duration:.2f}", "-c:v", "libx264", "-c:a", "aac",
            "-movflags", "+faststart", out,
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        return out

    def _reframe(self, src: str, out: str) -> None:
        # Imported lazily: pulls in OpenCV, only needed on the rendering host.
        from adaptive_reframe import AdaptiveReframePipeline

        mode = (self.cfg.reframe_mode or "auto").strip().lower()
        force = None if mode in {"auto", ""} else mode
        AdaptiveReframePipeline().reframe(src, out, force_mode=force)

    def _apply_captions(self, job: Job, words, raw_path: str,
                        out_path: str) -> None:
        """Burn captions onto the reframed clip; fall back to raw on any miss."""
        seg_start = job.segment[0] if job.segment else 0.0
        seg_end = job.segment[1] if job.segment else (job.duration or 1e9)
        local = _offset_words(words, seg_start, seg_end)

        burned = False
        if self.cfg.captions_enabled and local:
            job.stage = "burning captions"
            self.store.update(job)
            style = CaptionStyle(
                font=self.cfg.caption_font,
                fontsize=self.cfg.caption_fontsize,
                base_color=self.cfg.caption_base_color,
                highlight=self.cfg.caption_highlight,
                position=self.cfg.caption_position,
                max_words=self.cfg.caption_max_words,
            )
            burned = burn_captions(raw_path, local, out_path, style,
                                   play_w=self.params_wh()[0],
                                   play_h=self.params_wh()[1])
        if burned:
            Path(raw_path).unlink(missing_ok=True)
        else:
            # No captions (disabled / no words / ffmpeg miss): keep the reframe.
            shutil.move(raw_path, out_path)

    def params_wh(self) -> tuple[int, int]:
        from adaptive_reframe import ReframeParams
        p = ReframeParams()
        return p.out_w, p.out_h


def _offset_words(words, seg_start: float, seg_end: float):
    """Keep words inside [seg_start, seg_end) and rebase them to a 0-based
    timeline so they line up with the trimmed clip."""
    from .transcribe import Word

    out = []
    for w in words or []:
        if w.end <= seg_start or w.start >= seg_end:
            continue
        ns = max(0.0, w.start - seg_start)
        ne = max(ns + 0.05, w.end - seg_start)
        out.append(Word(ns, ne, w.text))
    return out


def _enforce_bounds(span: tuple[float, float], min_s: float, max_s: float,
                    total: float) -> tuple[float, float]:
    """Clamp the picked span into [min_s, max_s] seconds within the source.

    Over-long spans are trimmed from the end (keeping the chosen hook); too-short
    spans are extended, pulling the start back only if the end hits the source
    end. If the whole source is shorter than ``min_s``, the whole thing is used.
    """
    if total <= min_s:
        return (0.0, total)
    start = min(max(0.0, span[0]), total)
    end = min(max(start, span[1]), total)
    length = end - start
    if length > max_s:                      # too long -> trim the tail
        end = start + max_s
    elif length < min_s:                    # too short -> extend
        end = start + min_s
        if end > total:
            end = total
            start = max(0.0, end - min_s)
    return (start, end)


def _probe_duration(path: str) -> float:
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
               "-show_format", path]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return 0.0
