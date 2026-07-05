"""Arabic headline rendering for thumbnails.

Text is shaped with ``arabic_reshaper`` + ``python-bidi`` and drawn as a plain
string — never Pillow's ``direction="rtl"``, which requires a manually
installed fribidi.dll on Windows and hard-fails without it (verified on this
machine). Fonts come from the bundled OFL set in ``studio/assets/fonts`` with
Windows system-font fallbacks.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_FONT_PREFERENCE = ("lalezar", "cairo", "changa", "tajawal")
_WINDOWS_FALLBACKS = (r"C:/Windows/Fonts/arialbd.ttf",
                      r"C:/Windows/Fonts/tahomabd.ttf",
                      r"C:/Windows/Fonts/seguisb.ttf",
                      r"C:/Windows/Fonts/arial.ttf")


def resolve_font() -> str | None:
    """Best available Arabic display font path."""
    fonts_dir = Path(__file__).parent.parent / "assets" / "fonts"
    if fonts_dir.is_dir():
        ttfs = sorted(fonts_dir.glob("*.ttf"))
        for key in _FONT_PREFERENCE:
            for p in ttfs:
                if key in p.name.lower():
                    return str(p)
        if ttfs:
            return str(ttfs[0])
    for p in _WINDOWS_FALLBACKS:
        if Path(p).exists():
            return p
    return None


def _load_font(path: str | None, size: int):
    from PIL import ImageFont
    if not path:
        return ImageFont.load_default()
    try:
        font = ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()
    try:  # variable fonts (Cairo): pick the heaviest weight
        names = [n.decode() if isinstance(n, bytes) else str(n)
                 for n in font.get_variation_names()]
        for want in ("Black", "ExtraBold", "Bold"):
            if any(want.lower() in n.lower() for n in names):
                font.set_variation_by_name(
                    next(n for n in names if want.lower() in n.lower()))
                break
    except Exception:
        pass
    return font


def shape(text: str) -> str:
    """Reshape + reorder Arabic for plain-string drawing.

    ``use_unshaped_instead_of_isolated``: modern Arabic fonts (Cairo, Tajawal,
    Lalezar) ship initial/medial/final presentation forms but not the isolated
    ones — the base codepoint renders correctly there instead of tofu.
    """
    try:
        import arabic_reshaper  # type: ignore
        from bidi.algorithm import get_display  # type: ignore
        reshaper = arabic_reshaper.ArabicReshaper(
            configuration={"use_unshaped_instead_of_isolated": True})
        return get_display(reshaper.reshape(text))
    except Exception:
        logger.warning("arabic-reshaper/python-bidi missing — drawing raw text")
        return text


def _wrap_two_lines(words: list[str]) -> list[str]:
    """Balance the headline onto at most 2 lines by word count/length."""
    if len(words) <= 3:
        return [" ".join(words)]
    best, best_diff = None, 1e9
    for cut in range(2, len(words) - 1):
        a, b = " ".join(words[:cut]), " ".join(words[cut:])
        diff = abs(len(a) - len(b))
        if diff < best_diff:
            best, best_diff = [a, b], diff
    return best or [" ".join(words)]


def render_headline(text: str, max_width: int, *, base_size: int = 170,
                    fill=(255, 221, 0), stroke=(12, 12, 12)):
    """Render the headline as a tight-cropped RGBA image, or None."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except Exception:
        return None

    font_path = resolve_font()
    lines = [shape(ln) for ln in _wrap_two_lines(text.split())]

    size = base_size
    while size > 40:
        font = _load_font(font_path, size)
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        stroke_w = max(6, size // 9)
        widths = [probe.textbbox((0, 0), ln, font=font,
                                 stroke_width=stroke_w)[2] for ln in lines]
        if max(widths) <= max_width:
            break
        size = int(size * 0.92)
    font = _load_font(font_path, size)
    stroke_w = max(6, size // 9)

    # generous canvas; tight-crop at the end
    line_h = int(size * 1.28)
    W = max_width + stroke_w * 4
    H = line_h * len(lines) + stroke_w * 4 + size // 3
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # soft shadow pass
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    for i, ln in enumerate(lines):
        bbox = sd.textbbox((0, 0), ln, font=font, stroke_width=stroke_w)
        x = (W - (bbox[2] - bbox[0])) // 2 - bbox[0]
        y = i * line_h + size // 14
        sd.text((x, y), ln, font=font, fill=(0, 0, 0, 140),
                stroke_width=stroke_w, stroke_fill=(0, 0, 0, 140))
    img.alpha_composite(shadow.filter(
        ImageFilter.GaussianBlur(max(2, size // 12))))

    # main pass: fat dark stroke + bright fill
    for i, ln in enumerate(lines):
        bbox = d.textbbox((0, 0), ln, font=font, stroke_width=stroke_w)
        x = (W - (bbox[2] - bbox[0])) // 2 - bbox[0]
        y = i * line_h
        d.text((x, y), ln, font=font, fill=(*fill, 255),
               stroke_width=stroke_w, stroke_fill=(*stroke, 255))

    box = img.getbbox()
    return img.crop(box) if box else None
