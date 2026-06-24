"""Facebook Reels publisher via Playwright automation.

Posts a Reel from the saved ``sessions/facebook`` profile through the web Reels
composer at facebook.com/reels/create. Facebook frequently A/B-tests this flow,
so the locators below are deliberately forgiving and every step is logged.
"""

from __future__ import annotations

import logging

from ..config import StudioConfig
from ..metadata import VideoMeta
from .base import PublishResult
from .playwright_base import PlaywrightUnavailable, browser_session

logger = logging.getLogger(__name__)

REELS_URL = "https://www.facebook.com/reels/create"


class FacebookPublisher:
    name = "facebook"

    def __init__(self, cfg: StudioConfig) -> None:
        self.cfg = cfg

    def publish(self, video_path: str, meta: VideoMeta) -> PublishResult:
        log: list[str] = []
        # Facebook shows title/description together; reuse the caption.
        caption = meta.caption_for("facebook")
        try:
            with browser_session(self.cfg.session_dir_for("facebook"),
                                 headless=self.cfg.playwright_headless,
                                 viewport=(1280, 900)) as page:
                page.goto(REELS_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)

                if "/login" in page.url:
                    return PublishResult.failure(
                        self.name, "not logged in", needs_login=True, log=log)
                _dismiss_cookies(page)

                log.append("selecting file")
                page.locator("input[type=file]").first.set_input_files(
                    video_path, timeout=30000)
                page.wait_for_timeout(7000)  # upload + preview render

                # Composer is a multi-step "Next" wizard before the description.
                for _ in range(2):
                    if _click_text(page, ["Next"], timeout=4000):
                        page.wait_for_timeout(2500)

                log.append("writing description")
                _set_caption(page, caption)

                log.append("publishing")
                if not _click_text(page, ["Publish", "Share now", "Post"]):
                    return PublishResult.failure(
                        self.name, "could not find the Publish button", log=log)
                page.wait_for_timeout(9000)
                return PublishResult.success(self.name,
                                             url="https://www.facebook.com/", log=log)
        except PlaywrightUnavailable as exc:
            return PublishResult.failure(self.name, str(exc), log=log)
        except Exception as exc:  # pragma: no cover
            return PublishResult.failure(self.name, f"{type(exc).__name__}: {exc}",
                                         log=log)


def _dismiss_cookies(page) -> None:
    _click_text(page, ["Allow all cookies", "Only allow essential cookies",
                       "Decline optional cookies"], timeout=2500)


def _set_caption(page, caption: str) -> None:
    for sel in ("div[contenteditable=true][role=textbox]",
                "div[aria-label*='description'][contenteditable=true]",
                "div[contenteditable=true]"):
        try:
            box = page.locator(sel).first
            if box.count() > 0:
                box.click()
                box.type(caption[:2150], delay=5)
                return
        except Exception:
            continue


def _click_text(page, labels, timeout: int = 8000) -> bool:
    for label in labels:
        for getter in (
            lambda l=label: page.get_by_role("button", name=l, exact=True),
            lambda l=label: page.get_by_text(l, exact=True),
        ):
            try:
                el = getter().first
                if el.count() > 0:
                    el.click(timeout=timeout)
                    return True
            except Exception:
                continue
    return False
