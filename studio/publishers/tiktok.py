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
            # Raise (not return): a CAPTCHA needs a human, so retrying just wastes
            # another slow re-upload. Terminal -> surfaced once as needs_login.
            raise NeedsLogin("TikTok slider CAPTCHA — solve it in the browser, "
                             "then retry")
        log.append("clicking Post")
        if not _click_post(frame):
            return PublishResult.failure(self.name, "no Post button", log=log)
        page.wait_for_timeout(2500)
        _confirm_post_dialog(page, frame, log)   # 'Continue to post?' -> Post now
        # Real success: TikTok shows '✓ Video published' and navigates to
        # /tiktokstudio/content — wait for THAT, not just a closed composer.
        if _wait_published(page, frame, self.cfg.publish_upload_timeout, log):
            log.append("confirmed posted")
            return PublishResult.success(self.name, url=self.home_url, log=log)
        # Post was CLICKED but not confirmed. Raise (not return) so the base loop
        # does NOT retry — re-running _do_publish would re-upload = a DOUBLE post.
        # Screenshot first: the raise path skips the base class's failure shot,
        # which left us blind to WHAT blocked the post (modal/captcha/error).
        self._shot_unconfirmed(page, log)
        if _captcha(frame):
            raise NeedsLogin("TikTok slider CAPTCHA blocked the post — solve it "
                             "and retry")
        raise NeedsLogin("clicked Post but TikTok didn't confirm — check your "
                         "TikTok posts before retrying (avoids a double-post)")

    def _shot_unconfirmed(self, page, log) -> None:
        try:
            self.cfg.failures_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            p = self.cfg.failures_dir / f"tiktok_unconfirmed_{stamp}.png"
            page.screenshot(path=str(p))
            log.append(f"screenshot: {p}")
        except Exception:
            pass


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
    caller can surface it instead of reporting a false success.

    RE-CLICK LOGIC: TikTok silently IGNORES the Post click while its
    'Content check' is still running (verified by a failure screenshot: the
    composer sat fully-ready with an enabled Post button 20 minutes after our
    click). An enabled Post button still on screen is PROOF nothing was
    submitted, so re-clicking is double-post-safe — we re-click every ~20s
    until the composer actually goes away."""
    deadline = time.time() + max(60.0, float(timeout_s))
    last_click = time.time()
    while time.time() < deadline:
        page.wait_for_timeout(5000)
        try:
            u = page.url.lower()
            if "content" in u and "upload" not in u:
                log.append("navigated to posts")
                return True
        except Exception:
            pass
        # NOTE: "being uploaded" is a PROGRESS state, NOT completion — treating it
        # as success reported a post before the upload had actually finished.
        for t in ("Video published", "Manage your posts", "Your videos"):
            try:
                if frame.get_by_text(t, exact=False).count() > 0:
                    log.append(f"success: {t}")
                    return True
            except Exception:
                pass
        if _captcha(frame):
            return False
        # The 'Continue to post?' dialog can appear at any point after the
        # click (its copyright check finishes asynchronously) — always confirm.
        if _confirm_post_dialog(page, frame, log):
            last_click = time.time()
            continue
        try:
            btn = _post_locator(frame).first
            if (btn.count() > 0 and btn.is_enabled()
                    and time.time() - last_click > 20):
                log.append("Post button still enabled (click was ignored) — "
                           "re-clicking")
                _dismiss_popups(frame)
                _click_post(frame)
                last_click = time.time()
        except Exception:
            pass
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


def _confirm_post_dialog(page, frame, log: list[str]) -> bool:
    """Click through TikTok's post-click confirmation dialog.

    Clicking Post while TikTok's copyright/content check is still running pops
    'Continue to post?' — "The copyright check is incomplete... Do you want to
    continue posting before the check is complete?" with Cancel / **Post now**.
    Without clicking 'Post now' NOTHING posts (verified live). The dialog can
    live in the page or the composer frame — check both."""
    for ctx in (frame, page):
        for label in ("Post now", "Post Now", "Continue"):
            try:
                el = ctx.get_by_role("button", name=label, exact=True).first
                if el.count() > 0:
                    el.click(timeout=3000)
                    log.append(f"confirmation dialog: clicked '{label}'")
                    return True
            except Exception:
                continue
    return False


def _set_caption(frame, caption: str, log: list[str]) -> None:
    """Write the caption as ONE atomic insert — never per-key typing.

    Typing char-by-char into TikTok's Draft.js box DROPPED and REORDERED
    characters: each '#' pops the hashtag-autocomplete dropdown, which steals
    the caret mid-type (worse with RTL Arabic). A posted description came out
    mangled ("خيا" for "خيال", the opening glued after the hashtags). insert_text
    delivers the whole string in a single input event, then we VERIFY what
    landed and fall back to slow typing if the editor ate it."""
    text = caption[:2150]
    for sel in _CAPTION_SEL:
        try:
            box = frame.locator(sel).first
            if box.count() == 0:
                continue
            box.click()
            box.press("Control+A")
            box.press("Delete")
            box.page.keyboard.insert_text(text)
            frame.wait_for_timeout(800)
            box.press("Escape")            # close any hashtag dropdown
            got = ""
            try:
                got = (box.inner_text() or "").strip()
            except Exception:
                pass
            if len(got) >= int(0.8 * len(text)):
                log.append("caption set (atomic insert, verified)")
            else:
                log.append(f"caption verify failed ({len(got)}/{len(text)} "
                           "chars) — clearing and retyping slowly")
                box.click()
                box.press("Control+A")
                box.press("Delete")
                box.type(text, delay=60)
                box.press("Escape")
                log.append("caption set (slow retype)")
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
