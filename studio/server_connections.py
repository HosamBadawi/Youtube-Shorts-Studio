"""Health + YouTube connection status API.

Mounted by :func:`studio.server.create_app`. Every route is gated by the app's
``require_auth`` dependency. YouTube authorization itself is a one-time CLI
step (``python -m studio.login_setup``) — these routes only *report* status and
refresh the token, they never handle credentials.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from .health import platform_health, server_health


def build_connections_router(cfg, store, pipeline, vault, runner, runs,
                             require_auth) -> APIRouter:
    r = APIRouter()
    health_cache: dict[str, dict] = {}  # last health result

    def _status() -> dict:
        return {
            "platform": "youtube",
            "has_token": Path(cfg.youtube_token).exists(),
            "has_client_secret": Path(cfg.youtube_client_secret).exists(),
            "health": health_cache.get("youtube"),  # last check result (or None)
        }

    def _run_health() -> dict:
        res = platform_health("youtube", cfg, vault).to_dict()
        health_cache["youtube"] = res
        return res

    # --- health ------------------------------------------------------------
    @r.get("/api/health")
    async def health(_: None = Depends(require_auth)):
        # server_health does blocking disk/ffmpeg/Ollama probes — keep them off
        # the event loop.
        return await run_in_threadpool(server_health, cfg, store)

    @r.get("/api/connections")
    async def connections(_: None = Depends(require_auth)):
        return {"youtube": _status()}

    # --- runs (token refresh check) -----------------------------------------
    @r.post("/api/connections/youtube/health")
    async def run_health(_: None = Depends(require_auth)):
        if len(runs) > 40:
            for k in [k for k, v in list(runs.items()) if v.get("done")][:20]:
                runs.pop(k, None)
        rid = uuid.uuid4().hex[:12]
        runs[rid] = {"stage": "running", "done": False, "result": None,
                     "error": ""}

        def task():
            try:
                runs[rid]["result"] = _run_health()
            except Exception:  # pragma: no cover
                import logging
                logging.getLogger(__name__).exception("health check failed")
                runs[rid]["error"] = "the check failed (see server logs)"
            finally:
                runs[rid]["done"] = True
                runs[rid]["stage"] = "done"

        runner.submit(task)
        return {"ok": True, "run_id": rid}

    @r.get("/api/connections/run/{run_id}")
    async def run_status(run_id: str, _: None = Depends(require_auth)):
        return runs.get(run_id, {"stage": "", "done": True, "result": None,
                                 "error": "unknown run id"})

    return r
