"""Runtime configuration for YouTube Shorts Studio.

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


@dataclass
class StudioConfig:
    # --- where everything lives ---------------------------------------------
    workspace: str = "./workspace"          # uploads, renders, db
    host: str = "0.0.0.0"
    port: int = 8765

    # --- access control (the app is reachable from the public internet) -----
    # A single shared password gate. CHANGE THIS. Empty string disables the gate
    # (only safe on a trusted LAN / Tailscale).
    app_password: str = "change-me"
    # Set the Secure flag on the auth cookie (recommended when reached only over
    # the HTTPS tunnel). Leave False if you also log in over plain-HTTP LAN.
    cookie_secure: bool = False
    # Failed logins within 5 min before a temporary lockout (the tunnel
    # collapses every client to one IP, so this is a global cap).
    login_max_attempts: int = 20
    # Largest video upload accepted (MB). Guards against a disk-fill DoS.
    max_upload_mb: int = 2048

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
    # "auto" = pick the most capable installed model THAT FITS the GPU (a 35B
    # spills to system RAM on an 8 GB card and crawls). Or pin one —
    # "command-r7b-arabic" performs best on Arabic content; "qwen2.5:7b" is a
    # good multilingual pick.
    ollama_model: str = "auto"
    ollama_enabled: bool = True
    # Leave False for thinking models (qwen3, deepseek-r1): they otherwise return
    # empty output. Set True only if you deliberately want chain-of-thought.
    ollama_think: bool = False
    # Seconds to wait for an Ollama response. Big models on a small GPU spill to
    # CPU and need more; bump this if segment/caption picks fail.
    ollama_timeout: float = 240.0
    # Language for the post text (title/description). "auto" = match the
    # video's spoken language; or force e.g. "Arabic" / "English".
    metadata_language: str = "auto"

    # --- which AI writes copy / picks segments ------------------------------
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
    # NEVER cut a short from the opening of the long video — the intro/greeting/
    # setup is boring. A short must start no earlier than this many seconds in...
    intro_skip_seconds: float = 45.0
    # ...or this fraction of the runtime, whichever is LARGER (so a 90-min video
    # skips minutes of intro, not just 45s). Always leaves room for a full clip.
    intro_skip_frac: float = 0.05
    # Default number of distinct shorts to cut from one long video. This is a
    # MAXIMUM: if the AI finds fewer genuinely strong self-contained segments,
    # you get fewer (with the reason shown) instead of padded random cuts.
    shorts_per_video: int = 3

    # --- semantic segmentation (the map->validate->reduce selector) ---------
    # Ask SponsorBlock (crowdsourced, privacy-preserving lookup) which parts of
    # a YouTube video are sponsor/intro/outro segments and never cut from them.
    sponsorblock_enabled: bool = True
    # Sentences per LLM window and overlap between windows. Small local models
    # degrade on long inputs — keep windows in the ~3-5 minute range.
    segment_window_sentences: int = 32
    segment_overlap_sentences: int = 8

    # --- montage: cut silences so shorts feel fast and addictive ------------
    silence_cut_enabled: bool = True
    # A gap between spoken words longer than this (seconds) is removed...
    silence_min_gap: float = 0.45
    # ...keeping this much padding around the speech on each side.
    silence_pad: float = 0.12
    # Alternate a subtle punch-in zoom between jump cuts (classic montage feel).
    montage_zoom: bool = True
    montage_zoom_factor: float = 1.06

    # --- subscribe reminder overlay (burned into every short) ---------------
    subscribe_overlay_enabled: bool = True
    # When the animation appears, as a fraction of the short's duration.
    subscribe_at_frac: float = 0.4
    subscribe_duration: float = 3.0         # seconds on screen
    subscribe_sound: bool = True            # soft bell "ding" with the popup
    subscribe_text: str = "اشترك"           # button label before the click
    subscribed_text: str = "تم الاشتراك"     # button label after the click

    # --- AI thumbnails -------------------------------------------------------
    thumbs_enabled: bool = True
    # auto = pick per short; or force one: blur (blurred video frame),
    # burst (radial rays), flat (brand color + vignette).
    thumb_template: str = "auto"
    # Run the background-removal model on the GPU. Default False: CPU keeps the
    # whole 8 GB free for Ollama/Whisper and one thumbnail only takes seconds.
    thumb_use_gpu: bool = False

    # --- downloading source videos from a URL (yt-dlp) ----------------------
    download_prefer_mp4: bool = True
    # SSRF guard: only these hosts (+ subdomains) may be downloaded from. Empty =
    # any PUBLIC host (still blocks private/loopback/link-local/CGNAT). Keep it
    # tight since the URL is an authenticated-but-powerful input.
    download_host_allowlist: list[str] = field(
        default_factory=lambda: ["youtube.com", "youtu.be"])

    # Reframing technique. "auto" = let the engine classify per clip; or force
    # one for every short: crop_blur | blur_background | face_focus |
    # active_speaker | smart_crop | mirror_background | dynamic_canvas | no_crop.
    reframe_mode: str = "auto"

    # --- burned-in karaoke captions ------------------------------------------
    captions_enabled: bool = True
    caption_font: str = "Arial"
    caption_fontsize: int = 96
    caption_highlight: str = "#FFE000"   # color of the word being spoken
    caption_base_color: str = "#FFFFFF"
    caption_position: str = "lower"      # lower | center | bottom
    caption_max_words: int = 4           # words on screen at once

    # --- uploading to YouTube -------------------------------------------------
    # Since June 2026 the free Data API quota allows ~100 uploads/day, so the
    # old one-per-day guard is off by default. Turn it on to pace yourself.
    one_per_day: bool = False
    move_uploaded_on_success: bool = True   # move the short to uploaded/ when done
    # Bake the generated thumbnail in as the first ~0.1s of the video. This is
    # the only Shorts-thumbnail mechanism that works on every account (the API
    # thumbnail is also attempted, but Shorts support for it is rollout-gated).
    embed_thumb_first_frame: bool = True

    # Folder the web app browses for local source videos ("" = use download_dir).
    media_library: str = ""

    # --- credential vault (encrypted at rest, stores cloud LLM API keys) ----
    vault_db: str = "./secrets/vault.db"
    # Password used to scrypt-wrap the vault key for portable recovery. Empty ->
    # use app_password. (DPAPI is the no-prompt primary unwrap on Windows.)
    vault_recovery_password: str = ""

    # --- YouTube Data API (the official, free upload path) -------------------
    youtube_client_secret: str = "./secrets/youtube_client_secret.json"
    youtube_token: str = "./secrets/youtube_token.json"
    youtube_privacy: str = "public"         # public|unlisted|private (default;
    #                                         overridable per short in the UI)
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
    def thumbs_dir(self) -> Path:
        return self.workspace_path / "thumbs"

    @property
    def assets_dir(self) -> Path:
        """Generated runtime assets (subscribe-overlay frames, bell sound)."""
        return self.workspace_path / "assets"

    @property
    def db_path(self) -> Path:
        return self.workspace_path / "studio.db"

    @property
    def secrets_dir(self) -> Path:
        return Path(self.vault_db).expanduser().resolve().parent

    @property
    def vault_path(self) -> Path:
        return Path(self.vault_db).expanduser().resolve()

    def ensure_dirs(self) -> None:
        for p in (self.incoming_dir, self.rendered_dir, self.download_dir,
                  self.shorts_dir, self.uploaded_dir, self.library_path,
                  self.thumbs_dir, self.assets_dir, self.secrets_dir):
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
            "youtube_privacy": ({"public", "unlisted", "private"}, "private"),
            "thumb_template": ({"auto", "blur", "burst", "flat"}, "auto"),
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
                    continue  # dicts come from YAML only
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
