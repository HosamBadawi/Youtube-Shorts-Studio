"""Fetch a source video from a URL with yt-dlp (optionally accelerated by aria2c).

This is the "download a new long video from YouTube" input path. The shorts /
prepare commands accept either a local file or a URL; when given a URL they call
:func:`download` first and then feed the resulting file into the same pipeline.

aria2c is used as the external downloader when it's on PATH (much faster, multi-
connection); otherwise yt-dlp's native downloader is used. Only download content
you own or have the right to use.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def is_url(s: str) -> bool:
    s = s.strip().lower()
    return s.startswith("http://") or s.startswith("https://")


def _progress_hook(d: dict) -> None:
    if d.get("status") == "downloading":
        pct = d.get("_percent_str", "").strip()
        spd = d.get("_speed_str", "").strip()
        print(f"\r  downloading {pct} at {spd}        ", end="", flush=True)
    elif d.get("status") == "finished":
        print("\r  download complete, post-processing…           ", flush=True)


def download(url: str, save_dir: str, prefer_mp4: bool = True,
             aria2_connections: int = 16, quiet: bool = True) -> str:
    """Download ``url`` into ``save_dir`` and return the final file path."""
    try:
        from yt_dlp import YoutubeDL  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("yt-dlp not installed (pip install yt-dlp)") from exc

    Path(save_dir).mkdir(parents=True, exist_ok=True)

    if prefer_mp4:
        fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    else:
        fmt = "bestvideo+bestaudio/best"

    opts: dict = {
        "format": fmt,
        "outtmpl": str(Path(save_dir) / "%(title).80s - %(id)s.%(ext)s"),
        "noplaylist": True,
        "retries": 3,
        "continuedl": True,
        "quiet": quiet,
        "no_warnings": quiet,
        "progress_hooks": [_progress_hook],
    }
    if prefer_mp4:
        opts["merge_output_format"] = "mp4"
    if shutil.which("aria2c"):
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = [
            f"-x{aria2_connections}", f"-s{aria2_connections}", "-k1M",
        ]
        logger.info("Using aria2c (%d connections)", aria2_connections)

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    # Newer yt-dlp records the actual output path here (covers merged/remuxed).
    reqs = info.get("requested_downloads") if isinstance(info, dict) else None
    if reqs and reqs[0].get("filepath"):
        return reqs[0]["filepath"]
    # Fallbacks.
    with YoutubeDL(opts) as ydl:
        guess = ydl.prepare_filename(info)
    if prefer_mp4:
        mp4 = str(Path(guess).with_suffix(".mp4"))
        if Path(mp4).exists():
            return mp4
    return guess
