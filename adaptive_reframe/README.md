# Adaptive Vertical Reframing

> **Looking for the daily phone-driven uploader?** This repo now also ships
> **Daily Shorts Studio** (`studio/`) — a self-hosted web app you open from your
> phone to upload one clip a day and auto-publish it to YouTube Shorts,
> Instagram, TikTok and Facebook, with local Ollama writing the captions. The
> reframing engine below is the 9:16 stage it builds on. See
> **[STUDIO_README.md](STUDIO_README.md)**.

A self-contained, importable subsystem that converts any source clip into a
1080×1920 (9:16) vertical video by **automatically classifying the clip and
choosing the most appropriate reframing strategy**. It is designed to slot into
a larger OpusClip/Vidyo.ai/Captions-style Shorts pipeline as the per-clip
reframing stage.

Its guiding rule, taken directly from the brief: **preserving the original
editing and visual information always takes priority over forcing a
face-centered crop.** The classifier is therefore *preservation-biased* — it
only commits to an aggressive crop when the clip genuinely looks like a static
talking-head shot.

## The eight modes

| Mode | Kind | Used when |
|------|------|-----------|
| `face_focus` | focus | One stable, mostly static face — crop + gentle zoom keeps it centered. |
| `active_speaker` | focus | Multiple faces with a detectable talker — follows whoever is speaking. |
| `smart_crop` | focus | A clear subject of interest but no usable face — saliency-driven crop. |
| `scene_aware` | preserve | Several calm scenes — picks a per-scene sub-strategy (meta-strategy). |
| `blur_background` | preserve | Subject present but the frame is busy — fit original over a blurred fill. |
| `mirror_background` | preserve | Like blur, but fills the margins with a mirrored extension. |
| `dynamic_canvas` | preserve | Scenic / no subject — original over a generated color-matched canvas. |
| `no_crop` | preserve | Already near-vertical, or heavy overlays/text where legibility matters — letterbox only. |

Focus modes share One-Euro-smoothed crop trajectories so the framing never
jitters, and every crop is computed at an exact 9:16 aspect so the final resize
never distorts the picture.

## How classification works

A single sampled analysis pass (`ClipAnalyzer`) produces scalar signals per
clip: face presence/area/motion, average simultaneous subjects, cut rate,
global motion, an overlay/text score, whether an active speaker was found, and
the scene list. The `ReframeClassifier` then applies, in order:

1. **Near-vertical source** (`aspect ≤ vertical_aspect_max`) → `no_crop`.
2. **Preservation pressure** — cut rate, global motion, overlay score and
   multi-subject-without-speaker each contribute. If the summed pressure
   reaches `preserve_pressure_threshold`, a preservation mode is chosen
   (`no_crop` for heavy overlays, `scene_aware` for many calm scenes, `blur` /
   `dynamic_canvas` / `mirror` otherwise).
3. **Low pressure** → a focus mode (`active_speaker`, `face_focus`, or
   `smart_crop`).

Every threshold is exposed in `config.yaml`.

## Install

```bash
pip install -e .                 # or: pip install -r requirements.txt
# ffmpeg + ffprobe must be on PATH (apt-get install ffmpeg / brew install ffmpeg)
```

Two dependency notes worth knowing:

- **Smart Crop saliency needs the contrib build.** Install
  `opencv-contrib-python`, not plain `opencv-python`; otherwise Smart Crop
  falls back to a center-weighted heuristic.
- **MediaPipe and PySceneDetect are optional.** Without them the system falls
  back to OpenCV Haar cascades for faces and a histogram-difference detector
  for scenes.

## Programmatic use

```python
from adaptive_reframe import AdaptiveReframePipeline, ReframeMode

pipe = AdaptiveReframePipeline()                 # uses built-in defaults
result = pipe.reframe("clip.mp4", "clip_vertical.mp4")
print(result.decision.explain())                 # chosen mode + confidence + why

# Force a specific mode (skips auto-classification):
pipe.reframe("clip.mp4", "out.mp4", force_mode=ReframeMode.BLUR_BACKGROUND)
```

`reframe()` returns a `ReframeResult` carrying the `ReframeDecision` (mode,
confidence, human-readable reasons) and the full `ClipAnalysis`, so the calling
pipeline can log or branch on why a mode was picked.

To run completely free of Pydantic/YAML (e.g. inside a service that already has
its own config), construct `ReframeParams` directly and pass it to the
pipeline; `config.py` is the only module that imports Pydantic and is imported
only by the CLI.

## CLI

```bash
adaptive-reframe reframe input.mp4 -o output.mp4 [--mode blur_background] [--config config.yaml]
adaptive-reframe analyze input.mp4 [--config config.yaml]   # classify only, no render
```

## Configuration

Copy `config.example.yaml` to `config.yaml` and edit. Every key is optional and
falls back to the documented default. `force_mode` overrides auto-selection for
all clips.

## Tests

Pure-logic units (geometry, signal smoothing, and every classifier branch) have
no CV dependencies:

```bash
pytest tests/            # 18 tests
```

## Architecture

```
pipeline.py     orchestration: analyze -> classify (or force) -> render
analyzer.py     single sampled pass -> ClipAnalysis (scalar signals + raw faces/scenes)
classifier.py   preservation-biased mode selection (pure, stdlib only)
strategies.py   Strategy pattern + registry; focus/preserve/scene-aware implementations
detectors.py    face detection/tracking, lip-motion speaker heuristic, motion, scenes
imaging.py      crop/resize, letterbox, blur/mirror fill, dynamic canvas (cv2 + numpy)
geometry.py     pure math: aspect crops, One-Euro filter, path smoothing (stdlib only)
renderer.py     cv2 decode -> raw frames piped to ffmpeg -> muxes ORIGINAL audio
types.py        dataclasses/enums shared across modules (stdlib only)
config.py       Pydantic config + YAML loader (only module that needs Pydantic)
cli.py          argparse entry point
```

`types.py`, `geometry.py` and `classifier.py` are stdlib-only and unit-testable
without OpenCV, NumPy or Pydantic installed.

## Honest limitations

- **Active-speaker detection is a lip-motion heuristic** (mouth-ROI activity),
  not an audio-visual model like TalkNet. It is a reasonable, dependency-light
  approximation; swap in a stronger detector behind the same `ClipAnalysis`
  interface if you need higher accuracy.
- The MediaPipe face path depends on your installed MediaPipe build; the system
  detects incompatibility at runtime and falls back to Haar cascades.
- Smart Crop saliency requires `opencv-contrib-python` as noted above.

## Integration with the larger pipeline

This subsystem owns exactly one stage — turning a chosen horizontal clip into a
retention-optimized vertical render. The upstream stages (transcription,
scoring, clip selection) call `reframe()` once per selected clip, and the
downstream caption-burning stage consumes the 1080×1920 output. Original audio
is preserved through the render so caption timing stays aligned.
