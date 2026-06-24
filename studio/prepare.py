"""Upload-free test harness.

Two commands:

    python -m studio.prepare --check          # environment doctor (no video)
    python -m studio.prepare path/to/video.mp4

The second runs the FULL preparation pipeline - transcribe -> AI segment pick ->
9:16 reframe -> draft captions - and then **stops**. Nothing is ever published.
It prints the chosen Ollama model, the picked segment, the rendered file path,
and the drafted title/caption/hashtags so you can eyeball the result.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .config import PLATFORMS, StudioConfig
from .llm import OllamaClient

OK = "[ OK ]"
NO = "[FAIL]"
WARN = "[warn]"


# ---------------------------------------------------------------------------
def check(cfg: StudioConfig) -> int:
    print("\n=== Daily Shorts Studio - environment check ===\n")
    problems = 0

    # ffmpeg / ffprobe -------------------------------------------------------
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool):
            print(f"{OK} {tool} found on PATH")
        else:
            print(f"{NO} {tool} NOT found - install ffmpeg and add it to PATH")
            problems += 1

    # OpenCV (reframe engine) ------------------------------------------------
    try:
        import cv2  # type: ignore
        print(f"{OK} OpenCV {cv2.__version__} (reframe engine ready)")
    except Exception:
        print(f"{NO} OpenCV missing - pip install opencv-contrib-python")
        problems += 1

    # Whisper backend --------------------------------------------------------
    if _has("faster_whisper"):
        print(f"{OK} faster-whisper installed (transcription ready)")
    elif _has("whisper"):
        print(f"{OK} openai-whisper installed (transcription ready)")
    else:
        print(f"{WARN} no Whisper backend - clips still reframe, but no "
              f"transcript / AI segment picking. pip install faster-whisper")

    # CUDA (your 3060 Ti) ----------------------------------------------------
    try:
        import torch  # type: ignore
        if torch.cuda.is_available():
            print(f"{OK} CUDA available: {torch.cuda.get_device_name(0)}")
        else:
            print(f"{WARN} CUDA not available - Whisper will run on CPU (slower)")
    except Exception:
        print(f"{WARN} PyTorch not installed - faster-whisper picks CPU/int8")

    # Ollama + model selection ----------------------------------------------
    oll = OllamaClient(cfg.ollama_url, cfg.ollama_model, cfg.ollama_enabled,
                       timeout=cfg.ollama_timeout, think=cfg.ollama_think)
    if not cfg.ollama_enabled:
        print(f"{WARN} Ollama disabled in config (ollama_enabled: false)")
    elif not oll.available():
        print(f"{NO} Ollama not reachable at {cfg.ollama_url} - run `ollama serve`")
        problems += 1
    else:
        models = oll.list_models()
        if not models:
            print(f"{NO} Ollama is up but no models installed - `ollama pull llama3.1`")
            problems += 1
        else:
            print(f"{OK} Ollama up with {len(models)} model(s):")
            for m in sorted(models, key=lambda x: str(x.get("name"))):
                size = float(m.get("size", 0) or 0) / 1e9
                ps = m.get("details", {}).get("parameter_size", "?")
                print(f"        - {m.get('name'):<28} {ps:>6}  {size:5.1f} GB")
            chosen = oll.choose_model()
            if cfg.ollama_model.lower() in {"auto", ""}:
                print(f"  -->  auto-selected: {chosen}")
            else:
                print(f"  -->  pinned in config: {cfg.ollama_model}")

    # Platform logins (sessions present?) ------------------------------------
    print("\n  Platform login sessions (for later, when you publish):")
    for p in PLATFORMS:
        sess = cfg.session_dir_for(p)
        if p == "youtube":
            ready = Path(cfg.youtube_token).exists()
        else:
            ready = sess.exists() and any(sess.iterdir()) if sess.exists() else False
        print(f"        {p:<11} {'saved' if ready else 'not set up yet'}")

    print("\n" + ("All good - ready to test!" if problems == 0
                  else f"{problems} blocker(s) above to fix first.") + "\n")
    return 0 if problems == 0 else 1


# ---------------------------------------------------------------------------
def prepare(video: str, cfg: StudioConfig, niche: str = "",
            per_platform: bool = False) -> int:
    from .downloader import download, is_url
    if is_url(video):
        print(f"Downloading source from URL…\n  {video}")
        try:
            video = download(video, str(cfg.download_dir),
                             prefer_mp4=cfg.download_prefer_mp4)
            print(f"  saved: {video}\n")
        except Exception as exc:
            print(f"{NO} download failed: {exc}")
            return 1
    path = Path(video)
    if not path.exists():
        print(f"{NO} file not found: {video}")
        return 1

    cfg.ensure_dirs()
    from .jobs import JobStore
    from .pipeline import StudioPipeline

    store = JobStore(cfg.db_path)
    pipe = StudioPipeline(cfg, store)

    if cfg.ollama_enabled and pipe.ollama.available():
        model = pipe.ollama.resolve_model()
        print(f"\nUsing Ollama model: {model}")
    else:
        print("\nOllama unavailable - will reframe only, captions left blank.")

    print(f"Preparing (NO upload): {path.name}\n")
    job = store.create(str(path.resolve()))
    job = pipe.process_job(job, auto_metadata=True, per_platform=per_platform,
                           niche=niche)

    print("\n--------------------------------------------------------")
    if job.status == "error":
        print(f"{NO} prepare failed: {job.error}")
        return 1

    print(f"{OK} prepared successfully")
    print(f"  source duration : {job.duration:.1f}s")
    if job.segment:
        s, e = job.segment
        print(f"  AI picked clip  : {s:.0f}s -> {e:.0f}s  ({e - s:.0f}s)")
    else:
        print("  segment         : whole clip used (short enough / no transcript)")
    if job.transcript:
        snippet = job.transcript[:200].replace("\n", " ")
        print(f"  transcript      : {snippet}{'...' if len(job.transcript) > 200 else ''}")
    print(f"  vertical output : {job.output_path}")
    print("\n  Drafted metadata:")
    print(f"    title    : {job.meta.title or '(none)'}")
    print(f"    caption  : {job.meta.caption or '(none)'}")
    print(f"    hashtags : {' '.join(job.meta.hashtags) or '(none)'}")
    if job.meta.overrides:
        print(f"    per-platform overrides for: {', '.join(job.meta.overrides)}")
    print("\n  Open the vertical file above to check the framing.")
    print("  Nothing was uploaded. To publish, use the web app and tap Publish.\n")
    return 0


def _has(mod: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(mod) is not None


def _force_utf8_console() -> None:
    """Windows consoles default to cp1252 and choke on Arabic/emoji output.
    Re-encode stdout/stderr as UTF-8 (replacing anything truly unmappable)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    argv = argv if argv is not None else sys.argv[1:]
    cfg = StudioConfig.load()
    if not argv or argv[0] in {"--check", "-c", "check"}:
        return check(cfg)
    if argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    niche = ""
    if "--niche" in argv:
        i = argv.index("--niche")
        niche = argv[i + 1] if i + 1 < len(argv) else ""
        argv = argv[:i] + argv[i + 2:]
    # --model lets you choose the Ollama model per run (overrides studio.yaml).
    if "--model" in argv:
        i = argv.index("--model")
        if i + 1 < len(argv):
            cfg.ollama_model = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    per_platform = "--per-platform" in argv
    argv = [a for a in argv if a != "--per-platform"]
    if not argv:
        print("usage: python -m studio.prepare <video> [--model NAME] "
              "[--niche TEXT] [--per-platform]")
        return 2
    return prepare(argv[0], cfg, niche=niche, per_platform=per_platform)


if __name__ == "__main__":
    raise SystemExit(main())
