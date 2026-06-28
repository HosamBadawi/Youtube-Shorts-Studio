"""TikTok publisher (Playwright) — web upload studio.

Subclasses :class:`PlaywrightPublisher`. TikTok aggressively challenges
automated logins (CAPTCHA/device check), so ``login_steps`` is intentionally NOT
implemented — credentials_login surfaces ``needs_login`` and the reliable paths
are the Edge profile or a saved session captured on the host.
"""

from __future__ import annotations

import logging

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
        page.wait_for_timeout(4000)
        frame = _upload_frame(page)
        log.append("selecting file")
        frame.locator("input[type=file]").first.set_input_files(
            video_path, timeout=30000)
        log.append("waiting for processing")
        frame.wait_for_timeout(8000)
        _set_caption(frame, caption, log)
        if self.dry_run:
            return self.dry_stop(page, log)
        # Slow upload: TikTok keeps Post disabled until the video finishes
        # uploading. Wait for it to enable before clicking, or we'd post nothing.
        log.append("waiting for upload to finish")
        self.wait_uploaded(lambda: _post_locator(frame), log, "tiktok upload")
        log.append("clicking Post")
        if not _click_post(frame):
            return PublishResult.failure(self.name, "no Post button", log=log)
        # TikTok shows a "Manage your posts" / success modal when done.
        if self._confirm_published(page, ["Post"],
                                   r"uploaded|posted|view profile|"
                                   r"manage your posts|تم", 50000):
            log.append("confirmed posted")
            return PublishResult.success(self.name, url=self.home_url, log=log)
        return PublishResult.failure(self.name, "no post confirmation seen", log=log)


def _upload_frame(page):
    for f in page.frames:
        try:
            if f.locator("input[type=file]").count() > 0:
                return f
        except Exception:
            continue
    return page


def _set_caption(frame, caption: str, log: list[str]) -> None:
    for sel in ("div[contenteditable=true]", "div[data-text=true]",
                "[data-e2e=caption-input]"):
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
    return frame.locator("[data-e2e=post_video_button], button:has-text('Post')")


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
