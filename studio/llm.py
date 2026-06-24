"""Local Ollama integration (runs on your PC, no cloud, no cost).

Two jobs:

1. :func:`pick_segment` - given a timestamped transcript, ask the model which
   contiguous span makes the strongest standalone short, and return ``(start,
   end)`` seconds. This is the "semantically separate the video based on the
   transcription and timestamps" step.
2. :func:`generate_metadata` - draft a title, caption and hashtags (optionally
   tailored per platform).

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


class OllamaClient:
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
        """Pick the most capable installed chat model.

        Heuristic: drop embedding-only models, then rank by parameter count,
        with a small bump for proven instruct families. Bigger generally =
        smarter; you can always override ``ollama_model`` in studio.yaml.
        """
        models = self.list_models()
        candidates = [m for m in models
                      if not _is_embedding(str(m.get("name", "")))]
        if not candidates:
            return None
        best = max(candidates, key=_model_score)
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

    def _generate(self, prompt: str, want_json: bool = True) -> str | None:
        if not self.enabled:
            return None
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.6},
        }
        if want_json:
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

    # --- high-level ---------------------------------------------------------
    def pick_segment(self, timestamped_transcript: str, total_duration: float,
                     target_seconds: float) -> tuple[float, float] | None:
        """Return the best (start, end) span, or None to keep the whole clip."""
        if not timestamped_transcript.strip():
            return None
        self.resolve_model()
        prompt = _SEGMENT_PROMPT.format(
            target=int(target_seconds),
            total=int(total_duration),
            transcript=timestamped_transcript[:8000],
        )
        raw = self._generate(prompt, want_json=True)
        obj = _loads(raw)
        if not obj:
            return None
        try:
            start = max(0.0, float(obj["start"]))
            end = min(total_duration, float(obj["end"]))
        except (KeyError, TypeError, ValueError):
            return None
        if end - start < 3.0:  # implausibly short -> ignore
            return None
        logger.info("Ollama picked %.1f-%.1fs: %s", start, end,
                    obj.get("reason", ""))
        return (start, end)

    def pick_segments(self, timestamped_transcript: str, total_duration: float,
                      count: int, target_seconds: float
                      ) -> list[tuple[float, float, str]]:
        """Pick up to ``count`` DISTINCT, non-overlapping segments, each its own
        self-contained topic. Returns ``[(start, end, topic), ...]`` sorted by
        time. Empty list if the model is unreachable / returns nothing usable."""
        if not timestamped_transcript.strip() or count < 1:
            return []
        self.resolve_model()
        prompt = _MULTI_SEGMENT_PROMPT.format(
            count=count, target=int(target_seconds), total=int(total_duration),
            transcript=timestamped_transcript[:16000])
        obj = _loads(self._generate(prompt, want_json=True))
        if not obj:
            return []
        raw = obj.get("segments") if isinstance(obj, dict) else obj
        if not isinstance(raw, list):
            return []
        picked: list[tuple[float, float, str]] = []
        for item in raw:
            try:
                s = max(0.0, float(item["start"]))
                e = min(total_duration, float(item["end"]))
            except (KeyError, TypeError, ValueError):
                continue
            if e - s >= 3.0:
                picked.append((s, e, str(item.get("topic", "")).strip()))
        # Sort by time and drop any that overlap an already-kept earlier one.
        picked.sort(key=lambda x: x[0])
        out: list[tuple[float, float, str]] = []
        for s, e, topic in picked:
            if out and s < out[-1][1] - 1.0:   # overlaps the previous keep
                continue
            out.append((s, e, topic))
            if len(out) >= count:
                break
        logger.info("Ollama picked %d/%d segments", len(out), count)
        return out

    def generate_metadata(self, transcript_text: str, platform: str | None = None,
                          niche: str = "", language: str = "") -> VideoMeta | None:
        """Draft title/caption/hashtags. ``platform=None`` = generic base set.
        ``language`` (e.g. "Arabic") forces the output language; empty = match
        the transcript's language."""
        if not transcript_text.strip():
            transcript_text = "(no transcript available - infer from a generic " \
                              "engaging short-form video)"
        self.resolve_model()
        lang_rule = (f"Write the title, caption AND hashtags in {language}."
                     if language else
                     "Write everything in the SAME language as the transcript.")
        prompt = _METADATA_PROMPT.format(
            platform=platform or "generic short-form (YouTube/TikTok/Reels)",
            niche=niche or "general",
            language_rule=lang_rule,
            transcript=transcript_text[:6000],
        )
        raw = self._generate(prompt, want_json=True)
        obj = _loads(raw)
        if not obj:
            return None
        return VideoMeta(
            title=str(obj.get("title", "")).strip(),
            caption=str(obj.get("caption", "")).strip(),
            hashtags=normalize_hashtags(obj.get("hashtags")),
            source="ollama",
        )

    def generate_per_platform(self, transcript_text: str, platforms: list[str],
                              niche: str = "", language: str = "") -> VideoMeta:
        """Base set + a tailored override for every requested platform."""
        base = self.generate_metadata(transcript_text, None, niche,
                                      language) or VideoMeta()
        for p in platforms:
            tailored = self.generate_metadata(transcript_text, p, niche, language)
            if tailored:
                base.overrides[p] = {
                    "title": tailored.title,
                    "caption": tailored.caption,
                    "hashtags": tailored.hashtags,
                }
        base.source = "ollama"
        return base


# Models that only produce embeddings - useless for generation, never pick them.
_EMBEDDING_HINTS = ("embed", "bge", "nomic", "minilm", "gte", "e5", "mxbai")
# Small bump for families known to follow instructions well in this kind of task.
_FAMILY_BONUS = {
    "llama": 1.5, "qwen": 1.5, "qwen2": 1.5, "mistral": 1.2, "mixtral": 1.3,
    "gemma": 1.2, "phi": 1.0, "deepseek": 1.3, "command-r": 1.2,
}


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

Pick the most self-contained, hook-strong, emotionally engaging span. It should \
start on a strong hook and end on a satisfying or curiosity-driving note. The clip \
MUST be between 50 and 60 seconds long (target about {target}s). Never pick a span \
shorter than 50 seconds.

Transcript:
{transcript}

Respond with ONLY a JSON object:
{{"start": <seconds:number>, "end": <seconds:number>, "reason": "<short why>"}}"""


_MULTI_SEGMENT_PROMPT = """You are choosing {count} DISTINCT clips from a longer \
video ({total} seconds) to cut into separate vertical shorts. Below is the \
transcript with [mm:ss] timestamps.

Pick {count} NON-OVERLAPPING segments. Each one must:
- cover a DIFFERENT topic / self-contained idea (no two shorts about the same point),
- start on a strong hook and end on a satisfying or curiosity-driving beat,
- be between 50 and 60 seconds long (target ~{target}s), never under 50.
Spread them across the whole video so they don't overlap.

Transcript:
{transcript}

Respond with ONLY a JSON object:
{{"segments": [
  {{"start": <seconds:number>, "end": <seconds:number>, "topic": "<short topic>"}}
]}}  - exactly {count} items, ordered by start time."""


_METADATA_PROMPT = """You write high-performing social copy for short-form vertical \
video on {platform}. The content niche is: {niche}.

Based on this transcript, write metadata that maximizes watch-through and shares. \
Be punchy and native to the platform. Avoid clickbait lies. {language_rule}

Transcript:
{transcript}

Respond with ONLY a JSON object:
{{"title": "<<=80 chars, scroll-stopping>",
  "caption": "<1-3 sentence caption, no hashtags inside>",
  "hashtags": ["#tag1", "#tag2", "#tag3"]}}"""
