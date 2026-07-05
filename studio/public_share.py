"""Short-lived public file shares for API publishers.

The Instagram Content Publishing API doesn't accept an upload — Meta's servers
download the video from a **public URL**. This module hands out unguessable
one-off tokens mapping to local files; :mod:`studio.server` serves them on
``/pub/{token}`` WITHOUT the auth cookie (Meta's fetcher can't log in), so the
128-bit random token is the secret. Shares expire after ``TTL_S`` and the
registry is capped so it can never grow unbounded on a 24/7 host.
"""

from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path

TTL_S = 2 * 60 * 60          # ample for Meta to download + process a reel
_MAX = 32                    # safety cap; publishes are one-at-a-time anyway

_lock = threading.Lock()
_shares: dict[str, tuple[str, float]] = {}   # token -> (path, expires_at)


def register(path: str | Path) -> str:
    """Register ``path`` for public download; returns the URL token."""
    token = secrets.token_urlsafe(16)
    now = time.time()
    with _lock:
        for t in [t for t, (_, exp) in _shares.items() if exp < now]:
            _shares.pop(t, None)
        while len(_shares) >= _MAX:               # oldest out first
            _shares.pop(next(iter(_shares)), None)
        _shares[token] = (str(path), now + TTL_S)
    return token


def resolve(token: str) -> str | None:
    """Path for a live share token, or None (unknown/expired)."""
    with _lock:
        item = _shares.get(token)
        if not item:
            return None
        path, exp = item
        if exp < time.time():
            _shares.pop(token, None)
            return None
        return path


def revoke(token: str) -> None:
    with _lock:
        _shares.pop(token, None)
