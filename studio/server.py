"""FastAPI server + mobile web UI for Daily Shorts Studio.

Exposes a tiny REST API the phone page drives:

    POST /api/login                 password gate (single shared password)
    GET  /api/status                ollama/platform/today summary
    POST /api/upload                upload today's video -> starts processing
    GET  /api/job/{id}              poll processing/publish progress
    POST /api/job/{id}/meta         save manually-edited title/caption/hashtags
    POST /api/job/{id}/generate     (re)draft metadata with Ollama
    POST /api/job/{id}/publish      publish to the selected platforms
    GET  /api/preview/{id}          stream the rendered 9:16 mp4

Heavy work (transcribe/reframe/publish) runs on a single-worker thread pool so
the GPU is never asked to do two renders at once and the event loop stays free.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import (Depends, FastAPI, File, Form, HTTPException, Request,
                     Response, UploadFile)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import StudioConfig
from .jobs import STATUS_READY, JobStore
from .metadata import VideoMeta, normalize_hashtags
from .pipeline import StudioPipeline

logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"
COOKIE = "studio_auth"


def _token(password: str) -> str:
    return hmac.new(b"daily-shorts-studio", password.encode(), hashlib.sha256).hexdigest()


def create_app(cfg: StudioConfig | None = None) -> FastAPI:
    cfg = cfg or StudioConfig.load()
    cfg.ensure_dirs()
    store = JobStore(cfg.db_path)
    pipeline = StudioPipeline(cfg, store)
    worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio")
    expected = _token(cfg.app_password) if cfg.app_password else ""

    app = FastAPI(title="Daily Shorts Studio")

    # --- auth --------------------------------------------------------------
    def require_auth(request: Request) -> None:
        if not expected:  # password gate disabled
            return
        if request.cookies.get(COOKIE) != expected:
            raise HTTPException(status_code=401, detail="not authenticated")

    @app.post("/api/login")
    async def login(response: Response, password: str = Form("")):
        if expected and _token(password) != expected:
            raise HTTPException(status_code=401, detail="wrong password")
        resp = JSONResponse({"ok": True})
        if expected:
            resp.set_cookie(COOKIE, expected, httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 30)
        return resp

    # --- pages -------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    # --- status ------------------------------------------------------------
    @app.get("/api/status")
    async def status(request: Request):
        authed = (not expected) or request.cookies.get(COOKIE) == expected
        today = store.todays_job()
        return {
            "authed": authed,
            "needs_password": bool(expected),
            "ollama": pipeline.ollama.available() if authed else False,
            "ollama_model": (pipeline.ollama.resolve_model()
                             if authed and pipeline.ollama.available()
                             else cfg.ollama_model),
            "platforms": list(cfg.enabled_platforms),
            "published_today": store.published_today(),
            "one_per_day": cfg.one_per_day,
            "today_job": today.to_dict() if (authed and today) else None,
        }

    # --- upload / process --------------------------------------------------
    @app.post("/api/upload")
    async def upload(
        request: Request,
        file: UploadFile = File(...),
        auto_metadata: bool = Form(True),
        per_platform: bool = Form(False),
        niche: str = Form(""),
        title: str = Form(""),
        caption: str = Form(""),
        hashtags: str = Form(""),
        _: None = Depends(require_auth),
    ):
        suffix = Path(file.filename or "upload.mp4").suffix or ".mp4"
        job = store.create("")  # get an id first to name the file
        dest = cfg.incoming_dir / f"{job.id}{suffix}"
        with dest.open("wb") as fh:
            while chunk := await file.read(1024 * 1024):
                fh.write(chunk)
        job.source_path = str(dest)
        if title or caption:  # user typed metadata up front -> manual
            job.meta = VideoMeta(title=title, caption=caption,
                                 hashtags=normalize_hashtags(hashtags),
                                 source="manual")
        store.update(job)

        worker.submit(pipeline.process_job, job,
                      auto_metadata=auto_metadata and not (title and caption),
                      per_platform=per_platform, niche=niche)
        return {"ok": True, "job_id": job.id}

    @app.get("/api/job/{job_id}")
    async def get_job(job_id: str, _: None = Depends(require_auth)):
        job = store.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        return job.to_dict()

    @app.get("/api/jobs")
    async def list_jobs(_: None = Depends(require_auth)):
        return {"jobs": [j.to_dict() for j in store.list_recent()]}

    # --- metadata editing --------------------------------------------------
    @app.post("/api/job/{job_id}/meta")
    async def save_meta(job_id: str, request: Request,
                        _: None = Depends(require_auth)):
        job = store.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        data = await request.json()
        meta = VideoMeta.from_dict(data)
        meta.source = "manual" if job.meta.source == "manual" else "ollama+manual"
        job.meta = meta
        store.update(job)
        return {"ok": True, "meta": job.meta.to_dict()}

    @app.post("/api/job/{job_id}/generate")
    async def regenerate(job_id: str, request: Request,
                         _: None = Depends(require_auth)):
        job = store.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        body = await request.json()
        per_platform = bool(body.get("per_platform", False))
        niche = str(body.get("niche", ""))
        if not pipeline.ollama.available():
            raise HTTPException(503, "Ollama is not reachable")
        meta = pipeline._draft_metadata(job.transcript, per_platform, niche)
        job.meta = meta
        store.update(job)
        return {"ok": True, "meta": meta.to_dict()}

    # --- publish -----------------------------------------------------------
    @app.post("/api/job/{job_id}/publish")
    async def publish(job_id: str, request: Request,
                      _: None = Depends(require_auth)):
        job = store.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        if job.status != STATUS_READY and not job.output_path:
            raise HTTPException(409, "job is not ready to publish yet")
        if cfg.one_per_day and store.published_today():
            raise HTTPException(429, "already published today (one_per_day is on)")
        body = await request.json()
        platforms = [p for p in body.get("platforms", [])
                     if p in cfg.enabled_platforms]
        if not platforms:
            raise HTTPException(400, "no valid platforms selected")
        if not job.meta.is_complete():
            raise HTTPException(400, "add a title and caption before publishing")
        worker.submit(pipeline.publish_job, job, platforms)
        return {"ok": True, "platforms": platforms}

    # --- preview -----------------------------------------------------------
    @app.get("/api/preview/{job_id}")
    async def preview(job_id: str, _: None = Depends(require_auth)):
        job = store.get(job_id)
        if not job or not job.output_path or not Path(job.output_path).exists():
            raise HTTPException(404, "no preview yet")
        return FileResponse(job.output_path, media_type="video/mp4")

    return app
