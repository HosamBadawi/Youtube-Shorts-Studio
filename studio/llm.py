"""Local Ollama integration (runs on your PC, no cloud, no cost).

Three jobs:

1. :func:`generate_json` - one JSON-mode round-trip, optionally constrained by
   a JSON schema (Ollama >= 0.5 grammar-constrained decoding). This is the
   primitive :mod:`studio.segmenter` builds its map->validate->reduce passes on.
2. :func:`pick_segment` - given a timestamped transcript, ask the model which
   contiguous span makes the strongest standalone short (single-short path).
3. :func:`generate_copy` - draft the Arabic title (curiosity+result formula),
   YouTube description, thumbnail headline and topic hashtags for one short.

Talks to Ollama's HTTP API with the standard library only (urllib), so there is
no extra pip dependency. Every call degrades gracefully: if Ollama is
unreachable or returns junk, callers get ``None`` / safe fallbacks and the app
keeps working with manual entry.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

from .metadata import VideoMeta, normalize_hashtags

logger = logging.getLogger(__name__)


# Cloud providers and a curated set of current model ids (the UI also allows a
# custom model name, since these change often).
CLOUD_PROVIDERS = ("openai", "anthropic", "gemini")
CLOUD_MODELS = {
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o4-mini"],
    "anthropic": ["claude-opus-4-8", "claude-sonnet-4-6",
                  "claude-haiku-4-5-20251001"],
    "gemini": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash",
               "gemini-1.5-pro"],
}
CLOUD_DEFAULT = {"openai": "gpt-4o-mini", "anthropic": "claude-sonnet-4-6",
                 "gemini": "gemini-2.0-flash"}


class _BaseLLM:
    """Shared high-level prompting (segment picking + metadata). Subclasses
    implement ``available``, ``resolve_model`` and ``_generate``."""

    model = ""

    def available(self) -> bool:  # pragma: no cover - overridden
        return False

    def resolve_model(self) -> str:
        return self.model

    def _generate(self, prompt: str, want_json: bool = True,
                  schema: dict | None = None) -> str | None:  # noqa
        raise NotImplementedError

    # --- high-level (provider-agnostic) ------------------------------------
    def generate_json(self, prompt: str, schema: dict | None = None
                      ) -> dict | list | None:
        """One JSON round-trip. ``schema`` constrains decoding where the
        provider supports it (Ollama); elsewhere it's advisory — describe the
        expected shape in the prompt too. Schema-valid != semantically valid:
        callers still validate fields and retry once themselves."""
        self.resolve_model()
        return _loads(self._generate(prompt, want_json=True, schema=schema))
    def pick_segment(self, timestamped_transcript: str, total_duration: float,
                     target_seconds: float, min_seconds: float | None = None,
                     max_seconds: float | None = None, min_start: float = 0.0
                     ) -> tuple[float, float] | None:
        if not timestamped_transcript.strip():
            return None
        self.resolve_model()
        lo, hi = _bounds(target_seconds, min_seconds, max_seconds)
        prompt = _SEGMENT_PROMPT.format(
            target=int(target_seconds), min=int(lo), max=int(hi),
            min_start=int(min_start),
            total=int(total_duration), transcript=timestamped_transcript[:8000])
        obj = _loads(self._generate(prompt, want_json=True))
        if not obj:
            return None
        try:
            start = max(0.0, float(obj["start"]))
            end = min(total_duration, float(obj["end"]))
        except (KeyError, TypeError, ValueError):
            return None
        start = max(start, min_start)  # respect the intro floor (clamp, don't drop)
        if end - start < 3.0:
            return None
        return (start, end)

    def generate_copy(self, transcript_text: str, niche: str = "",
                      language: str = "", avoid_titles: list[str] | None = None
                      ) -> VideoMeta | None:
        """Title (curiosity+result), description, thumbnail headline, hashtags
        for one short — a single structured round-trip, validated + 1 retry."""
        if not transcript_text.strip():
            transcript_text = "(no transcript available - infer from a generic " \
                              "engaging short-form video)"
        self.resolve_model()
        lang_rule = (f"Write everything in {language}." if language else
                     "Write everything in the SAME language as the transcript.")
        avoid = [t.strip() for t in (avoid_titles or []) if t.strip()]
        avoid_rule = ""
        if avoid:
            avoid_rule = ("\n   Do NOT reuse the opening words of these already-"
                          "used titles (vary the formula):\n   - "
                          + "\n   - ".join(avoid[:8]))
        prompt = _COPY_PROMPT.format(
            niche=niche or "general", language_rule=lang_rule,
            avoid_rule=avoid_rule, transcript=transcript_text[:6000])
        for _attempt in range(2):
            obj = _loads(self._generate(prompt, want_json=True,
                                        schema=_COPY_SCHEMA))
            if not isinstance(obj, dict):
                continue
            title = str(obj.get("title", "")).strip()[:100]
            description = str(obj.get("description", "")).strip()
            headline = str(obj.get("headline", "")).strip()
            if not title or not description:
                continue
            # A headline that just repeats the title defeats its purpose;
            # keep the shorter emotional fragment if the model echoed it.
            if headline and headline == title:
                headline = " ".join(headline.split()[:5])
            return VideoMeta(
                title=title,
                description=description,
                hashtags=normalize_hashtags(obj.get("hashtags")),
                thumbnail_headline=" ".join(headline.split()[:7]),
                source="ai")
        return None


class OllamaClient(_BaseLLM):
    def __init__(self, base_url: str = "http://localhost:11434",
                 model: str = "auto", enabled: bool = True,
                 timeout: float = 120.0, think: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.enabled = enabled
        self.timeout = timeout
        # "thinking" models (qwen3, deepseek-r1, ...) otherwise spend their whole
        # budget reasoning and return an EMPTY response - especially with
        # format=json. Disabling it gives fast, direct answers.
        self.think = think
        self._resolved = False  # have we auto-picked a model yet?
        self._think_supported = True  # cleared if Ollama rejects the field

    # --- low-level ----------------------------------------------------------
    def available(self) -> bool:
        if not self.enabled:
            return False
        try:
            with urllib.request.urlopen(self.base_url + "/api/tags",
                                        timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    # --- model discovery / auto-selection ----------------------------------
    def list_models(self) -> list[dict]:
        """Return the models Ollama currently has, newest API shape:
        ``[{"name": "llama3.1:latest", "size": .., "details": {..}}, ...]``."""
        try:
            with urllib.request.urlopen(self.base_url + "/api/tags",
                                        timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body.get("models", []) or []
        except Exception:
            return []

    def choose_model(self) -> str | None:
        """Pick the most capable installed chat model THAT FITS the GPU.

        Heuristic: drop embedding-only models, prefer the highest-ranked model
        whose weights fit in ~6 GB (a 7-9B q4 — leaves headroom on an 8 GB
        card); only if nothing fits, fall back to the smallest model rather
        than the biggest (a 35B spilling to CPU is unusably slow). You can
        always override ``ollama_model`` in studio.yaml.
        """
        models = self.list_models()
        candidates = [m for m in models
                      if not _is_embedding(str(m.get("name", "")))]
        if not candidates:
            return None
        fitting = [m for m in candidates
                   if float(m.get("size", 0) or 0) <= _FIT_BYTES]
        if fitting:
            best = max(fitting, key=_model_score)
        else:
            best = min(candidates, key=lambda m: float(m.get("size", 0) or 0))
            logger.warning(
                "No installed Ollama model fits comfortably in VRAM; using the "
                "smallest (%s). Consider `ollama pull command-r7b-arabic`.",
                best.get("name"))
        return str(best.get("name"))

    def resolve_model(self) -> str:
        """If ``model`` is 'auto'/empty, auto-select once and cache it."""
        if self._resolved:
            return self.model
        if self.model and self.model.strip().lower() not in {"auto", ""}:
            self._resolved = True
            return self.model
        picked = self.choose_model()
        if picked:
            self.model = picked
            self._resolved = True
            logger.info("Auto-selected Ollama model: %s", picked)
        return self.model

    def _generate(self, prompt: str, want_json: bool = True,
                  schema: dict | None = None) -> str | None:
        if not self.enabled:
            return None
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            # Structured selection work wants determinism; prose can wander a bit.
            "options": {"temperature": 0.2 if schema else 0.6},
        }
        if schema:
            # Ollama >= 0.5: grammar-constrained decoding from a JSON schema.
            # Older builds reject non-"json" formats -> retried without below.
            payload["format"] = schema
        elif want_json:
            payload["format"] = "json"
        if not self.think and self._think_supported:
            payload["think"] = False
        return self._post_generate(payload)

    def _post_generate(self, payload: dict) -> str | None:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/api/generate", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body.get("response", "")
        except urllib.error.HTTPError as exc:
            # Older Ollama builds don't know the "think" field -> 400. Drop it
            # once and retry so we still work everywhere.
            if exc.code == 400 and "think" in payload:
                self._think_supported = False
                payload.pop("think", None)
                return self._post_generate(payload)
            # Pre-0.5 Ollama rejects a JSON-schema "format" -> plain json mode.
            if exc.code == 400 and isinstance(payload.get("format"), dict):
                payload["format"] = "json"
                return self._post_generate(payload)
            logger.warning("Ollama HTTP %s: %s", exc.code,
                           exc.read().decode()[:200])
            return None
        except urllib.error.URLError as exc:
            logger.warning("Ollama unreachable (%s). Is `ollama serve` running "
                           "and `ollama pull %s` done?", exc, self.model)
            return None
        except Exception as exc:  # pragma: no cover
            logger.warning("Ollama request failed: %s", exc)
            return None

    # high-level methods (pick_segment(s), generate_metadata, …) inherited.


class CloudLLM(_BaseLLM):
    """OpenAI / Anthropic / Gemini via their HTTP APIs (stdlib urllib)."""

    def __init__(self, provider: str, model: str, api_key: str,
                 timeout: float = 120.0) -> None:
        self.provider = provider
        self.model = model or CLOUD_DEFAULT.get(provider, "")
        self.api_key = api_key
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key) and bool(self.model)

    def list_models(self) -> list[str]:
        return CLOUD_MODELS.get(self.provider, [])

    def _generate(self, prompt: str, want_json: bool = True,
                  schema: dict | None = None) -> str | None:
        # ``schema`` is enforced by Ollama only; cloud providers get JSON mode
        # and rely on the shape being described in the prompt (callers validate).
        if not self.available():
            return None
        try:
            if self.provider == "openai":
                return self._openai(prompt, want_json or bool(schema))
            if self.provider == "anthropic":
                return self._anthropic(prompt)
            if self.provider == "gemini":
                return self._gemini(prompt, want_json or bool(schema))
        except urllib.error.HTTPError as exc:
            logger.warning("%s HTTP %s: %s", self.provider, exc.code,
                           exc.read().decode()[:200])
        except Exception as exc:
            logger.warning("%s request failed: %s", self.provider, exc)
        return None

    def _post(self, url: str, body: dict, headers: dict) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _openai(self, prompt: str, want_json: bool) -> str:
        body = {"model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.6}
        if want_json:
            body["response_format"] = {"type": "json_object"}
        d = self._post("https://api.openai.com/v1/chat/completions", body,
                       {"Authorization": f"Bearer {self.api_key}"})
        return d["choices"][0]["message"]["content"]

    def _anthropic(self, prompt: str) -> str:
        body = {"model": self.model, "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}]}
        d = self._post("https://api.anthropic.com/v1/messages", body,
                       {"x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01"})
        return "".join(b.get("text", "") for b in d.get("content", [])
                       if b.get("type") == "text")

    def _gemini(self, prompt: str, want_json: bool) -> str:
        body: dict = {"contents": [{"parts": [{"text": prompt}]}]}
        if want_json:
            body["generationConfig"] = {"responseMimeType": "application/json"}
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{self.model}:generateContent?key={self.api_key}")
        d = self._post(url, body, {})
        return d["candidates"][0]["content"]["parts"][0]["text"]


def make_llm(cfg, vault=None) -> _BaseLLM:
    """Build the active LLM client from config (Ollama or a cloud provider)."""
    provider = (getattr(cfg, "llm_provider", "ollama") or "ollama").lower()
    if provider == "ollama" or provider not in CLOUD_PROVIDERS:
        return OllamaClient(cfg.ollama_url, cfg.ollama_model, cfg.ollama_enabled,
                            timeout=cfg.ollama_timeout, think=cfg.ollama_think)
    key = ""
    if vault is not None and getattr(vault, "enabled", False):
        key = vault.get_api_key(provider) or ""
    return CloudLLM(provider, getattr(cfg, "llm_model", ""), key,
                    timeout=cfg.ollama_timeout)


# Models that only produce embeddings - useless for generation, never pick them.
_EMBEDDING_HINTS = ("embed", "bge", "nomic", "minilm", "gte", "e5", "mxbai")
# On-disk weight budget for auto-selection: a q4 model this size still leaves
# working VRAM on an 8 GB card (7-9B q4 ~= 4.4-5.5 GB).
_FIT_BYTES = 6.0e9
# Small bump for families known to follow instructions well in this kind of task.
_FAMILY_BONUS = {
    "llama": 1.5, "qwen": 1.5, "qwen2": 1.5, "mistral": 1.2, "mixtral": 1.3,
    "gemma": 1.2, "phi": 1.0, "deepseek": 1.3, "command-r": 1.2,
}


def _bounds(target: float, lo: float | None, hi: float | None
            ) -> tuple[float, float]:
    """Resolve the (min, max) seconds shown to the model. Falls back to a band
    around ``target`` when explicit bounds aren't passed."""
    lo = float(lo) if lo else max(5.0, target * 0.85)
    hi = float(hi) if hi else max(lo + 1.0, target * 1.15)
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _is_embedding(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in _EMBEDDING_HINTS)


