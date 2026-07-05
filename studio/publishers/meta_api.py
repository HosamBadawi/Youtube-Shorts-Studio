"""Official Meta Graph API publishers for Facebook Pages + Instagram.

Reliable alternative to browser automation: resumable/server-managed uploads, no
CAPTCHA, and a real post id on success. Free for accounts you own (Development
Mode — no App Review). The Page access token is read from the encrypted vault
(``vault.get_api_key("meta_page")``); never store it in yaml.

- :class:`FacebookApiPublisher` — Page Reels via ``/{page-id}/video_reels``
  (start -> resumable binary upload -> finish=PUBLISHED). Self-contained: needs
  only the token, the Page id, and the local file.
- :class:`InstagramApiPublisher` — Content Publishing via ``/{ig-id}/media``
  (create container -> poll FINISHED -> ``/media_publish``). IG fetches the video
  from a PUBLIC url, so the caller supplies ``video_url`` (served via the tunnel).

Uses stdlib urllib only (no new deps), matching the rest of the codebase.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from ..config import StudioConfig
from ..metadata import VideoMeta
from .base import PublishResult

logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"


def _probe_seconds(path: str) -> float:
    """Duration of a local video (0.0 on failure — then the API path is tried
    and Meta's own validation is the backstop)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", path],
            capture_output=True, text=True, timeout=60).stdout
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return 0.0


