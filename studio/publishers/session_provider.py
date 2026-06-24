"""Resolve a logged-in Playwright page via an ordered fallback chain.

Chain (when ``session_strategy`` is "auto"):

  1. **edge_profile**     reuse the host's Microsoft Edge logins (copy + msedge).
  2. **saved_session**    the persistent profile captured by ``login_setup`` /
                          the Connections "Run login" action.
  3. **credentials_login**log in with the username/password (+TOTP) stored in the
                          encrypted vault, persisting the session for next time.

Each link must land **verified-logged-in** before it's used; otherwise the next
link is tried. A platform that needs human action (2FA/CAPTCHA) raises
:class:`NeedsLogin`, which the publisher turns into a ``needs_login`` result
(never an infinite retry).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from ..config import StudioConfig
from . import edge_profile
from .playwright_base import _import_sync_playwright, launch_persistent

logger = logging.getLogger(__name__)

ORDER = ("edge_profile", "saved_session", "credentials_login")


class NeedsLogin(Exception):
    """The platform requires human login (2FA/CAPTCHA) — surface, don't retry."""


class SessionUnavailable(Exception):
    """No strategy in the chain produced a logged-in session."""


def _safe_close(ctx) -> None:
    try:
        if ctx is not None:
            ctx.close()
    except Exception:
        pass


class SessionProvider:
    def __init__(self, cfg: StudioConfig, platform: str, vault,
                 home_url: str, is_logged_in, login_steps) -> None:
        self.cfg = cfg
        self.platform = platform
        self.vault = vault
        self.home_url = home_url
        self.is_logged_in = is_logged_in
        self.login_steps = login_steps
        self.winning = ""

    def chain(self) -> list[str]:
        full = list(ORDER)
        if not edge_profile.available(self.cfg):
            full.remove("edge_profile")
        if not (self.vault and self.vault.enabled):
            full.remove("credentials_login")
        pref = self.cfg.session_strategy_for(self.platform)
        if pref and pref != "auto":
            # Honour a pin only if it's actually possible; else fall back so a
            # misconfigured pin doesn't silently disable publishing.
            return [pref] if pref in full else (full or [pref])
        return full

    # ------------------------------------------------------------------
    @contextmanager
    def page(self, headless: bool = True,
             viewport: tuple[int, int] | None = (1280, 900)):
        sync_playwright = _import_sync_playwright()
        errors: list[str] = []
        with sync_playwright() as p:
            for strat in self.chain():
                ctx = None
                try:
                    ctx = self._open(p, strat, headless, viewport)
                    if ctx is None:
                        continue
                    # Bound every action/navigation so a hung page can't wedge
                    # the shared worker forever.
                    ms = int(max(self.cfg.health_check_timeout, 60) * 1000)
                    try:
                        ctx.set_default_timeout(ms)
                        ctx.set_default_navigation_timeout(ms)
                    except Exception:
                        pass
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    if self._prepare_and_verify(page, strat):
                        self.winning = strat
                        logger.info("[%s] session via %s", self.platform, strat)
                        try:
                            yield page
                        finally:
                            _safe_close(ctx)
                        return
                    errors.append(f"{strat}: not logged in")
                    _safe_close(ctx)
                except NeedsLogin:
                    _safe_close(ctx)
                    raise
                except Exception as exc:
                    errors.append(f"{strat}: {type(exc).__name__}: {exc}")
                    _safe_close(ctx)
        raise SessionUnavailable(
            f"{self.platform}: no logged-in session ({'; '.join(errors) or 'no strategies'})")

    # ------------------------------------------------------------------
    def _open(self, p, strat: str, headless: bool, viewport):
        if strat == "edge_profile":
            return edge_profile.open_context(p, self.cfg, headless, viewport)
        # saved_session and credentials_login both use the persistent per-platform
        # dir, so a successful credential login is reused as a saved session next.
        # Prefer the installed Edge (same browser as edge_profile reuse).
        profile_dir = self.cfg.session_dir_for(self.platform)
        profile_dir.mkdir(parents=True, exist_ok=True)
        return launch_persistent(p, profile_dir, headless, viewport)

    def _prepare_and_verify(self, page, strat: str) -> bool:
        if strat == "credentials_login":
            creds = self.vault.get(self.platform) if self.vault else None
            if not creds or not creds["password"].reveal():
                return False
            page.goto(self.home_url, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            if self.is_logged_in(page):   # session dir may already be logged in
                return True
            # login_steps must raise NeedsLogin if it hits 2FA/CAPTCHA.
            self.login_steps(page, creds)
            page.wait_for_timeout(3000)
            return self.is_logged_in(page)
        # edge_profile / saved_session: just open the home and check.
        page.goto(self.home_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        return self.is_logged_in(page)
