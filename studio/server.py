"""FastAPI server + mobile web UI for Daily Shorts Studio.

Flow the phone page drives:

    POST /api/login                 password gate
    GET  /api/status                ollama / platform / model summary
    GET  /api/library               list local source videos to pick from
    POST /api/generate              source (upload | url | local) + count
                                    -> kicks off multi-short generation
    GET  /api/batch/{id}            pre-render progress + the shorts as they land
    GET  /api/job/{id}              one short's status
    POST /api/job/{id}/meta         save edited title/caption/hashtags
    POST /api/job/{id}/generate     redraft a short's metadata with Ollama
    POST /api/job/{id}/publish      publish that short to selected platforms
    GET  /api/preview/{id}          stream a short's rendered mp4

Heavy work (download/transcribe/reframe/publish) runs on a single-worker thread
pool so the GPU is never double-booked and the event loop stays responsive.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
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
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def _token(password: str) -> str:
    return hmac.new(b"daily-shorts-studio", password.encode(),
                    hashlib.sha256).hexdigest()


def create_app(cfg: StudioConfig | None = None) -> FastAPI:
    cfg = cfg or StudioConfig.load()
    cfg.ensure_dirs()
    store = JobStore(cfg.db_path)
    pipeline = StudioPipeline(cfg, store)
    worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio")
    net = ThreadPoolExecutor(max_workers=2, thread_name_prefix="studio-net")
    expected = _token(cfg.app_password) if cfg.app_password else ""
    batches: dict[str, dict] = {}    # pre-render progress per batch
    downloads: dict[str, dict] = {}  # progress per download job

    app = FastAPI(title="Daily Shorts Studio")

    # --- auth --------------------------------------------------------------
    def require_auth(request: Request) -> None:
        if not expected:
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
        return {
            "authed": authed,
            "needs_password": bool(expected),
            "ollama": pipeline.ollama.available() if authed else False,
            "ollama_model": (pipeline.ollama.resolve_model()
                             if authed and pipeline.ollama.available()
                             else cfg.ollama_model),
            "platforms": list(cfg.enabled_platforms),
            "reframe_mode": cfg.reframe_mode,
            "default_count": cfg.shorts_per_video,
        }

    # --- local library -----------------------------------------------------
    @app.get("/api/library")
    async def library(_: None = Depends(require_auth)):
        root = cfg.library_path
        items = []
        if root.exists():
            for p in sorted(root.iterdir()):
                if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                    items.append({"name": p.name,
                                  "size_mb": round(p.stat().st_size / 1e6, 1)})
        return {"dir": str(root), "videos": items}

    def _resolve_local(name: str) -> Path:
        # Only allow files directly inside the configured library (no traversal).
        p = (cfg.library_path / Path(name).name).resolve()
        if not p.exists() or cfg.library_path.resolve() not in p.parents:
            raise HTTPException(404, "video not found in library")
        return p

    # --- download only (a standalone feature) ------------------------------
    @app.post("/api/download")
    async def start_download(url: str = Form(...), _: None = Depends(require_auth)):
        from .downloader import download, next_index
        if not url.strip():
            raise HTTPException(400, "no URL provided")
        did = uuid.uuid4().hex[:12]
        downloads[did] = {"stage": "starting", "done": False, "error": "",
                          "file": ""}

        def prog(d: dict) -> None:
            if d.get("status") == "downloading":
                downloads[did]["stage"] = "downloading " + \
                    d.get("_percent_str", "").strip()
            elif d.get("status") == "finished":
                downloads[did]["stage"] = "finishing…"

        def run() -> None:
            try:
                idx = next_index(cfg.library_path)
                path = download(url.strip(), str(cfg.library_path),
                                prefer_mp4=cfg.download_prefer_mp4,
                                name=str(idx), on_progress=prog)
                downloads[did]["file"] = Path(path).name
                downloads[did]["stage"] = "done"
            except Exception as exc:  # pragma: no cover
                logger.exception("download failed")
                downloads[did]["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                downloads[did]["done"] = True

        net.submit(run)
        return {"ok": True, "download_id": did}

    @app.get("/api/download/{did}")
    async def download_status(did: str, _: None = Depends(require_auth)):
        return downloads.get(did, {"stage": "", "done": True,
                                   "error": "unknown id", "file": ""})

    # --- shorts library (all generated shorts, for publishing later) -------
    @app.get("/api/shorts")
    async def list_shorts(_: None = Depends(require_auth)):
        jobs = [j for j in store.list_recent(60) if j.output_path]
        return {"shorts": [j.to_dict() for j in jobs]}

    # --- generate shorts ---------------------------------------------------
    @app.post("/api/generate")
    async def generate(
        source_type: str = Form(...),          # upload | url | local
        url: str = Form(""),
        name: str = Form(""),
        count: int = Form(0),
        niche: str = Form(""),
        file: UploadFile = File(None),
        _: None = Depends(require_auth),
    ):
        if source_type == "upload":
            if not file:
                raise HTTPException(400, "no file uploaded")
            from .downloader import next_index
            suffix = Path(file.filename or "src.mp4").suffix or ".mp4"
            # Save uploaded long videos into the library as 1.mp4, 2.mp4, … so
            # their shorts follow the same naming (1_1.mp4, …).
            dest = cfg.library_path / f"{next_index(cfg.library_path)}{suffix}"
            with dest.open("wb") as fh:
                while chunk := await file.read(1024 * 1024):
                    fh.write(chunk)
            source = str(dest)
        elif source_type == "url":
            if not url.strip():
                raise HTTPException(400, "no URL provided")
            source = url.strip()
        elif source_type == "local":
            source = str(_resolve_local(name))
        else:
            raise HTTPException(400, "invalid source_type")

        n = count or cfg.shorts_per_video
        batch_id = uuid.uuid4().hex[:12]
        batches[batch_id] = {"stage": "starting", "done": False, "error": ""}

        def on_stage(s: str) -> None:
            if batch_id in batches:
                batches[batch_id]["stage"] = s

        def run() -> None:
            try:
                pipeline.generate_shorts(source, n, niche=niche,
                                         batch_id=batch_id, on_stage=on_stage)
            except Exception as exc:  # pragma: no cover
                logger.exception("batch failed")
                batches[batch_id]["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                batches[batch_id]["done"] = True
                batches[batch_id]["stage"] = "done"

        worker.submit(run)
        return {"ok": True, "batch_id": batch_id, "count": n}

    @app.get("/api/batch/{batch_id}")
    async def get_batch(batch_id: str, _: None = Depends(require_auth)):
        b = batches.get(batch_id, {"stage": "", "done": True, "error": ""})
        shorts = [j.to_dict() for j in store.list_by_batch(batch_id)]
        return {"batch_id": batch_id, "stage": b["stage"], "done": b["done"],
                "error": b["error"], "shorts": shorts}

    # --- per-short ---------------------------------------------------------
    @app.get("/api/job/{job_id}")
    async def get_job(job_id: str, _: None = Depends(require_auth)):
        job = store.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        return job.to_dict()

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
        if not pipeline.ollama.available():
            raise HTTPException(503, "Ollama is not reachable")
        body = await request.json()
        niche = str(body.get("niche", ""))
        language = pipeline._metadata_language("")
        meta = pipeline.ollama.generate_metadata(job.transcript, None, niche,
                                                 language)
        if meta:
            job.meta = meta
            store.update(job)
        return {"ok": True, "meta": job.meta.to_dict()}

    @app.post("/api/job/{job_id}/publish")
    async def publish(job_id: str, request: Request,
                      _: None = Depends(require_auth)):
        job = store.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        if not job.output_path or job.status not in (STATUS_READY, "done"):
            raise HTTPException(409, "short is not ready to publish yet")
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

    @app.get("/api/preview/{job_id}")
    async def preview(job_id: str, _: None = Depends(require_auth)):
        job = store.get(job_id)
        if not job or not job.output_path or not Path(job.output_path).exists():
            raise HTTPException(404, "no preview yet")
        return FileResponse(job.output_path, media_type="video/mp4")

    return app
