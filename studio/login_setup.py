"""One-time YouTube authorization.

Run once on the host PC (with a screen attached) so uploads can run headlessly
from your phone afterwards:

    python -m studio.login_setup

A browser opens Google's OAuth consent screen; the refresh token is cached to
``youtube_token`` (see studio.yaml). Re-run this whenever the app asks for a
re-auth (e.g. after the thumbnail permission was added to the requested scopes).
"""

from __future__ import annotations

import sys

from .config import StudioConfig


def login(cfg: StudioConfig) -> bool:
    from .publishers.youtube import YouTubePublisher

    try:
        YouTubePublisher(cfg).authorize(interactive=True)
        print("[youtube] authorized ✓")
        return True
    except Exception as exc:
        print(f"[youtube] failed: {exc}")
        return False


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    cfg = StudioConfig.load()
    cfg.ensure_dirs()
    return 0 if login(cfg) else 1


if __name__ == "__main__":
    raise SystemExit(main())
