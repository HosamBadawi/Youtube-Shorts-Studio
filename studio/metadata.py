"""Value objects for the text that ships with a Short.

Kept stdlib-only and JSON-friendly so it crosses the web boundary and the
SQLite store without any framework coupling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_TITLE_MAX = 100          # YouTube hard-caps titles at 100 chars
_DESCRIPTION_MAX = 5000   # YouTube description limit
_HASHTAG_MAX = 15


def normalize_hashtags(tags: list[str] | str | None) -> list[str]:
    """Accept a list or a free-form string; return clean ``#word`` tokens."""
    if not tags:
        return []
    if isinstance(tags, str):
        raw = tags.replace(",", " ").split()
    else:
        raw = []
        for t in tags:
            raw.extend(str(t).replace(",", " ").split())
    out: list[str] = []
    seen = set()
    for tok in raw:
        tok = tok.strip().lstrip("#").strip()
        if not tok:
            continue
        tag = "#" + "".join(ch for ch in tok if ch.isalnum() or ch in "_")
        key = tag.lower()
        if len(tag) > 1 and key not in seen:
            seen.add(key)
            out.append(tag)
    return out


@dataclass
class VideoMeta:
    """Everything the user reviews before a Short is uploaded."""

    title: str = ""
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    # The 3-6 word Arabic headline drawn onto the thumbnail (NOT the title).
    thumbnail_headline: str = ""
    source: str = "manual"  # "manual" | "ai" | "ai+manual"

    # --- what actually gets sent to YouTube ----------------------------------
    def youtube_title(self) -> str:
        title = self.title.strip()
        if "#shorts" not in title.lower():
            # Harmless legacy signal (classification is duration+aspect now).
            title = (title + " #Shorts").strip()
        return title[:_TITLE_MAX]

    def youtube_description(self) -> str:
        body = self.description.strip()
        tags = normalize_hashtags(self.hashtags)[:_HASHTAG_MAX]
        if not any(t.lower() == "#shorts" for t in tags):
            tags = ["#Shorts", *tags]
        text = body
        if tags:
            text = (body + "\n\n" + " ".join(tags)).strip()
        return text[:_DESCRIPTION_MAX]

    # --- (de)serialization --------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "hashtags": self.hashtags,
            "thumbnail_headline": self.thumbnail_headline,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "VideoMeta":
        d = d or {}
        # Rows written before the redesign stored the description as "caption".
        description = str(d.get("description") or d.get("caption") or "")
        return cls(
            title=str(d.get("title", "")),
            description=description,
            hashtags=normalize_hashtags(d.get("hashtags")),
            thumbnail_headline=str(d.get("thumbnail_headline", "")),
            source=str(d.get("source", "manual")),
        )

    def is_complete(self) -> bool:
        return bool(self.title.strip() and self.description.strip())
