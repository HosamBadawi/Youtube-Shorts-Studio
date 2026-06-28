"""Interactive credential + 6-digit-code login, driven from the phone UI.

Modern Edge/Chrome encrypt cookies with **App-Bound Encryption**, so a browser
launched by automation can't decrypt the host profile's logins — reusing the live
Edge profile shows up *signed out*. The reliable path is therefore a **dedicated
automation profile** (``workspace/sessions/<platform>``) that we log into once and
then reuse for every upload.

This module performs that first login using the username/password (and optional
TOTP secret) saved in the vault. When the platform asks for a 6-digit code and no
TOTP secret is stored, the run **pauses** (``stage='awaiting_code'``) until the
user submits the code from their phone via ``POST /api/connections/login/code``;
the run then resumes and the session is persisted for reuse.

Everything degrades gracefully: a CAPTCHA / extra security check that can't be
solved from the phone returns ``needs_captcha`` with a clear "do it once at the
PC" message rather than hanging.
"""

from __future__ import annotations

import logging
import queue

from .publishers import get_publisher
from .publishers.playwright_base import (PlaywrightUnavailable,
                                         _import_sync_playwright,
                                         launch_persistent)
from .publishers.session_provider import NeedsLogin

logger = logging.getLogger(__name__)

CODE_WAIT_SECONDS = 180


def interactive_login(cfg, vault, platform: str, run: dict,
                      code_q: "queue.Queue") -> dict:
    """Log ``platform`` into its dedicated session profile.

    ``run`` is the shared status dict (this function flips ``stage`` to
    ``awaiting_code`` while it blocks on ``code_q`` for the user's 6-digit code).
    Returns a result dict: ``{ok, detail, strategy?, needs_login?, needs_captcha?}``.
    Never raises — the caller records the dict as the run result.
    """
    platform = platform.lower()
    if platform == "youtube":
        return {"ok": False, "detail": "YouTube uses the official API — run "
                                       "`python -m studio.login_setup youtube`."}
    if not (vault and vault.enabled):
        return {"ok": False, "detail": "credential vault unavailable "
                                       "(pip install cryptography)"}
    creds = vault.get(platform)
    if not creds or not creds["username"].reveal() or not creds["password"].reveal():
        return {"ok": False, "detail": "enter a username + password first, then "
                                       "tap Log in"}

    pub = get_publisher(platform, cfg, vault)

    def get_code(reason: str = "") -> str:
        # Prefer a stored TOTP secret (hands-free); else ask the phone for it.
        totp = creds["totp_secret"].reveal()
        if totp:
            from .vault import totp_now
            return totp_now(totp)
        run["stage"] = "awaiting_code"
        run["prompt"] = reason or "Enter the 6-digit code from your authenticator/SMS"
        try:
            code = code_q.get(timeout=CODE_WAIT_SECONDS)
        except queue.Empty:
            raise NeedsLogin("timed out waiting for the 6-digit code")
        run["stage"] = "running"
        run["prompt"] = ""
        return (code or "").strip()

    try:
        sync_playwright = _import_sync_playwright()
    except PlaywrightUnavailable as exc:
        return {"ok": False, "detail": str(exc)}

    profile_dir = cfg.session_dir_for(platform)
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = None
        try:
            ctx = launch_persistent(p, profile_dir, cfg.playwright_headless,
                                    viewport=(1280, 900))
            ms = int(max(cfg.health_check_timeout, 90) * 1000)
            try:
                ctx.set_default_timeout(ms)
                ctx.set_default_navigation_timeout(ms)
            except Exception:
                pass
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # Already logged in from a previous session? Then we're done.
            try:
                page.goto(pub.home_url, wait_until="domcontentloaded")
                for _ in range(4):
                    page.wait_for_timeout(1200)
                    if pub.is_logged_in(page):
                        return {"ok": True, "detail": "already logged in — "
                                "session ready", "strategy": "saved_session"}
            except Exception:
                pass

            try:
                pub.login_steps(page, creds, get_code)
            except NeedsLogin as exc:
                return {"ok": False, "detail": str(exc), "needs_login": True}

            page.wait_for_timeout(3000)
            if pub.is_logged_in(page):
                return {"ok": True, "detail": "logged in — session saved ✓",
                        "strategy": "credentials_login"}
            return {"ok": False, "needs_captcha": True,
                    "detail": "login didn't complete — the site likely showed a "
                              "CAPTCHA / extra check that can't be solved from the "
                              f"phone. Do this one login at the PC: "
                              f"python -m studio.login_setup {platform}"}
        except NeedsLogin as exc:
            return {"ok": False, "detail": str(exc), "needs_login": True}
        except Exception as exc:  # pragma: no cover
            logger.warning("[%s] interactive login failed: %s", platform, exc)
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        finally:
            try:
                if ctx is not None:
                    ctx.close()
            except Exception:
                pass
