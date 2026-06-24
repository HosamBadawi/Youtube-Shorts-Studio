"""Speech-to-text -> timestamped segments.

Prefers ``faster-whisper`` (CTranslate2, runs nicely on your 3060 Ti via CUDA),
and falls back to the reference ``openai-whisper`` if that's what is installed.
If neither is present, :func:`transcribe` returns an empty result with a note
instead of raising, so the rest of the pipeline still runs (you just lose the
AI-assisted segment picking and caption drafting).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Word:
    """A single spoken word with its timing - drives the on-screen captions."""

    start: float
    end: float
    text: str


@dataclass
class Segment:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return {"start": round(self.start, 2), "end": round(self.end, 2),
                "text": self.text.strip()}


@dataclass
class Transcript:
    text: str = ""
    segments: list[Segment] = field(default_factory=list)
    words: list[Word] = field(default_factory=list)
    language: str = ""
    available: bool = False
    note: str = ""

    def timestamped(self) -> str:
        """A compact ``[mm:ss] text`` block to feed the LLM."""
        lines = []
        for s in self.segments:
            m, sec = divmod(int(s.start), 60)
            lines.append(f"[{m:02d}:{sec:02d}] {s.text.strip()}")
        return "\n".join(lines)


def transcribe(video_path: str, model: str = "base", device: str = "auto",
               enabled: bool = True, language: str = "") -> Transcript:
    if not enabled:
        return Transcript(available=False, note="transcription disabled in config")

    lang = (language or "").strip().lower()
    lang = None if lang in {"", "auto"} else lang
    fw = _try_faster_whisper(video_path, model, device, lang)
    if fw is not None:
        return fw
    ow = _try_openai_whisper(video_path, model, lang)
    if ow is not None:
        return ow
    return Transcript(
        available=False,
        note="no Whisper backend installed (pip install faster-whisper)",
    )


def _resolve_device(device: str) -> tuple[str, str]:
    """Return (device, compute_type) for faster-whisper."""
    if device == "auto":
        try:
            import torch  # type: ignore

            if torch.cuda.is_available():
                return "cuda", "float16"
        except Exception:
            pass
        try:  # detect the GPU via CTranslate2 even without PyTorch installed
            import ctranslate2  # type: ignore

            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda", "float16"
        except Exception:
            pass
        return "cpu", "int8"
    return device, ("float16" if device == "cuda" else "int8")


def _enable_cuda_dlls() -> None:
    """Make pip-installed NVIDIA CUDA libs (nvidia-cublas-cu12 / nvidia-cudnn-cu12)
    discoverable on Windows so CTranslate2 can run faster-whisper on the GPU
    without the user editing PATH. No-op elsewhere / if the libs aren't present."""
    import os
    import sys

    if sys.platform != "win32":
        return
    import importlib.util

    dirs: list[str] = []
    # Every nvidia-*-cu12 wheel (cublas, cudnn, cuda_runtime, cuda_nvrtc, ...)
    # drops its DLLs under nvidia/<lib>/bin. GPU needs the WHOLE set on the
    # search path - cublas alone fails to load without cudart, etc.
    try:
        spec = importlib.util.find_spec("nvidia")
        for root in (spec.submodule_search_locations if spec else []) or []:
            if os.path.isdir(root):
                for name in os.listdir(root):
                    binp = os.path.join(root, name, "bin")
                    if os.path.isdir(binp):
                        dirs.append(binp)
    except Exception:
        pass
    # PyTorch (if installed with CUDA) bundles the same DLLs in torch/lib.
    try:
        spec = importlib.util.find_spec("torch")
        if spec and spec.submodule_search_locations:
            libp = os.path.join(spec.submodule_search_locations[0], "lib")
            if os.path.isdir(libp):
                dirs.append(libp)
    except Exception:
        pass
    # Both are needed: add_dll_directory for the directly-loaded libs, and PATH
    # so Windows resolves their TRANSITIVE deps (cublas -> cudart, etc.).
    for d in dirs:
        try:
            os.add_dll_directory(d)
        except Exception:
            pass
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")


def _try_faster_whisper(video_path: str, model: str, device: str,
                        language: str | None = None):
    try:
        _enable_cuda_dlls()
        from faster_whisper import WhisperModel  # type: ignore
    except Exception:
        return None
    dev, compute = _resolve_device(device)
    attempts = [(dev, compute)]
    if dev == "cuda":  # if the GPU path dies (missing CUDA libs), still deliver
        attempts.append(("cpu", "int8"))
    for d, c in attempts:
        try:
            logger.info("Transcribing with faster-whisper (%s on %s, lang=%s)",
                        model, d, language or "auto")
            wm = WhisperModel(model, device=d, compute_type=c)
            seg_iter, info = wm.transcribe(video_path, vad_filter=True,
                                           word_timestamps=True, language=language)
            segments: list[Segment] = []
            words: list[Word] = []
            for s in seg_iter:
                segments.append(Segment(s.start, s.end, s.text))
                for w in (getattr(s, "words", None) or []):
                    if w.word and w.word.strip():
                        words.append(Word(w.start, w.end, w.word.strip()))
            text = " ".join(s.text.strip() for s in segments).strip()
            return Transcript(text=text, segments=segments, words=words,
                              language=getattr(info, "language", "") or "",
                              available=bool(segments),
                              note=f"faster-whisper ({model} on {d})")
        except Exception as exc:  # pragma: no cover - backend/runtime issues
            logger.warning("faster-whisper on %s failed: %s", d, exc)
    return None


def _try_openai_whisper(video_path: str, model: str, language: str | None = None):
    try:
        import whisper  # type: ignore
    except Exception:
        return None
    try:
        logger.info("Transcribing with openai-whisper (%s, lang=%s)", model,
                    language or "auto")
        wm = whisper.load_model(model)
        result = wm.transcribe(video_path, word_timestamps=True,
                               language=language)
        segments: list[Segment] = []
        words: list[Word] = []
        for s in result.get("segments", []):
            segments.append(Segment(s["start"], s["end"], s["text"]))
            for w in s.get("words", []) or []:
                token = (w.get("word") or "").strip()
                if token:
                    words.append(Word(w["start"], w["end"], token))
        return Transcript(text=result.get("text", "").strip(), segments=segments,
                          words=words, language=result.get("language", ""),
                          available=bool(segments), note="openai-whisper")
    except Exception as exc:  # pragma: no cover
        logger.warning("openai-whisper failed: %s", exc)
        return None
