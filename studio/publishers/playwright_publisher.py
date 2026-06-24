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
import re
import time
from pathlib import Path

from ..config import StudioConfig
from ..health import HealthStatus
from ..metadata import VideoMeta
from .base import PublishResult
from .playwright_base import PlaywrightUnavailable
from .session_provider import NeedsLogin, SessionProvider, SessionUnavailable

logger = logging.getLogger(__name__)


class PlaywrightPublisher:
    name: str = ""
    home_url: str = ""

    def __init__(self, cfg: StudioConfig, vault=None) -> None:
        self.cfg = cfg
        self.vault = vault
        self.provider = SessionProvider(cfg, self.name, vault, self.home_url,
                                        self.is_logged_in, self.login_steps)
        self.on_attempt = None  # optional callback(platform, n, max) for the UI

    # --- subclass hooks -----------------------------------------------------
    def is_logged_in(self, page) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError

    def _do_publish(self, page, video_path: str, meta: VideoMeta,
                    log: list[str]) -> PublishResult:  # pragma: no cover
        raise NotImplementedError

    def login_steps(self, page, creds) -> None:
        """Default: this platform can't auto-login -> ask for human login."""
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
            with self.provider.page(headless=True, viewport=(1280, 900)):
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

    def _confirm_published(self, page, action_labels, patterns: str,
                           timeout_ms: int) -> bool:
        """Language-agnostic publish confirmation. Succeeds on ANY of: success
        text matching ``patterns``; the action button(s) disappearing (the
        composer closed); or a navigation away from the composer. This avoids
        false "failed" verdicts on non-English UIs (which would otherwise trigger
        a retry that re-posts)."""
        pat = re.compile(patterns, re.I)
        start_url = page.url
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            try:
                if page.get_by_text(pat).count() > 0:
                    return True
            except Exception:
                pass
            try:
                if action_labels and all(
                        page.get_by_role("button", name=l).count() == 0
                        for l in action_labels):
                    return True
            except Exception:
                pass
            try:
                u = page.url
                if u != start_url and "create" not in u.lower() \
                        and "upload" not in u.lower():
                    return True
            except Exception:
                pass
            page.wait_for_timeout(1000)
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
