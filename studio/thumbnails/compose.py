"""Thumbnail composition: background template + subject sticker + headline.

Design decisions (from the pre-implementation research): the face is the REAL
frame cutout (identity-exact), only the background is synthetic, and the
Arabic headline is typeset — diffusion is never used for faces or text.
Everything is drawn at 2x and LANCZOS-downscaled for crisp edges; output is
1080x1920 JPEG kept under YouTube's 2 MB thumbnail limit.
"""

from __future__ import annotations

import logging
import math

from .headline import render_headline

logger = logging.getLogger(__name__)

CANVAS = (1080, 1920)
_SCALE = 2  # design at 2x

_ACCENT = (255, 59, 78)


def make_thumbnail(frame_bgr, cutout_rgba, headline_text: str, template: str,
                   out_path: str) -> str:
    """Compose and save the thumbnail; returns ``out_path``."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

    W, H = CANVAS[0] * _SCALE, CANVAS[1] * _SCALE
    template = (template or "auto").lower()
    if template == "auto":
        template = "blur" if cutout_rgba is not None else "burst"

    canvas = _background(template, frame_bgr, W, H)

    # --- subject sticker -----------------------------------------------------
    if cutout_rgba is not None:
        subj = cutout_rgba.convert("RGBA")
        target_h = int(H * 0.62)
        ratio = target_h / subj.height
        subj = subj.resize((max(1, int(subj.width * ratio)), target_h),
                           Image.LANCZOS)
        subj = ImageEnhance.Color(subj).enhance(1.18)
        subj = ImageEnhance.Contrast(subj).enhance(1.12)
        subj = ImageEnhance.Sharpness(subj).enhance(1.15)
        x = (W - subj.width) // 2
        y = H - subj.height  # anchored to the bottom edge

        alpha = subj.getchannel("A")
        # sticker outline: dilated alpha filled white behind the subject
        grow = 22
        outline_a = alpha.filter(ImageFilter.MaxFilter(9))
        for _ in range(grow // 8):
            outline_a = outline_a.filter(ImageFilter.MaxFilter(9))
        outline = Image.new("RGBA", subj.size, (255, 255, 255, 0))
        outline.putalpha(outline_a)
        white = Image.new("RGBA", subj.size, (255, 255, 255, 255))
        outline = Image.composite(white, Image.new("RGBA", subj.size,
                                                   (0, 0, 0, 0)), outline_a)
        # drop shadow
        shadow_a = outline_a.filter(ImageFilter.GaussianBlur(30))
        shadow = Image.composite(
            Image.new("RGBA", subj.size, (0, 0, 0, 115)),
            Image.new("RGBA", subj.size, (0, 0, 0, 0)), shadow_a)
        canvas.alpha_composite(shadow, (x, min(H - subj.height + 26, y + 26)))
        canvas.alpha_composite(outline, (x, y))
        canvas.alpha_composite(subj, (x, y))
    elif frame_bgr is not None and template != "blur":
        # no cutout: show the sharp frame itself as a mid layer
        mid = _cover(_to_pil(frame_bgr), W, int(H * 0.62))
        fade = Image.new("L", mid.size, 255)
        fd = ImageDraw.Draw(fade)
        for i in range(mid.height // 3):
            fd.line([(0, i), (mid.width, i)],
                    fill=int(255 * i / (mid.height / 3)))
        mid.putalpha(fade)
        canvas.alpha_composite(mid, (0, H - mid.height))

    # --- headline --------------------------------------------------------------
    # base_size 330 at 2x ≈ 15% of the canvas width per line — the huge
    # "MrBeast-scale" typography; auto-shrink still guarantees the fit.
    head = render_headline(headline_text, int(W * 0.94), base_size=330)
    if head is not None:
        # contrast band behind the text
        band_h = int(H * 0.34)
        band = Image.new("RGBA", (W, band_h), (0, 0, 0, 0))
        bd = ImageDraw.Draw(band)
        for i in range(band_h):
            bd.line([(0, i), (W, i)],
                    fill=(0, 0, 0, int(165 * (1 - i / band_h))))
        canvas.alpha_composite(band, (0, 0))

        head = head.rotate(-2, expand=True, resample=Image.BICUBIC)
        hx = (W - head.width) // 2
        hy = max(int(H * 0.02), int(H * 0.15) - head.height // 2)
        canvas.alpha_composite(head, (hx, hy))
        # accent bar under the headline block
        bar_w = int(head.width * 0.7)
        bar = Image.new("RGBA", (bar_w, 14), (*_ACCENT, 255))
        canvas.alpha_composite(bar, ((W - bar_w) // 2, hy + head.height + 18))

    _vignette(canvas)

    out = canvas.convert("RGB").resize(CANVAS, Image.LANCZOS)
    for quality in (90, 85, 78, 70):
        out.save(out_path, "JPEG", quality=quality, optimize=True)
        import os
        if os.path.getsize(out_path) < 1_900_000:
            break
    return out_path


# ---------------------------------------------------------------------------
# backgrounds
# ---------------------------------------------------------------------------
def _to_pil(frame_bgr):
    from PIL import Image
    return Image.fromarray(frame_bgr[:, :, ::-1])


def _cover(img, w: int, h: int):
    """Scale + center-crop to exactly (w, h)."""
    from PIL import Image
    ratio = max(w / img.width, h / img.height)
    img = img.resize((max(1, int(img.width * ratio)),
                      max(1, int(img.height * ratio))), Image.LANCZOS)
    x = (img.width - w) // 2
    y = (img.height - h) // 2
    return img.crop((x, y, x + w, y + h)).convert("RGBA")


def _background(template: str, frame_bgr, W: int, H: int):
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

    if template == "blur" and frame_bgr is not None:
        bg = _cover(_to_pil(frame_bgr), W, H)
        bg = bg.resize((int(W * 1.15), int(H * 1.15)), Image.LANCZOS)
        x = (bg.width - W) // 2
        bg = bg.crop((x, (bg.height - H) // 2, x + W, (bg.height - H) // 2 + H))
        bg = bg.filter(ImageFilter.GaussianBlur(16))
        bg = ImageEnhance.Brightness(bg).enhance(0.62)
        bg = ImageEnhance.Color(bg).enhance(1.25)
        return bg.convert("RGBA")

    if template == "burst":
        bg = Image.new("RGBA", (W, H), (74, 8, 16, 255))
        d = ImageDraw.Draw(bg)
        cx, cy = W // 2, int(H * 0.55)
        radius = int(math.hypot(W, H))
        wedges = 28
        for i in range(wedges):
            if i % 2 == 0:
                a0 = i * (360 / wedges)
                a1 = a0 + (360 / wedges)
                d.pieslice((cx - radius, cy - radius, cx + radius,
                            cy + radius), a0, a1, fill=(126, 12, 24, 255))
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.ellipse((cx - W // 2, cy - W // 2, cx + W // 2, cy + W // 2),
                   fill=(255, 120, 60, 90))
        bg.alpha_composite(glow.filter(ImageFilter.GaussianBlur(120)))
        return bg

    # "flat" (and any fallback): dark gradient + red glow
    bg = Image.new("RGBA", (W, H), (16, 19, 24, 255))
    d = ImageDraw.Draw(bg)
    top, bottom = (16, 19, 24), (35, 43, 54)
    for i in range(H):
        f = i / H
        d.line([(0, i), (W, i)], fill=tuple(
            int(top[c] + (bottom[c] - top[c]) * f) for c in range(3)))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((W // 6, int(H * 0.45), W - W // 6, int(H * 1.05)),
               fill=(*_ACCENT, 46))
    bg.alpha_composite(glow.filter(ImageFilter.GaussianBlur(140)))
    return bg


def _vignette(canvas) -> None:
    from PIL import Image, ImageDraw, ImageFilter
    W, H = canvas.size
    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse((-W // 3, -H // 4, W + W // 3, H + H // 4), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(160)).point(
        lambda v: 255 - v)
    dark = Image.new("RGBA", (W, H), (0, 0, 0, 90))
    dark.putalpha(mask.point(lambda v: v * 90 // 255))
    canvas.alpha_composite(dark)
