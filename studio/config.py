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
    # Set the Secure flag on the auth cookie (recommended when reached only over
    # the HTTPS tunnel). Leave False if you also log in over plain-HTTP LAN.
    cookie_secure: bool = False
    # Max failed logins per client IP within 5 min before a temporary lockout.
    login_max_attempts: int = 8

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

    # --- which AI writes captions / picks segments --------------------------
    # ollama (local) | openai | anthropic | gemini. Chosen from the web UI;
    # cloud API keys are stored ENCRYPTED in the vault, never in this file.
    llm_provider: str = "ollama"
    llm_model: str = ""   # model id for the provider ("" = sensible default)

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
    one_per_day: bool = False               # block a 2nd publish on the same day
    playwright_headless: bool = True        # set False to watch automation run

    # Post Facebook Reels to a PAGE instead of your personal profile. Set this to
    # the Page URL (e.g. https://www.facebook.com/profile.php?id=...) and the
    # publisher starts the Reel from the Page so it's posted as the Page. Blank =
    # post to your personal profile.
    facebook_page_url: str = ""

    # Folder the web app browses for local source videos ("" = use download_dir).
    media_library: str = ""

    # --- publish reliability (browser-automation platforms) -----------------
    publish_max_attempts: int = 3           # retries per platform per publish
    publish_backoff_base: float = 2.0       # seconds; exponential w/ full jitter
    publish_backoff_factor: float = 2.0
    publish_backoff_max: float = 30.0
    screenshot_on_failure: bool = True      # save a screenshot on a failed try
    health_check_timeout: float = 45.0      # per-platform health/login budget (s)
    # How long to wait for the video upload to FINISH before clicking the final
    # Post/Share button (platforms disable it mid-upload). Raise for slow upload
    # speeds — a long short on a slow line can take many minutes.
    publish_upload_timeout: float = 1200.0  # seconds (20 min)
    move_uploaded_on_success: bool = True   # move the short to uploaded/ when done

    # --- session strategy (how a logged-in browser is obtained) -------------
    # auto = try the whole chain [edge_profile -> saved_session ->
    # credentials_login]; or pin one. Per-platform overrides win.
    session_strategy: str = "auto"
    session_strategy_overrides: dict = field(default_factory=dict)

    # Reuse the logins saved in your Microsoft Edge profile. Empty disables it.
    # Point at the Edge "User Data" dir, e.g.
    #   C:/Users/<you>/AppData/Local/Microsoft/Edge/User Data
    edge_user_data_dir: str = ""
    edge_profile_dir: str = "Default"       # which profile (see edge://version)
    # Private working dir the profile is COPIED into (default = safe copy mode).
    edge_automation_dir: str = ""           # "" -> secrets/edge_profile
    # Use the LIVE Edge profile directly (your exact cookies/extensions) instead
    # of a copy. Requires Edge to be CLOSED while automation runs.
    edge_use_live_profile: bool = False
    # If Edge is open when automation needs the profile, close it automatically
    # (so a remote publish isn't blocked by a profile lock).
    edge_close_if_running: bool = False
    # After an interactive health/connect check that closed Edge, reopen it so
    # your everyday browser comes back. (Not done after a publish, to avoid the
    # browser flapping open/closed between platforms.)
    edge_reopen_after: bool = False

    # --- credential vault (encrypted at rest) -------------------------------
    vault_db: str = "./secrets/vault.db"
    # Password used to scrypt-wrap the vault key for portable recovery. Empty ->
    # use app_password. (DPAPI is the no-prompt primary unwrap on Windows.)
    vault_recovery_password: str = ""

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
    def library_path(self) -> Path:
        """Where long source videos live (downloads land here as 1.mp4, 2.mp4…)."""
        return Path(self.media_library).expanduser() if self.media_library \
            else self.download_dir

    @property
    def shorts_dir(self) -> Path:
        return self.workspace_path / "shorts"

    @property
    def uploaded_dir(self) -> Path:
        return self.workspace_path / "uploaded"

    @property
    def rendered_dir(self) -> Path:
        return self.workspace_path / "rendered"

    @property
    def sessions_dir(self) -> Path:
        return self.workspace_path / "sessions"  # Playwright per-platform logins

    @property
    def db_path(self) -> Path:
        return self.workspace_path / "studio.db"

    @property
    def failures_dir(self) -> Path:
        # Failure screenshots. NOT under web/static — never web-served.
        return self.workspace_path / "failures"

    @property
    def rehearsals_dir(self) -> Path:
        # Dry-run composer screenshots, served (auth-gated) via /api/rehearsal.
        return self.workspace_path / "rehearsals"

    @property
    def secrets_dir(self) -> Path:
        return Path(self.vault_db).expanduser().resolve().parent

    @property
    def vault_path(self) -> Path:
        return Path(self.vault_db).expanduser().resolve()

    @property
    def edge_automation_path(self) -> Path:
        # Default under the locked-down secrets/ dir: the copy holds live auth
        # cookies + the cookie master key, so it must be ACL-restricted + ignored.
        if self.edge_automation_dir:
            return Path(self.edge_automation_dir).expanduser()
        return self.secrets_dir / "edge_profile"

    def session_strategy_for(self, platform: str) -> str:
        ov = self.session_strategy_overrides or {}
        return str(ov.get(platform, self.session_strategy) or "auto")

    def session_dir_for(self, platform: str) -> Path:
        return self.sessions_dir / platform

    def ensure_dirs(self) -> None:
        for p in (self.incoming_dir, self.rendered_dir, self.sessions_dir,
                  self.download_dir, self.shorts_dir, self.uploaded_dir,
                  self.library_path, self.failures_dir, self.rehearsals_dir,
                  self.secrets_dir):
            p.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        """Advisory: clamp a typo'd enum field to a safe default — loud but
        non-fatal, matching the YAML-parse fallback. A bad ``cloudflare_mode``
        clamps to ``off`` so a typo can never silently open a public tunnel."""
        import logging
        enums = {
            "cloudflare_mode": ({"quick", "named", "off"}, "off"),
            "reframe_mode": ({"auto", "crop_blur", "blur_background",
                              "face_focus", "active_speaker", "smart_crop",
                              "mirror_background", "dynamic_canvas", "no_crop",
                              "scene_aware"}, "auto"),
            "caption_position": ({"lower", "center", "bottom"}, "lower"),
            "whisper_device": ({"auto", "cpu", "cuda"}, "auto"),
            "session_strategy": ({"auto", "edge_profile", "saved_session",
                                  "credentials_login"}, "auto"),
            "youtube_privacy": ({"public", "unlisted", "private"}, "private"),
        }
        log = logging.getLogger(__name__)
        for name, (allowed, safe) in enums.items():
            val = str(getattr(self, name, "") or "").strip().lower()
            if val and val not in allowed:
                log.error("config: %s=%r is invalid — using %r (allowed: %s)",
                          name, val, safe, ", ".join(sorted(allowed)))
                print(f"\n⚠️  config: {name}={val!r} is not valid — using "
                      f"{safe!r}.\n")
                setattr(self, name, safe)

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
                cur = getattr(cfg, f.name)
                if isinstance(cur, dict):
                    continue  # dicts (e.g. strategy overrides) come from YAML only
                setattr(cfg, f.name, _coerce(os.environ[env_key], cur))
        cfg.validate()
        return cfg


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception:  # pragma: no cover - PyYAML missing
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        # A typo (e.g. '=' instead of ':') would otherwise silently revert ALL
        # settings to defaults — make that loud instead.
        import logging
        logging.getLogger(__name__).error(
            "FAILED to parse %s (%s) — running on DEFAULTS until fixed!",
            path, exc)
        print(f"\n⚠️  Could not parse {path}: {exc}\n"
              f"   Using DEFAULT settings until you fix it (check for '=' vs ':' "
              f"and use forward slashes in Windows paths).\n")
        return {}
    if data is not None and not isinstance(data, dict):
        print(f"\n⚠️  {path} is not a key: value mapping — using defaults.\n")
        return {}
    return data or {}


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
