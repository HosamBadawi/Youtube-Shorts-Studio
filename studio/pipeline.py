"""Ingest -> process -> upload orchestration.

``generate_shorts`` is the main path: download (if URL) -> transcribe -> mask
junk (SponsorBlock + LLM) -> semantic segment selection (sentence-snapped) ->
per short: trim -> silence-cut montage -> face-tracked 9:16 reframe -> karaoke
captions -> subscribe-reminder overlay -> Arabic title/description/headline ->
composed thumbnail. Each short lands as a READY job for phone review.
``upload_job`` then pushes a reviewed short to YouTube (optionally baking the
thumbnail in as the first frame).

Honesty rule: if the AI finds fewer strong self-contained segments than
requested, fewer shorts are produced and the batch note says so — there is no
blind fixed-interval padding.

ffmpeg / ffprobe must be on PATH (same requirement as the reframe renderer).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from .captions import CaptionStyle, burn_captions
from .config import StudioConfig
from .jobs import (STATUS_DONE, STATUS_ERROR, STATUS_PROCESSING,
                   STATUS_PUBLISHING, STATUS_READY, Job, JobStore)
from .llm import make_llm
from .metadata import VideoMeta
from .transcribe import transcribe

logger = logging.getLogger(__name__)

# Whisper language codes -> human names for the copy prompt.
_LANG_NAMES = {
    "ar": "Arabic", "en": "English", "fr": "French", "es": "Spanish",
    "de": "German", "tr": "Turkish", "ur": "Urdu", "fa": "Persian",
    "hi": "Hindi", "id": "Indonesian", "ru": "Russian", "pt": "Portuguese",
}

# YouTube classifies <=180s vertical videos as Shorts; keep headroom for the
# optional first-frame thumbnail embed (+0.1s).
_SHORTS_MAX_SECONDS = 179.5


class StudioPipeline:
    def __init__(self, cfg: StudioConfig, store: JobStore, vault=None) -> None:
        self.cfg = cfg
        self.store = store
        if vault is None:
            from .vault import CredentialVault
            vault = CredentialVault(cfg)
        self.vault = vault
        # The active LLM (local Ollama or a cloud provider) — rebuilt by the
        # server when the user changes the model in the UI.
        self.llm = make_llm(cfg, vault)

    # ======================================================================
    # MULTI-SHORT  (one long video / URL -> up to N reviewable short Jobs)
    # ======================================================================
    def generate_shorts(self, source: str, count: int, niche: str = "",
                        batch_id: str = "", on_stage=None,
                        min_s: float | None = None,
                        max_s: float | None = None,
                        face_tracking: bool = True,
                        caption_pos: float | None = None) -> list[str]:
        """Returns the created job ids. Progress goes to the batches table
        (when ``batch_id`` is set) and to ``on_stage(str)`` (CLI use)."""
        min_len = min_s if min_s else self.cfg.min_short_seconds
        max_len = max_s if max_s else self.cfg.max_short_seconds
        if min_len > max_len:
            min_len, max_len = max_len, min_len
        max_len = min(max_len, _SHORTS_MAX_SECONDS)
        target_len = (self.cfg.target_short_seconds
                      if (min_s is None and max_s is None)
                      else (min_len + max_len) / 2.0)
        from .downloader import claim_index, download, is_url

        def stage(s: str, percent: float | None = None,
                  note: str | None = None) -> None:
            if batch_id:
                self.store.batch_update(batch_id, stage=s, percent=percent,
                                        note=note)
            if on_stage:
                try:
                    on_stage(s)
                except Exception:
                    pass

        if self.cfg.ollama_enabled:
            self.llm.resolve_model()

        video_id = None
        if is_url(source):
            from .sponsorblock import extract_video_id
            video_id = extract_video_id(source)
            stage("downloading video", 2)
            with claim_index(self.cfg.library_path) as idx:  # 1.mp4, 2.mp4, …
                source = download(source, str(self.cfg.library_path),
                                  prefer_mp4=self.cfg.download_prefer_mp4,
                                  name=str(idx),
                                  allowlist=self.cfg.download_host_allowlist)
        stage("probing", 8)
        duration = _probe_duration(source)

        stage("transcribing", 12)
        tr = transcribe(source, self.cfg.whisper_model, self.cfg.whisper_device,
                        self.cfg.whisper_enabled, self.cfg.whisper_language)

        # --- semantic selection ---------------------------------------------
        stage("selecting the best moments", 34)
        floor = self._intro_floor(duration)
        junk: list[tuple[float, float]] = []
        if video_id and self.cfg.sponsorblock_enabled:
            from .sponsorblock import fetch_junk_segments
            junk = fetch_junk_segments(video_id)

        # NEVER overlap a short that was already generated from this same
        # source video (any batch, any day): its spans are masked exactly
        # like sponsor segments, plus a hard filter below as a second gate.
        used = self.store.used_segments_for_source(source)
        if used:
            logger.info("excluding %d span(s) already used by earlier shorts",
                        len(used))

        picks = []
        note = ""
        if tr.available:
            from .segmenter import select_segments
            picks = select_segments(
                self.llm, tr, duration, count, min_len, max_len,
                min_start=floor, junk=junk + used,
                window=self.cfg.segment_window_sentences,
                overlap=self.cfg.segment_overlap_sentences,
                on_stage=lambda s: stage(s, 36))
            picks = [p for p in picks if not _overlaps(p.start, p.end, used)]
        if not picks:
            if tr.available and self.llm.available():
                note = ("no strong self-contained segments found — try wider "
                        "duration bounds or a different video")
                if used:
                    note = ("no strong segments left that don't overlap the "
                            "shorts already made from this video")
                stage("done", 100, note=note)
                return []
            # No transcript / no LLM: one honest default clip after the intro
            # floor beats producing nothing at all.
            from .segmenter import SegmentPick
            window = _first_free_window(duration, floor, target_len, used)
            if window is None:
                stage("done", 100, note="this video is already fully covered "
                                        "by earlier shorts")
                return []
            picks = [SegmentPick(window[0], window[1], topic="",
                                 reason="AI selection unavailable — default "
                                        "window after the intro")]
            note = "AI selection unavailable — one default clip was cut"
        elif len(picks) < count:
            note = (f"found {len(picks)} strong segment(s) out of the "
                    f"{count} requested — quality over padding")
        if note:
            stage("rendering", 40, note=note)

        language = self._metadata_language(tr.language)
        longstem = Path(source).stem  # e.g. "1" -> shorts "1_1.mp4", "1_2.mp4"…

        job_ids: list[str] = []
        used_titles: list[str] = []
        for idx, pick in enumerate(picks, 1):
            # each short owns an equal slice of the 40..98 percent band
            span = 58.0 / len(picks)
            base = 40 + span * (idx - 1)
            job = self.store.create(source, batch_id=batch_id,
                                    topic=pick.topic, score=pick.score,
                                    reason=pick.reason)
            job.duration = duration
            job.segment = (pick.start, pick.end)
            job.transcript = " ".join(
                w.text for w in tr.words
                if pick.start <= w.start < pick.end)
            job.status = STATUS_PROCESSING
            self.store.update(job)
            try:
                stage(f"rendering short {idx}/{len(picks)}",
                      base + span * 0.15)
                out = str(self.cfg.shorts_dir / f"{longstem}_{idx}.mp4")
                if Path(out).exists():   # a previous batch from the same
                    # source — don't silently overwrite; uniquify with job id
                    out = str(self.cfg.shorts_dir
                              / f"{longstem}_{idx}_{job.id[:6]}.mp4")
                self._render_short(job, source, tr.words, out,
                                   face_tracking=face_tracking,
                                   caption_pos=caption_pos)

                if self.llm.available():
                    job.stage = f"short {idx}: writing the copy"
                    self.store.update(job)
                    stage(f"short {idx}/{len(picks)}: writing copy",
                          base + span * 0.65)
                    meta = self.llm.generate_copy(
                        job.transcript or pick.topic, niche, language,
                        avoid_titles=used_titles)
                    if meta:
                        job.meta = meta
                        used_titles.append(meta.title)

                if self.cfg.thumbs_enabled:
                    job.stage = f"short {idx}: composing thumbnail"
                    self.store.update(job)
                    stage(f"short {idx}/{len(picks)}: thumbnail",
                          base + span * 0.85)
                    self._make_thumbnail(job, source)

                job.status = STATUS_READY
                job.stage = ""
            except Exception as exc:  # pragma: no cover - cv2/ffmpeg runtime
                logger.exception("short %d render failed", idx)
                job.status = STATUS_ERROR
                job.error = f"{type(exc).__name__}: {exc}"
            self.store.update(job)
            job_ids.append(job.id)
        stage("done", 100)
        return job_ids

    # ======================================================================
    # SINGLE-SHORT  (CLI test path: python -m studio.prepare)
    # ======================================================================
    def process_job(self, job: Job, *, auto_metadata: bool = True,
                    niche: str = "") -> Job:
        try:
            job.status = STATUS_PROCESSING
            job.stage = "probing"
            self.store.update(job)

            if self.cfg.ollama_enabled:
                self.llm.resolve_model()

            src = job.source_path
            job.duration = _probe_duration(src)

            job.stage = "transcribing"
            self.store.update(job)
            tr = transcribe(src, self.cfg.whisper_model,
                            self.cfg.whisper_device, self.cfg.whisper_enabled,
                            self.cfg.whisper_language)
            job.transcript = tr.text
            if tr.note:
                logger.info("transcription: %s", tr.note)

            if job.duration > self.cfg.keep_whole_if_under_seconds \
                    and tr.available:
                job.stage = "selecting highlight"
                self.store.update(job)
                floor = self._intro_floor(job.duration)
                span = self.llm.pick_segment(
                    tr.timestamped(), job.duration,
                    self.cfg.target_short_seconds,
                    self.cfg.min_short_seconds, self.cfg.max_short_seconds,
                    min_start=floor)
                if span is None:  # LLM failed -> default window AFTER intro
                    span = (floor, min(floor + self.cfg.target_short_seconds,
                                       job.duration))
                span = _enforce_bounds(span, self.cfg.min_short_seconds,
                                       self.cfg.max_short_seconds,
                                       job.duration)
                job.segment = span

            out_path = str(self.cfg.rendered_dir / f"{job.id}.mp4")
            self._render_short(job, src, tr.words, out_path)

            if auto_metadata and not job.meta.is_complete() \
                    and self.llm.available():
                job.stage = "writing the copy"
                self.store.update(job)
                language = self._metadata_language(tr.language)
                meta = self.llm.generate_copy(job.transcript, niche, language)
                if meta:
                    job.meta = meta

            if self.cfg.thumbs_enabled:
                job.stage = "composing thumbnail"
                self.store.update(job)
                self._make_thumbnail(job, src)

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

    # ======================================================================
    # PER-SHORT RENDER CHAIN (trim -> montage -> reframe -> captions -> overlay)
    # ======================================================================
    def _render_short(self, job: Job, source: str, words, out_path: str,
                      face_tracking: bool = True,
                      caption_pos: float | None = None) -> None:
        span = job.segment or (0.0, job.duration or _probe_duration(source))
        start, end = span
        # All intermediates are declared up front so the finally can sweep
        # whatever a mid-chain failure left behind.
        raw_path = str(self.cfg.rendered_dir / f"{job.id}_raw.mp4")
        captioned = str(self.cfg.rendered_dir / f"{job.id}_cap.mp4")
        overlaid = str(self.cfg.rendered_dir / f"{job.id}_sub.mp4")
        seg_path = self._trim(source, job.id, span)
        local_words = _offset_words(words, start, end)
        seg_dur = end - start

        try:
            # 1. silence-cut montage (jump cuts + alternating punch-in) -------
            if self.cfg.silence_cut_enabled and local_words:
                from . import montage
                intervals = montage.speech_intervals(
                    local_words, seg_dur, self.cfg.silence_min_gap,
                    self.cfg.silence_pad)
                if intervals:
                    job.stage = "cutting silences"
                    self.store.update(job)
                    cut_path = str(self.cfg.incoming_dir
                                   / f"{job.id}_cut.mp4")
                    try:
                        seg_dur = montage.cut_video(
                            seg_path, cut_path, intervals,
                            zoom_alternate=self.cfg.montage_zoom,
                            zoom=self.cfg.montage_zoom_factor)
                        local_words = montage.remap_words(local_words,
                                                          intervals)
                        Path(seg_path).unlink(missing_ok=True)
                        seg_path = cut_path
                    except Exception:
                        logger.warning("silence cut failed — using the "
                                       "uncut clip", exc_info=True)
                        Path(cut_path).unlink(missing_ok=True)

            # 2. 9:16 reframe (face-tracked, or static when disabled) ---------
            job.stage = "reframing to 9:16"
            self.store.update(job)
            self._reframe(seg_path, raw_path, face_tracking)

            # 3. karaoke captions ---------------------------------------------
            self._apply_captions(job, local_words, raw_path, captioned,
                                 caption_pos)

            # 4. subscribe-reminder overlay ------------------------------------
            final = captioned
            if self.cfg.subscribe_overlay_enabled:
                from .subscribe import apply_overlay
                job.stage = "adding subscribe reminder"
                self.store.update(job)
                if apply_overlay(self.cfg, captioned, overlaid):
                    Path(captioned).unlink(missing_ok=True)
                    final = overlaid

            shutil.move(final, out_path)
            job.output_path = out_path
        finally:
            # sweep every intermediate a failure may have stranded
            for p in (seg_path, str(self.cfg.incoming_dir /
                                    f"{job.id}_cut.mp4"),
                      raw_path, captioned, overlaid):
                Path(p).unlink(missing_ok=True)

    def _make_thumbnail(self, job: Job, source: str) -> None:
        """Compose the thumbnail from the ORIGINAL source frames (full
        resolution, pre-crop) within the picked span."""
        try:
            from .thumbnails import generate_thumbnail
            span = job.segment or (0.0, job.duration)
            headline = job.meta.thumbnail_headline or job.topic
            path = generate_thumbnail(self.cfg, job.id, source, span, headline)
            if path:
                job.thumb_path = path
        except Exception:
            logger.warning("thumbnail generation failed", exc_info=True)

    def rebuild_thumbnail(self, job_id: str, frame_t: float | None = None,
                          headline: str | None = None,
                          template: str | None = None) -> None:
        """Server-triggered recomposition (runs on the worker thread).
        Writes ONLY the columns it owns — an upload may finish concurrently
        on the publisher thread and must not be clobbered."""
        job = self.store.get(job_id)
        if not job:
            return
        self.store.patch(job_id, stage="recomposing thumbnail")
        try:
            from .thumbnails import rebuild_thumbnail
            path = rebuild_thumbnail(self.cfg, job.id, headline=headline,
                                     frame_t=frame_t, template=template)
            if path:
                self.store.patch(job_id, thumb_path=path)
                if headline is not None:
                    fresh = self.store.get(job_id)
                    if fresh:
                        fresh.meta.thumbnail_headline = headline
                        self.store.patch_meta(job_id, fresh.meta)
        except Exception:
            logger.warning("thumbnail rebuild failed", exc_info=True)
        self.store.patch(job_id, stage="")

    def regenerate_copy(self, job: Job, niche: str = "") -> VideoMeta:
        """Redraft title/description/headline for one short (server path)."""
        language = self._metadata_language("")
        meta = self.llm.generate_copy(job.transcript, niche, language)
        if meta:
            job.meta = meta
            self.store.patch_meta(job.id, meta)
        return job.meta

    def _metadata_language(self, transcript_lang: str) -> str:
        """Resolve the copy language: config override, else the detected
        spoken language mapped to a name (empty -> model matches transcript)."""
        cfg_lang = (self.cfg.metadata_language or "auto").strip()
        if cfg_lang.lower() not in {"auto", ""}:
            return cfg_lang
        return _LANG_NAMES.get((transcript_lang or "").lower(), "")

    def _intro_floor(self, duration: float) -> float:
        """Earliest second a short may start — NEVER cut from the long video's
        intro. The larger of an absolute floor and a fraction of the runtime,
        but never so large that a full-length clip wouldn't fit after it."""
        floor = max(float(self.cfg.intro_skip_seconds),
                    float(self.cfg.intro_skip_frac) * float(duration))
        room = max(0.0, float(duration) - float(self.cfg.max_short_seconds))
        return max(0.0, min(floor, room))

    # ======================================================================
    # UPLOAD (YouTube only)
    # ======================================================================
    def upload_job(self, job: Job, *, embed_thumb: bool | None = None) -> Job:
        """Runs on the single-slot publisher pool. The endpoint's checks are
        check-then-queue, so everything is RE-validated here on a fresh row —
        a double-tap or a second job queued behind an in-flight upload must
        not slip past the status / one_per_day gates."""
        import json as _json

        from .publishers import get_publisher
        from .publishers.base import PublishResult

        fresh = self.store.get(job.id)
        if not fresh or not fresh.output_path:
            return job
        already = (fresh.results or {}).get("youtube", {})
        if self.cfg.one_per_day and self.store.published_today() \
                and not already.get("ok"):
            self.store.patch(job.id, status=STATUS_DONE, stage="",
                             results_json=_json.dumps({"youtube": {
                                 "platform": "youtube", "ok": False,
                                 "error": "already uploaded today "
                                          "(one_per_day is on)"}}))
            return fresh
        job = fresh
        self.store.patch(job.id, status=STATUS_PUBLISHING,
                         stage="uploading to YouTube")

        embed = (self.cfg.embed_thumb_first_frame
                 if embed_thumb is None else embed_thumb)
        upload_path = job.output_path
        temp_embed = ""
        if embed and job.thumb_path and Path(job.thumb_path).exists():
            self.store.patch(job.id,
                             stage="baking thumbnail into the first frame")
            temp_embed = str(self.cfg.rendered_dir / f"{job.id}_upload.mp4")
            try:
                _embed_first_frame(job.output_path, job.thumb_path,
                                   temp_embed)
                upload_path = temp_embed
            except Exception:
                logger.warning("first-frame embed failed — uploading the "
                               "original", exc_info=True)
                Path(temp_embed).unlink(missing_ok=True)  # partial encode
                temp_embed = ""
                upload_path = job.output_path

        self.store.patch(job.id, stage="uploading to YouTube")
        try:
            pub = get_publisher("youtube", self.cfg, self.vault)
            result = pub.publish(upload_path, job.meta,
                                 privacy=job.privacy or None,
                                 thumb_path=job.thumb_path or None)
        except Exception as exc:  # pragma: no cover
            result = PublishResult.failure("youtube", str(exc))
        finally:
            if temp_embed:
                Path(temp_embed).unlink(missing_ok=True)

        job.results["youtube"] = result.to_dict()
        output_path = job.output_path
        if result.ok:
            self.store.mark_published_today(job.id)
            if self.cfg.move_uploaded_on_success:
                output_path = self._move_to_uploaded(job.output_path)
        # Narrow write: a concurrent thumbnail rebuild or meta save owns the
        # other columns.
        self.store.patch(job.id, status=STATUS_DONE, stage="",
                         youtube_id=result.video_id, thumb_api=result.thumb,
                         output_path=output_path,
                         results_json=_json.dumps(job.results))
        job.status = STATUS_DONE
        return job

    def _move_to_uploaded(self, output_path: str) -> str:
        """After a short is uploaded, move its file into the uploaded/ folder.
        Returns the (possibly unchanged) path."""
        src = Path(output_path)
        if not src.exists():
            return output_path
        self.cfg.uploaded_dir.mkdir(parents=True, exist_ok=True)
        dest = self.cfg.uploaded_dir / src.name
        try:
            if dest.resolve() == src.resolve():
                return output_path  # re-upload: already in uploaded/
            if dest.exists():
                dest.unlink()
            shutil.move(str(src), str(dest))
            return str(dest)
        except OSError as exc:  # pragma: no cover
            logger.warning("could not move %s to uploaded/: %s", src, exc)
            return output_path

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
        # Bound the trim: a hung ffmpeg on short #2 must not wedge the batch.
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL,
                           timeout=max(300.0, duration * 6))
        except Exception:
            Path(out).unlink(missing_ok=True)  # partial encode
            raise
        return out

    def _reframe(self, src: str, out: str, face_tracking: bool = True) -> None:
        # Imported lazily: pulls in OpenCV, only needed on the rendering host.
        from adaptive_reframe import AdaptiveReframePipeline

        if face_tracking:
            mode = (self.cfg.reframe_mode or "auto").strip().lower()
            force = None if mode in {"auto", ""} else mode
        else:
            # Face tracking off (e.g. a 2-person podcast where the tracked
            # crop jumps between speakers): blur_background shows the FULL
            # scene, statically, with blurred margins — nothing ever moves.
            force = "blur_background"
        AdaptiveReframePipeline().reframe(src, out, force_mode=force)

    def _apply_captions(self, job: Job, local_words, raw_path: str,
                        out_path: str,
                        caption_pos: float | None = None) -> None:
        """Burn captions onto the reframed clip; fall back to raw on a miss.
        ``local_words`` are already clip-local (offset + silence-remapped).
        ``caption_pos`` (percent up from the bottom — bigger = higher) wins
        over the config; 0/None falls back to ``caption_pos_pct`` and then
        the position preset."""
        burned = False
        if self.cfg.captions_enabled and local_words:
            job.stage = "burning captions"
            self.store.update(job)
            pos_pct = caption_pos or self.cfg.caption_pos_pct or None
            style = CaptionStyle(
                font=self.cfg.caption_font,
                fontsize=self.cfg.caption_fontsize,
                base_color=self.cfg.caption_base_color,
                highlight=self.cfg.caption_highlight,
                position=self.cfg.caption_position,
                pos_pct=pos_pct,
                max_words=self.cfg.caption_max_words,
            )
            burned = burn_captions(raw_path, local_words, out_path, style,
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


# ---------------------------------------------------------------------------
# module-level helpers (also imported by the CLI tools)
# ---------------------------------------------------------------------------
def _overlaps(start: float, end: float,
              spans: list[tuple[float, float]], tol: float = 0.5) -> bool:
    """True when [start, end] overlaps any span by more than ``tol`` seconds."""
    return any(min(end, b) - max(start, a) > tol for a, b in spans)


def _first_free_window(duration: float, floor: float, target_len: float,
                       used: list[tuple[float, float]]
                       ) -> tuple[float, float] | None:
    """First [start, start+target_len] window after ``floor`` that does not
    overlap any previously-used span (fallback path when the AI selector is
    unavailable). None when the video has no room left."""
    start = floor
    while start + 5.0 <= duration:
        end = min(start + target_len, duration)
        if end - start >= 5.0 and not _overlaps(start, end, used):
            return (start, end)
        start += 5.0
    return None


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

    Only the single-short CLI path still needs this numeric clamp — the
    semantic segmenter snaps to sentence boundaries instead.
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


def _embed_first_frame(video: str, thumb_jpg: str, out: str) -> None:
    """Prepend the thumbnail as the first ~0.1s of the video (the only
    Shorts-thumbnail mechanism that works on every account). One re-encode
    pass; audio start is padded with 100ms of silence via adelay."""
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
         "-show_format", video],
        capture_output=True, text=True, check=True, timeout=60)
    data = json.loads(probe.stdout)
    duration = float(data.get("format", {}).get("duration", 0.0))
    if duration + 0.1 > 180.0:
        raise RuntimeError("no headroom to embed a frame (>= 180s)")
    width = height = 0
    fps = "30"
    has_audio = False
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not width:
            width, height = int(s.get("width", 0)), int(s.get("height", 0))
            fps = s.get("r_frame_rate", "30/1")
        elif s.get("codec_type") == "audio":
            has_audio = True

    fc = (f"[0:v]scale={width}:{height},setsar=1,fps={fps}[intro];"
          f"[intro][1:v]concat=n=2:v=1:a=0[v]")
    if has_audio:
        fc += ";[1:a]adelay=100|100[a]"
    cmd = ["ffmpeg", "-y", "-loop", "1", "-t", "0.1", "-i", thumb_jpg,
           "-i", video, "-filter_complex", fc, "-map", "[v]"]
    if has_audio:
        cmd += ["-map", "[a]", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-movflags", "+faststart", out]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL,
                   timeout=max(300.0, duration * 8))


def _probe_duration(path: str) -> float:
    try:
        # A local path is made absolute so it can never be read as an ffprobe
        # option (defense-in-depth for arg injection); URLs pass through.
        if "://" not in path:
            path = os.path.abspath(path)
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
               "-show_format", path]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True,
                             timeout=60)
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception as exc:
        # Do NOT fail silently — a 0.0 duration produces broken 0-length shorts.
        logger.warning("ffprobe duration failed for %s: %s", path, exc)
        return 0.0
