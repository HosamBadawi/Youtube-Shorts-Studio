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

import asyncio
import hashlib
import hmac
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

from starlette.concurrency import run_in_threadpool
from pathlib import Path

from fastapi import (Depends, FastAPI, File, Form, HTTPException, Request,
                     Response, UploadFile)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import StudioConfig
from .jobs import STATUS_READY, JobStore
from .metadata import VideoMeta, normalize_hashtags
from .pipeline import StudioPipeline
from .server_connections import build_connections_router, load_overrides
from .server_models import build_models_router, load_llm_selection
from .vault import CredentialVault, _lockdown

logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"
COOKIE = "studio_auth"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
DEFAULT_PASSWORD = "change-me"


def _gate_key(cfg: StudioConfig) -> bytes:
    """Per-install random HMAC key for the auth cookie (persisted in secrets/).
    Replaces a hardcoded key so cookies aren't forgeable across installs."""
    p = cfg.secrets_dir / "gate.key"
    try:
        if p.exists():
            return bytes.fromhex(p.read_text().strip())
    except Exception:
        pass
    key = os.urandom(32)
    try:
        cfg.secrets_dir.mkdir(parents=True, exist_ok=True)
        p.write_text(key.hex())
        _lockdown(p)
    except Exception:
        pass
    return key


def _token(key: bytes, password: str) -> str:
    return hmac.new(key, password.encode(), hashlib.sha256).hexdigest()


