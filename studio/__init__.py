"""YouTube Shorts Studio.

A self-hosted service that sits on top of the :mod:`adaptive_reframe` library
and turns a long YouTube video into ready-to-upload Shorts: semantically
selected segments, face-tracked 9:16 reframing, burned-in Arabic karaoke
captions, silence-cut montage pacing, a subscribe-reminder overlay, AI titles /
descriptions, and a composed thumbnail — all reviewed and uploaded from a
phone-friendly web page reachable anywhere through a free Cloudflare tunnel.

Layers
------
- :mod:`studio.config`      runtime configuration (paths, Ollama, YouTube).
- :mod:`studio.transcribe`  Whisper transcription -> word timestamps.
- :mod:`studio.segmenter`   semantic segment selection (map -> validate -> reduce).
- :mod:`studio.sponsorblock` crowd-sourced sponsor/intro masking.
- :mod:`studio.llm`         local Ollama / cloud LLM prompting.
- :mod:`studio.metadata`    title / description / headline value objects.
- :mod:`studio.montage`     silence cutting + punch-in zoom.
- :mod:`studio.subscribe`   the animated subscribe-reminder overlay.
- :mod:`studio.thumbnails`  frame pick -> cutout -> compose -> Arabic headline.
- :mod:`studio.jobs`        SQLite-backed job store.
- :mod:`studio.pipeline`    ingest -> transcribe -> select -> render -> upload.
- :mod:`studio.publishers`  YouTube via the official Data API v3.
- :mod:`studio.server`      FastAPI app + mobile web UI.
"""

from __future__ import annotations

__all__ = ["__version__"]
__version__ = "1.0.0"
