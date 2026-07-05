"""The subscribe-reminder overlay burned into every short.

The classic "green screen subscribe notification" effect, but generated
programmatically with Pillow — a white pill slides in, a hand cursor clicks
the red اشترك button, it flips to تم الاشتراك while the bell swings, then the
pill fades out — so there is no stock footage, no chroma key, and the label is
real shaped Arabic. Frames are rendered once into ``workspace/assets/`` and
reused; a soft bell "ding" is synthesized with the stdlib ``wave`` module.

The overlay is applied in one ffmpeg pass (PNG-sequence input + ``overlay``
with an ``enable=between(t,..)`` window). Any failure returns False and the
caller keeps the un-overlaid short.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

_VERSION = 1
_W, _H = 640, 240
_SECONDS = 2.6


# ---------------------------------------------------------------------------
# asset generation (Pillow)
# ---------------------------------------------------------------------------
def ensure_assets(assets_dir: Path, *, text: str = "اشترك",
                  subscribed_text: str = "تم الاشتراك", fps: int = 30,
                  with_sound: bool = True) -> dict | None:
    """Render (or reuse) the animation frames + bell sound. Returns the
    manifest dict with absolute paths, or None if Pillow is unavailable."""
    out_dir = Path(assets_dir) / "subscribe"
    manifest_path = out_dir / "manifest.json"
    params = {"version": _VERSION, "text": text,
              "subscribed_text": subscribed_text, "fps": fps}
    try:
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        if all(m.get(k) == v for k, v in params.items()) and \
                (out_dir / (m["pattern"] % 0)).exists():
            return _absolutize(m, out_dir)
    except Exception:
        pass

    try:
        from PIL import Image
    except Exception:
        logger.warning("Pillow missing — subscribe overlay disabled")
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    n_frames = int(_SECONDS * fps)
    for i in range(n_frames):
        frame = _draw_frame(i / fps, text, subscribed_text)
        frame.save(out_dir / f"sub_{i:03d}.png")

    sound = None
    if with_sound:
        try:
            _write_bell(out_dir / "bell.wav")
            sound = "bell.wav"
        except Exception:  # pragma: no cover
            logger.warning("bell synthesis failed", exc_info=True)

    manifest = {**params, "n_frames": n_frames, "pattern": "sub_%03d.png",
                "width": _W, "height": _H, "sound": sound}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    logger.info("subscribe overlay assets rendered (%d frames)", n_frames)
    return _absolutize(manifest, out_dir)


def _absolutize(manifest: dict, out_dir: Path) -> dict:
    m = dict(manifest)
    m["frames_dir"] = str(out_dir.resolve())
    m["sound_path"] = (str((out_dir / m["sound"]).resolve())
                       if m.get("sound") else None)
    return m


def _smooth(t: float) -> float:
    """Smoothstep easing, clamped to [0, 1]."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def _shape_arabic(text: str) -> str:
    """Reshaper+bidi path (Pillow's direction='rtl' needs fribidi.dll on
    Windows and hard-fails without it — never use it). Isolated forms are
    left unshaped: modern Arabic fonts cover base codepoints but often lack
    the isolated presentation-form block."""
    try:
        import arabic_reshaper  # type: ignore
        from bidi.algorithm import get_display  # type: ignore
        reshaper = arabic_reshaper.ArabicReshaper(
            configuration={"use_unshaped_instead_of_isolated": True})
        return get_display(reshaper.reshape(text))
    except Exception:
        return text


def _font(size: int):
    from PIL import ImageFont
    fonts_dir = Path(__file__).parent / "assets" / "fonts"
    names: list[Path] = []
    if fonts_dir.is_dir():
        ttfs = sorted(fonts_dir.glob("*.ttf"))
        for key in ("lalezar", "cairo", "changa", "tajawal"):
            names += [p for p in ttfs if key in p.name.lower()]
        names += ttfs
    names += [Path(r"C:/Windows/Fonts/arialbd.ttf"),
              Path(r"C:/Windows/Fonts/tahomabd.ttf"),
              Path(r"C:/Windows/Fonts/seguisb.ttf"),
              Path(r"C:/Windows/Fonts/arial.ttf")]
    for p in names:
        try:
            if p.exists():
                return ImageFont.truetype(str(p), size)
        except Exception:
            continue
    return ImageFont.load_default()


