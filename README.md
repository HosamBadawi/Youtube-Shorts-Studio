# ▶️ YouTube Shorts Studio

**Turn one long YouTube video into several ready-to-upload Shorts — semantically cut, montage-paced, captioned, thumbnailed, and published — all on your own PC, all free.**

Paste a YouTube link into a phone-friendly web page and the pipeline does the rest:

1. **Download** the source (yt-dlp, aria2c-accelerated).
2. **Transcribe** with Whisper `large-v3` (GPU) — word-level timestamps.
3. **Find the best moments** with a local LLM (Ollama). Every short is one
   *complete idea*: it opens on a hook and ends after the payoff, snapped to
   sentence boundaries. Sponsor reads, intros and outros are masked
   (crowd-sourced [SponsorBlock](https://sponsor.ajay.app) data + LLM
   classification) and never end up in a clip. **The LLM never emits
   timestamps** — it returns sentence indices that are resolved against the
   Whisper word timings in code.
4. **Cut the silences** — jump-cut montage with a subtle alternating punch-in
   zoom; caption timings are remapped through the cuts so they stay in sync.
5. **Reframe to 9:16** with a face-tracking, preservation-biased engine
   ([`adaptive_reframe`](adaptive_reframe/README.md)).
6. **Burn karaoke captions** — word-by-word highlight with real
   **right-to-left Arabic** layout (most tools scramble Arabic word order;
   this one positions every word itself).
7. **Add a subscribe reminder** — an animated اشترك button with a bell ding,
   generated programmatically (Pillow), burned in mid-short.
8. **Write the copy** — a curiosity+result title, a description, hashtags and
   a thumbnail headline, all from the local LLM.
9. **Compose a thumbnail** — the best face frame is auto-picked and cut out
   (the presenter's *real pixels*, never an AI face), placed on a bold
   background under a huge Arabic headline. Downloadable to your phone, and
   bakeable into the video's first frame.
10. **Review & upload** from your phone (free Cloudflare tunnel): edit the
    copy, swap the thumbnail frame, pick privacy, tap **Upload** — official
    YouTube Data API v3.

```
YouTube URL ── yt-dlp ──► Whisper large-v3 (word timestamps)
                              │
              SponsorBlock ──►│◄── LLM junk classification
                              ▼
               semantic segmenter (map → validate → reduce,
               sentence-snapped, honest when fewer clips exist)
                              │  per short:
                              ▼
   trim ─► silence-cut montage ─► face-tracked 9:16 ─► RTL karaoke captions
        ─► subscribe reminder ─► title/description/headline ─► thumbnail
                              │
                              ▼
              phone review UI ─► YouTube Data API upload
```

## Why it's different

- **Semantic, not random** — no fixed-interval cutting, no mid-sentence
  boundaries. If only 3 of the 5 requested clips are genuinely strong, you
  get 3 and the UI tells you why (quality over padding).
- **Real Arabic support end-to-end** — transcription, captions, titles,
  thumbnail typography (shaped with `arabic-reshaper` + `python-bidi`,
  bundled OFL fonts).
- **Identity-safe thumbnails** — the face is a literal pixel cutout
  ([rembg](https://github.com/danielgatis/rembg) / BiRefNet), never a
  diffusion lookalike.
- **Self-hosted & free** — your GPU, your models, your data. Runs on a
  single 8 GB card (RTX 3060 Ti class): Whisper, a 7B LLM and the matting
  model are scheduled so they never fight for VRAM.

## Quickstart

**Prerequisites:** Python 3.11+, `ffmpeg`/`ffprobe` on PATH,
[Ollama](https://ollama.com) with a model pulled (`ollama pull qwen2.5:7b`),
optionally `cloudflared` for phone access and `aria2c` for faster downloads.

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-studio.txt
# GPU transcription (NVIDIA):
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cuda-runtime-cu12 nvidia-cuda-nvrtc-cu12

cp studio.example.yaml studio.yaml    # then edit — SET A STRONG app_password
python -m studio.login_setup          # one-time YouTube OAuth (see guide)
python -m studio                      # prints a local + tunnel URL for your phone
```

Full setup guide (YouTube API credentials, tunnel modes, every config knob):
**[STUDIO_README.md](STUDIO_README.md)**.

CLI tools (no upload): `python -m studio.prepare --check` (environment
doctor), `python -m studio.shorts <video-or-URL> --count 3` (batch to
`workspace/shorts/`), `python -m studio.sample_modes <video>` (compare
reframe looks).

## Security posture

Designed to sit on the public internet behind a tunnel: password gate with
per-install HMAC cookies and global login backoff, fail-closed startup on a
default password, security headers + strict CSP, SSRF-guarded downloads
(host allowlist + private-IP blocking), AES-256-GCM vault for cloud API keys
(DPAPI-wrapped on Windows), ACL-locked OAuth token, and no secrets in the
repo (`secrets/`, `workspace/`, `studio.yaml` are all gitignored). Read
[STUDIO_README.md](STUDIO_README.md#security-notes) before exposing it.

## Repo layout

```
studio/             the app: server, pipeline, segmenter, montage, subscribe
                    overlay, thumbnails, captions, YouTube publisher, web UI
adaptive_reframe/   the standalone face-tracking 9:16 reframing engine
REDESIGN_PLAN.md    the architecture/design document this build followed
STUDIO_README.md    full deploy + usage guide
studio.example.yaml every configuration option
```

## Acknowledgements

[yt-dlp](https://github.com/yt-dlp/yt-dlp) · [faster-whisper](https://github.com/SYSTRAN/faster-whisper) · [Ollama](https://ollama.com) · [OpenCV](https://opencv.org) · [ffmpeg](https://ffmpeg.org) · [libass](https://github.com/libass/libass) · [rembg](https://github.com/danielgatis/rembg) · [SponsorBlock](https://sponsor.ajay.app) (CC BY-NC-SA 4.0 data) · [FastAPI](https://fastapi.tiangolo.com) · Google Fonts ([Cairo, Tajawal, Lalezar](studio/assets/fonts/README.md), OFL)

## License

[MIT](LICENSE). Only download / publish content you own or have the right to use.
