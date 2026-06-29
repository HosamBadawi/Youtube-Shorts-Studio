"""Base class for the browser-automation publishers (IG / TikTok / Facebook).

Owns every cross-cutting reliability concern so each platform file is just DOM
steps:

- ``publish()`` — never raises. Tries up to ``publish_max_attempts`` times with
  full-jitter exponential backoff. Obtains a logged-in page from the
  :class:`SessionProvider` chain; a "not logged in" / no-session outcome returns
  ``needs_login`` (no pointless retries). A failed transient attempt captures a
  screenshot under ``workspace/failures/`` and retries.
- ``health()`` — opens a session via the chain and reports whether the platform
  is logged in (no upload), plus which strategy won.

Subclasses implement ``is_logged_in(page)``, ``_do_publish(page, video, meta,
log)`` (which MUST wait for a confirmed-published signal before returning ok, so
a retry never double-posts), and optionally ``login_steps(page, creds)``.
"""

from __future__ import annotations

import logging
import random
import time
from pathlib import Path

from ..config import StudioConfig
from ..health import HealthStatus
from ..metadata import VideoMeta
from .base import PublishResult
from .playwright_base import PlaywrightUnavailable
from .session_provider import NeedsLogin, SessionProvider, SessionUnavailable

logger = logging.getLogger(__name__)


def code_for(creds, get_code, reason: str) -> str:
    """Return a 6-digit 2FA/login code: auto-generated from a stored TOTP secret
    if present, else requested from the phone UI via ``get_code(reason)``.
    Returns "" when neither is available (caller raises ``NeedsLogin``)."""
    try:
        totp = creds["totp_secret"].reveal()
    except Exception:
        totp = ""
    if totp:
        from ..vault import totp_now
        return totp_now(totp)
    if get_code:
        return (get_code(reason) or "").strip()
    return ""