def _draw_frame(t: float, text: str, subscribed_text: str):
    """One RGBA frame of the animation at time ``t`` seconds."""
    from PIL import Image, ImageDraw, ImageFilter

    img = Image.new("RGBA", (_W, _H), (0, 0, 0, 0))

    # --- timeline ------------------------------------------------------------
    appear = _smooth(t / 0.4)                    # slide-up + fade-in
    fade = 1.0 - _smooth((t - 2.2) / 0.4)        # fade-out at the end
    alpha = appear * fade
    if alpha <= 0.01:
        return img
    clicked = t >= 1.1
    press = 1.0 - 0.08 * math.sin(_smooth((t - 1.05) / 0.25) * math.pi) \
        if 1.05 <= t <= 1.30 else 1.0            # button squash on click
    swing = (math.sin((t - 1.1) * 18) * 18 * math.exp(-(t - 1.1) * 3.5)
             if clicked else 0.0)                # bell swing after click

    dy = int((1.0 - appear) * 40)                # slide offset

    layer = Image.new("RGBA", (_W, _H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # --- pill with drop shadow -------------------------------------------------
    pill = (24, 40 + dy, _W - 24, _H - 28 + dy)
    shadow = Image.new("RGBA", (_W, _H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (pill[0], pill[1] + 10, pill[2], pill[3] + 10), radius=28,
        fill=(0, 0, 0, 120))
    layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10)))
    d.rounded_rectangle(pill, radius=28, fill=(255, 255, 255, 255),
                        outline=(225, 228, 232, 255), width=2)

    # --- bell (left side) ------------------------------------------------------
    bell_cx, bell_cy = pill[0] + 78, (pill[1] + pill[3]) // 2
    bell = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bell)
    color = (90, 96, 106, 255)
    bd.pieslice((28, 22, 92, 86), 180, 360, fill=color)          # dome
    bd.polygon([(28, 54), (92, 54), (98, 78), (22, 78)], fill=color)
    bd.rounded_rectangle((16, 78, 104, 88), radius=5, fill=color)
    bd.ellipse((52, 88, 68, 102), fill=color)                    # clapper
    bd.rounded_rectangle((55, 12, 65, 26), radius=4, fill=color)  # handle
    if clicked and abs(swing) > 1:                               # ring arcs
        bd.arc((0, 20, 26, 90), 240, 300, fill=color, width=4)
        bd.arc((94, 20, 120, 90), 60, 120, fill=color, width=4)
    bell = bell.rotate(swing, center=(60, 55), resample=Image.BICUBIC)
    layer.alpha_composite(bell, (bell_cx - 60, bell_cy - 58))

    # --- button ---------------------------------------------------------------
    label = subscribed_text if clicked else text
    fill = (42, 42, 46, 255) if clicked else (255, 0, 0, 255)
    bw, bh = int(330 * press), int(96 * press)
    bcx = pill[0] + 150 + (pill[2] - pill[0] - 150) // 2
    bcy = (pill[1] + pill[3]) // 2
    box = (bcx - bw // 2, bcy - bh // 2, bcx + bw // 2, bcy + bh // 2)
    d.rounded_rectangle(box, radius=int(18 * press), fill=fill)
    font = _font(int(52 * press))
    shaped = _shape_arabic(label)
    tb = d.textbbox((0, 0), shaped, font=font)
    d.text((bcx - (tb[2] - tb[0]) / 2 - tb[0],
            bcy - (tb[3] - tb[1]) / 2 - tb[1]),
           shaped, font=font, fill=(255, 255, 255, 255))

    # --- hand cursor gliding in, gone shortly after the click -------------------
    if 0.4 <= t <= 1.35:
        prog = _smooth((t - 0.4) / 0.7)
        hx = int(_W - 60 - (_W - 60 - bcx) * prog)
        hy = int(_H - 10 - (_H - 10 - (bcy + 26)) * prog)
        hd = ImageDraw.Draw(layer)
        hd.polygon([(hx, hy), (hx + 26, hy + 34), (hx + 12, hy + 34),
                    (hx + 18, hy + 52), (hx + 10, hy + 55), (hx + 4, hy + 36),
                    (hx - 6, hy + 44)],
                   fill=(255, 255, 255, 255), outline=(40, 40, 40, 255))

    if alpha < 1.0:
        a = layer.getchannel("A").point(lambda v: int(v * alpha))
        layer.putalpha(a)
    img.alpha_composite(layer)
    return img


def _write_bell(path: Path, rate: int = 44100) -> None:
    """A soft pop + two-partial ding, stdlib only."""
    samples: list[int] = []
    total = int(rate * 0.55)
    for i in range(total):
        t = i / rate
        v = 0.0
        if t < 0.05:                                     # pop
            v += 0.45 * math.sin(2 * math.pi * 220 * t) * (1 - t / 0.05)
        if t >= 0.08:                                    # ding
            td = t - 0.08
            env = math.exp(-td * 9)
            v += env * (0.38 * math.sin(2 * math.pi * 880 * td)
                        + 0.18 * math.sin(2 * math.pi * 1760 * td))
        samples.append(max(-32767, min(32767, int(v * 32767))))
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(b"".join(s.to_bytes(2, "little", signed=True)
                               for s in samples))


# ---------------------------------------------------------------------------
# ffmpeg overlay pass
# ---------------------------------------------------------------------------
def _probe(path: str) -> tuple[float, int, int, bool]:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format",
         "-show_streams", path],
        capture_output=True, text=True, check=True, timeout=60)
    data = json.loads(out.stdout)
    duration = float(data.get("format", {}).get("duration", 0.0))
    width = height = 0
    has_audio = False
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not width:
            width, height = int(s.get("width", 0)), int(s.get("height", 0))
        elif s.get("codec_type") == "audio":
            has_audio = True
    return duration, width, height, has_audio


