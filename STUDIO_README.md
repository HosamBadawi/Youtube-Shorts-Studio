# Daily Shorts Studio

Self-hosted daily Shorts publisher. Host it on your home PC (your RTX 3060 Ti),
open a phone-friendly page from anywhere through a **free Cloudflare tunnel**,
upload today's clip, and it will:

1. **Transcribe** the audio (Whisper, GPU-accelerated).
2. **Pick the best segment** with your **local Ollama** model (only if the source
   is long) and trim to it.
3. **Reframe to vertical 9:16** using the built-in `adaptive_reframe` engine.
4. **Draft a title / caption / hashtags** with Ollama — which you can **edit or
   write yourself** in the form.
5. **Publish** to **YouTube Shorts, Instagram, TikTok, and Facebook** with one tap.

It's free end-to-end: YouTube uses the official (free) Data API; Instagram,
TikTok and Facebook use Playwright browser automation against your saved logins;
remote access is a free Cloudflare quick tunnel; the AI runs locally in Ollama.

```
phone ──https──> Cloudflare tunnel ──> FastAPI (studio/) ──> adaptive_reframe (9:16)
                                              │                Whisper + Ollama (local)
                                              └──> publishers: YouTube API · IG/TikTok/FB (Playwright)
```

---

## 1. Prerequisites (on the host PC)

- **Python 3.11+**
- **ffmpeg + ffprobe** on PATH — https://www.gyan.dev/ffmpeg/builds/ (Windows) or `winget install Gyan.FFmpeg`
- **Ollama** — https://ollama.com → then pull a model:
  ```powershell
  ollama pull llama3.1
  ```
- **cloudflared** — https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
  (drop `cloudflared.exe` on your PATH). Free, no account needed for quick mode.

## 2. Install

```powershell
cd E:\Hosam_Mahmoud\Social_Media\files
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-studio.txt
python -m playwright install chromium
```

> The reframe engine needs `opencv-contrib-python` (already in `requirements.txt`)
> and works on GPU for Whisper if you have CUDA/PyTorch installed.

## 3. Configure

```powershell
copy studio.example.yaml studio.yaml
```

Edit `studio.yaml` and **change `app_password`** — the app is reachable from the
public internet through the tunnel, so this password is your gate.

## 4. One-time platform logins

Run on the PC **with a screen** so the browser/consent windows can open:

```powershell
python -m studio.login_setup all
```

- **Instagram / TikTok / Facebook**: a browser opens — log in (handle 2FA), then
  just **close the window**. The session is saved under `workspace/sessions/`.
- **YouTube**: needs a free Google Cloud OAuth client first:
  1. https://console.cloud.google.com → new project → enable **YouTube Data API v3**.
  2. *APIs & Services → Credentials → Create credentials → OAuth client ID →
     Application type: Desktop app*. Download the JSON.
  3. Save it to `secrets/youtube_client_secret.json` (path is configurable).
  4. `python -m studio.login_setup youtube` opens Google's consent screen once;
     the refresh token is cached to `secrets/youtube_token.json`.

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

## Daily flow (from your phone)

1. Tap **choose a video**, pick today's clip.
2. (Optional) type a niche so the AI copy is on-topic; toggle AI captions on/off.
3. **Upload & prepare** → watch it transcribe → segment → reframe → caption.
4. On the **Review** screen: watch the 9:16 preview, edit the title/caption/
   hashtags (or hit **Regenerate with AI**), tick the platforms.
5. **Publish now**. Per-platform results (with links) show when done.

`one_per_day: true` blocks a second publish on the same day — flip it off in
`studio.yaml` if you want more.

---

## How each platform is published

| Platform   | Method | Notes |
|------------|--------|-------|
| YouTube    | Official Data API v3 | Free quota (~6 uploads/day). `#Shorts` auto-added. |
| Instagram  | Playwright (web Reels) | Uses saved login. Web "Create" modal flow. |
| TikTok     | Playwright (tiktok.com/upload) | Uses saved login. |
| Facebook   | Playwright (facebook.com/reels/create) | Uses saved login. |

The browser publishers click through the real upload UI. When a site changes its
layout a step may time out — set `playwright_headless: false` in `studio.yaml`,
re-run, and watch where it stops; the selectors live in
`studio/publishers/<platform>.py` with fallbacks that are easy to adjust. If a
session expires you'll see **"login needed"** in the results — just re-run
`python -m studio.login_setup <platform>`.

## Security notes

- Keep `app_password` strong; the tunnel makes the app world-reachable.
- `workspace/sessions/` and `secrets/` hold live logins/tokens — never commit or
  share them.
- Prefer `cloudflare_mode: named` (or Tailscale) for a stable, lockable URL.

## Troubleshooting

- **"Ollama offline"** in the status bar → `ollama serve` isn't running or the
  model isn't pulled. Captions then fall back to manual entry.
- **No transcript / no AI segment** → `faster-whisper` not installed; the clip is
  still reframed and published, you just write captions yourself.
- **Reframe errors** → ensure `opencv-contrib-python` and ffmpeg are installed.
- **Tunnel URL not shown** → `cloudflared` isn't on PATH; the local URL still works
  on your LAN.
