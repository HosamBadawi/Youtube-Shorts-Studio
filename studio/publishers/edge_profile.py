"""Reuse the logins saved in the host's Microsoft Edge profile (Windows).

Strategy (from the design research): never launch the user's LIVE Edge profile
(it locks, can hang Playwright, and risks corrupting their real Cookies). Instead
**copy** the needed subset of the ``User Data`` dir into a private working dir and
launch ``channel="msedge"`` (the signed Edge binary) against the copy — so the
DPAPI/App-Bound-Encryption cookie key still decrypts in-process for the same
Windows user. This is best-effort and non-destructive: if anything is locked or
missing, it returns ``None`` and the session chain falls through to the saved
session.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

from ..config import StudioConfig

logger = logging.getLogger(__name__)

# Big, churny, frequently-locked dirs we never need for auth — skip them.
_SKIP_DIRS = {"Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache",
              "DawnWebGPUCache", "GraphiteDawnCache", "Service Worker",
              "ShaderCache", "GrShaderCache", "component_crx_cache",
              "extensions_crx_cache", "Crashpad"}
_LOCK_FILES = {"SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile"}


def available(cfg: StudioConfig) -> bool:
    d = (cfg.edge_user_data_dir or "").strip()
    return bool(d) and Path(d).expanduser().exists()


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy src -> dst, skipping cache dirs and tolerating locked files."""
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in _SKIP_DIRS:
            continue
        target = dst / item.name
        try:
            if item.is_dir():
                _copy_tree(item, target)
            else:
                shutil.copy2(item, target)
        except (OSError, shutil.Error):
            # A locked/mid-write file (e.g. Cookies while Edge runs) — skip it;
            # if it was essential the login simply won't verify and we fall back.
            continue


def prepare_copy(cfg: StudioConfig) -> Path | None:
    """Copy the live Edge profile subset into the private working dir.

    Returns the working ``User Data`` path to launch against, or None.
    Re-copies only when the source Cookies file is newer than the last copy.
    """
    if not available(cfg):
        return None
    src_root = Path(cfg.edge_user_data_dir).expanduser()
    profile = cfg.edge_profile_dir or "Default"
    work_root = cfg.edge_automation_path / "User Data"
    marker = cfg.edge_automation_path / ".cookies_mtime"

    src_cookies = src_root / profile / "Network" / "Cookies"
    src_mtime = src_cookies.stat().st_mtime if src_cookies.exists() else 0.0
    fresh = (work_root.exists() and marker.exists()
             and _read_float(marker) >= src_mtime and src_mtime > 0)
    if fresh:
        _strip_locks(work_root / profile)
        return work_root

    try:
        if work_root.exists():
            shutil.rmtree(work_root, ignore_errors=True)
        work_root.mkdir(parents=True, exist_ok=True)
        # The copy holds live auth cookies + the cookie master key -> lock it to
        # the current user immediately (before content lands where possible).
        _lock(cfg.edge_automation_path)
        _lock(work_root)
        ls = src_root / "Local State"   # holds the (DPAPI/ABE-wrapped) key
        if ls.exists():
            shutil.copy2(ls, work_root / "Local State")
        if (src_root / profile).exists():
            _copy_tree(src_root / profile, work_root / profile)
        _strip_locks(work_root / profile)
        # Only mark "fresh" if the essential auth files actually came across
        # (they can be skipped if Edge held a write lock) — else recopy next run.
        essential = (work_root / "Local State").exists() and \
            (work_root / profile / "Network" / "Cookies").exists()
        if essential:
            marker.write_text(str(src_mtime))
        return work_root
    except Exception as exc:  # pragma: no cover
        logger.warning("edge profile copy failed: %s", exc)
        return None


def _lock(path: Path) -> None:
    try:
        from ..vault import _lockdown
        _lockdown(path)
    except Exception:
        pass


def _strip_locks(profile_dir: Path) -> None:
    for name in _LOCK_FILES:
        for p in (profile_dir.parent / name, profile_dir / name):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass


def _read_float(p: Path) -> float:
    try:
        return float(p.read_text().strip())
    except Exception:
        return 0.0


def edge_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq msedge.exe"],
                             capture_output=True, text=True)
        return "msedge.exe" in (out.stdout or "")
    except Exception:
        return False


def ensure_edge_closed(cfg: StudioConfig) -> bool:
    """Return True if Edge is closed (closing it first if configured to)."""
    if not edge_running():
        return True
    if not cfg.edge_close_if_running:
        return False
    logger.info("closing Edge to free the live profile…")
    # Edge's "startup boost" respawns background msedge processes right after a
    # kill, which kept the profile locked and made the whole browser chain fall
    # through ("no logged-in session"). Re-issue the kill while waiting instead
    # of killing once and hoping.
    for attempt in range(4):
        try:
            subprocess.run(["taskkill", "/IM", "msedge.exe", "/F", "/T"],
                           check=False, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except Exception:
            pass
        for _ in range(6):
            if not edge_running():
                time.sleep(0.8)   # let the profile lock release
                if not edge_running():   # startup boost may respawn — re-check
                    return True
                break                    # respawned -> kill again
            time.sleep(0.5)
    return not edge_running()


def reopen_edge(cfg: StudioConfig) -> None:
    """Relaunch Edge normally (detached) so the user's browser comes back after
    an automation that closed it. Best-effort."""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for exe in candidates:
        if Path(exe).exists():
            try:
                subprocess.Popen([exe], close_fds=True)
                return
            except Exception:
                pass
    try:
        subprocess.Popen(["cmd", "/c", "start", "microsoft-edge:"])
    except Exception:
        pass


def open_context(p, cfg: StudioConfig, headless: bool,
                 viewport: tuple[int, int] | None):
    """Launch an msedge context using the live profile (if configured) or a
    safe copy. Returns the context, or None to fall through the chain."""
    if not available(cfg):
        return None
    profile = cfg.edge_profile_dir or "Default"

    if cfg.edge_use_live_profile:
        user_data = Path(cfg.edge_user_data_dir).expanduser()
    else:
        work_root = prepare_copy(cfg)
        if work_root is None:
            return None
        user_data = work_root

    from .playwright_base import launch_persistent
    # Edge's "startup boost" (or the user) can bring msedge back in the seconds
    # between a close and our launch — Playwright then "opens in existing
    # browser session" and times out. So close IMMEDIATELY before each launch
    # attempt and retry the launch itself.
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        if cfg.edge_use_live_profile and not ensure_edge_closed(cfg):
            logger.warning("edge profile locked: Edge is open and could not be "
                           "closed (attempt %d/3)", attempt)
            time.sleep(2.0)
            continue
        try:
            return launch_persistent(p, user_data, headless,
                                     viewport=(viewport or (1280, 900)),
                                     profile_dir=profile)
        except Exception as exc:
            last_exc = exc
            logger.warning("edge launch attempt %d/3 failed (%s)", attempt,
                           str(exc)[:200])
    logger.warning("edge msedge launch failed (%s); falling through", last_exc)
    return None
