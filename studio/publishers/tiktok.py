"""TikTok publisher (Playwright) — web upload studio.

Subclasses :class:`PlaywrightPublisher`. TikTok aggressively challenges
automated logins (CAPTCHA/device check), so ``login_steps`` is intentionally NOT
implemented — credentials_login surfaces ``needs_login`` and the reliable paths
are the Edge profile or a saved session captured on the host.
"""

from __future__ import annotations

import logging
import time

from .base import PublishResult
from .playwright_publisher import PlaywrightPublisher, code_for as _code_for
from .session_provider import NeedsLogin

logger = logging.getLogger(__name__)

UPLOAD_URL = "https://www.tiktok.com/upload?lang=en"
LOGIN_URL = "https://www.tiktok.com/login/phone-or-email/email"


class TikTokPublisher(PlaywrightPublisher):
    name = "tiktok"
    home_url = "https://www.tiktok.com/"

    def is_logged_in(self, page) -> bool:
        try:
            for c in page.context.cookies():
                if c.get("name") == "sessionid" and c.get("value"):
                    return True
        except Exception:
            pass
        try:
            return "/login" not in page.url and \
                "log in to tiktok" not in (page.content() or "").lower()
        except Exception:
            return False

    def login_steps(self, page, creds, get_code=None) -> None:
        """Best-effort email/username login. TikTok frequently shows a slider
        CAPTCHA on automated logins — when that happens this won't complete and
        the caller reports ``needs_captcha`` (do the first login once at the PC)."""
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        try:
            page.fill("input[name=username]", creds["username"].reveal(),
                      timeout=10000)
            page.fill("input[type=password]", creds["password"].reveal())
            page.locator("button[type=submit]").first.click()
        except Exception as exc:
            raise NeedsLogin(f"tiktok login form not found ({exc})")
        page.wait_for_timeout(5000)
        content = (page.content() or "").lower()
        if "verification" in content or "code" in content or "6-digit" in content:
            code = _code_for(creds, get_code,
                             "TikTok needs your 6-digit verification code")
            if code:
                try:
                    page.fill("input[autocomplete=one-time-code], "
                              "input[name=code]", code, timeout=8000)
                    page.locator("button[type=submit]").first.click()
                    page.wait_for_timeout(4000)
                except Exception:
                    pass

    def _do_publish(self, page, video_path, meta, log) -> PublishResult:
        caption = meta.caption_for("tiktok")
        page.goto(UPLOAD_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)                  # redirects to /tiktokstudio/upload
        frame = _upload_frame(page)
        log.append("selecting file")
        frame.locator("input[type=file]").first.set_input_files(
            video_path, timeout=30000)
        log.append("uploading")
        # Wait for the Studio composer (caption editor) to render, then clear the
        # onboarding popups ("Preview your video on your phone" / "Got it") that
        # otherwise sit on top of the caption box and the Post button.
        _wait_for(frame, _CAPTION_SEL, tries=40)
        _dismiss_popups(frame)
        _set_caption(frame, caption, log)
        if self.dry_run:
            return self.dry_stop(page, log)
        # Slow upload: TikTok keeps Post disabled until the video finishes
        # uploading. Wait for it to enable before clicking, or we'd post nothing.
        log.append("waiting for upload to finish")
        self.wait_uploaded(lambda: _post_locator(frame), log, "tiktok upload")
        _dismiss_popups(frame)                       # popups can also appear late
        if _captcha(frame):
            return PublishResult.failure(
                self.name, "TikTok slider CAPTCHA — solve it in the browser, then "
                "retry", needs_login=True, log=log)
        log.append("clicking Post")
        if not _click_post(frame):
            return PublishResult.failure(self.name, "no Post button", log=log)
        # Real success: TikTok shows '✓ Video published' and navigates to
        # /tiktokstudio/content — wait for THAT, not just a closed composer.
        if _wait_published(page, frame, self.cfg.publish_upload_timeout, log):
            log.append("confirmed posted")
            return PublishResult.success(self.name, url=self.home_url, log=log)
        if _captcha(frame):
            return PublishResult.failure(
                self.name, "TikTok slider CAPTCHA blocked the post — solve it and "
                "retry", needs_login=True, log=log)
        return PublishResult.failure(self.name, "no post confirmation seen", log=log)


