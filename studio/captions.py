"""TikTok-style burned-in captions.

Turns Whisper word timings into an animated **ASS** subtitle - a few big, bold,
outlined words at a time with the currently-spoken word highlighted and enlarged
- then burns it onto the vertical video with ffmpeg's ``ass`` filter.

Why ASS + ffmpeg (not per-frame drawing): libass renders crisp outlines, handles
bidirectional/Arabic shaping, and the burn is a single fast ffmpeg pass.

Public entry point: :func:`burn_captions`.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .transcribe import Word

logger = logging.getLogger(__name__)


@dataclass
class CaptionStyle:
    font: str = "Arial"
    fontsize: int = 96
    base_color: str = "#FFFFFF"     # normal words
    highlight: str = "#FFE000"      # the word being spoken (bright yellow)
    outline: int = 6                # black border thickness
    shadow: int = 2
    position: str = "lower"         # "lower" | "center" | "bottom"
    # Numeric override for the vertical position: percent UP FROM THE BOTTOM
    # of the frame where the text's bottom edge sits — bigger = higher, like
    # CSS `bottom` (16 ≈ "lower", 48 ≈ center, 6 ≈ "bottom"). None keeps the
    # preset behaviour above, byte-for-byte.
    pos_pct: float | None = None
    max_words: int = 4              # words shown on screen at once
    max_line_seconds: float = 2.5   # force a new line after this long
    highlight_scale: float = 1.18   # how much the active word grows
    crf: int = 18
    preset: str = "medium"


# ---------------------------------------------------------------------------
def burn_captions(video_in: str, words: list[Word], out_path: str,
                  style: CaptionStyle | None = None,
                  play_w: int = 1080, play_h: int = 1920) -> bool:
    """Burn karaoke captions onto ``video_in`` -> ``out_path``.

    Returns True on success. Returns False (and writes nothing) if there are no
    words or ffmpeg fails, so the caller can fall back to the un-captioned file.
    """
    style = style or CaptionStyle()
    words = [w for w in words if w.text.strip() and w.end > w.start]
    if not words:
        return False

    out = Path(out_path)
    ass_path = out.with_suffix(".ass")
    ass_path.write_text(build_ass(words, style, play_w, play_h), encoding="utf-8")

    # Run with cwd = output folder and reference the .ass by bare name: this
    # sidesteps the notorious Windows drive-colon/backslash escaping in the
    # ffmpeg filter argument.
    # setsar=1 declares a square-pixel aspect ratio and +faststart puts the moov
    # atom at the front — both are Meta API upload requirements for the FINAL
    # file (undeclared SAR / trailing moov made IG reel containers ERROR).
    cmd = [
        "ffmpeg", "-y", "-i", str(Path(video_in).resolve()),
        "-vf", f"ass={ass_path.name},setsar=1",  # bare name + cwd: Windows escaping
        "-c:v", "libx264", "-crf", str(style.crf), "-preset", style.preset,
        "-pix_fmt", "yuv420p", "-c:a", "copy",
        "-movflags", "+faststart", str(out.resolve()),
    ]
    try:
        subprocess.run(cmd, check=True, cwd=str(ass_path.parent),
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       timeout=3600)
        return True
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b"").decode("utf-8", "replace")[-400:]
        logger.warning("caption burn failed: %s", err)
        return False
    finally:
        try:
            ass_path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
def build_ass(words: list[Word], style: CaptionStyle,
              play_w: int, play_h: int) -> str:
    """Assemble a full ASS document with one karaoke event per spoken word."""
    align, margin_v = _placement(style, play_h)
    base = _ass_color(style.base_color)
    big = int(round(style.fontsize * style.highlight_scale))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{style.font},{style.fontsize},{base},{base},&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,{style.outline},{style.shadow},{align},80,80,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    accent = _ass_color(style.highlight)
    base = _ass_color(style.base_color)
    events = []
    for chunk in _chunk_words(words, style.max_words, style.max_line_seconds):
        if _is_rtl("".join(w.text for w in chunk)):
            # RTL (Arabic/Hebrew): inline per-word colour overrides scramble the
            # word order under libass's bidi. We lay each word out at an absolute
            # \pos (positions measured with Pillow) which bypasses bidi entirely,
            # giving the SAME single-word "pop" highlight as English in the
            # correct right-to-left order. Falls back to bidi-safe karaoke if
            # Pillow / the font can't be loaded.
            events.extend(_rtl_pos_events(chunk, style, accent, big,
                                          play_w, play_h))
        else:
            # LTR (English, ...): one event per word with the active word
            # enlarged + coloured (the "pop" highlight).
            for i, w in enumerate(chunk):
                start = w.start
                end = chunk[i + 1].start if i + 1 < len(chunk) else w.end
                if end <= start:
                    end = start + 0.15
                text = _render_line(chunk, i, style, big)
                events.append(
                    f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{text}")
    return header + "\n".join(events) + "\n"


def _rtl_pos_events(chunk: list[Word], style: CaptionStyle, accent: str,
                    big: int, play_w: int, play_h: int) -> list[str]:
    """Single-word-highlight events for an RTL line, positioned absolutely.

    Word widths are measured with Pillow (Arabic reshaped so the joined width is
    right), then words are laid out centred, right-to-left. For each word we emit
    a persistent white base event plus a yellow+enlarged overlay event that shows
    only while that word is spoken.
    """
    widths = _measure_widths(chunk, style.font, style.fontsize)
    if not widths:
        return [_karaoke_event(chunk, accent, _ass_color(style.base_color))]
    space = _space_width(style.font, style.fontsize)
    total = sum(widths) + space * (len(chunk) - 1)
    right = (play_w + total) / 2.0          # right edge of the centred line
    y = _baseline_y(style, play_h)
    centers = []
    cursor = right
    for wdt in widths:                      # word 0 (spoken first) = rightmost
        centers.append(cursor - wdt / 2.0)
        cursor -= wdt + space
    cstart, cend = _ts(chunk[0].start), _ts(chunk[-1].end)
    out = []
    for i, w in enumerate(chunk):
        tok = _escape(w.text)
        pos = f"\\an2\\pos({centers[i]:.0f},{y})"
        out.append(f"Dialogue: 0,{cstart},{cend},Cap,,0,0,0,,{{{pos}}}{tok}")
        s = w.start
        e = w.end if w.end > w.start else w.start + 0.15
        out.append(f"Dialogue: 1,{_ts(s)},{_ts(e)},Cap,,0,0,0,,"
                   f"{{{pos}\\c{accent}&\\fs{big}\\b1}}{tok}")
    return out


def _measure_widths(chunk: list[Word], font_name: str, size: int):
    try:
        from PIL import ImageFont  # type: ignore
    except Exception:
        return None
    path = _resolve_font_file(font_name)
    if not path:
        return None
    try:
        font = ImageFont.truetype(path, size)
    except Exception:
        return None
    try:
        import arabic_reshaper  # type: ignore
        reshape = arabic_reshaper.reshape
    except Exception:
        def reshape(s):  # joined width unavailable -> looser spacing, still ok
            return s
    return [font.getlength(reshape(w.text)) for w in chunk]


def _space_width(font_name: str, size: int) -> float:
    try:
        from PIL import ImageFont  # type: ignore
        font = ImageFont.truetype(_resolve_font_file(font_name), size)
        return (font.getlength("a a") - font.getlength("aa")) + size * 0.08
    except Exception:
        return size * 0.3


_FONT_FILES = {
    "arial": ["arialbd.ttf", "arial.ttf"],
    "tahoma": ["tahomabd.ttf", "tahoma.ttf"],
    "segoe ui": ["segoeuib.ttf", "segoeui.ttf"],
    "calibri": ["calibrib.ttf", "calibri.ttf"],
    "times new roman": ["timesbd.ttf", "times.ttf"],
}


def _resolve_font_file(name: str) -> str | None:
    import os

    if os.path.isfile(name):
        return name
    fonts_dir = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts")
    candidates = _FONT_FILES.get(name.lower(), [name + ".ttf", name + "bd.ttf"])
    for cand in candidates + ["arialbd.ttf", "arial.ttf"]:
        p = os.path.join(fonts_dir, cand)
        if os.path.isfile(p):
            return p
    return None


def _clamped_pct(style: CaptionStyle) -> float | None:
    """The numeric position override (percent up from the bottom), kept
    safely on screen (2%..85%)."""
    if style.pos_pct is None or style.pos_pct <= 0:
        return None
    return min(85.0, max(2.0, float(style.pos_pct)))


def _baseline_y(style: CaptionStyle, play_h: int) -> int:
    pct = _clamped_pct(style)
    if pct is not None:
        # same integer lift as _placement's MarginV, so the RTL absolute
        # path and the LTR margin path land on the identical pixel row
        return play_h - int(play_h * pct / 100.0)
    if style.position == "center":
        return int(play_h * 0.52)
    if style.position == "bottom":
        return play_h - int(play_h * 0.06)
    return play_h - int(play_h * 0.16)      # "lower"


def _karaoke_event(chunk: list[Word], accent: str, base: str) -> str:
    """One Dialogue using ASS karaoke: \\2c = colour before a word is spoken
    (base), \\1c = colour once spoken (highlight). Each \\k holds for the word's
    own duration (including the gap until the next word), so the highlight tracks
    the audio."""
    start, end = chunk[0].start, chunk[-1].end
    parts = [f"{{\\1c{accent}&\\2c{base}&\\b1}}"]
    for i, w in enumerate(chunk):
        nxt = chunk[i + 1].start if i + 1 < len(chunk) else w.end
        cs = max(1, int(round((nxt - w.start) * 100)))  # centiseconds
        parts.append(f"{{\\k{cs}}}{_escape(w.text)} ")
    return f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{''.join(parts).rstrip()}"


def _render_line(chunk: list[Word], active: int, style: CaptionStyle,
                 big: int) -> str:
    accent = _ass_color(style.highlight)
    parts = []
    for i, w in enumerate(chunk):
        token = _escape(w.text)
        if i == active:
            # Inline colour overrides require a trailing '&': \c&Hbbggrr&
            parts.append(f"{{\\c{accent}&\\fs{big}\\b1}}{token}{{\\r}}")
        else:
            parts.append(token)
    return " ".join(parts)


# Unicode blocks that imply a right-to-left line (Arabic, Hebrew, etc.).
_RTL_RANGES = ((0x0590, 0x05FF), (0x0600, 0x06FF), (0x0750, 0x077F),
               (0x08A0, 0x08FF), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))


def _is_rtl(text: str) -> bool:
    return any(any(a <= ord(ch) <= b for a, b in _RTL_RANGES) for ch in text)


def _chunk_words(words: list[Word], max_words: int,
                 max_seconds: float) -> list[list[Word]]:
    """Group words into short on-screen lines, breaking on length, time, gaps
    and sentence punctuation."""
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur:
            gap = w.start - cur[-1].end
            span = w.end - cur[0].start
            if (len(cur) >= max_words or span > max_seconds or gap > 0.7
                    or cur[-1].text[-1:] in ".!?،؟…"):
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def _placement(style: CaptionStyle, play_h: int) -> tuple[int, int]:
    """Return (ASS alignment, MarginV). Alignment 2 = bottom-center; a bigger
    MarginV lifts the text further UP from the bottom edge."""
    pct = _clamped_pct(style)
    if pct is not None:
        # bottom-anchored, lifted pct% up from the bottom — mirrors
        # _baseline_y so the LTR and RTL paths land at the same height
        return 2, int(play_h * pct / 100.0)
    if style.position == "center":
        return 5, 0                      # true middle
    if style.position == "bottom":
        return 2, int(play_h * 0.06)     # ~115px: very bottom edge
    return 2, int(play_h * 0.16)         # "lower": low, but clear of the edge


def _ass_color(hex_color: str) -> str:
    """#RRGGBB -> ASS &HAABBGGRR (opaque)."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return "&H00FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def _escape(text: str) -> str:
    return (text.replace("\\", "").replace("{", "(").replace("}", ")")
            .replace("\n", " ").strip())


def _ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"
