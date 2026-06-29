"""Instagram Reels publisher (Playwright).

Subclasses :class:`PlaywrightPublisher`, which owns the session chain, retries,
backoff, health and failure screenshots. This file is only the DOM steps:
``is_logged_in`` / ``_do_publish`` (Create -> Post -> Next -> caption -> Share,
then WAIT for the shared confirmation before reporting success) and an opt-in
``login_steps``.
"""

from __future__ import annotations

import logging
import time

from .base import PublishResult
from .playwright_publisher import PlaywrightPublisher, code_for as _code_for
from .session_provider import NeedsLogin

logger = logging.getLogger(__name__)


class InstagramPublisher(PlaywrightPublisher):
    name = "instagram"
    home_url = "https://www.instagram.com/"

    def is_logged_in(self, page) -> bool:
        # Cookie check first — robust in headless (the DOM varies). Instagram
        # sets sessionid + ds_user_id once you're logged in.
        try:
            for c in page.context.cookies():
                if c.get("name") in ("sessionid", "ds_user_id") and c.get("value"):
                    return True
        except Exception:
            pass
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
        log.append("uploading (waiting for it to finish)")
        _click_text(page, ["OK"], timeout=5000)         # "video will be a reel"
        # The crop screen's Next only appears once the upload/processing is done —
        # wait it out so a slow upload doesn't skip straight past the flow.
        self.wait_uploaded(
            lambda: page.get_by_role("button", name="Next"),
            log, "instagram upload")
        for _ in range(2):                              # crop -> Next, edit -> Next
            if _click_text(page, ["Next"]):
                page.wait_for_timeout(2500)
        log.append("writing caption")
        _set_caption(page, caption)
        if self.dry_run:
            return self.dry_stop(page, log)
        log.append("sharing")
        if not _click_text(page, ["Share"]):
            return PublishResult.failure(self.name, "no Share button", log=log)
        # Clicking Share starts the REAL upload ("Sharing…") — on a slow line that
        # runs for minutes. Wait for it to FINISH (returning early closes the
        # browser and aborts the post — the false-"success" we hit before).
        if _wait_sharing_done(page, self.cfg.publish_upload_timeout, log):
            log.append("confirmed shared")
            return PublishResult.success(self.name, url=self.home_url, log=log)
        return PublishResult.failure(
            self.name, "the reel did not finish sharing in time", log=log)

    def login_steps(self, page, creds, get_code=None) -> None:
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
            _submit_2fa(page, creds, get_code)
        _dismiss_dialogs(page)


def _submit_2fa(page, creds, get_code=None) -> None:
    code = _code_for(creds, get_code,
                     "Instagram sent a 6-digit login code — enter it")
    if not code:
        raise NeedsLogin("instagram 2FA required — add a TOTP secret or log in "
                         "on the host")
    try:
        page.fill("input[name=verificationCode], input[autocomplete=one-time-code]",
                  code, timeout=8000)
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


def _sharing(page) -> bool:
    """True while Instagram is still uploading/finalizing the reel."""
    for t in ("Sharing", "Posting"):
        try:
            if page.get_by_text(t, exact=False).count() > 0:
                return True
        except Exception:
            pass
    return False


def _wait_sharing_done(page, timeout_s: float, log: list[str]) -> bool:
    """Wait for the post-Share upload to complete: the 'Sharing…' indicator
    appears, then disappears. Generous timeout for slow upload speeds."""
    for _ in range(20):                        # let 'Sharing…' appear (~30s)
        if _sharing(page):
            break
        page.wait_for_timeout(1500)
    deadline = time.time() + max(60.0, float(timeout_s))
    while time.time() < deadline:
        page.wait_for_timeout(5000)
        if not _sharing(page):
            log.append("sharing finished")
            page.wait_for_timeout(4000)        # settle before closing
            return True
    return False
