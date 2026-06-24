"""Per-platform publishers.

YouTube uses the official (free) Data API v3. Instagram, TikTok and Facebook
have no free upload API for personal accounts, so they are driven with
Playwright browser automation against saved login sessions - which is exactly
the "web automation" approach the project owner opted into.

Use :func:`get_publisher` to obtain the right implementation by name.
"""

from __future__ import annotations

from ..config import StudioConfig
from .base import PublishResult, Publisher


def get_publisher(platform: str, cfg: StudioConfig) -> Publisher:
    platform = platform.lower()
    if platform == "youtube":
        from .youtube import YouTubePublisher

        return YouTubePublisher(cfg)
    if platform == "instagram":
        from .instagram import InstagramPublisher

        return InstagramPublisher(cfg)
    if platform == "tiktok":
        from .tiktok import TikTokPublisher

        return TikTokPublisher(cfg)
    if platform == "facebook":
        from .facebook import FacebookPublisher

        return FacebookPublisher(cfg)
    raise ValueError(f"unknown platform: {platform}")


__all__ = ["get_publisher", "Publisher", "PublishResult"]
