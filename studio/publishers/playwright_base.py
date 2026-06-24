"""Shared Playwright plumbing for the browser-automation publishers.

Each platform gets its own *persistent* browser profile under
``workspace/sessions/<platform>`` so you log in once (cookies/local-storage
survive between runs). The synchronous Playwright API is used because publishing
happens on a background worker thread, never inside the async web server.

If Playwright (or its browsers) isn't installed, helpers raise
:class:`PlaywrightUnavailable`, which publishers translate into a friendly
``PublishResult.failure``.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)


class PlaywrightUnavailable(RuntimeError):
    pass


def _import_sync_playwright():
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise PlaywrightUnavailable(
            "Playwright not installed. Run:\n"
            "  pip install playwright\n"
            "  python -m playwright install chromium") from exc
    return sync_playwright


def launch_persistent(p, user_data_dir: Path, headless: bool,
                      viewport: tuple[int, int] | None = None,
                      profile_dir: str | None = None):
    """Launch a persistent context that feels like the user's normal **Edge**:

    - ``chromium_sandbox=True`` so Playwright doesn't add ``--no-sandbox`` (which
      shows the yellow "unsupported command-line flag" banner).
    - removes ``--disable-extensions`` / ``--disable-component-extensions-…`` so
      the profile's installed extensions load.
    - non-headless opens **maximized** with ``no_viewport=True``.

    Falls back to bundled Chromium only if Edge isn't available.
    """
    # NOTE: we do NOT pass --disable-blink-features=AutomationControlled — Edge
    # shows it in the grey "unsupported command-line flag" bar. We hide the
    # webdriver signal with an init script instead (no flag, no banner).
    args = []
    if profile_dir:
        args.append(f"--profile-directory={profile_dir}")
    kw: dict = dict(
        user_data_dir=str(user_data_dir),
        headless=headless,
        chromium_sandbox=True,           # avoids the --no-sandbox banner
        args=args,
        ignore_default_args=[
            "--disable-extensions",
            "--disable-component-extensions-with-background-pages",
            "--enable-automation",       # harmless no-op on current Playwright
        ],
    )
    if headless:
        if viewport:
            kw["viewport"] = {"width": viewport[0], "height": viewport[1]}
    else:
        args.append("--start-maximized")
        kw["no_viewport"] = True
    try:
        ctx = p.chromium.launch_persistent_context(channel="msedge", **kw)
    except Exception as exc:
        logger.info("msedge channel unavailable (%s); using bundled chromium", exc)
        ctx = p.chromium.launch_persistent_context(**kw)
    try:
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    except Exception:
        pass
    return ctx


@contextmanager
def browser_session(profile_dir: Path, headless: bool = True,
                    viewport: tuple[int, int] = (412, 915)):
    """Yield a Playwright ``page`` backed by a persistent per-platform profile.

    The mobile-ish viewport nudges some sites toward simpler upload flows.
    """
    sync_playwright = _import_sync_playwright()
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            viewport={"width": viewport[0], "height": viewport[1]},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            yield page
        finally:
            context.close()


def capture_login(profile_dir: Path, start_url: str, platform: str) -> bool:
    """Open a headed browser so the user can log in once; persists the session.

    Blocks until the user closes the window. Returns True if the profile now
    holds cookies (a rough "did they log in" check).
    """
    sync_playwright = _import_sync_playwright()
    profile_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n[{platform}] A browser window is opening.")
    print(f"  1. Log in to {platform} normally (handle any 2FA).")
    print("  2. When you're fully logged in, just CLOSE the browser window.")
    print("  The session is saved automatically.\n")
    with sync_playwright() as p:
        context = launch_persistent(p, profile_dir, headless=False,
                                    viewport=(1180, 820))
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(start_url, wait_until="domcontentloaded")
        except Exception:
            pass
        # Wait until the user closes the context.
        closed = {"v": False}
        context.on("close", lambda: closed.__setitem__("v", True))
        try:
            while not closed["v"]:
                page.wait_for_timeout(1000)
        except Exception:
            pass  # context closed -> loop's page handle goes away
        try:
            cookies = context.cookies()
        except Exception:
            cookies = []
    ok = len(cookies) > 0
    print(f"[{platform}] {'session saved.' if ok else 'no cookies captured - try again.'}")
    return ok
