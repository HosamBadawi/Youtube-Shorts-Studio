"""SponsorBlock lookup: which parts of a YouTube video are junk (sponsor reads,
intros, outros, self-promo) according to crowd-sourced data.

Uses the privacy-preserving k-anonymity endpoint: only the first 4 hex chars of
sha256(videoID) leave this machine; the server returns every video matching the
prefix and we filter locally, so it never learns which video was processed.

Data © the SponsorBlock project (https://sponsor.ajay.app), licensed
CC BY-NC-SA 4.0 — used here for personal, self-hosted processing.

stdlib only. Any failure (offline, API change, no data) returns [] — the
segmenter's own LLM junk classification still covers intros/outros.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

_API = "https://sponsor.ajay.app/api/skipSegments"
_CATEGORIES = ["sponsor", "selfpromo", "interaction", "intro", "outro",
               "preview"]

_ID_PATTERNS = (
    re.compile(r"[?&]v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})"),
)


def extract_video_id(url: str) -> str | None:
    """Pull the 11-char YouTube video id out of any common URL form."""
    url = (url or "").strip()
    for pat in _ID_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def fetch_junk_segments(video_id: str, timeout: float = 4.0
                        ) -> list[tuple[float, float]]:
    """Return merged, sorted (start, end) junk ranges for ``video_id``.

    Empty list on any error or when the (often small/Arabic) channel simply has
    no SponsorBlock coverage — callers must not treat [] as "clean video".
    """
    if not video_id:
        return []
    prefix = hashlib.sha256(video_id.encode("utf-8")).hexdigest()[:4]
    url = (f"{_API}/{prefix}?categories="
           + urllib.parse.quote(json.dumps(_CATEGORIES)))
    try:
        req = urllib.request.Request(url, headers={"User-Agent":
                                                   "youtube-shorts-studio"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.info("SponsorBlock lookup skipped (%s)", exc)
        return []

    spans: list[tuple[float, float]] = []
    for video in body if isinstance(body, list) else []:
        if video.get("videoID") != video_id:
            continue
        for seg in video.get("segments", []) or []:
            span = seg.get("segment") or []
            try:
                s, e = float(span[0]), float(span[1])
            except (IndexError, TypeError, ValueError):
                continue
            if e > s >= 0:
                spans.append((s, e))
    merged = _merge(spans)
    if merged:
        logger.info("SponsorBlock: %d junk range(s) for %s", len(merged),
                    video_id)
    return merged


def _merge(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge overlapping/adjacent ranges into a sorted minimal set."""
    out: list[tuple[float, float]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1] + 0.5:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out
