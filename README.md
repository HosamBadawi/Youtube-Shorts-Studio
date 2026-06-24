# 🎬 Daily Shorts Studio

**Turn one long video into several ready-to-post vertical Shorts — automatically, on your own PC.**

Feed it a long video (a local file *or* a YouTube URL) and it will:

1. **Download** the source (yt-dlp, optionally aria2c-accelerated) — or use a local file.
2. **Transcribe** the audio with Whisper (`large-v3`, GPU-accelerated).
3. **Split it into N distinct shorts** — a local LLM picks non-overlapping, self-contained segments, *each its own topic*.
4. **Reframe** each segment to vertical **9:16** with an adaptive strategy (face-crop, blur-background, or a subject-tracked *crop + blur* hybrid).
5. **Burn TikTok-style captions** — word-by-word highlight, with full **right-to-left Arabic** support (correct order, single-word highlight).
6. **Write the post text** — title, caption, and hashtags in the video's own language.
7. *(optional)* **Publish** to YouTube Shorts, Instagram, TikTok, and Facebook — driven from a phone-friendly web page over a free Cloudflare tunnel.

Everything runs **locally and free**: Whisper + the LLM (via [Ollama](https://ollama.com)) run on your machine; the only "official API" used is YouTube's (free).

```
long video / URL
      │  yt-dlp
      ▼
  Whisper large-v3 (GPU)  ──►  transcript + word timestamps
      │
      ▼  local LLM (Ollama)
  N distinct segments (each its own topic)
      │   for each segment:
      ▼
  ffmpeg trim ─► adaptive_reframe 9:16 ─► burn captions ─► LLM title/caption/hashtags
      │
      ▼
  workspace/shorts/*.mp4  (+ a .txt with the post text)
```

---

## Why it's different

- **Multi-short, semantically split** — not just "clip the first 60s". The LLM reads the whole transcript and carves out several *distinct* shorts.
- **Real Arabic support** — accurate transcription (`large-v3` + forced language) and **correct RTL captions** (most auto-caption tools scramble Arabic word order; this one positions each word so it doesn't).
- **Adaptive 9:16 reframing** — a preservation-biased engine ([`adaptive_reframe`](adaptive_reframe/README.md)) that won't butcher the composition.
- **Self-hosted & free** — your GPU, your models, your data. No per-video SaaS fees.

---

## Quickstart

### 1. Prerequisites
- **Python 3.11+**
- **ffmpeg + ffprobe** on PATH
- **[Ollama](https://ollama.com)** running locally, with a model pulled (e.g. `ollama pull command-r7b-arabic` or `ollama pull qwen2.5:7b`)
- *(optional, for GPU)* an NVIDIA card; *(optional)* `aria2c` for faster downloads

### 2. Install
```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-studio.txt
# GPU transcription (NVIDIA):
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cuda-runtime-cu12 nvidia-cuda-nvrtc-cu12
```

### 3. Configure
```bash
cp studio.example.yaml studio.yaml      # then edit
```
Key settings: `whisper_model: large-v3`, `whisper_language` (e.g. `ar`), `ollama_model`, `reframe_mode` (`crop_blur` recommended), `shorts_per_video`.

### 4. Make shorts (no upload)
```bash
# from a YouTube URL:
python -m studio.shorts "https://www.youtube.com/watch?v=XXXX" --count 3 --niche "your topic"

# from a local file:
python -m studio.shorts "path/to/long_video.mp4" --count 5 --model command-r7b-arabic
```
Each short lands in `workspace/shorts/` as an `.mp4` plus a `.txt` with its title/caption/hashtags.

Other entry points:
```bash
python -m studio.prepare --check                 # environment doctor
python -m studio.prepare "video_or_URL"          # single best short, pick technique
python -m studio.sample_modes "video" --modes face_focus,crop_blur   # compare looks
```

### 5. (Optional) Phone web app + publishing
See **[STUDIO_README.md](STUDIO_README.md)** for the full guide: the FastAPI web UI, the free Cloudflare tunnel so you can drive it from your phone, the one-time platform logins, and the YouTube API setup.

---

## Reframing techniques

| Mode | Look |
|------|------|
| `face_focus` | Tight subject crop that fills the frame |
| `crop_blur` *(recommended)* | Subject-tracked crop **+** blurred margins — keeps on-screen graphics, subject stays big |
| `blur_background` | Full scene centred over a blurred fill |
| `auto` | The engine classifies each clip and picks a strategy |

Set your default with `reframe_mode:` in `studio.yaml`, or per-run with `--mode`.

## Models

Any [Ollama](https://ollama.com) model works (`--model NAME`). Good picks:
- **`command-r7b-arabic`** — Arabic-specialised, fits 8 GB VRAM, fast.
- **`qwen2.5:7b` / `aya-expanse:8b`** — strong multilingual, fast.
- A large model (30B+) — higher quality, slower if it spills past your VRAM.

## Repo layout

```
studio/             the Daily Shorts Studio app (download, transcribe, segment,
                    caption, metadata, publish, web server)
adaptive_reframe/   the standalone 9:16 reframing engine  (see its own README)
STUDIO_README.md    full deploy + publishing guide
studio.example.yaml all configuration options
```

## Acknowledgements

[yt-dlp](https://github.com/yt-dlp/yt-dlp) · [faster-whisper](https://github.com/SYSTRAN/faster-whisper) · [Ollama](https://ollama.com) · [OpenCV](https://opencv.org) · [ffmpeg](https://ffmpeg.org) · [libass](https://github.com/libass/libass) · [Playwright](https://playwright.dev) · [FastAPI](https://fastapi.tiangolo.com)

## License

[MIT](LICENSE). Only download / publish content you own or have the right to use.