class _MetaBase:
    name = ""

    def __init__(self, cfg: StudioConfig, vault=None) -> None:
        self.cfg = cfg
        self.vault = vault
        self.dry_run = False
        self.on_attempt = None
        self.ver = (cfg.meta_graph_version or "v21.0").strip()

    # --- token + low-level HTTP --------------------------------------------
    def _token(self) -> str:
        if self.vault and getattr(self.vault, "enabled", False):
            return self.vault.get_api_key("meta_page") or ""
        return ""

    def _get(self, path: str, params: dict, timeout: float = 30.0) -> dict:
        # Token in the Authorization header, never the query string (URLs land in
        # access/proxy logs).
        token = params.pop("access_token", "")
        q = urllib.parse.urlencode(params)
        url = f"{GRAPH}/{self.ver}/{path}" + (f"?{q}" if q else "")
        headers = {"Authorization": f"OAuth {token}"} if token else {}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post(self, path: str, params: dict, timeout: float = 60.0) -> dict:
        token = params.pop("access_token", "")
        url = f"{GRAPH}/{self.ver}/{path}"
        data = urllib.parse.urlencode(params).encode("utf-8")
        headers = {"Authorization": f"OAuth {token}"} if token else {}
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    @staticmethod
    def _err(exc: Exception) -> str:
        if isinstance(exc, urllib.error.HTTPError):
            try:
                body = json.loads(exc.read().decode("utf-8"))
                e = body.get("error", {})
                return (f"{e.get('type','HTTPError')}: {e.get('message', exc)}"
                        f" (code {e.get('code')})")
            except Exception:
                return f"HTTP {exc.code}"
        return f"{type(exc).__name__}: {exc}"

    # --- health -------------------------------------------------------------
    def health(self):
        import time as _t
        from ..health import HealthStatus
        if not self._token():
            return HealthStatus(self.name, False, "no Meta Page token saved",
                                checked_at=_t.time())
        try:
            self._check()
            return HealthStatus(self.name, True, "Meta API authorized",
                                strategy="api", checked_at=_t.time())
        except Exception as exc:
            return HealthStatus(self.name, False, self._err(exc),
                                checked_at=_t.time())

    def _check(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class FacebookApiPublisher(_MetaBase):
    name = "facebook"

    def _check(self) -> None:
        self._get(self.cfg.facebook_page_id or "me",
                  {"fields": "id,name", "access_token": self._token()})

    def publish(self, video_path: str, meta: VideoMeta) -> PublishResult:
        log: list[str] = []
        token = self._token()
        page_id = (self.cfg.facebook_page_id or "").strip()
        if not token:
            return PublishResult.failure(self.name, "no Meta Page token saved",
                                         needs_login=True)
        if not page_id:
            return PublishResult.failure(self.name, "facebook_page_id not set")
        caption = meta.caption_for("facebook")
        if self.dry_run:
            return PublishResult.rehearsed(
                self.name, log=["DRY RUN — token+page ok; would upload via the "
                                "Page Reels API (nothing posted)"])
        try:
            # 1) start -> {video_id, upload_url}
            start = self._post(f"{page_id}/video_reels",
                               {"upload_phase": "start", "access_token": token})
            video_id = start["video_id"]
            upload_url = start["upload_url"]
            log.append(f"reel init {video_id}")
            # 2) resumable binary upload (single shot; Meta accepts the whole file)
            with open(video_path, "rb") as fh:
                body = fh.read()
            req = urllib.request.Request(upload_url, data=body, method="POST",
                                         headers={
                                             "Authorization": f"OAuth {token}",
                                             "offset": "0",
                                             "file_size": str(len(body)),
                                         })
            with urllib.request.urlopen(req, timeout=max(120.0,
                                        self.cfg.publish_upload_timeout)) as r:
                up = json.loads(r.read().decode("utf-8"))
            if not up.get("success", True):
                return PublishResult.failure(self.name, f"upload failed: {up}",
                                             log=log)
            log.append("uploaded")
            # 3) finish -> PUBLISHED
            fin = self._post(f"{page_id}/video_reels",
                             {"upload_phase": "finish", "video_id": video_id,
                              "video_state": "PUBLISHED", "description": caption,
                              "access_token": token})
            log.append(f"finish: {fin}")
            url = f"https://www.facebook.com/reel/{video_id}"
            return PublishResult.success(self.name, url=url, log=log)
        except Exception as exc:
            return PublishResult.failure(self.name, self._err(exc), log=log)


class InstagramApiPublisher(_MetaBase):
    name = "instagram"

    def _check(self) -> None:
        ig = (self.cfg.instagram_business_id or "").strip()
        if not ig:
            raise RuntimeError("instagram_business_id not set")
        self._get(ig, {"fields": "id,username", "access_token": self._token()})

    def publish(self, video_path: str, meta: VideoMeta,
                video_url: str | None = None) -> PublishResult:
        log: list[str] = []
        token = self._token()
        ig = (self.cfg.instagram_business_id or "").strip()
        if not token:
            return PublishResult.failure(self.name, "no Meta Page token saved",
                                         needs_login=True)
        if not ig:
            return PublishResult.failure(self.name, "instagram_business_id not set")
        # HYBRID: the IG API rejects reels over ~90s (verified: 85s FINISHED,
        # 172s ERROR 2207077) even though the app allows 3 min. Longer reels
        # automatically fall back to the (proven) browser publisher.
        limit = float(getattr(self.cfg, "instagram_api_max_seconds", 90.0) or 90.0)
        dur = _probe_seconds(video_path)
        if dur > limit:
            log.append(f"reel is {dur:.0f}s > the IG API's {limit:.0f}s cap — "
                       "falling back to the browser publisher")
            from .instagram import InstagramPublisher

            browser = InstagramPublisher(self.cfg, self.vault)
            browser.dry_run = self.dry_run
            browser.on_attempt = self.on_attempt
            res = browser.publish(video_path, meta)
            res.log = log + list(res.log or [])
            return res
        caption = meta.caption_for("instagram")
        if self.dry_run:
            return PublishResult.rehearsed(
                self.name, log=["DRY RUN — token+ig ok; would publish a Reel via "
                                "the Content Publishing API (nothing posted)"])
        if not video_url:
            # Self-serve: register the file as a short-lived public share on the
            # tunnel so Meta's servers can download it (they can't log in).
            from ..cloudflared import public_url
            from ..public_share import register
            base = public_url(self.cfg)
            if not base:
                return PublishResult.failure(
                    self.name, "IG API needs the public tunnel URL and none is "
                               "active — is the Cloudflare tunnel running?")
            video_url = f"{base}/pub/{register(video_path)}"
            log.append("registered public share for Meta download")
        try:
            # 1) create the REELS container (IG fetches the file from video_url)
            cont = self._post(f"{ig}/media",
                              {"media_type": "REELS", "video_url": video_url,
                               "caption": caption, "access_token": token})
            cid = cont["id"]
            log.append(f"container {cid}")
            # 2) poll until Meta finishes downloading/processing the video
            deadline = time.time() + max(180.0, self.cfg.publish_upload_timeout)
            status = ""
            while time.time() < deadline:
                st = self._get(cid, {"fields": "status_code",
                                     "access_token": token})
                status = st.get("status_code", "")
                if status in ("FINISHED", "ERROR", "EXPIRED"):
                    break
                time.sleep(5)
            log.append(f"container status {status}")
            if status != "FINISHED":
                return PublishResult.failure(
                    self.name, f"container not ready ({status})", log=log)
            # 3) publish
            pub = self._post(f"{ig}/media_publish",
                             {"creation_id": cid, "access_token": token})
            mid = pub.get("id", "")
            log.append(f"published {mid}")
            return PublishResult.success(
                self.name, url=f"https://www.instagram.com/reel/{mid}", log=log)
        except Exception as exc:
            return PublishResult.failure(self.name, self._err(exc), log=log)
