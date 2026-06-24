"""One-time login capture for each platform.

Run once per platform on the host PC (with a screen attached) so the publishers
can reuse the saved session later, headlessly, from your phone:

    python -m studio.login_setup all          # do every platform
    python -m studio.login_setup instagram    # or one at a time
    python -m studio.login_setup youtube       # runs the YouTube OAuth flow

For instagram / tiktok / facebook a browser window opens: log in normally
(including 2FA), then close the window. For youtube a browser opens Google's
OAuth consent screen and the refresh token is cached.
"""

from __future__ import annotations

import sys

from .config import PLATFORMS, StudioConfig

_START_URLS = {
    "instagram": "https://www.instagram.com/accounts/login/",
    "tiktok": "https://www.tiktok.com/login",
    "facebook": "https://www.facebook.com/login",
}


def login(platform: str, cfg: StudioConfig) -> bool:
    platform = platform.lower()
    if platform == "youtube":
        from .publishers.youtube import YouTubePublisher

        try:
            YouTubePublisher(cfg).authorize(interactive=True)
            print("[youtube] authorized ✓")
            return True
        except Exception as exc:
            print(f"[youtube] failed: {exc}")
            return False

    if platform not in _START_URLS:
        print(f"unknown platform: {platform}")
        return False

    from .publishers.playwright_base import capture_login

    return capture_login(cfg.session_dir_for(platform), _START_URLS[platform],
                         platform)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        print("platforms:", ", ".join(PLATFORMS))
        return 2
    cfg = StudioConfig.load()
    cfg.ensure_dirs()
    targets = list(PLATFORMS) if argv[0] == "all" else argv
    ok = True
    for p in targets:
        ok = login(p, cfg) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
