"""Instagram Reels publisher via Playwright automation of instagram.com.

Uses the saved ``sessions/instagram`` profile. Instagram's web "Create" flow is
a multi-step modal (select file -> crop/next -> next -> caption -> share). The
steps below click through it with text-based locators that tolerate minor copy
changes; verify against the live site if a step times out.
"""

from __future__ import annotations

import logging

from ..config import StudioConfig
from ..metadata import VideoMeta
from .base import PublishResult
from .playwright_base import PlaywrightUnavailable, browser_session

logger = logging.getLogger(__name__)

HOME_URL = "https://www.instagram.com/"


class InstagramPublisher:
    name = "instagram"

    def __init__(self, cfg: StudioConfig) -> None:
        self.cfg = cfg

    def publish(self, video_path: str, meta: VideoMeta) -> PublishResult:
        log: list[str] = []
        caption = meta.caption_for("instagram")
        try:
            with browser_session(self.cfg.session_dir_for("instagram"),
                                 headless=self.cfg.playwright_headless,
                                 viewport=(1280, 900)) as page:
                page.goto(HOME_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(4000)

                if "/accounts/login" in page.url:
                    return PublishResult.failure(
                        self.name, "not logged in", needs_login=True, log=log)

                _dismiss_dialogs(page, log)

                log.append("opening Create")
                if not _open_create(page):
                    return PublishResult.failure(
                        self.name, "could not open the Create dialog", log=log)

                log.append("selecting file")
                page.wait_for_timeout(1500)
                page.locator("input[type=file]").first.set_input_files(
                    video_path, timeout=30000)
                page.wait_for_timeout(6000)  # video processing / OK-for-Reel modal
                _click_text(page, ["OK"])  # "Reels" confirmation if shown

                # Crop step -> Next, Edit step -> Next.
                for _ in range(2):
                    if _click_text(page, ["Next"]):
                        page.wait_for_timeout(2500)

                log.append("writing caption")
                _set_caption(page, caption)

                log.append("sharing")
                if not _click_text(page, ["Share"]):
                    return PublishResult.failure(
                        self.name, "could not find the Share button", log=log)
                page.wait_for_timeout(8000)
                return PublishResult.success(self.name, url=HOME_URL, log=log)
        except PlaywrightUnavailable as exc:
            return PublishResult.failure(self.name, str(exc), log=log)
        except Exception as exc:  # pragma: no cover
            return PublishResult.failure(self.name, f"{type(exc).__name__}: {exc}",
                                         log=log)


def _dismiss_dialogs(page, log) -> None:
    for label in ("Not Now", "Not now", "Allow all cookies", "Decline"):
        _click_text(page, [label], timeout=2000)


def _open_create(page) -> bool:
    for getter in (
        lambda: page.get_by_role("link", name="New post"),
        lambda: page.get_by_role("button", name="New post"),
        lambda: page.locator("svg[aria-label='New post']"),
        lambda: page.locator("a[href='#']:has(svg[aria-label='New post'])"),
    ):
        try:
            el = getter().first
            if el.count() > 0:
                el.click(timeout=8000)
                page.wait_for_timeout(1200)
                # A submenu may offer "Post"; click it if present.
                _click_text(page, ["Post"], timeout=2000)
                return True
        except Exception:
            continue
    return False


def _set_caption(page, caption: str) -> None:
    for sel in ("textarea[aria-label*='caption']",
                "div[aria-label*='caption'][contenteditable=true]",
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
