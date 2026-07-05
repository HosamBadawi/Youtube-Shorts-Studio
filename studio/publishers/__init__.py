"""YouTube publishing via the official (free) Data API v3.

Use :func:`get_publisher` to obtain the publisher.
"""

from __future__ import annotations

from ..config import StudioConfig
from .base import PublishResult, Publisher


def get_publisher(platform: str, cfg: StudioConfig, vault=None) -> Publisher:
    if platform.lower() == "youtube":
        from .youtube import YouTubePublisher

        return YouTubePublisher(cfg, vault)
    raise ValueError(f"unknown platform: {platform}")


__all__ = ["get_publisher", "Publisher", "PublishResult"]
