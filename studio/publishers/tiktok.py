"""TikTok publisher via Playwright automation of the web upload studio.

Uses the saved ``sessions/tiktok`` browser profile. Selectors target
tiktok.com/upload as of 2025; if TikTok reshuffles its DOM you may need to
adjust the locator fallbacks below - that's the known cost of web automation.
"""

from __future__ import annotations

import logging

from ..config import StudioConfig
from ..metadata import VideoMeta
from .base import PublishResult
from .playwright_base import PlaywrightUnavailable, browser_session

logger = logging.getLogger(__name__)

UPLOAD_URL = "https://www.tiktok.com/upload?lang=en"


class TikTokPublisher:
    name = "tiktok"

    def __init__(self, cfg: StudioConfig) -> None:
        self.cfg = cfg

    def publish(self, video_path: str, meta: VideoMeta) -> PublishResult:
        log: list[str] = []
        caption = meta.caption_for("tiktok")
        try:
            with browser_session(self.cfg.session_dir_for("tiktok"),
                                 headless=self.cfg.playwright_headless,
                                 viewport=(1280, 900)) as page:
                page.goto(UPLOAD_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(4000)

                if _looks_logged_out(page):
                    return PublishResult.failure(
                        self.name, "not logged in", needs_login=True, log=log)

                # The upload UI lives inside an iframe on some locales.
                frame = _upload_frame(page)
                log.append("locating file input")
                file_input = frame.locator("input[type=file]").first
                file_input.set_input_files(video_path, timeout=30000)
                log.append("file selected; waiting for processing")
                frame.wait_for_timeout(8000)

                _set_caption(frame, caption, log)

                log.append("clicking Post")
                posted = _click_post(frame)
                if not posted:
                    return PublishResult.failure(
                        self.name, "could not find the Post button", log=log)
                page.wait_for_timeout(8000)
                return PublishResult.success(self.name,
                                             url="https://www.tiktok.com/", log=log)
        except PlaywrightUnavailable as exc:
            return PublishResult.failure(self.name, str(exc), log=log)
        except Exception as exc:  # pragma: no cover
            return PublishResult.failure(self.name, f"{type(exc).__name__}: {exc}",
                                         log=log)


def _upload_frame(page):
    for f in page.frames:
        try:
            if f.locator("input[type=file]").count() > 0:
                return f
        except Exception:
            continue
    return page


def _looks_logged_out(page) -> bool:
    try:
        txt = (page.content() or "").lower()
    except Exception:
        return False
    return "log in to tiktok" in txt or "/login" in page.url


def _set_caption(frame, caption: str, log: list[str]) -> None:
    selectors = [
        "div[contenteditable=true]",
        "div[data-text=true]",
        "[data-e2e=caption-input]",
    ]
    for sel in selectors:
        try:
            box = frame.locator(sel).first
            if box.count() > 0:
                box.click()
                # Clear any auto-filled filename caption, then type ours.
                box.press("Control+A")
                box.press("Delete")
                box.type(caption[:2150], delay=8)
                log.append("caption set")
                return
        except Exception:
            continue
    log.append("warning: caption field not found")


def _click_post(frame) -> bool:
    for getter in (
        lambda: frame.get_by_role("button", name="Post"),
        lambda: frame.locator("button:has-text('Post')"),
        lambda: frame.locator("[data-e2e=post_video_button]"),
    ):
        try:
            btn = getter().first
            if btn.count() > 0:
                btn.click(timeout=15000)
                return True
        except Exception:
            continue
    return False
