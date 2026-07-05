"""Entry point: ``python -m studio``.

Boots the FastAPI server with uvicorn and (unless disabled) opens the Cloudflare
tunnel so the app is reachable from your phone. Press Ctrl+C to stop both.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .cloudflared import start_tunnel
from .config import StudioConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="studio",
                                     description="YouTube Shorts Studio server.")
    parser.add_argument("--config", default=None, help="Path to studio.yaml")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-tunnel", action="store_true",
                        help="Don't start the Cloudflare tunnel.")
    parser.add_argument("--allow-insecure", action="store_true",
                        help="Allow network exposure with no/default password "
                             "(NOT recommended).")
    parser.add_argument("-v", "--verbose", action="count", default=1)
    args = parser.parse_args(argv)

    # Windows consoles default to cp1252 and crash on Arabic/emoji log output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    logging.basicConfig(
        level=logging.WARNING - min(args.verbose, 2) * 10,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = StudioConfig.load(args.config)
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if args.no_tunnel:
        cfg.cloudflare_mode = "off"
    cfg.ensure_dirs()

    try:
        import uvicorn  # type: ignore
    except Exception:
        print("uvicorn is not installed. Run: pip install -r requirements-studio.txt",
              file=sys.stderr)
        return 1

    # Fail closed: never put an unprotected admin UI on a public/LAN address.
    weak_auth = (not cfg.app_password) or cfg.app_password == "change-me"
    loopback = {"127.0.0.1", "localhost", "::1"}
    public = cfg.cloudflare_mode != "off" or cfg.host not in loopback
    if weak_auth and public and not args.allow_insecure:
        why = ("no app_password is set" if not cfg.app_password
               else "app_password is still the default 'change-me'")
        print(f"\n⛔ Refusing to expose the app: {why}.\n"
              f"   That would put an unprotected admin UI on a public URL.\n"
              f"   Fix one of:\n"
              f"     • set a strong app_password in studio.yaml (recommended), or\n"
              f"     • run locally:  python -m studio --no-tunnel --host 127.0.0.1, or\n"
              f"     • override (NOT recommended):  --allow-insecure\n",
              file=sys.stderr)
        return 2

    tunnel = start_tunnel(cfg)
    print(f"Local:  http://localhost:{cfg.port}")
    try:
        # Import string keeps reload/workers happy; factory builds from config.
        from .server import create_app

        app = create_app(cfg)
        uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
    finally:
        if tunnel:
            tunnel.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
