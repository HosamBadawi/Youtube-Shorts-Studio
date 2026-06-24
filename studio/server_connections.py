"""Connections / Accounts API: manage how each platform logs in + health.

Mounted by :func:`studio.server.create_app`. Every route is gated by the app's
``require_auth`` dependency. Credentials are **write-only** — status routes never
return secrets. Long actions (health/login that launch a browser) run on the
shared worker and report through a run-id poll, matching the batches/downloads
pattern already used elsewhere.

Per-platform session-strategy choices persist to ``workspace/connections.json``
and are applied to ``cfg.session_strategy_overrides`` so the SessionProvider
honours them immediately.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from .config import PLATFORMS
from .health import platform_health, server_health

STRATEGIES = ("auto", "edge_profile", "saved_session", "credentials_login")


def _overrides_path(cfg) -> Path:
    return cfg.workspace_path / "connections.json"


def load_overrides(cfg) -> None:
    """Apply saved per-platform strategy choices onto the live config."""
    try:
        data = json.loads(_overrides_path(cfg).read_text(encoding="utf-8"))
        cfg.session_strategy_overrides = {
            k: v for k, v in data.items() if k in PLATFORMS and v in STRATEGIES}
    except Exception:
        pass


def _save_override(cfg, platform: str, strategy: str) -> None:
    cfg.session_strategy_overrides = dict(cfg.session_strategy_overrides or {})
    cfg.session_strategy_overrides[platform] = strategy
    try:
        _overrides_path(cfg).write_text(
            json.dumps(cfg.session_strategy_overrides), encoding="utf-8")
    except Exception:
        pass


def _has_session(cfg, platform: str) -> bool:
    if platform == "youtube":
        return Path(cfg.youtube_token).exists()
    d = cfg.session_dir_for(platform)
    return d.exists() and any(d.iterdir())


def build_connections_router(cfg, store, pipeline, vault, runner, runs,
                             require_auth) -> APIRouter:
    r = APIRouter()
    health_cache: dict[str, dict] = {}  # last health result per platform

    def _check_platform(platform: str) -> str:
        platform = platform.lower()
        if platform not in PLATFORMS:
            raise HTTPException(404, "unknown platform")
        return platform

    def _status(platform: str) -> dict:
        return {
            "platform": platform,
            "strategy": cfg.session_strategy_for(platform),
            "has_session": _has_session(cfg, platform),
            "credentials": vault.status(platform),
            "is_api": platform == "youtube",
            "edge_configured": bool(cfg.edge_user_data_dir),
            "health": health_cache.get(platform),  # last check result (or None)
        }

    def _run_health(platform: str) -> dict:
        res = platform_health(platform, cfg, vault).to_dict()
        health_cache[platform] = res
        return res

    # --- health ------------------------------------------------------------
    @r.get("/api/health")
    async def health(_: None = Depends(require_auth)):
        return server_health(cfg, store)

    # --- list / detail -----------------------------------------------------
    @r.get("/api/connections")
    async def connections(_: None = Depends(require_auth)):
        return {"strategies": list(STRATEGIES),
                "vault_enabled": bool(vault and vault.enabled),
                "platforms": [_status(p) for p in cfg.enabled_platforms]}

    @r.get("/api/connections/{platform}")
    async def connection(platform: str, _: None = Depends(require_auth)):
        return _status(_check_platform(platform))

    # --- credentials (write-only) ------------------------------------------
    @r.post("/api/connections/{platform}/credentials")
    async def set_credentials(platform: str, request: Request,
                              _: None = Depends(require_auth)):
        platform = _check_platform(platform)
        if not (vault and vault.enabled):
            raise HTTPException(503, "credential vault unavailable "
                                     "(pip install cryptography)")
        body = await request.json()
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        totp = str(body.get("totp_secret", "")).strip()
        if not username or not password:
            raise HTTPException(400, "username and password are required")
        vault.store(platform, username, password, totp)
        return {"ok": True, "credentials": vault.status(platform)}

    @r.delete("/api/connections/{platform}/credentials")
    async def clear_credentials(platform: str, _: None = Depends(require_auth)):
        platform = _check_platform(platform)
        vault.delete(platform)
        return {"ok": True, "credentials": vault.status(platform)}

    # --- strategy ----------------------------------------------------------
    @r.post("/api/connections/{platform}/strategy")
    async def set_strategy(platform: str, request: Request,
                           _: None = Depends(require_auth)):
        platform = _check_platform(platform)
        body = await request.json()
        strategy = str(body.get("strategy", "auto"))
        if strategy not in STRATEGIES:
            raise HTTPException(400, "invalid strategy")
        _save_override(cfg, platform, strategy)
        return {"ok": True, "strategy": strategy}

    # --- runs (health / connect) -------------------------------------------
    def _start(fn) -> str:
        # Cap the in-memory runs dict so a 24/7 host doesn't grow it unbounded.
        if len(runs) > 40:
            for k in [k for k, v in list(runs.items()) if v.get("done")][:20]:
                runs.pop(k, None)
        rid = uuid.uuid4().hex[:12]
        runs[rid] = {"stage": "running", "done": False, "result": None,
                     "error": ""}

        def task():
            try:
                runs[rid]["result"] = fn()
            except Exception as exc:  # pragma: no cover
                runs[rid]["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                runs[rid]["done"] = True
                runs[rid]["stage"] = "done"

        runner.submit(task)
        return rid

    @r.post("/api/connections/{platform}/health")
    async def run_health(platform: str, _: None = Depends(require_auth)):
        platform = _check_platform(platform)
        rid = _start(lambda: _run_health(platform))
        return {"ok": True, "run_id": rid}

    # "Connect" = the same health run; for browser platforms it will, via the
    # session chain, log in with stored credentials and persist the session.
    @r.post("/api/connections/{platform}/login")
    async def run_login(platform: str, _: None = Depends(require_auth)):
        platform = _check_platform(platform)
        rid = _start(lambda: _run_health(platform))
        return {"ok": True, "run_id": rid}

    @r.get("/api/connections/run/{run_id}")
    async def run_status(run_id: str, _: None = Depends(require_auth)):
        return runs.get(run_id, {"stage": "", "done": True, "result": None,
                                 "error": "unknown run id"})

    return r
