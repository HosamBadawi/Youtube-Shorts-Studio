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


def get_publisher(platform: str, cfg: StudioConfig, vault=None) -> Publisher:
    platform = platform.lower()
    # Official Meta Graph API (when enabled) for IG + FB Page — reliable, no
    # browser, no CAPTCHA. Routed PER PLATFORM: each one uses the API only when
    # its id is configured, so e.g. Facebook can go API while Instagram (not yet
    # linked to the Page) keeps the working browser automation.
    if getattr(cfg, "meta_api_enabled", False):
        from .meta_api import FacebookApiPublisher, InstagramApiPublisher

        if platform == "facebook" and (cfg.facebook_page_id or "").strip():
            return FacebookApiPublisher(cfg, vault)
        if platform == "instagram" and (cfg.instagram_business_id or "").strip():
            return InstagramApiPublisher(cfg, vault)
    if platform == "youtube":
        from .youtube import YouTubePublisher

        return YouTubePublisher(cfg, vault)
    if platform == "instagram":
        from .instagram import InstagramPublisher

        return InstagramPublisher(cfg, vault)
    if platform == "tiktok":
        from .tiktok import TikTokPublisher

        return TikTokPublisher(cfg, vault)
    if platform == "facebook":
        from .facebook import FacebookPublisher

        return FacebookPublisher(cfg, vault)
    raise ValueError(f"unknown platform: {platform}")


__all__ = ["get_publisher", "Publisher", "PublishResult"]