def _upload_frame(page):
    for f in page.frames:
        try:
            if f.locator("input[type=file]").count() > 0:
                return f
        except Exception:
            continue
    return page


# TikTok Studio's caption box is a Draft.js editor inside [data-e2e=caption_container].
_CAPTION_SEL = ("div[data-e2e=caption_container] div[contenteditable=true]",
                "div.public-DraftEditor-content",
                "div[contenteditable=true]")


def _wait_for(frame, sels, tries: int = 40, gap: int = 1500) -> bool:
    """Poll until any of ``sels`` is present (composer rendered)."""
    for _ in range(tries):
        for sel in sels:
            try:
                if frame.locator(sel).count() > 0:
                    return True
            except Exception:
                pass
        frame.wait_for_timeout(gap)
    return False


def _captcha(frame) -> bool:
    """True if TikTok's slider/puzzle CAPTCHA is up (a human must solve it)."""
    for t in ("Drag the slider", "slider", "puzzle", "Verify to continue"):
        try:
            if frame.get_by_text(t, exact=False).count() > 0:
                return True
        except Exception:
            pass
    return False


def _wait_published(page, frame, timeout_s: float, log: list[str]) -> bool:
    """Wait for TikTok's REAL confirmation: navigation to /tiktokstudio/content,
    or a 'Video published' toast. Bails out (False) if a CAPTCHA appears so the
    caller can surface it instead of reporting a false success."""
    deadline = time.time() + max(60.0, float(timeout_s))
    while time.time() < deadline:
        page.wait_for_timeout(5000)
        try:
            u = page.url.lower()
            if "content" in u and "upload" not in u:
                log.append("navigated to posts")
                return True
        except Exception:
            pass
        for t in ("Video published", "being uploaded", "Manage your posts"):
            try:
                if frame.get_by_text(t, exact=False).count() > 0:
                    log.append(f"success: {t}")
                    return True
            except Exception:
                pass
        if _captcha(frame):
            return False
    return False


def _dismiss_popups(frame) -> None:
    """Close TikTok Studio onboarding/preview popups that overlay the composer."""
    for label in ("Got it", "Skip", "Maybe later", "Not now", "OK", "Close",
                  "I got it"):
        try:
            el = frame.get_by_role("button", name=label, exact=True).first
            if el.count() > 0:
                el.click(timeout=2000)
                frame.wait_for_timeout(400)
        except Exception:
            continue


def _set_caption(frame, caption: str, log: list[str]) -> None:
    for sel in _CAPTION_SEL:
        try:
            box = frame.locator(sel).first
            if box.count() > 0:
                box.click()
                box.press("Control+A")
                box.press("Delete")
                box.type(caption[:2150], delay=8)
                log.append("caption set")
                return
        except Exception:
            continue
    log.append("warning: caption field not found")


def _post_locator(frame):
    # ONLY the precise data-e2e button. A 'has-text(Post)' match also hits the
    # "Posts" sidebar nav link, whose click navigates away and pops the
    # "are you sure you want to exit?" modal instead of publishing.
    return frame.locator("[data-e2e=post_video_button]")


def _click_post(frame) -> bool:
    for getter in (
        lambda: frame.locator("[data-e2e=post_video_button]"),
        # exact=True so it can't match the "Posts" sidebar item.
        lambda: frame.get_by_role("button", name="Post", exact=True),
    ):
        try:
            btn = getter().first
            if btn.count() > 0:
                try:
                    btn.scroll_into_view_if_needed(timeout=5000)
                except Exception:
                    pass
                btn.click(timeout=15000)
                return True
        except Exception:
            continue
    return False
