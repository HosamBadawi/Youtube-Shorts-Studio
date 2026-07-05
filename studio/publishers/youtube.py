"""YouTube Shorts publisher via the official YouTube Data API v3 (free).

A video is treated as a Short automatically by YouTube when it is vertical
and <= 3 minutes; ``#Shorts`` is still appended to the title as a harmless
legacy signal (see :meth:`VideoMeta.youtube_title`).

After a successful upload, ``thumbnails.set`` is attempted best-effort:
Shorts support for API thumbnails is account/rollout-dependent, the channel
must be phone-verified, and the call needs the broader ``youtube.force-ssl``
scope — a token authorized before that scope was added will get a 403, which
is recorded (not fatal) with a re-auth hint.

One-time setup (see STUDIO_README.md):
  1. Create an OAuth *Desktop* client in Google Cloud Console, enable the
     "YouTube Data API v3", download the client secret JSON.
  2. Point ``youtube_client_secret`` at it.
  3. `python -m studio.login_setup` opens a browser to authorize; the refresh
     token is cached in ``youtube_token``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import StudioConfig
from ..metadata import VideoMeta
from .base import PublishResult

logger = logging.getLogger(__name__)

# force-ssl covers upload + thumbnails.set. Existing upload-only tokens keep
# working for uploads; thumbnails.set then reports "needs re-auth".
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

_PRIVACY = {"public", "unlisted", "private"}


class YouTubePublisher:
    name = "youtube"

    def __init__(self, cfg: StudioConfig, vault=None) -> None:
        self.cfg = cfg
        self.vault = vault  # unused (official API); kept for a uniform signature
        self.dry_run = False  # verify auth but don't upload

    # ------------------------------------------------------------------
    def health(self):
        """Refresh the cached OAuth token without uploading; report status."""
        import time as _t
        from ..health import HealthStatus
        try:
            self.authorize(interactive=False)
            return HealthStatus(self.name, True, "authorized",
                                strategy="api", checked_at=_t.time())
        except Exception as exc:
            return HealthStatus(self.name, False, f"{type(exc).__name__}: {exc}",
                                checked_at=_t.time())

    # ------------------------------------------------------------------
    def authorize(self, interactive: bool = True):
        """Return authorized credentials, refreshing or running the OAuth flow."""
        from google.auth.transport.requests import Request  # type: ignore
        from google.oauth2.credentials import Credentials  # type: ignore
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore

        token_path = Path(self.cfg.youtube_token)
        secret_path = Path(self.cfg.youtube_client_secret)
        creds = None
        if token_path.exists():
            # Don't pass SCOPES here: an older upload-only token must keep
            # refreshing (thumbnails.set just 403s and is reported).
            creds = Credentials.from_authorized_user_file(str(token_path))
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif interactive:
            if not secret_path.exists():
                raise FileNotFoundError(
                    f"YouTube client secret not found at {secret_path}")
            flow = InstalledAppFlow.from_client_secrets_file(str(secret_path),
                                                             SCOPES)
            creds = flow.run_local_server(port=0)
        else:
            raise RuntimeError("YouTube not authorized; run "
                               "`python -m studio.login_setup`")
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        from ..vault import _lockdown  # OAuth refresh token == publish access
        _lockdown(token_path)
        return creds

    # ------------------------------------------------------------------
    def publish(self, video_path: str, meta: VideoMeta,
                privacy: str | None = None,
                thumb_path: str | None = None) -> PublishResult:
        log: list[str] = []
        try:
            from googleapiclient.discovery import build  # type: ignore
            from googleapiclient.http import MediaFileUpload  # type: ignore
        except Exception:
            return PublishResult.failure(
                self.name,
                "google-api-python-client not installed "
                "(pip install -r requirements-studio.txt)")

        try:
            creds = self.authorize(interactive=False)
        except Exception as exc:
            return PublishResult.failure(self.name, str(exc), needs_login=True,
                                         log=log)

        if self.dry_run:
            log.append("DRY RUN — authorized; would upload via the API "
                       "(nothing posted)")
            return PublishResult.rehearsed(self.name, log=log)

        privacy = (privacy or "").strip().lower()
        if privacy not in _PRIVACY:
            privacy = self.cfg.youtube_privacy

        try:
            youtube = build("youtube", "v3", credentials=creds)
            body = {
                "snippet": {
                    "title": meta.youtube_title(),
                    "description": meta.youtube_description(),
                    "categoryId": self.cfg.youtube_category_id,
                },
                "status": {
                    "privacyStatus": privacy,
                    "selfDeclaredMadeForKids": False,
                },
            }
            # Resumable CHUNKED upload (8 MB chunks) instead of one big request —
            # far more reliable on slow/flaky upload lines, and ``num_retries``
            # auto-retries transient 5xx/network errors per chunk with backoff.
            media = MediaFileUpload(video_path, chunksize=8 * 1024 * 1024,
                                    resumable=True, mimetype="video/mp4")
            req = youtube.videos().insert(part="snippet,status", body=body,
                                          media_body=media)
            response = None
            while response is None:
                status, response = req.next_chunk(num_retries=5)
                if status:
                    log.append(f"upload {int(status.progress() * 100)}%")
            vid = response["id"]
            url = f"https://youtube.com/shorts/{vid}"
            log.append("uploaded")
            thumb = self._set_thumbnail(youtube, vid, thumb_path, log)
            return PublishResult.success(self.name, url=url, video_id=vid,
                                         thumb=thumb, log=log)
        except Exception as exc:  # pragma: no cover - network/API
            return PublishResult.failure(self.name, str(exc), log=log)

    # ------------------------------------------------------------------
    def _set_thumbnail(self, youtube, video_id: str,
                       thumb_path: str | None, log: list[str]) -> str:
        """Best-effort thumbnails.set — never fails the upload."""
        if not thumb_path or not Path(thumb_path).exists():
            return ""
        try:
            from googleapiclient.errors import HttpError  # type: ignore
            from googleapiclient.http import MediaFileUpload  # type: ignore
        except Exception:
            return ""
        try:
            media = MediaFileUpload(thumb_path, mimetype="image/jpeg")
            youtube.thumbnails().set(videoId=video_id,
                                     media_body=media).execute()
            log.append("API thumbnail set")
            return "ok"
        except Exception as exc:
            reason = str(exc)
            hint = "failed"
            if "403" in reason or "insufficient" in reason.lower() \
                    or "forbidden" in reason.lower():
                hint = ("needs re-auth (run `python -m studio.login_setup`) "
                        "or the channel isn't phone-verified")
            elif "429" in reason or "uploadRateLimitExceeded" in reason:
                hint = "rate-limited — try again later"
            logger.info("thumbnails.set failed: %s", reason)
            log.append(f"API thumbnail not set ({hint})")
            return hint
