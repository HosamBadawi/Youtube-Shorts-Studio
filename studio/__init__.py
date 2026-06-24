"""Daily Shorts Studio.

A small self-hostable service that sits on top of the :mod:`adaptive_reframe`
library and turns "I have one clip for today" into "it is reframed to 9:16,
captioned, and published to Facebook / Instagram / TikTok / YouTube Shorts" -
all driven from a phone-friendly web page you can reach from anywhere through a
free Cloudflare tunnel.

Layers
------
- :mod:`studio.config`      runtime configuration (paths, Ollama, platforms).
- :mod:`studio.transcribe`  Whisper transcription -> timestamped segments.
- :mod:`studio.llm`         local Ollama: pick the best segment + write metadata.
- :mod:`studio.metadata`    title / caption / hashtag value objects.
- :mod:`studio.jobs`        SQLite-backed job store (one publish per day guard).
- :mod:`studio.pipeline`    ingest -> transcribe -> segment -> reframe -> publish.
- :mod:`studio.publishers`  YouTube (official API) + IG/TikTok/FB (Playwright).
- :mod:`studio.server`      FastAPI app + mobile web UI.
"""

from __future__ import annotations

__all__ = ["__version__"]
__version__ = "0.1.0"
