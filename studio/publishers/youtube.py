"""YouTube Shorts publisher via the official YouTube Data API v3 (free).

A video is treated as a Short automatically by YouTube when it is vertical and
<= 60s; we additionally ensure ``#Shorts`` is present in the title (handled in
:meth:`VideoMeta.title_for`).

One-time setup (see STUDIO_README.md):
  1. Create an OAuth *Desktop* client in Google Cloud Console, enable the
     "YouTube Data API v3", download the client secret JSON.
  2. Point ``youtube_client_secret`` at it.
  3. First publish (or `python -m studio.login_setup youtube`) opens a browser
     to authorize; the refresh token is cached in ``youtube_token``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import StudioConfig
from ..metadata import VideoMeta
from .base import PublishResult

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class YouTubePublisher:
    name = "youtube"

    def __init__(self, cfg: StudioConfig) -> None:
        self.cfg = cfg

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
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
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
                               "`python -m studio.login_setup youtube`")
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    # ------------------------------------------------------------------
    def publish(self, video_path: str, meta: VideoMeta) -> PublishResult:
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

        try:
            youtube = build("youtube", "v3", credentials=creds)
            body = {
                "snippet": {
                    "title": meta.title_for("youtube"),
                    "description": meta.caption_for("youtube"),
                    "categoryId": self.cfg.youtube_category_id,
                },
                "status": {
                    "privacyStatus": self.cfg.youtube_privacy,
                    "selfDeclaredMadeForKids": False,
                },
            }
            media = MediaFileUpload(video_path, chunksize=-1, resumable=True,
                                    mimetype="video/mp4")
            req = youtube.videos().insert(part="snippet,status", body=body,
                                          media_body=media)
            response = None
            while response is None:
                status, response = req.next_chunk()
                if status:
                    log.append(f"upload {int(status.progress() * 100)}%")
            vid = response["id"]
            url = f"https://youtube.com/shorts/{vid}"
            log.append("published")
            return PublishResult.success(self.name, url=url, log=log)
        except Exception as exc:  # pragma: no cover - network/API
            return PublishResult.failure(self.name, str(exc), log=log)
