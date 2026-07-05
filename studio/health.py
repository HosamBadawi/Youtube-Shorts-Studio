"""Health checks: server self-health and per-platform login health.

``HealthStatus`` is the small value object the publisher also returns (kept here
to avoid an import cycle). ``server_health`` answers "is this box able to do its
job?" (ffmpeg, dirs, Ollama, DB, disk). ``platform_health`` answers "is the
YouTube token still valid?" without uploading anything.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from .config import StudioConfig


@dataclass
class HealthStatus:
    name: str
    ok: bool
    detail: str = ""
    strategy: str = ""
    checked_at: float = 0.0
    screenshot: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "strategy": self.strategy, "checked_at": self.checked_at}


def server_health(cfg: StudioConfig, store=None) -> dict:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = "", critical: bool = True) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail,
                       "critical": critical})

    # workspace writable
    try:
        cfg.ensure_dirs()
        probe = cfg.workspace_path / ".health"
        probe.write_text("ok")
        probe.unlink()
        add("workspace writable", True, str(cfg.workspace_path))
    except Exception as exc:
        add("workspace writable", False, str(exc))

    add("ffmpeg", shutil.which("ffmpeg") is not None, "required for rendering")
    add("ffprobe", shutil.which("ffprobe") is not None)

    # Ollama (optional)
    try:
        from .llm import OllamaClient
        up = OllamaClient(cfg.ollama_url, cfg.ollama_model,
                          cfg.ollama_enabled).available()
        add("ollama", up, cfg.ollama_url, critical=False)
    except Exception as exc:
        add("ollama", False, str(exc), critical=False)

    # DB read
    try:
        if store is not None:
            store.list_recent(1)
        add("database", True)
    except Exception as exc:
        add("database", False, str(exc))

    # disk free
    try:
        free_gb = shutil.disk_usage(cfg.workspace_path).free / 1e9
        add("disk free", free_gb > 2.0, f"{free_gb:.1f} GB free")
    except Exception as exc:
        add("disk free", False, str(exc), critical=False)

    # cloudflared (optional — only for the phone tunnel)
    add("cloudflared", shutil.which(cfg.cloudflared_bin) is not None,
        "remote access tunnel", critical=False)

    ok = all(c["ok"] for c in checks if c["critical"])
    return {"ok": ok, "checks": checks}


def platform_health(platform: str, cfg: StudioConfig, vault=None) -> HealthStatus:
    platform = platform.lower()
    try:
        # get_publisher already returns YouTubePublisher for "youtube" — one
        # uniform dispatch, no special-case.
        from .publishers import get_publisher
        return get_publisher(platform, cfg, vault).health()
    except Exception as exc:  # pragma: no cover
        return HealthStatus(platform, False, f"{type(exc).__name__}: {exc}")