def apply_overlay(cfg, video_in: str, video_out: str) -> bool:
    """Burn the subscribe animation onto ``video_in``. False = keep original."""
    if not getattr(cfg, "subscribe_overlay_enabled", False):
        return False
    try:
        duration, width, height, has_audio = _probe(video_in)
    except Exception:
        logger.warning("subscribe overlay: probe failed", exc_info=True)
        return False
    if duration < 8.0 or not width:
        return False

    assets = ensure_assets(cfg.assets_dir, text=cfg.subscribe_text,
                           subscribed_text=cfg.subscribed_text,
                           with_sound=cfg.subscribe_sound)
    if not assets:
        return False

    show = float(cfg.subscribe_duration)
    t0 = max(1.0, min(duration * float(cfg.subscribe_at_frac),
                      duration - show - 0.5))
    t1 = t0 + show
    ow = int(width * 0.55) // 2 * 2
    oy = int(height * 0.42)
    pattern = str(Path(assets["frames_dir"]) / assets["pattern"])
    use_sound = bool(cfg.subscribe_sound and assets.get("sound_path")
                     and has_audio)

    fc = (f"[1:v]format=rgba,scale={ow}:-1,setpts=PTS+{t0:.3f}/TB[ov];"
          f"[0:v][ov]overlay=(W-w)/2:{oy}"
          f":enable='between(t,{t0:.3f},{t1:.3f})':eof_action=pass[v]")
    cmd = ["ffmpeg", "-y", "-i", video_in,
           "-framerate", str(assets["fps"]), "-start_number", "0",
           "-i", pattern]
    if use_sound:
        ms = int(t0 * 1000)
        cmd += ["-i", assets["sound_path"]]
        fc += (f";[2:a]volume=0.45,adelay={ms}|{ms}[ding];"
               f"[0:a][ding]amix=inputs=2:duration=first:normalize=0[a]")
    cmd += ["-filter_complex", fc, "-map", "[v]"]
    if use_sound:
        cmd += ["-map", "[a]", "-c:a", "aac"]
    elif has_audio:
        cmd += ["-map", "0:a", "-c:a", "copy"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-movflags", "+faststart", video_out]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL,
                       timeout=max(300.0, duration * 8))
        return True
    except Exception:
        logger.warning("subscribe overlay: ffmpeg pass failed", exc_info=True)
        return False