def _param_billions(model: dict) -> float:
    """Best-effort parameter count in billions, from details or file size."""
    ps = str(model.get("details", {}).get("parameter_size", "")).upper().strip()
    try:
        if ps.endswith("B"):
            return float(ps[:-1])
        if ps.endswith("M"):
            return float(ps[:-1]) / 1000.0
    except ValueError:
        pass
    # Fallback: estimate from on-disk size (~0.6 GB per B at 4-bit quant).
    size = float(model.get("size", 0) or 0)
    return (size / 1e9) / 0.6 if size else 0.0


def _model_score(model: dict) -> tuple[float, float]:
    name = str(model.get("name", "")).lower()
    family = str(model.get("details", {}).get("family", "")).lower()
    bonus = max((b for key, b in _FAMILY_BONUS.items()
                 if key in family or key in name), default=0.0)
    return (_param_billions(model) + bonus, _param_billions(model))


def _loads(raw: str | None) -> dict | None:
    """Parse a JSON object out of an LLM response, tolerating stray prose."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            return None
    return None


_SEGMENT_PROMPT = """You are selecting the single best clip for a vertical short \
from a longer video. The video is {total} seconds long. Below is its transcript \
with [mm:ss] timestamps.

HARD RULE — NEVER use the opening of the video. The first {min_start} seconds are \
intro / greeting / setup / throat-clearing and are BANNED. Your clip MUST start at \
or after {min_start} seconds (start >= {min_start}).