class PlaywrightPublisher:
    name: str = ""
    home_url: str = ""

    def __init__(self, cfg: StudioConfig, vault=None) -> None:
        self.cfg = cfg
        self.vault = vault
        self.provider = SessionProvider(cfg, self.name, vault, self.home_url,
                                        self.is_logged_in, self.login_steps)
        self.on_attempt = None  # optional callback(platform, n, max) for the UI
        self.dry_run = False    # rehearsal: drive the flow but never click post

    # --- subclass hooks -----------------------------------------------------
    def is_logged_in(self, page) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError

    def _do_publish(self, page, video_path: str, meta: VideoMeta,
                    log: list[str]) -> PublishResult:  # pragma: no cover
        raise NotImplementedError

    def login_steps(self, page, creds, get_code=None) -> None:
        """Default: this platform can't auto-login -> ask for human login.

        ``get_code(reason)`` (optional) returns a 6-digit 2FA/login code on
        demand — auto from a stored TOTP secret, or by prompting the phone UI."""
        raise NeedsLogin(f"{self.name}: automatic login not supported")

    # --- publish (retry loop) ----------------------------------------------
    def publish(self, video_path: str, meta: VideoMeta) -> PublishResult:
        attempts = max(1, int(self.cfg.publish_max_attempts))
        last = PublishResult.failure(self.name, "not attempted")
        for n in range(1, attempts + 1):
            self._notify(n, attempts)
            try:
                with self.provider.page(
                        headless=self.cfg.playwright_headless,
                        viewport=(1280, 900)) as page:
                    log = [f"try {n}/{attempts} via {self.provider.winning}"]
                    if not self.is_logged_in(page):
                        return PublishResult.failure(self.name, "not logged in",
                                                     needs_login=True, log=log)
                    try:
                        result = self._do_publish(page, video_path, meta, log)
                    except NeedsLogin:
                        raise
                    except Exception as exc:
                        log.append(f"error: {type(exc).__name__}: {exc}")
                        result = PublishResult.failure(
                            self.name, f"{type(exc).__name__}: {exc}", log=log)
                    if result.ok:
                        result.log = log
                        return result
                    self._maybe_shot(page, n, log)
                    result.log = log
                    last = result
            except (SessionUnavailable, NeedsLogin) as exc:
                # No usable session / human action required -> not retryable.
                return PublishResult.failure(self.name, str(exc),
                                             needs_login=True)
            except PlaywrightUnavailable as exc:
                return PublishResult.failure(self.name, str(exc))
            except Exception as exc:  # pragma: no cover
                last = PublishResult.failure(
                    self.name, f"{type(exc).__name__}: {exc}")
            if n < attempts:
                self._backoff(n)
        return last

    # --- health -------------------------------------------------------------
    def health(self) -> HealthStatus:
        try:
            return self._health()
        finally:
            # Bring the user's Edge back after an interactive check that closed
            # it (publish deliberately doesn't, to avoid flapping).
            if self.cfg.edge_use_live_profile and self.cfg.edge_reopen_after:
                from . import edge_profile
                edge_profile.reopen_edge(self.cfg)

    def _health(self) -> HealthStatus:
        try:
            with self.provider.page(headless=self.cfg.playwright_headless,
                                    viewport=(1280, 900)):
                return HealthStatus(self.name, True, "logged in",
                                    strategy=self.provider.winning,
                                    checked_at=time.time())
        except SessionUnavailable:
            return HealthStatus(self.name, False,
                                "not logged in — set up a session/credentials",
                                checked_at=time.time())
        except NeedsLogin as exc:
            return HealthStatus(self.name, False, f"login needed: {exc}",
                                checked_at=time.time())
        except PlaywrightUnavailable as exc:
            return HealthStatus(self.name, False, str(exc), checked_at=time.time())
        except Exception as exc:  # pragma: no cover
            return HealthStatus(self.name, False, f"{type(exc).__name__}: {exc}",
                                checked_at=time.time())

    # --- helpers ------------------------------------------------------------
    def _notify(self, n: int, total: int) -> None:
        if self.on_attempt:
            try:
                self.on_attempt(self.name, n, total)
            except Exception:
                pass

    def _backoff(self, n: int) -> None:
        delay = min(self.cfg.publish_backoff_base
                    * (self.cfg.publish_backoff_factor ** (n - 1)),
                    self.cfg.publish_backoff_max)
        time.sleep(random.uniform(delay * 0.5, delay))  # partial jitter

    def dry_stop(self, page, log: list[str]) -> PublishResult:
        """Rehearsal stop: the composer is filled and the final post button is in
        reach — screenshot it and return a dry-run success WITHOUT posting. Call
        this immediately before the real Share/Post/Publish click."""
        log.append("DRY RUN — composer ready, stopping before the post click")
        url = ""
        try:
            self.cfg.rehearsals_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            name = f"{self.name}_{stamp}.png"
            page.screenshot(path=str(self.cfg.rehearsals_dir / name),
                            full_page=False)
            url = f"/api/rehearsal/{name}"
            log.append(f"rehearsal screenshot: {name}")
        except Exception as exc:
            log.append(f"rehearsal screenshot failed: {exc}")
        return PublishResult.rehearsed(self.name, shot_url=url, log=log)

    def wait_uploaded(self, get_button, log: list[str],
                      label: str = "upload") -> bool:
        """Block until the final submit button exists AND is enabled. Platforms
        keep that button disabled while the video uploads, so this is the reliable
        'upload finished' signal — essential on slow upload connections. ``get_button``
        is a callable returning a Locator. Returns True when ready; False on
        timeout (the caller may still attempt the click)."""
        deadline = time.time() + max(60.0, float(self.cfg.publish_upload_timeout))
        while time.time() < deadline:
            try:
                btn = get_button()
                if btn is not None and btn.count() > 0 and btn.first.is_enabled():
                    log.append(f"{label}: ready")
                    return True
            except Exception:
                pass
            time.sleep(2.0)
        log.append(f"{label}: not confirmed ready after "
                   f"{int(self.cfg.publish_upload_timeout)}s — attempting anyway")
        return False

    def _maybe_shot(self, page, n: int, log: list[str]) -> None:
        if not self.cfg.screenshot_on_failure:
            return
        try:
            self.cfg.failures_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = self.cfg.failures_dir / f"{self.name}_{stamp}_try{n}.png"
            page.screenshot(path=str(path))
            log.append(f"screenshot: {path}")
        except Exception:
            pass
