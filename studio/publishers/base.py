"""Publisher interface shared by every platform."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..config import StudioConfig
from ..metadata import VideoMeta


@dataclass
class PublishResult:
    platform: str
    ok: bool
    url: str = ""          # link to the published post, when known
    error: str = ""        # human-readable failure reason
    needs_login: bool = False  # True -> run `python -m studio.login_setup <p>`
    log: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "ok": self.ok,
            "url": self.url,
            "error": self.error,
            "needs_login": self.needs_login,
            "log": self.log,
        }

    @classmethod
    def success(cls, platform: str, url: str = "", log=None) -> "PublishResult":
        return cls(platform, True, url=url, log=list(log or []))

    @classmethod
    def failure(cls, platform: str, error: str, *, needs_login: bool = False,
                log=None) -> "PublishResult":
        return cls(platform, False, error=error, needs_login=needs_login,
                   log=list(log or []))


class Publisher(Protocol):
    """A platform publisher. ``publish`` must never raise - return a
    :class:`PublishResult` describing success or failure instead, so one bad
    platform never aborts the others."""

    name: str

    def __init__(self, cfg: StudioConfig) -> None: ...

    def publish(self, video_path: str, meta: VideoMeta) -> PublishResult: ...
