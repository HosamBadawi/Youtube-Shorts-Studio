"""Launch a free Cloudflare tunnel so your phone can reach the PC.

Two modes (see ``cloudflare_mode`` in config):

* ``quick`` - ``cloudflared tunnel --url http://localhost:<port>``. No account,
  no DNS; Cloudflare hands back an ephemeral ``https://<random>.trycloudflare.com``
  URL that we parse from the process output and print. The URL changes each run.
* ``named`` - ``cloudflared tunnel run --token <token>``. A stable URL you set up
  once in the Cloudflare Zero Trust dashboard.

Requires the ``cloudflared`` binary on PATH (free download from Cloudflare).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading

from .config import StudioConfig

logger = logging.getLogger(__name__)
_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def start_tunnel(cfg: StudioConfig) -> subprocess.Popen | None:
    if cfg.cloudflare_mode == "off":
        return None
    if not shutil.which(cfg.cloudflared_bin):
        logger.warning("cloudflared not found on PATH; skipping tunnel. "
                       "Install it or set cloudflare_mode: off.")
        return None

    if cfg.cloudflare_mode == "named":
        if not cfg.cloudflare_token:
            logger.warning("cloudflare_mode=named but no cloudflare_token set.")
            return None
        cmd = [cfg.cloudflared_bin, "tunnel", "run", "--token",
               cfg.cloudflare_token]
    else:  # quick
        cmd = [cfg.cloudflared_bin, "tunnel", "--url",
               f"http://localhost:{cfg.port}"]

    logger.info("Starting Cloudflare tunnel (%s mode)…", cfg.cloudflare_mode)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    threading.Thread(target=_watch, args=(proc,), daemon=True).start()
    return proc


def _watch(proc: subprocess.Popen) -> None:
    announced = False
    for line in proc.stdout:  # type: ignore[union-attr]
        m = _URL_RE.search(line)
        if m and not announced:
            announced = True
            url = m.group(0)
            print("\n" + "=" * 56)
            print(f"  📱 Open this on your phone:  {url}")
            print("=" * 56 + "\n")
        elif "ERR" in line or "error" in line.lower():
            logger.debug("cloudflared: %s", line.strip())
