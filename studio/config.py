"""Runtime configuration for Daily Shorts Studio.

Everything has a sane default so the app boots with zero config; a ``studio.yaml``
(or env vars) only overrides what you care about. Loading is dependency-light:
PyYAML if present, otherwise pure-env fallback - so the server can start even in
a minimal environment.

Precedence (lowest to highest): dataclass defaults < studio.yaml < environment.
Environment variables are prefixed ``STUDIO_`` (e.g. ``STUDIO_OLLAMA_MODEL``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

PLATFORMS = ("youtube", "instagram", "tiktok", "facebook")


@dataclass
class StudioConfig:
    # --- where everything lives ---------------------------------------------
    workspace: str = "./workspace"          # uploads, renders, sessions, db
    host: str = "0.0.0.0"
    port: int = 8765

    # --- access control (the app is reachable from the public internet) -----
    # A single shared password gate. CHANGE THIS. Empty string disables the gate
    # (only safe on a trusted LAN / Tailscale).
    app_password: str = "change-me"

    # --- remote access ------------------------------------------------------
    # "quick"  -> free, ephemeral https URL via `cloudflared tunnel --url` (no
    #             account, URL changes each run).
    # "named"  -> stable URL using a tunnel token (set cloudflare_token).
    # "off"    -> bind locally only, expose it yourself.
    cloudflare_mode: str = "quick"
    cloudflare_token: str = ""              # for cloudflare_mode == "named"
    cloudflared_bin: str = "cloudflared"    # path/name of the cloudflared binary

    # --- local AI (Ollama, hosted on this PC) -------------------------------
    ollama_url: str = "http://localhost:11434"
    # "auto" = let Python query Ollama and pick the most capable installed model.
    # Or pin a specific one, e.g. "llama3.1" / "qwen2.5:14b".
    ollama_model: str = "auto"
    ollama_enabled: bool = True
    # Leave False for thinking models (qwen3, deepseek-r1): they otherwise return
    # empty output. Set True only if you deliberately want chain-of-thought.
    ollama_think: bool = False
    # Seconds to wait for an Ollama response. Big models (e.g. 35B) on a small
    # GPU spill to CPU and need more; bump this if segment/caption picks fail.
    ollama_timeout: float = 240.0
    # Language for the post text (title/caption/hashtags). "auto" = match the
    # video's spoken language; or force e.g. "Arabic" / "English".
    metadata_language: str = "auto"

    # --- transcription ------------------------------------------------------
    whisper_model: str = "base"             # tiny|base|small|medium|large-v3
    whisper_device: str = "auto"            # auto|cpu|cuda  (you have a 3060 Ti)
    whisper_enabled: bool = True
    # Force the spoken language ("ar", "en", …) for much better accuracy than
    # auto-detect. "auto" = let Whisper detect it.
    whisper_language: str = "auto"

    # --- short selection ----------------------------------------------------
    # When a long video is uploaded, the target length of the auto-picked short.
    target_short_seconds: float = 55.0
    # Hard floor: a picked short is never shorter than this (extended if needed).
    min_short_seconds: float = 50.0
    # Hard ceiling: a picked short is never longer than this (trimmed if needed).
    max_short_seconds: float = 60.0
    # If the source is already <= this, skip segmentation and use it whole.
    keep_whole_if_under_seconds: float = 90.0
    # Default number of distinct shorts to cut from one long video (studio.shorts).
    shorts_per_video: int = 3

    # --- downloading source videos from a URL (yt-dlp) ----------------------
    download_prefer_mp4: bool = True

    # Reframing technique. "auto" = let the engine classify per clip; or force
    # one for every short: crop_blur | blur_background | face_focus |
    # active_speaker | smart_crop | mirror_background | dynamic_canvas | no_crop.
    reframe_mode: str = "auto"

    # --- burned-in TikTok-style captions ------------------------------------
    captions_enabled: bool = True
    caption_font: str = "Arial"
    caption_fontsize: int = 96
    caption_highlight: str = "#FFE000"   # color of the word being spoken
    caption_base_color: str = "#FFFFFF"
    caption_position: str = "lower"      # lower | center | bottom
    caption_max_words: int = 4           # words on screen at once

    # --- publishing ---------------------------------------------------------
    enabled_platforms: list[str] = field(default_factory=lambda: list(PLATFORMS))
    one_per_day: bool = True                # block a 2nd publish on the same day
    playwright_headless: bool = True        # set False to watch automation run

    # YouTube Data API (the only platform using an official API).
    youtube_client_secret: str = "./secrets/youtube_client_secret.json"
    youtube_token: str = "./secrets/youtube_token.json"
    youtube_privacy: str = "public"         # public|unlisted|private
    youtube_category_id: str = "22"         # 22 = People & Blogs

    # ------------------------------------------------------------------------
    @property
    def workspace_path(self) -> Path:
        return Path(self.workspace).expanduser().resolve()

    @property
    def incoming_dir(self) -> Path:
        return self.workspace_path / "incoming"

    @property
    def download_dir(self) -> Path:
        return self.workspace_path / "downloads"

    @property
    def rendered_dir(self) -> Path:
        return self.workspace_path / "rendered"

    @property
    def sessions_dir(self) -> Path:
        return self.workspace_path / "sessions"  # Playwright per-platform logins

    @property
    def db_path(self) -> Path:
        return self.workspace_path / "studio.db"

    def session_dir_for(self, platform: str) -> Path:
        return self.sessions_dir / platform

    def ensure_dirs(self) -> None:
        for p in (self.incoming_dir, self.rendered_dir, self.sessions_dir):
            p.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None) -> "StudioConfig":
        """Build config from defaults <- yaml file <- environment."""
        data: dict[str, Any] = {}

        candidate = Path(path) if path else Path("studio.yaml")
        if candidate.exists():
            data.update(_read_yaml(candidate))

        cfg = cls()
        for f in fields(cls):
            if f.name in data and data[f.name] is not None:
                setattr(cfg, f.name, data[f.name])
            env_key = "STUDIO_" + f.name.upper()
            if env_key in os.environ:
                setattr(cfg, f.name, _coerce(os.environ[env_key], getattr(cfg, f.name)))
        return cfg


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception:  # pragma: no cover - PyYAML missing
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _coerce(raw: str, current: Any) -> Any:
    """Coerce an env string toward the type of the current default value."""
    if isinstance(current, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int):
        try:
            return int(raw)
        except ValueError:
            return current
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError:
            return current
    if isinstance(current, list):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return raw