Open on the SPICIEST moment you can find — a bold claim, a surprising or \
controversial statement, a provocative question, or a strong emotional beat that \
stops a scroller dead in the first 2 seconds. Pick the most self-contained, \
hook-strong, emotionally engaging span. It MUST start ON that hook (never mid-setup) \
and end on a satisfying or curiosity-driving note. The clip MUST be between {min} \
and {max} seconds long (target about {target}s). Never pick a span shorter than \
{min} seconds.

Transcript:
{transcript}

Respond with ONLY a JSON object:
{{"start": <seconds:number, must be >= {min_start}>, "end": <seconds:number>, \
"reason": "<short why this is a spicy hook>"}}"""


_COPY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "headline": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "description", "headline", "hashtags"],
}


_COPY_PROMPT = """You write high-performing YouTube Shorts copy. The channel \
niche is: {niche}. Base everything ONLY on the clip transcript below — never \
promise anything the clip doesn't deliver. {language_rule}

Write four things:

1. "title" — a YouTube Shorts title following the Curiosity + Result formula:
   [شيء غريب أو معلومة] + هل تعلم / لن تصدق + النتيجة
   Style examples of the PATTERN (do not copy them literally):
   - لن تصدق ماذا حدث بعد...
   - هل تعلم السر الحقيقي وراء...
   - أغرب حقيقة ستغير نظرتك...
   Maximum 80 characters. Highly clickable but never a lie.{avoid_rule}

2. "description" — 2-3 SHORT lines: a hook question about the clip's core \
claim, one line of value/context, one call-to-action line (follow / watch to \
the end). No links, no hashtags inside it.

3. "headline" — 3 to 6 words for the THUMBNAIL in huge letters: the emotional \
core of the clip (a shock, a secret, a number, a question). NOT a copy of the \
title.

4. "hashtags" — 3-5 specific TOPIC hashtags from this clip (never generic \
like #video or #viral), in the same language as the title.

Transcript:
{transcript}

Respond with ONLY a JSON object:
{{"title": "...", "description": "...", "headline": "...", \
"hashtags": ["#tag1", "#tag2", "#tag3"]}}"""
