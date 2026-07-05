# YouTube Shorts Studio

Self-hosted YouTube Shorts factory. Host it on your home PC (your RTX 3060 Ti),
open a phone-friendly page from anywhere through a **free Cloudflare tunnel**,
paste a long YouTube link, and it will:

1. **Download** the video (yt-dlp, aria2c-accelerated).
2. **Transcribe** it (Whisper large-v3, GPU) with word timestamps.
3. **Find the best moments** with your **local Ollama** model — every short is
   one complete idea that starts on a hook and ends after the payoff, snapped
   to sentence boundaries. Sponsor reads / intros / outros are masked
   (SponsorBlock + LLM classification) and never end up in a short.
4. **Cut the silences** so shorts feel fast (jump cuts + a subtle alternating
   punch-in zoom), keeping the karaoke captions perfectly in sync.
5. **Reframe to vertical 9:16** with the built-in face-tracking
   `adaptive_reframe` engine and **burn Egyptian-Arabic karaoke captions**.
6. **Add a subscribe reminder** mid-short — an animated اشترك button with a
   bell ding, generated programmatically (no stock footage).
7. **Write the copy**: an Arabic title (curiosity + result formula), a YouTube
   description, topic hashtags, and a 3-6 word thumbnail headline.
8. **Compose a thumbnail**: the best face frame is auto-picked and cut out
   (the presenter's real pixels — never an AI face), placed on a bold
   background with the huge Arabic headline.
9. **Review on your phone**: edit the title/description, regenerate the
   thumbnail, pick a different frame — then **upload to YouTube** with one tap
   (official Data API; ~100 uploads/day on the free quota).

It's free end-to-end: the AI runs locally in Ollama, YouTube uses the official
(free) Data API, and remote access is a free Cloudflare quick tunnel.

```
phone ──https──> Cloudflare tunnel ──> FastAPI (studio/)
                                          │  Whisper + Ollama + rembg (local)
                                          │  segmenter → montage → reframe →
                                          │  captions → subscribe → thumbnail
                                          └──> YouTube Data API v3 (official)
```

---

## 1. Prerequisites (on the host PC)

- **Python 3.11+**
- **ffmpeg + ffprobe** on PATH — https://www.gyan.dev/ffmpeg/builds/ or `winget install Gyan.FFmpeg`
- **Ollama** — https://ollama.com → pull a model that fits an 8 GB card:
  ```powershell
  ollama pull qwen2.5:7b
  ```
- **cloudflared** — https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
  (drop `cloudflared.exe` on your PATH). Free, no account needed for quick mode.

## 2. Install

```powershell
cd YoutubeShortsStudio
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-studio.txt
```

> The first thumbnail run downloads the background-removal model (~1 GB,
> one time). The reframe engine needs `opencv-contrib-python` (in
> `requirements.txt`); Whisper uses CUDA automatically when available.

## 3. Configure

```powershell
copy studio.example.yaml studio.yaml
```

Edit `studio.yaml` and **change `app_password`** — the app is reachable from
the public internet through the tunnel, so this password is your gate.

## 4. One-time YouTube authorization

1. https://console.cloud.google.com → new project → enable **YouTube Data API v3**.
2. *APIs & Services → Credentials → Create credentials → OAuth client ID →
   Application type: Desktop app*. Download the JSON.
3. Save it to `secrets/youtube_client_secret.json` (path is configurable).
4. `python -m studio.login_setup` opens Google's consent screen once; the
   refresh token is cached to `secrets/youtube_token.json`.

> Re-run `python -m studio.login_setup` if the app ever reports
> "re-auth needed" — e.g. after this release, which added the thumbnail
> permission (`youtube.force-ssl`) to the requested scopes.

## 5. Run

```powershell
python -m studio          # or: .\start_studio.ps1
```

The console prints a local URL **and** a `https://<random>.trycloudflare.com`
URL. Open that one on your phone, enter the app password, and you're in.

> The quick-tunnel URL changes every run. For a **permanent** URL, set
> `cloudflare_mode: named` and a `cloudflare_token` from the Cloudflare Zero
> Trust dashboard (Tunnels → create → copy the token).

---

## The flow (from your phone)

1. **Create** tab → paste a YouTube link (or upload / pick from the library).
2. Options: how many shorts (a **maximum** — if the AI finds only 3 genuinely
   strong segments out of 5 requested, you get 3 and it says why), min/max
   duration, optional niche.
3. **Generate** → watch the progress bar (download → transcribe → select →
   per-short render/copy/thumbnail).
4. **Shorts** tab: per card — video preview next to its thumbnail; edit the
   title/description/hashtags; *Regenerate* / *Pick frame* / *Headline* to
   reshape the thumbnail; "Why this clip?" shows the AI's reasoning.
5. Pick privacy and **Upload to YouTube** (or *Upload all*). Results show a
   direct link. The generated thumbnail is baked in as the first frame
   (toggleable via `embed_thumb_first_frame`) and also submitted via the API.

## Thumbnails on Shorts — what to expect

The Shorts **feed** never displays thumbnails (it's full-screen autoplay).
Thumbnails appear in **search, your channel grid, hashtag pages and suggested
cards** — that's where the composed thumbnail earns its clicks. The
first-frame embed guarantees your design is also what frame-selection
surfaces show; the `thumbnails.set` API call is attempted after every upload
and its outcome is reported per short (Shorts support for it is
account-dependent and needs a phone-verified channel).

## CLI tools (no upload, for testing)

```powershell
python -m studio.prepare --check          # environment doctor
python -m studio.prepare video.mp4        # full prep for ONE short, no upload
python -m studio.shorts video.mp4 --count 3   # cut N shorts to workspace/shorts/
python -m studio.sample_modes video.mp4   # compare reframe modes side by side
```

## Security notes

- **Change `app_password`** — the tunnel makes the app world-reachable. The
  login gate is rate-limited and uses a per-install random key; cookies are
  Secure whenever the tunnel is on.
- **Cloud LLM API keys are encrypted at rest** (AES-256-GCM, key wrapped with
  Windows DPAPI). The YouTube OAuth token file is ACL-locked to your user.
- `workspace/` and `secrets/` hold tokens and rendered content — gitignored;
  never commit or share them.
- Downloads are SSRF-guarded (`download_host_allowlist`, private-IP blocking).

## Troubleshooting

- **"Ollama offline"** in the status pill → `ollama serve` isn't running or no
  model is pulled. Segment selection and copy then fall back gracefully.
- **Fewer shorts than requested** → that's by design (quality over padding);
  widen min/max duration or lower the count.
- **No transcript** → `faster-whisper` not installed or no speech; one default
  clip is cut and captions are skipped.
- **Thumbnail has no cutout** → `rembg`/`onnxruntime` missing, or the first
  model download was interrupted; the composer falls back to a full-frame
  design automatically.
- **Upload says re-auth needed** → run `python -m studio.login_setup` on the PC.
- **Tunnel URL not shown** → `cloudflared` isn't on PATH; the local URL still
  works on your LAN.
