"""Instagram Reels publisher (Playwright).

Subclasses :class:`PlaywrightPublisher`, which owns the session chain, retries,
backoff, health and failure screenshots. This file is only the DOM steps:
``is_logged_in`` / ``_do_publish`` (Create -> Post -> Next -> caption -> Share,
then WAIT for the shared confirmation before reporting success) and an opt-in
``login_steps``.
"""

from __future__ import annotations

import logging

from .base import PublishResult
from .playwright_publisher import PlaywrightPublisher
from .session_provider import NeedsLogin

logger = logging.getLogger(__name__)


class InstagramPublisher(PlaywrightPublisher):
    name = "instagram"
    home_url = "https://www.instagram.com/"

    def is_logged_in(self, page) -> bool:
        try:
            if "/accounts/login" in page.url or "/accounts/onetap" in page.url:
                return False
            return page.locator(
                "svg[aria-label='New post'], svg[aria-label='Home'], "
                "[aria-label='New post']").count() > 0
        except Exception:
            return False

    def _do_publish(self, page, video_path, meta, log) -> PublishResult:
        caption = meta.caption_for("instagram")
        _dismiss_dialogs(page)
        log.append("opening Create")
        if not _open_create(page):
            return PublishResult.failure(self.name, "could not open Create", log=log)
        log.append("selecting file")
        page.wait_for_timeout(1500)
        page.locator("input[type=file]").first.set_input_files(
            video_path, timeout=30000)
        page.wait_for_timeout(6000)
        _click_text(page, ["OK"])                       # "video will be a reel"
        for _ in range(2):                              # crop -> Next, edit -> Next
            if _click_text(page, ["Next"]):
                page.wait_for_timeout(2500)
        log.append("writing caption")
        _set_caption(page, caption)
        log.append("sharing")
        if not _click_text(page, ["Share"]):
            return PublishResult.failure(self.name, "no Share button", log=log)
        if self._confirm_published(page, ["Share"],
                                   r"shared|has been shared|your reel|posted|"
                                   r"تم|نشر", 45000):
            log.append("confirmed shared")
            return PublishResult.success(self.name, url=self.home_url, log=log)
        return PublishResult.failure(self.name,
                                     "no share confirmation seen", log=log)

    def login_steps(self, page, creds) -> None:
        page.goto("https://www.instagram.com/accounts/login/",
                  wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        _dismiss_dialogs(page)
        try:
            page.fill("input[name=username]", creds["username"].reveal(),
                      timeout=10000)
            page.fill("input[name=password]", creds["password"].reveal())
            page.locator("button[type=submit]").first.click()
        except Exception as exc:
            raise NeedsLogin(f"instagram login form not found ({exc})")
        page.wait_for_timeout(5000)
        content = _content(page)
        if "/challenge" in page.url or "security code" in content \
                or "two-factor" in content or "6-digit" in content:
            _submit_2fa(page, creds)
        _dismiss_dialogs(page)


def _submit_2fa(page, creds) -> None:
    totp = creds["totp_secret"].reveal()
    if not totp:
        raise NeedsLogin("instagram 2FA required — add a TOTP secret or log in "
                         "on the host")
    from ..vault import totp_now
    try:
        page.fill("input[name=verificationCode], input[autocomplete=one-time-code]",
                  totp_now(totp), timeout=8000)
        _click_text(page, ["Confirm", "Continue", "Next"])
        page.wait_for_timeout(4000)
    except Exception:
        raise NeedsLogin("instagram 2FA — could not submit the code")


def _dismiss_dialogs(page) -> None:
    for label in ("Not Now", "Not now", "Allow all cookies", "Decline",
                  "Save Info", "Save info"):
        _click_text(page, [label], timeout=1500)


def _open_create(page) -> bool:
    for getter in (
        lambda: page.get_by_role("link", name="New post"),
        lambda: page.get_by_role("button", name="New post"),
        lambda: page.locator("svg[aria-label='New post']"),
        lambda: page.locator("[aria-label='New post']"),
    ):
        try:
            el = getter().first
            if el.count() > 0:
                el.click(timeout=8000)
                page.wait_for_timeout(1200)
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


def _content(page) -> str:
    try:
        return (page.content() or "").lower()
    except Exception:
        return ""
