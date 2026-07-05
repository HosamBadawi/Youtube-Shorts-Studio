# YouTube Shorts Studio — Redesign & Implementation Plan

**Date:** 2026-07-05 · **Status:** awaiting approval — no code written yet
**Hardware target:** single RTX 3060 Ti (8 GB VRAM), Windows 10, Python 3.11
**Method:** 6 parallel codebase-analysis passes + 7 web-research investigations, each research
finding independently fact-checked (verdicts noted inline where they changed a decision).

---

## Table of contents

1. [Current architecture review](#1-current-architecture-review)
2. [Problems with the current design](#2-problems-with-the-current-design)
3. [Refactoring strategy](#3-refactoring-strategy)
4. [New architecture](#4-new-architecture)
5. [Folder restructuring](#5-folder-restructuring)
6. [Semantic segment selection (AI pipeline core)](#6-semantic-segment-selection)
7. [Thumbnail generation — research findings & recommended pipeline](#7-thumbnail-generation)
8. [Title / description / headline generation](#8-title--description--headline-generation)
9. [YouTube upload & platform facts (verified 2026)](#9-youtube-upload--platform-facts)
10. [Backend architecture & API design](#10-backend-architecture--api-design)
11. [Database considerations](#11-database-considerations)
12. [Frontend architecture & UI/UX redesign](#12-frontend-architecture--uiux-redesign)
13. [Model selection & GPU orchestration](#13-model-selection--gpu-orchestration)
14. [Performance budget](#14-performance-budget)
15. [Scalability](#15-scalability)
16. [Risks](#16-risks)
17. [Implementation phases](#17-implementation-phases)

---

## 1. Current architecture review

The project is in much better shape for this redesign than the brief implies. The target
user flow **already exists end-to-end** — it just carries three dead platforms and has
quality gaps in exactly the places you identified.

```
phone ──HTTPS──▶ Cloudflare tunnel ──▶ FastAPI (studio/server.py, password gate)
                                        │
              ┌─────────────────────────┼─────────────────────────────┐
              ▼                         ▼                             ▼
     worker pool (1 thread)     net pool (2 threads)         publisher pool (1 thread)
     generate_shorts():         yt-dlp downloads             YouTube Data API v3 (OAuth)
       yt-dlp download          connection health            Instagram  (Playwright)  ◀ delete
       faster-whisper (GPU)                                  TikTok     (Playwright)  ◀ delete
       llm.pick_segments()                                   Facebook   (Playwright)  ◀ delete
       ffmpeg trim per segment                               Meta Graph API           ◀ delete
       adaptive_reframe (9:16, CPU)
       ASS caption burn (ffmpeg)
       llm.generate_metadata()
              │
              ▼
     SQLite workspace/studio.db (jobs table, polled by the web UI every 2 s)
```

**What already works and maps directly onto the target flow:**

| Requirement | Existing implementation | State |
|---|---|---|
| Paste YouTube URL | `downloader.py` — yt-dlp + aria2c, SSRF-guarded host allowlist | ✔ keep |
| Configure min/max/count | `generate_shorts(count, min_s, max_s)` + UI steppers | ✔ keep |
| Transcription | `transcribe.py` — faster-whisper, word-level timestamps, CUDA w/ CPU fallback | ✔ untouched |
| Egyptian Arabic captions | `captions.py` — ASS karaoke burn, custom RTL word layout via Pillow measurement (bypasses libass bidi scrambling) | ✔ untouched |
| Face tracking | `adaptive_reframe/` — self-contained, CPU, 9 modes, returns per-frame face track (`ReframeResult.analysis.faces`) that studio currently *discards* | ✔ untouched (we will *read* its output) |
| LLM segment picking | `llm.py pick_segments()` — Ollama JSON mode, intro-skip floor | ◐ replace internals (§6) |
| Title/caption drafting | `llm.py generate_metadata()` — generic multi-platform prompt | ◐ replace prompts (§8) |
| Review & edit on phone | Web UI job cards, editable meta, video preview | ◐ rewrite UI (§12) |
| Upload to YouTube | `publishers/youtube.py` — official Data API, resumable upload, OAuth token | ✔ keep + extend |
| Thumbnails | **does not exist** (`grep -i thumbnail` → zero matches) | ✚ new (§7) |

**Key modules and sizes:** `server.py` 454 · `pipeline.py` 463 · `llm.py` 500 ·
`config.py` 365 · `captions.py` 319 · `vault.py` 323 · publishers ~1,650 across 11 files ·
web UI ~975 lines vanilla JS/CSS/HTML (no framework, no build step).

**Security posture (worth preserving):** password gate with global exponential backoff +
HMAC cookie; fail-closed startup on default password; AES-256-GCM vault with
DPAPI-wrapped key; SSRF guard on downloads; ffprobe arg-injection defense. This was
recently hardened for public release — the redesign must not regress it.

---

## 2. Problems with the current design

### 2.1 The "random cuts" you're seeing — root causes located

1. **`_fill_to_count()` (pipeline.py:421)** — when the LLM returns fewer segments than
   requested (which the code comment admits happens "routinely"), it silently tops up with
   **evenly-spaced blind windows** of `target_len`. These ignore the transcript entirely —
   they are the mid-sentence, mid-explanation cuts you're seeing. The guarantee "always
   yields N shorts" is achieved by abandoning semantics.
2. **`_enforce_bounds()` (pipeline.py:398)** — numeric clamping: an over-long span is cut
   at exactly `start + max_s` **regardless of speech**; a too-short one is blindly extended.
   Even a perfect LLM pick gets mangled to fit the duration band.
3. **One-shot prompt over a truncated transcript (llm.py:97)** — the whole transcript is
   sent in a single prompt, capped at 48,000 chars. A ~90+ minute video loses its tail;
   an 8B-class model degrades long before that (research: local 8B models are reliable at
   ~1.5–2.5k chars per reasoning chunk — a fraction of what it's being fed).
4. **No sentence awareness** — the LLM sees `[mm:ss]` segment stamps and emits raw seconds;
   nothing snaps boundaries to sentence starts/ends from the word timestamps that
   `transcribe.py` already produces.
5. **Intro skip is a crude time floor** (`max(45s, 5% of duration)`) — good instinct, but
   it can't catch mid-video sponsor reads or an outro, and it wastes content when a video
   has a 10-second intro.

### 2.2 Multi-platform debt

~2,300 lines exist solely for IG/TikTok/Facebook: 8 publisher files, `login_flow.py`,
`public_share.py`, the Playwright dependency, the Edge-profile copier, 2FA code queues,
per-platform metadata overrides and caption caps, `/pub/{token}` (an **unauthenticated**
public file endpoint that exists only so Meta's servers can fetch reels), rehearse/screenshot
endpoints, and ~40 config fields. All deletable with zero impact on the YouTube path
(verified: import graph is one-way; YouTube uses OAuth token file, not the vault sessions).

### 2.3 Other debt worth fixing while we're in there

- `_trim()` duplicated 3× (pipeline.py, shorts.py, sample_modes.py).
- `VideoMeta.caption` doubles as the YouTube description; no real `description` concept.
- Auto model selection picks the **largest** installed Ollama model — on an 8 GB card a
  35B spills to CPU and crawls (config comment acknowledges it). It should prefer a model
  that fits.
- Download progress is regex-scraped from a stage string by the frontend; the backend
  should just report a numeric percent.
- No thumbnail concept anywhere; publish results modeled as a per-platform dict.

---

## 3. Refactoring strategy

**Strangle, don't rewrite.** The render chain (trim → reframe → captions), transcription,
vault, tunnel, job store, and the YouTube publisher are healthy. The strategy:

1. **Delete first** (Phase 1): remove all IG/TT/FB code and config in one commit while the
   existing tests/flows still run. This shrinks every later diff.
2. **Replace the selector** (Phase 2): a new `segmenter.py` module replaces the internals of
   segment picking behind the same `(start, end, topic)` contract, so pipeline changes are
   minimal and testable in isolation (it's pure transcript-in → spans-out).
3. **Add capabilities as new modules** (Phases 3–4): `thumbnails/` package, new prompt
   methods on `_BaseLLM`. Nothing inside `captions.py`, `transcribe.py`, or
   `adaptive_reframe/` changes.
4. **Rewrite the thin shell last** (Phase 5): the web UI is ~975 lines and half of it dies
   with the platforms; rewriting against a stable, already-extended API is cheaper than
   incremental surgery. Keep it vanilla (no framework/build step — right call for a
   self-hosted single-user app, confirmed by frontend analysis).

Each phase leaves the app fully working. Git history preserves the deleted platform code
if it's ever wanted again.

---

## 4. New architecture

```
                    ┌────────────────────────────────────────────────────────────┐
 phone (PWA-ish     │  FastAPI  studio/server.py           password gate + HMAC  │
 mobile web) ──────▶│                                                            │
 via Cloudflare     │  /api/generate ─▶ worker pool (1 thread — owns the GPU)    │
 tunnel             │  /api/batch, /api/job ◀── SQLite polling (2 s)             │
                    │  /api/job/{id}/thumbnail, /frames, /upload                 │
                    └───────────────┬────────────────────────────────────────────┘
                                    ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │ StudioPipeline.generate_shorts()                                │
        │                                                                 │
        │ 1 download        yt-dlp (+aria2c)                              │
        │ 2 transcribe      faster-whisper GPU ──▶ words + sentence table │
        │ 3 mask junk       SponsorBlock API (+ LLM fallback)   ◀── NEW   │
        │ 4 select          segmenter.py map→validate→reduce    ◀── NEW   │
        │                   (Ollama structured outputs, sentence-snapped) │
        │ 5 per short:      trim ─▶ adaptive_reframe ─▶ caption burn      │
        │ 6 per short:      title + description + headline (one LLM call) │
        │ 7 per short:      thumbnail: frame-pick ─▶ cutout ─▶ compose    │
        │                   (+ optional diffusion background, Phase 7)    │
        │ 8 review (human)  edit title/desc, regenerate/re-pick thumbnail │
        │ 9 upload          YouTube Data API: insert (+ first-frame opt.) │
        │                   + thumbnails.set best-effort                  │
        └─────────────────────────────────────────────────────────────────┘

 GPU timeline per batch (8 GB, strictly sequential — never concurrent):
 [whisper ~1-2GB] ─▶ [ollama 7-9B q4 ~5GB] ─▶ (Phase 7 only: evict ollama ─▶ SDXL/klein ─▶ free) ─▶ [ollama reloads ~3-10s]
 CPU throughout: ffmpeg encodes, adaptive_reframe, rembg cutout, Pillow compositing
```

---

## 5. Folder restructuring

Minimal moves — renames churn git history and the layout is mostly sound. Deletions do the
cleaning; one new package is added.

```
YoutubeShortsStudio/
├── studio/
│   ├── server.py              # slimmed: YouTube-only endpoints, numeric progress
│   ├── server_models.py       # unchanged (LLM picker)
│   ├── server_connections.py  # shrunk to: health + YouTube OAuth status
│   ├── pipeline.py            # orchestration; _fill_to_count deleted
│   ├── segmenter.py           # ✚ NEW semantic selection (map→validate→reduce)
│   ├── sponsorblock.py        # ✚ NEW junk-segment lookup (k-anonymity API)
│   ├── thumbnails/            # ✚ NEW package
│   │   ├── frames.py          #   candidate frame extraction + scoring
│   │   ├── cutout.py          #   rembg / birefnet-portrait subject matting
│   │   ├── compose.py         #   templates: outline, shadow, background, export
│   │   ├── headline.py        #   Arabic text rendering (reshaper+bidi, fonts)
│   │   └── background.py      #   Phase 7: optional diffusion background client
│   ├── assets/fonts/          # ✚ NEW bundled OFL Arabic display fonts
│   ├── llm.py                 # + structured-output call, new prompt methods
│   ├── metadata.py            # VideoMeta: + description, + thumbnail_headline;
│   │                          #   platform overrides/caps deleted
│   ├── captions.py            # UNTOUCHED
│   ├── transcribe.py          # UNTOUCHED (+ tiny helper: sentence table builder)
│   ├── publishers/
│   │   ├── __init__.py        # factory → youtube only
│   │   ├── base.py            # PublishResult (simplified)
│   │   └── youtube.py         # + thumbnails.set, + per-short privacy
│   ├── jobs.py                # + thumb_path column (existing migration pattern)
│   ├── config.py              # ~40 platform fields deleted; thumbnail knobs added
│   ├── downloader.py / cloudflared.py / vault.py / dpapi.py / health.py  # kept
│   ├── login_setup.py         # YouTube OAuth branch only
│   └── web/                   # rewritten (index.html, app.js, style.css)
├── adaptive_reframe/          # UNTOUCHED
├── tests/                     # + segmenter & thumbnail unit tests
└── STUDIO_README.md           # rewritten for the new identity

DELETED: studio/publishers/{instagram,tiktok,facebook,meta_api,playwright_publisher,
         playwright_base,session_provider,edge_profile}.py, studio/login_flow.py,
         studio/public_share.py, playwright dependency, workspace/{sessions,rehearsals}
```

Rebranding: all docs/strings become **YouTube Shorts Studio**; `pyproject.toml` name,
`__init__.py` docstrings, DPAPI entropy string stays (changing it would orphan the vault).

---

## 6. Semantic segment selection

This is the core quality fix. Design synthesized from how OpusClip-class tools work, the
open-source blueprints (SamurAIGPT/AI-Youtube-Shorts-Generator, ClipsAI, FunClip), and
verified constraints of local 8B models.

### 6.1 Principles

- **The LLM never emits timestamps.** It sees a **numbered sentence table** and returns
  sentence **indices**; code maps indices → exact times from Whisper word timestamps.
  (Research verdict: LLM-emitted raw timestamps are the documented failure mode of naive
  clippers; index/quote-based grounding is the robust pattern.)
- **Every boundary is a sentence boundary.** Duration enforcement extends/trims by whole
  sentences, never by raw seconds. `_enforce_bounds` survives only as a final ±0.3 s
  safety clamp with word-level snapping.
- **Count is a maximum, honestly reported.** If only 3 of 5 requested shorts pass quality
  validation, the UI says so ("وجدنا ٣ مقاطع قوية فقط") instead of padding with junk.
  `_fill_to_count()` is deleted. An optional "relaxed" re-pass with a lower score threshold
  runs once before giving up.
- **Junk is masked before selection**, not just floored.

### 6.2 Algorithm

```
INPUT: Transcript (words w/ timestamps), duration, count, min_s, max_s, video_id

1. SENTENCE TABLE   split words into sentences on punctuation (.؟!،…) + >0.7s gaps;
                    build:  [i] start_s  end_s  «text»   (id, times, text)

2. JUNK MASK        SponsorBlock k-anonymity lookup (sha256(video_id)[:4]):
                    categories sponsor, selfpromo, interaction, intro, outro, preview.
                    Fallback / additionally: one LLM call classifying the first and last
                    ~90s of sentences as greeting/intro/sponsor/content.
                    Masked sentence ranges are ineligible. (Replaces the blind 45s floor;
                    keep intro_skip_seconds as a config-able minimum backstop.)

3. MAP (per window) windows of ~25–40 sentences (~3–5 min), 25% overlap.
                    One Ollama call per window with format = JSON SCHEMA (grammar-
                    constrained, Ollama ≥0.5), temperature 0.2:
                      {clips: [{start_idx, end_idx, hook_quote, score 0-100,
                                reason, topic_ar}], max 3}
                    Prompt rubric: hook strength / self-contained (no prior context
                    needed) / payoff at the end / min-max duration stated / must not
                    start mid-setup. Written to work in Arabic.

4. VALIDATE (code)  indices in range & unmasked; hook_quote fuzzy-matches the start
                    sentence (rapidfuzz; else shift to best match); duration from the
                    sentence table within [min_s, max_s] — else extend/trim whole
                    sentences toward the rubric ("end after the payoff"); reject if
                    impossible. Pydantic parse + one retry per window on failure.

5. REDUCE           merge candidates from all windows; dedup >50% time-overlap keeping
                    higher score; if candidates > count: one final LLM call ranking the
                    top ~10 by their text (relative ranking is far more reliable at 8B
                    size than absolute scores); take top-count, re-sorted by start time.

OUTPUT: [(start, end, topic_ar, score, reason)] — up to count items
```

### 6.3 Why not embeddings/TextTiling instead of an LLM?

ClipsAI-style topic segmentation finds *boundaries* but can't judge *virality/hook*.
Optionally (cheap, later): bias window boundaries to TextTiling topic shifts. Not in v1.

### 6.4 What stays

`pick_segment` (single-short path), the `(start, end, topic)` contract into
`generate_shorts`, trim/reframe/caption chain, and per-segment word offsetting — all
unchanged. The segmenter is a drop-in replacement for `llm.pick_segments` + `_fill_to_count`.

---

## 7. Thumbnail generation

### 7.1 Research summary — the comparison you asked for

Seven investigations, each adversarially fact-checked. Numbers are for **this card**
(RTX 3060 Ti, 8 GB, Ampere) as of mid-2026:

| Approach | Speed /img | VRAM | Identity of the real face | Arabic headline | Verdict |
|---|---|---|---|---|---|
| **FLUX.1-dev** GGUF Q4_K_S, 20 steps | ~90–150 s | ~6.5 GB peak + `--lowvram` | ✘ re-synthesized → drift | ✘ broken ligatures | Possible but painful; wrong tool for faces/text |
| **FLUX.1-schnell** Q4, 4 steps | ~25–45 s | ~6.5 GB | ✘ | ✘ | Double quality hit (distilled + Q4) |
| **FLUX.2 klein 4B** GGUF Q4 (Jan 2026, Apache-2.0) | ~15–30 s | ~2.6 GB weights | ✘ | ✘ (model card warns text distorts) | Best FLUX-family fit for 8 GB — background-only role |
| **SDXL** base 20 steps | ~10–35 s (reports vary) | ~7–8 GB fp16 | ✘ | ✘ | Fits alone, not alongside Ollama |
| **SDXL-Lightning finetune** (Juggernaut XL Lightning, 4–6 steps, CFG 1.5–2, DPM++ SDE) | ~8–12 s | ~5 GB w/ cpu-offload | ✘ | ✘ | **Best diffusion option** for backgrounds |
| **Z-Image Turbo** 6B FP8 (Alibaba, Apache-2.0) | ~2–3 s | fits 8 GB | ✘ | ✘ (EN/ZH text only) | Fastest credible background option |
| **InstantID / PhotoMaker / PuLID face adapters** | — | 10–22 GB class | ◐ "97% similar" ≠ the person | ✘ | **Rejected**: OOM on 8 GB (confirmed: even 12–16 GB cards OOM), insightface models are non-commercial, and a near-miss face of a real presenter reads as a deepfake |
| **Pure Python composition** (this plan's core) | ~2–6 s, CPU-only possible | 0–3.5 GB | ✔ **exact pixels** | ✔ pixel-perfect | **Winner for face + text** |
| **ComfyUI as a service** | n/a (serving layer) | — | — | — | Right serving layer *if/when* diffusion is added (verified `POST /free` VRAM handoff) |

**Three verified facts force the architecture:**

1. **No open diffusion path preserves a real person's identity exactly** — measured
   79–91 % face-similarity across InstantID/PuLID/FaceID; every comparison concludes none
   reach 100 %. Commercial thumbnail tools (Pikzels etc.) use the creator's *real photo*
   (cutout/face-swap onto real pixels) or hours-long per-person training. For a channel
   whose viewers know the presenter, "almost him" is worse than nothing.
2. **No local diffusion model renders Arabic script** — RTL + contextual letter shaping
   comes out as gibberish (confirmed for FLUX and SDXL families; even FLUX.2 klein's own
   model card warns about text distortion). The headline must be typeset, not generated.
3. **The identity-adapter stacks don't fit 8 GB anyway** (InstantID needs ~18 GB;
   ~10 GB even with sequential offload) and their insightface dependencies are
   licensed non-commercial — risky for a monetized channel.

### 7.2 Recommended architecture: **composition-first hybrid**

The viral-thumbnail formula (big expressive face + bold background + huge headline) is
achieved with the *real* face and deterministic compositing; diffusion is an optional
later upgrade for backgrounds only.

```
per short:
1. FRAME PICK      sample frames at ~2 fps from the source video within [start,end]
                   (full-res original, pre-crop — better pixels than the rendered 9:16).
                   Detect faces with adaptive_reframe's own FaceDetector (import, zero
                   engine changes). Score = face_area × sharpness(Laplacian var) ×
                   eyes-open × mouth-expressiveness (MediaPipe Face Landmarker's 52
                   blendshapes, CPU) × brightness. Keep top 5 candidates.
2. CUTOUT          rembg with birefnet-portrait (MIT; hair-strand quality winner).
                   CPU ~10-30 s or GPU <1 s — configurable; CPU default (zero VRAM).
                   NOT bria-rmbg (CC BY-NC — monetized channel risk).
3. FACE POLISH     2× upscale of the subject if small (Real-ESRGAN x2, CPU ok);
                   saturation/contrast pop (~1.2×). No GFPGAN-style restoration
                   (identity drift).
4. BACKGROUND      v1 templates (zero VRAM, deterministic):
                   a) blurred + darkened + zoomed frame from the same short
                   b) radial burst / split-gradient in palette colors
                   c) brand-color flat + vignette
                   Phase 7 upgrade: SDXL-Lightning (Juggernaut) or FLUX.2 klein via
                   ComfyUI headless — identity-free prompt from the short's topic.
5. HEADLINE (AR)   3–6 word headline from the existing Ollama model (§8).
                   Rendered with Pillow via arabic-reshaper + python-bidi
                   (VERIFIED on this machine: Pillow's native direction="rtl" hard-fails
                   on Windows without a manually installed fribidi.dll — the reshaper
                   path is the reliable default; auto-upgrade to raqm when available).
                   Bundled OFL fonts: Cairo Black / Changa ExtraBold / Lalezar.
                   Thick multi-layer stroke (dilate alpha) + drop shadow, yellow-or-
                   white on dark, 2–4° rotation option.
6. COMPOSE         sticker outline around the cutout (dilated alpha fill), drop shadow,
                   subject ~55–70% of canvas height, headline in the clear zone.
                   Design at 2160×3840, LANCZOS downscale → 1080×1920 JPEG ≤ 2 MB
                   (+ 1280×720 16:9 variant saved alongside).
7. REVIEW          UI shows the thumbnail; user can regenerate (new headline / next
                   frame candidate) or tap one of the 5 candidate frames.
```

Total cost per thumbnail in v1: **~5–30 s, CPU-only, zero VRAM contention.**

*(Note: the reference image mentioned in the brief did not come through with the message —
the style above follows the standard viral-Shorts formula. Share the image before Phase 4
and the default template + palette will be tuned to match it.)*

### 7.3 Where the thumbnail actually appears (verified — set expectations)

YouTube **Shorts feed never shows thumbnails** (full-screen autoplay). The thumbnail
matters in: search results, channel grid, hashtag/audio pages, and home/suggested cards.
Official policy still says Shorts get frame-selection only; a desktop-Studio
"Upload thumbnail" button rolled out gradually through 2025–26 (account-dependent), and
`thumbnails.set` behavior on Shorts is account-dependent. Strategy (§9): first-frame
embed (option, default on) + best-effort `thumbnails.set`.

---

## 8. Title / description / headline generation

One structured LLM call per short (not three) against the existing Ollama client —
new prompt + method on `_BaseLLM`, so the cloud providers inherit it too:

```json
{
  "title":       "≤80 chars, Arabic, curiosity+result formula",
  "description": "2–3 short Arabic lines: hook line, value line, CTA line; then 3–5 topic hashtags",
  "headline":    "3–6 word Arabic thumbnail headline (the emotional core, NOT the title repeated)",
  "hook_score":  0-100
}
```

**Title prompt (curiosity + result), enforced by instruction + few-shot examples:**

> الصيغة: [شيء غريب أو معلومة] + هل تعلم / لن تصدق + النتيجة
> أمثلة النمط: «لن تصدق ماذا حدث بعد…» · «هل تعلم السر الحقيقي وراء…» · «أغرب حقيقة ستغير نظرتك…»
> ممنوع: كذب العناوين، الحشو، تكرار نفس الافتتاحية في كل العناوين

Rules encoded in the prompt: title must be answerable *by the clip itself* (no bait
without payoff — the segment's transcript is the only source); vary openings across the
batch (the batch's already-generated titles are passed in as "avoid these patterns");
`#Shorts` still auto-appended at upload (harmless legacy signal; classification is
duration+aspect now — verified). Validation: length caps, dedup vs. batch, one retry.

`VideoMeta` gains `description` and `thumbnail_headline` fields (caption field retired;
platform overrides and per-platform caps deleted). Everything editable in the review UI
before upload, exactly as now.

---

## 9. YouTube upload & platform facts

All verified against official docs July 2026:

- **Quota is no longer the constraint it was.** Dec 2025: `videos.insert` cut from ~1,600
  to ~100 units; June 2026: uploads moved to a **dedicated bucket of 100 uploads/day**,
  separate from the 10,000-unit pool. (The old "~6/day" in STUDIO_README is obsolete.)
- **`thumbnails.set` costs 50 units** from the shared pool, 2 MB max, **requires the
  broader `youtube` or `youtube.force-ssl` OAuth scope** — the current token is
  `youtube.upload` only, so adding it means a one-time re-consent
  (`python -m studio.login_setup youtube`). Requires a phone-verified channel.
- **Private-lock caveat:** videos from unverified API projects (created after July 2020)
  are restricted to private until the project passes YouTube's compliance audit. Your
  uploads evidently publish fine today, so your project/token is grandfathered or
  audited — **we keep the existing OAuth client untouched** and treat this as a
  do-not-break invariant (verify once during Phase 6 testing).
- **Shorts classification 2026:** ≤ 3 minutes + square/vertical aspect = Short,
  automatically. Nothing else needed. (Max-duration slider capped at 180 s; keep the
  current 150–172 s sweet spot as default.)

**Upload flow per short:**

1. (option, default **on**) embed the thumbnail as the first ~0.1 s frame — matched-params
   encode + concat (stream-copy attempt, re-encode fallback). Works on 100 % of accounts,
   fully automatable; the only universally reliable Shorts-thumbnail mechanism today.
   Toggleable per batch in the UI for those who dislike the 3-frame flash.
2. `videos.insert` — resumable upload (existing code), per-short title/description,
   per-short privacy (new: public/unlisted/private dropdown, default from config).
3. `thumbnails.set` best-effort — try once, record outcome on the job
   (`thumb_api: ok|unsupported|unverified`), never fail the upload over it.

---

## 10. Backend architecture & API design

FastAPI stays; polling stays (SSE adds complexity for zero benefit at 1 user; the 2 s
cadence through the tunnel is proven). Thread pools reduce to `worker` (1, GPU-serial) and
`net` (2). The `publisher` pool dies with the browsers — YouTube API uploads run on `net`.

### Final API surface

| Method | Path | Change | Purpose |
|---|---|---|---|
| POST | `/api/login`, `/api/logout` | keep | password gate |
| GET | `/api/status` | slim | auth, ollama, defaults (drop `platforms`) |
| GET | `/api/library` | keep | downloaded sources |
| POST | `/api/download` · GET `/api/download/{id}` | +numeric `percent` | yt-dlp |
| POST | `/api/generate` | keep | count, min/max seconds, niche, source |
| GET | `/api/batch/{id}` | +per-stage progress struct | poll batch |
| GET | `/api/shorts` · `/api/job/{id}` | keep | library / job detail |
| POST | `/api/job/{id}/meta` | fields: title, description, headline | save edits |
| POST | `/api/job/{id}/generate` | new prompts | regenerate copy |
| GET | `/api/preview/{id}` | keep | stream mp4 |
| GET | `/api/job/{id}/thumbnail` | ✚ | current thumbnail JPEG |
| GET | `/api/job/{id}/frames` | ✚ | 5 candidate frames (JPEG strip) |
| POST | `/api/job/{id}/thumbnail` | ✚ | regenerate `{frame_t?, headline?, template?}` |
| POST | `/api/job/{id}/upload` | replaces `/publish` | `{privacy?, embed_thumb?}` → YouTube |
| GET | `/api/health` | slim | server + YouTube token + ollama |
| — | `/api/models*` | keep | LLM picker + vault keys |
| ✘ | `/pub/{token}`, `/api/job/{id}/rehearse`, `/api/rehearsal/*`, `/api/connections/{platform}/*` (credentials/strategy/login-now/code) | **deleted** | |

---

## 11. Database considerations

**SQLite stays.** Single user, single writer thread, ~tens of rows/day — Postgres or an
ORM would be pure ceremony. The existing `ALTER TABLE`-migration pattern handles the delta:

- `jobs` + columns: `thumb_path TEXT`, `thumb_api TEXT`, `score REAL`, `reason TEXT`,
  `youtube_id TEXT` (promoted out of results JSON), `privacy TEXT`.
- `meta_json` schema becomes `{title, description, headline, source}`
  (`VideoMeta.from_dict` keeps reading old rows; legacy `caption` mapped → `description`).
- `results_json` simplifies to a single upload result.
- New table `batches(id, source, created_at, params_json, stage, error)` — replaces the
  in-memory `batches` dict so batch progress survives a server restart (an actual current
  bug: restarting mid-generation orphans the progress view).

---

## 12. Frontend architecture & UI/UX redesign

**Rewrite; stay vanilla** (ES modules, no framework, no build step). Carry over the proven
patterns: the `api()` fetch wrapper, the card id→DOM incremental-update cache (flicker-free
polling), lazy `<video preload="none" playsinline>` previews, safe-area/bottom-tab shell.

### New identity

- **Name:** YouTube Shorts Studio. Wordmark: ▶ **Shorts Studio** with the play-glyph in accent red.
- **Palette (dark, mobile-first):** ink `#0B0D10` bg · surface `#151920` · elevated `#1D232C` ·
  text `#F2F4F8` / muted `#98A2B3` · **accent red `#FF3B4E`** (subtle red→orange gradient
  `#FF3B4E → #FF7A1A` for primary actions only) · success `#2BD576` · warn `#FFB020`.
  No purple/pink anywhere — a deliberate break from the old branding.
- **Type:** system stack for UI; **Cairo** (bundled, subsetted) for Arabic content text.
  All content inputs `dir="auto"`; layout LTR (matches current usage), Arabic-safe.
- Inline SVG icons (no emoji icons).

### Screens (4 tabs → effectively a 3-step wizard + settings)

1. **Create** — one hero input: paste URL (or pick from library / upload). Below it three
   controls: shorts count (1–10 stepper), duration range (dual slider 15–180 s), optional
   niche. One primary button: **Generate**. Recent batches listed underneath.
2. **Progress** — per-batch stage checklist (download % → transcribe → analyze →
   short i/N rendering) with real numeric progress; short cards stream in as each renders.
3. **Review** *(the heart of the app)* — vertical feed of short cards, one per screen-width:
   9:16 video preview side-by-side with the thumbnail; title + description editable inline
   (16 px inputs, no iOS zoom); thumbnail row: [↻ regenerate] [🖼 pick frame → 5-candidate
   strip] [✎ headline]; per-card **Upload** with privacy dropdown; sticky **Upload all**
   bar with per-short checkmarks and the daily-upload counter. Segment topic + hook-score
   badge shown ("لماذا هذا المقطع؟" reveals the LLM's reason — honest AI).
4. **Settings** — LLM model picker (existing), YouTube connection status (read-only +
   "re-auth needed" hint), caption style defaults, thumbnail template default,
   first-frame-embed toggle, app password change.

Flow economy: **paste → generate → review → upload = 4 taps** plus edits. That is the
whole product.

---

## 13. Model selection & GPU orchestration

- **LLM:** keep the user's existing Arabic Ollama model as default — the segmenter reads
  Arabic and writes numbers + short Arabic strings, well within a 7–9B model with
  structured outputs. Change `auto` selection to prefer the largest model that **fits in
  VRAM** (≤ ~5 GB weights) instead of the largest installed; log a warning when a spill
  is likely. (If segment quality disappoints, Arabic-tuned 9Bs — SILMA/Hala class — are the
  upgrade path; also the CloudLLM escape hatch already exists.)
- **Whisper:** unchanged (`large-v3` per current studio.yaml). Loaded per call, released after.
- **Structured outputs:** upgrade `OllamaClient._generate` to accept a JSON schema for
  `format` (Ollama ≥ 0.5 grammar-constrained decoding; verified). Keep schemas flat;
  Pydantic validate + one retry (schema-valid ≠ semantically-valid).
- **GPU discipline:** everything already runs on one worker thread = natural serialization.
  Phase 7 (diffusion) adds: evict Ollama (`keep_alive: 0` — verified; `OLLAMA_MAX_VRAM` is
  dead/removed, do not design around it) → ComfyUI `POST /prompt` → `POST /free`
  (async-flagged; poll `nvidia-smi`/retry before assuming free) → Ollama auto-reloads
  (~3–10 s). ComfyUI runs headless as a separate NSSM-supervised service —
  **never embed diffusers in the FastAPI process on Windows** (documented allocator
  fragmentation leak whose only known fix is glibc-specific).

---

## 14. Performance budget

For a 60-min source video → 5 shorts on this machine (rough, honest):

| Stage | Time | Resource |
|---|---|---|
| Download (yt-dlp+aria2c) | 1–5 min | net |
| faster-whisper large-v3 | ~8–15 min | GPU 1–2 GB |
| SponsorBlock + sentence table | seconds | CPU |
| Segmenter (~15 windows + rank) | 3–8 min | GPU (Ollama) |
| Per short: trim+reframe+captions | ~3–6 min each | CPU |
| Per short: copy generation | ~10–30 s each | GPU (Ollama) |
| Per short: thumbnail v1 | ~5–30 s each | CPU |
| **Total (5 shorts)** | **~45–75 min** | |

Optimizations if needed later (not v1): NVENC for trim/burn encodes, parallel CPU renders
while GPU transcribes the *next* batch, whisper `medium` fallback toggle.

## 15. Scalability

Deliberately none beyond what exists: one user, one GPU, one worker. The architecture
scales *down* (CPU-only mode still works: whisper int8 CPU, composition thumbnails,
templates only). If it ever needs multi-user: the job store and polling API are already
stateless-server-friendly; that day is not designed for now.

## 16. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| 8B model produces weak segment picks in Arabic | Medium | relative-ranking reduce pass; hook_quote validation; honest fewer-than-N; CloudLLM fallback already wired; prompt iteration on real videos in Phase 2 |
| `thumbnails.set` rejected for Shorts on this account | Medium | first-frame embed is the primary mechanism; API attempt is best-effort telemetry |
| OAuth scope change invalidates token mid-migration | Certain (one-time) | Phase 6 includes guided re-auth; upload scope kept until then |
| SponsorBlock has no data for Arabic channels | High | it's a *bonus* filter; LLM first/last-90s classification is the primary junk detector; toggleable (also CC BY-NC-SA — acceptable for personal use, revisit if productized) |
| First-frame flash annoys viewers | Low | per-batch toggle, default on; 0.08–0.1 s is 2–3 frames |
| Windows Arabic text rendering | Closed | verified: reshaper+bidi path works without fribidi.dll; fonts bundled in repo |
| Regression to captions/reframe (the crown jewels) | Low | those modules are untouched; golden-output smoke test (render one known clip, diff frame hashes) added before refactors |
| Deleting platform code breaks hidden imports | Low | publishers analysis mapped every cross-reference (config, connections, health, login_setup, web); Phase 1 is one reviewed commit with the app booted + a short generated end-to-end |
| VRAM OOM when Phase 7 diffusion lands | Medium | strict sequential handoff (verified pattern); Phase 7 is optional and last |

## 17. Implementation phases

Each phase ends with the app running end-to-end. Estimates are working-session scale.

| Phase | Scope | Est. |
|---|---|---|
| **0. Baseline** | branch `redesign/`; golden-output smoke test for captions+reframe; rotate the plaintext `app_password` currently sitting in studio.yaml | 0.5 d |
| **1. YouTube-only cleanup + rebrand** | delete 10 files (~2,300 lines) + ~40 config fields + connections UI endpoints; slim VideoMeta; drop playwright dep; rename/rebrand docs & strings; verify: boot, generate, upload | 1 d |
| **2. Semantic segmenter** | sentence table, sponsorblock.py, segmenter.py (map→validate→reduce), structured-output support in OllamaClient, delete `_fill_to_count`, sentence-snapped bounds; CLI harness to eyeball picks on 3 real videos; unit tests on synthetic transcripts | 2–3 d |
| **3. Copy generation** | title/description/headline prompt + VideoMeta fields + DB migration + batch-dedup validation | 1 d |
| **4. Thumbnails v1** | thumbnails/ package: frame scoring (reuse FaceDetector + MediaPipe blendshapes), rembg birefnet-portrait cutout, template compositor, Arabic headline renderer, bundled fonts; API endpoints; candidate-frame picker | 2–3 d |
| **5. Frontend rewrite** | new shell, palette, 4 screens, review-card UX, thumbnail picker, numeric progress; port api()/card-cache patterns | 2–3 d |
| **6. Upload upgrades** | first-frame embed (ffmpeg concat w/ fallback), scope upgrade + guided re-auth, thumbnails.set best-effort, per-short privacy, remove one_per_day (obsolete at 100/day quota — keep as optional guard) | 1 d |
| **7. (Optional) Diffusion backgrounds** | ComfyUI headless service (NSSM), Juggernaut-XL-Lightning workflow, Ollama evict/handoff, background.py client, template "AI background" option in review UI | 2 d when wanted |

**Total core (0–6): ~8–12 working days.** Phase 7 is deferred until v1 thumbnails prove
insufficient — composition may well be enough, and it keeps the GPU free.

---

*Prepared by Claude (Fable 5) — sources for every load-bearing claim are cited in the
research reports; the fact-check verdicts (confirmed/refuted/uncertain) were applied
above wherever they changed a recommendation.*
