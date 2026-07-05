"""Facebook Reels publisher (Playwright).

Subclasses :class:`PlaywrightPublisher`. NOTE (2026): posting Reels from a
personal profile via desktop web is increasingly restricted — the durable path
is a **Page** via Meta Business Suite. We keep ``/reels/create`` as a forgiving
fallback; for Pages, log in (Edge profile / saved session) to an account that
manages the target Page.
"""

from __future__ import annotations

import logging
import time

from .base import PublishResult
from .playwright_publisher import PlaywrightPublisher, code_for as _code_for
from .session_provider import NeedsLogin

logger = logging.getLogger(__name__)

REELS_URL = "https://www.facebook.com/reels/create"


class FacebookPublisher(PlaywrightPublisher):
    name = "facebook"
    home_url = "https://www.facebook.com/"

    def is_logged_in(self, page) -> bool:
        try:
            for c in page.context.cookies():
                if c.get("name") == "c_user" and c.get("value"):
                    return True
        except Exception:
            pass
        return "/login" not in page.url

    def _open_reel_composer(self, page, log) -> None:
        """Open the Page's OWN reel composer so the reel is authored BY THE PAGE.

        Going to /reels/create composes as the PERSONAL profile (verified: the
        composer showed the personal avatar + a 'Friends' audience, and nothing
        landed on the Page). The reliable route is the Page's composer row
        (Live video | Photo/video | Reel) while in Manage-Page mode — clicking
        'Reel' there authors as the Page. Falls back to /reels/create only when no
        Page URL is configured."""
        url = (self.cfg.facebook_page_url or "").strip()
        if url:
            log.append("opening Page composer")
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)
                _dismiss_cookies(page)
                for getter in (
                    lambda: page.get_by_role("button", name="Reel", exact=True),
                    lambda: page.get_by_role("link", name="Reel", exact=True),
                    lambda: page.get_by_text("Reel", exact=True),
                    lambda: page.locator("[aria-label='Reel']"),
                ):
                    try:
                        el = getter().first
                        if el.count() > 0:
                            el.click(timeout=8000)
                            page.wait_for_timeout(6000)
                            _dismiss_cookies(page)
                            log.append("Page reel composer open (as the Page)")
                            return
                    except Exception:
                        continue
                log.append("Page 'Reel' button not found — falling back")
            except Exception:
                pass
        page.goto(REELS_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        _dismiss_cookies(page)

    def _do_publish(self, page, video_path, meta, log) -> PublishResult:
        # FB reel flow (verified): attach -> Next -> [description box] -> Next ->
        # [audience + Post]. The caption MUST be typed on the screen after the
        # FIRST Next (the Post screen has no description box).
        caption = meta.caption_for("facebook")
        self._open_reel_composer(page, log)
        log.append("selecting file")
        page.locator("input[type=file]").first.set_input_files(
            video_path, timeout=30000)
        # The first Next only enables once the upload has progressed — wait it out
        # for slow connections, then advance to the description screen.
        log.append("uploading (waiting for it to finish)")
        self.wait_uploaded(
            lambda: page.get_by_role("button", name="Next", exact=True),
            log, "facebook upload")
        if not _click_text(page, ["Next"], timeout=10000):  # upload -> description
            return PublishResult.failure(
                self.name, "reel upload didn't reach the Next step", log=log)
        page.wait_for_timeout(3500)
        log.append("writing description")
        _set_caption(page, caption)
        if not _wait_click(page, ["Next"], tries=8):         # description -> share
            return PublishResult.failure(self.name, "no second Next", log=log)
        page.wait_for_timeout(3000)
        if self.dry_run:
            return self.dry_stop(page, log)
        # FB keeps Post inert until the (slow) upload to its servers finishes, so
        # click it and wait for the composer to actually submit (navigate away),
        # re-clicking until it takes. Returning early aborts the upload.
        log.append("posting (waiting for the upload to finish)")
        if _wait_posted(page, self.cfg.publish_upload_timeout, log):
            log.append("confirmed published")
            return PublishResult.success(self.name, url=self.home_url, log=log)
        return PublishResult.failure(
            self.name, "the reel did not submit in time", log=log)

    def login_steps(self, page, creds, get_code=None) -> None:
        page.goto("https://www.facebook.com/login", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        _dismiss_cookies(page)
        try:
            page.fill("input[name=email], #email", creds["username"].reveal(),
                      timeout=10000)
            page.fill("input[name=pass], #pass", creds["password"].reveal())
            page.locator("button[name=login], [data-testid=royal_login_button]"
                         ).first.click()
        except Exception as exc:
            raise NeedsLogin(f"facebook login form not found ({exc})")
        page.wait_for_timeout(5000)
        if "checkpoint" in page.url or "two-factor" in _content(page) \
                or "login code" in _content(page):
            code = _code_for(creds, get_code,
                             "Facebook needs your 6-digit login code")
            if not code:
                raise NeedsLogin("facebook 2FA required — add a TOTP secret or "
                                 "log in on the host")
            try:
                page.fill("input[name=approvals_code], input[autocomplete="
                          "one-time-code]", code, timeout=8000)
                _click_text(page, ["Continue", "Submit", "Next"])
                page.wait_for_timeout(4000)
            except Exception:
                raise NeedsLogin("facebook 2FA — could not submit the code")


def _dismiss_cookies(page) -> None:
    _click_text(page, ["Allow all cookies", "Only allow essential cookies",
                       "Decline optional cookies"], timeout=2500)


def _wait_click(page, labels, tries: int = 6, gap: int = 1500) -> bool:
    """Click one of ``labels`` once it appears/enables (FB enables Next only after
    the upload progresses), retrying up to ``tries`` times."""
    for _ in range(tries):
        if _click_text(page, labels, timeout=3000):
            return True
        page.wait_for_timeout(gap)
    return False


def _wait_posted(page, timeout_s: float, log: list[str]) -> bool:
    """Click Post and wait until the composer is actually LEFT (= submitted). FB
    keeps the button inert while the slow upload runs, so re-click until it takes.

    The ONLY trustworthy signal is the composer navigating away from the reel
    composer URL. A loose 'published' substring is NOT proof: it matched FB help
    text (e.g. the "Videos are now reels / published" notice) and produced a
    false success while the reel was never submitted. We therefore require either
    a real navigation away, or an EXACT-match success toast — never a substring.
    """
    deadline = time.time() + max(120.0, float(timeout_s))
    while time.time() < deadline:
        _click_text(page, ["Post", "Publish", "Share now"], timeout=6000)
        page.wait_for_timeout(10000)
        try:
            u = page.url
        except Exception:
            return True                        # FB navigated/closed the context
        if "/reel/" not in u and "/reels/create" not in u:
            log.append("left composer — submitted")
            page.wait_for_timeout(5000)        # settle before closing
            return True
        # Secondary, strict signal: an EXACT success toast (no substring match).
        for t in ("Your reel is being shared.", "Your reel has been shared.",
                  "Your reel is now live."):
            try:
                if page.get_by_text(t, exact=True).count() > 0:
                    log.append("posted toast seen (exact)")
                    page.wait_for_timeout(4000)
                    return True
            except Exception:
                pass
    return False


def _set_caption(page, caption: str) -> None:
    # ONE atomic insert, never per-key typing — '#' keystrokes pop the hashtag
    # autocomplete, which steals the caret mid-type and mangles the text (seen
    # live on TikTok; same editor pattern here).
    for sel in ("div[contenteditable=true][role=textbox]",
                "div[aria-label*='description'][contenteditable=true]",
                "div[role=textbox]",
                "div[contenteditable=true]"):
        try:
            box = page.locator(sel).first
            if box.count() > 0:
                box.click()
                page.keyboard.insert_text(caption[:2150])
                page.wait_for_timeout(500)
                # NO Escape: it can pop a "discard" dialog on the composer.
                # insert_text is atomic, so no dropdown steals the caret anyway.
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