def create_app(cfg: StudioConfig | None = None) -> FastAPI:
    cfg = cfg or StudioConfig.load()
    cfg.ensure_dirs()
    load_overrides(cfg)
    load_llm_selection(cfg)
    store = JobStore(cfg.db_path)
    vault = CredentialVault(cfg)
    pipeline = StudioPipeline(cfg, store, vault)
    worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio")
    net = ThreadPoolExecutor(max_workers=2, thread_name_prefix="studio-net")
    # Publishing drives a browser (no GPU). Its own single-slot pool keeps a long
    # publish from head-of-line-blocking GPU renders on `worker`, while staying
    # single-slot so two browser sessions never contend over the live Edge profile.
    publisher = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-pub")
    gate_key = _gate_key(cfg)
    expected = _token(gate_key, cfg.app_password) if cfg.app_password else ""
    batches: dict[str, dict] = {}    # pre-render progress per batch
    downloads: dict[str, dict] = {}  # progress per download job
    runs: dict[str, dict] = {}       # progress per connection health/login run

    def _cap(d: dict, limit: int = 40) -> None:
        """Evict completed entries so these progress dicts don't grow unbounded
        on a 24/7 host (only ever-`done` entries are dropped)."""
        if len(d) > limit:
            done = [k for k, v in list(d.items()) if v.get("done")]
            for k in done[: len(d) - limit]:
                d.pop(k, None)

    if cfg.app_password == DEFAULT_PASSWORD:
        logger.warning("app_password is still the default '%s' — change it in "
                       "studio.yaml before exposing the tunnel!", DEFAULT_PASSWORD)

    app = FastAPI(title="Daily Shorts Studio")
    # Mark the cookie Secure whenever the app is reachable over the HTTPS tunnel
    # (so the 30-day auth cookie can't leak over a plain-HTTP hop).
    cookie_secure = cfg.cookie_secure or (cfg.cloudflare_mode != "off")

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; media-src 'self'; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "frame-ancestors 'none'")
        if cookie_secure:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    # --- auth --------------------------------------------------------------
    def require_auth(request: Request) -> None:
        if not expected:
            return
        if not hmac.compare_digest(request.cookies.get(COOKIE) or "", expected):
            raise HTTPException(status_code=401, detail="not authenticated")

    # Global (not per-IP — the tunnel collapses every client to one IP) backoff:
    # each recent failure slows the next attempt, capping the brute-force rate
    # without ever hard-locking out the one legitimate user.
    auth_fails: list[float] = []

    @app.post("/api/login")
    async def login(request: Request, password: str = Form("")):
        import time as _t
        now = _t.time()
        auth_fails[:] = [t for t in auth_fails if now - t < 300]
        if expected and not hmac.compare_digest(_token(gate_key, password),
                                                expected):
            auth_fails.append(now)
            await asyncio.sleep(min(0.5 * (2 ** min(len(auth_fails), 6)), 15.0))
            raise HTTPException(status_code=401, detail="wrong password")
        auth_fails.clear()
        resp = JSONResponse({"ok": True})
        if expected:
            resp.set_cookie(COOKIE, expected, httponly=True, samesite="lax",
                            secure=cookie_secure, max_age=60 * 60 * 24 * 30)
        return resp

    @app.post("/api/logout")
    async def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, samesite="lax", secure=cookie_secure)
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
        authed = (not expected) or hmac.compare_digest(
            request.cookies.get(COOKIE) or "", expected)
        # available()/resolve_model() do blocking Ollama HTTP — probe once, off the
        # event loop, so a slow/down Ollama can't stall unrelated requests.
        ollama_up = False
        model = cfg.llm_model or cfg.ollama_model
        if authed:
            def _probe():
                up = pipeline.llm.available()
                return up, (pipeline.llm.resolve_model() if up else model)
            ollama_up, model = await run_in_threadpool(_probe)
        return {
            "authed": authed,
            "needs_password": bool(expected),
            "ollama": ollama_up,
            "ollama_model": model,
            "llm_provider": cfg.llm_provider,
            "platforms": list(cfg.enabled_platforms),
            "reframe_mode": cfg.reframe_mode,
            "default_count": cfg.shorts_per_video,
            "length_min": cfg.min_short_seconds,
            "length_max": cfg.max_short_seconds,
            "needs_password_change": cfg.app_password == DEFAULT_PASSWORD,
            "vault_enabled": bool(vault and vault.enabled),
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
        _cap(downloads)
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
                                name=str(idx), on_progress=prog,
                                allowlist=cfg.download_host_allowlist)
                downloads[did]["file"] = Path(path).name
                downloads[did]["stage"] = "done"
            except Exception:  # pragma: no cover
                logger.exception("download failed")
                downloads[did]["error"] = "download failed — check the URL " \
                    "(see server logs for details)"
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
        min_seconds: float = Form(0),
        max_seconds: float = Form(0),
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
            from .downloader import is_url
            # Must be a real http(s) URL — stops a value like "-loglevel" from
            # slipping past is_url() and reaching ffprobe/ffmpeg as an argument.
            if not is_url(url.strip()):
                raise HTTPException(400, "provide a valid http(s) URL")
            source = url.strip()
        elif source_type == "local":
            source = str(_resolve_local(name))
        else:
            raise HTTPException(400, "invalid source_type")

        n = count or cfg.shorts_per_video
        # Optional length range (seconds); target = midpoint. 0 -> use config.
        mn = min_seconds if min_seconds > 0 else None
        mx = max_seconds if max_seconds > 0 else None
        if mn and mx and mn > mx:
            mn, mx = mx, mn
        batch_id = uuid.uuid4().hex[:12]
        _cap(batches)
        batches[batch_id] = {"stage": "starting", "done": False, "error": ""}

        def on_stage(s: str) -> None:
            if batch_id in batches:
                batches[batch_id]["stage"] = s

        def run() -> None:
            try:
                pipeline.generate_shorts(source, n, niche=niche,
                                         batch_id=batch_id, on_stage=on_stage,
                                         min_s=mn, max_s=mx)
            except Exception:  # pragma: no cover
                logger.exception("batch failed")
                batches[batch_id]["error"] = "generation failed " \
                    "(see server logs for details)"
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
        if not pipeline.llm.available():
            raise HTTPException(503, "the selected AI model is not reachable")
        body = await request.json()
        niche = str(body.get("niche", ""))
        language = pipeline._metadata_language("")
        meta = pipeline.llm.generate_metadata(job.transcript, None, niche,
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
        publisher.submit(pipeline.publish_job, job, platforms)
        return {"ok": True, "platforms": platforms}

    @app.post("/api/job/{job_id}/rehearse")
    async def rehearse(job_id: str, request: Request,
                       _: None = Depends(require_auth)):
        """Dry run: drive each platform's upload to the final post button and
        screenshot the ready composer — but post NOTHING. Safe to run anytime."""
        job = store.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        if not job.output_path or job.status not in (STATUS_READY, "done"):
            raise HTTPException(409, "short is not ready yet")
        body = await request.json()
        platforms = [p for p in body.get("platforms", [])
                     if p in cfg.enabled_platforms]
        if not platforms:
            raise HTTPException(400, "no valid platforms selected")
        publisher.submit(pipeline.publish_job, job, platforms, dry_run=True)
        return {"ok": True, "platforms": platforms, "dry_run": True}

    @app.get("/api/rehearsal/{name}")
    async def rehearsal(name: str, _: None = Depends(require_auth)):
        # Serve a dry-run screenshot. Validate the name stays inside the dir.
        if "/" in name or "\\" in name or not name.endswith(".png"):
            raise HTTPException(400, "bad name")
        p = (cfg.rehearsals_dir / name).resolve()
        if cfg.rehearsals_dir.resolve() not in p.parents or not p.exists():
            raise HTTPException(404, "no such screenshot")
        return FileResponse(p, media_type="image/png")

    @app.get("/api/preview/{job_id}")
    async def preview(job_id: str, _: None = Depends(require_auth)):
        job = store.get(job_id)
        if not job or not job.output_path or not Path(job.output_path).exists():
            raise HTTPException(404, "no preview yet")
        return FileResponse(job.output_path, media_type="video/mp4")

    @app.get("/pub/{token}")
    async def public_share(token: str):
        # Deliberately NO auth: Meta's servers download the reel from here for
        # the IG Content Publishing API and cannot log in. The 128-bit random,
        # short-lived token (studio.public_share) is the secret.
        from .public_share import resolve
        path = resolve(token)
        if not path or not Path(path).exists():
            raise HTTPException(404, "unknown or expired share")
        return FileResponse(path, media_type="video/mp4")

    # --- connections / accounts + health ----------------------------------
    # Connection health/login runs launch a browser; route them through the
    # 'net' pool so a slow check never head-of-line-blocks GPU render/publish.
    app.include_router(build_connections_router(
        cfg, store, pipeline, vault, net, runs, require_auth))
    app.include_router(build_models_router(cfg, pipeline, vault, require_auth))

    return app
