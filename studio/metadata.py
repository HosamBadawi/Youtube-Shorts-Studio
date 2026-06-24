"""Value objects for the text that ships with a video.

Kept stdlib-only and JSON-friendly so it crosses the web boundary and the
SQLite store without any framework coupling. Per-platform overrides let Ollama
(or you, by hand) tailor wording while a single base caption covers the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Length ceilings that keep us safely inside each platform's limits.
_CAPTION_LIMITS = {
    "youtube": 5000,
    "facebook": 2200,
    "instagram": 2200,
    "tiktok": 2200,
}
_HASHTAG_CAP = {"youtube": 15, "facebook": 10, "instagram": 30, "tiktok": 8}


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
    """The base caption set, plus optional per-platform overrides.

    ``overrides[platform]`` may carry any of ``title`` / ``caption`` /
    ``hashtags``; missing keys fall back to the base value.
    """

    title: str = ""
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    source: str = "manual"  # "manual" | "ollama" | "ollama+manual"

    # --- resolution per platform -------------------------------------------
    def title_for(self, platform: str) -> str:
        title = str(self.overrides.get(platform, {}).get("title", self.title)).strip()
        if platform == "youtube" and "#shorts" not in title.lower():
            # The single strongest signal to YouTube that this is a Short.
            title = (title + " #Shorts").strip()
        return title[:100]  # YouTube hard-caps titles at 100 chars

    def caption_for(self, platform: str) -> str:
        ov = self.overrides.get(platform, {})
        body = str(ov.get("caption", self.caption)).strip()
        tags = normalize_hashtags(ov.get("hashtags", self.hashtags))
        tags = tags[: _HASHTAG_CAP.get(platform, 10)]
        if platform == "youtube" and not any(t.lower() == "#shorts" for t in tags):
            tags = ["#Shorts", *tags]
        text = body
        if tags:
            text = (body + "\n\n" + " ".join(tags)).strip()
        return text[: _CAPTION_LIMITS.get(platform, 2200)]

    # --- (de)serialization --------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "caption": self.caption,
            "hashtags": self.hashtags,
            "overrides": self.overrides,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "VideoMeta":
        d = d or {}
        return cls(
            title=str(d.get("title", "")),
            caption=str(d.get("caption", "")),
            hashtags=normalize_hashtags(d.get("hashtags")),
            overrides=dict(d.get("overrides") or {}),
            source=str(d.get("source", "manual")),
        )

    def is_complete(self) -> bool:
        return bool(self.title.strip() and self.caption.strip())
