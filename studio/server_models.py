"""Model selection API: pick the local Ollama model, or a cloud provider
(OpenAI / Anthropic / Gemini) with an API key stored encrypted in the vault.

The choice persists to ``workspace/llm.json`` and rebuilds the pipeline's active
LLM immediately. API keys are write-only (status returns only booleans).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from .llm import CLOUD_MODELS, CLOUD_PROVIDERS, OllamaClient, make_llm

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

    @r.delete("/api/models/key/{provider}")
    async def del_key(provider: str, _: None = Depends(require_auth)):
        provider = provider.lower()
        if provider not in CLOUD_PROVIDERS:
            raise HTTPException(404, "unknown provider")
        if vault and vault.enabled:
            vault.delete_api_key(provider)
        return {"ok": True}

    return r
