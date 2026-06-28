"""Model selection API: pick the local Ollama model, or a cloud provider
(OpenAI / Anthropic / Gemini) with an API key stored encrypted in the vault.

The choice persists to ``workspace/llm.json`` and rebuilds the pipeline's active
LLM immediately. API keys are write-only (status returns only booleans).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .llm import CLOUD_MODELS, CLOUD_PROVIDERS, CloudLLM, OllamaClient, make_llm

PROVIDERS = ("ollama",) + CLOUD_PROVIDERS


def _path(cfg) -> Path:
    return cfg.workspace_path / "llm.json"


def load_llm_selection(cfg) -> None:
    try:
        d = json.loads(_path(cfg).read_text(encoding="utf-8"))
        if d.get("provider") in PROVIDERS:
            cfg.llm_provider = d["provider"]
        if isinstance(d.get("model"), str):
            cfg.llm_model = d["model"]
            if cfg.llm_provider == "ollama" and d["model"]:
                cfg.ollama_model = d["model"]
    except Exception:
        pass


def _save(cfg) -> None:
    try:
        _path(cfg).write_text(
            json.dumps({"provider": cfg.llm_provider, "model": cfg.llm_model}),
            encoding="utf-8")
    except Exception:
        pass


def build_models_router(cfg, pipeline, vault, require_auth) -> APIRouter:
    r = APIRouter()

    def _ollama_models() -> list[str]:
        try:
            oc = OllamaClient(cfg.ollama_url, cfg.ollama_model, cfg.ollama_enabled)
            if oc.available():
                return [str(m.get("name")) for m in oc.list_models()]
        except Exception:
            pass
        return []

    @r.get("/api/models")
    async def models(_: None = Depends(require_auth)):
        return {
            "provider": cfg.llm_provider,
            "model": cfg.llm_model or (cfg.ollama_model
                                       if cfg.llm_provider == "ollama" else ""),
            "providers": list(PROVIDERS),
            "ollama_models": _ollama_models(),
            "cloud_models": CLOUD_MODELS,
            "keys": {p: bool(vault and vault.enabled and vault.has_api_key(p))
                     for p in CLOUD_PROVIDERS},
            "vault_enabled": bool(vault and vault.enabled),
            "active_available": pipeline.llm.available(),
        }

    @r.post("/api/models/select")
    async def select(request: Request, _: None = Depends(require_auth)):
        body = await request.json()
        provider = str(body.get("provider", "ollama")).lower()
        if provider not in PROVIDERS:
            raise HTTPException(400, "unknown provider")
        model = str(body.get("model", "")).strip()
        cfg.llm_provider = provider
        cfg.llm_model = model
        if provider == "ollama":
            cfg.ollama_model = model or "auto"
        _save(cfg)
        pipeline.llm = make_llm(cfg, vault)
        return {"ok": True, "provider": provider, "model": model,
                "available": pipeline.llm.available()}

    @r.post("/api/models/key")
    async def set_key(request: Request, _: None = Depends(require_auth)):
        if not (vault and vault.enabled):
            raise HTTPException(503, "credential vault unavailable "
                                     "(pip install cryptography)")
        body = await request.json()
        provider = str(body.get("provider", "")).lower()
        key = str(body.get("key", "")).strip()
        if provider not in CLOUD_PROVIDERS:
            raise HTTPException(400, "unknown provider")
        if not key:
            raise HTTPException(400, "API key required")
        vault.set_api_key(provider, key)
        if cfg.llm_provider == provider:        # refresh the active client
            pipeline.llm = make_llm(cfg, vault)
        return {"ok": True, "provider": provider, "has_key": True}

    @r.post("/api/models/test")
    async def test_model(request: Request, _: None = Depends(require_auth)):
        body = await request.json()
        provider = str(body.get("provider", cfg.llm_provider)).lower()
        model = str(body.get("model", "")).strip()
        if provider == "ollama":
            # First call to a cold model loads it into VRAM and can take 1–2 min
            # for a big (35B) model — give it the configured generation timeout,
            # not a short one, so "model still loading" isn't reported as a fail.
            client = OllamaClient(cfg.ollama_url, model or cfg.ollama_model,
                                  cfg.ollama_enabled,
                                  timeout=max(float(cfg.ollama_timeout), 180.0))
        elif provider in CLOUD_PROVIDERS:
            key = (vault.get_api_key(provider)
                   if (vault and vault.enabled) else "")
            client = CloudLLM(provider, model, key, timeout=60)
        else:
            raise HTTPException(400, "unknown provider")
        if not client.available():
            return {"ok": False, "error": "not reachable — check the API key, "
                                          "the model id, or that Ollama is running"}
        reply = await run_in_threadpool(
            client._generate, "Reply with exactly: OK", False)
        if reply and reply.strip():
            return {"ok": True, "reply": reply.strip()[:140]}
        return {"ok": False, "error": "no reply (wrong key/model, or a thinking "
                                      "model returned empty)"}

    @r.delete("/api/models/key/{provider}")
    async def del_key(provider: str, _: None = Depends(require_auth)):
        provider = provider.lower()
        if provider not in CLOUD_PROVIDERS:
            raise HTTPException(404, "unknown provider")
        if vault and vault.enabled:
            vault.delete_api_key(provider)
        return {"ok": True}

    return r
