"""Silence-cut montage: remove dead air between words so shorts feel fast.

Given the clip-local Whisper words, dead gaps longer than ``min_gap`` are cut
out (keeping ``pad`` seconds of breathing room around speech), and a subtle
punch-in zoom alternates between the resulting jump cuts — the classic
"addictive" short-form editing rhythm. Caption word timestamps are remapped
through the cut so karaoke captions stay in sync.

All cutting happens in ONE ffmpeg filter_complex pass (trim/atrim + concat)
with 15 ms audio micro-fades at every joint to kill clicks. If cutting isn't
worthwhile (or looks suspicious), :func:`speech_intervals` returns [] and the
caller keeps the original clip.
"""

from __future__ import annotations

import json
import logging
import subprocess

from .transcribe import Word

logger = logging.getLogger(__name__)

_MAX_INTERVALS = 80   # ffmpeg filter-graph length safety (Windows arg limits)
_FADE = 0.015         # audio micro-fade at each cut joint (seconds)


# ---------------------------------------------------------------------------
# interval planning (pure python — unit-tested)
# ---------------------------------------------------------------------------
def speech_intervals(words, duration: float, min_gap: float = 0.45,
                     pad: float = 0.12) -> list[tuple[float, float]]:
    """Keep-intervals covering speech. ``[]`` means "don't cut".

    Words must be clip-local (0-based). Guards: nothing to cut (<1.5s of
    removable silence isn't worth a re-encode) and a sanity floor — if cutting
    would drop more than 45% of the clip something is off (music, bad
    transcript) and the original is kept.
    """
    if not words or duration <= 0:
        return []
    groups: list[list] = [[words[0]]]
    for prev, cur in zip(words, words[1:]):
        if cur.start - prev.end <= min_gap:
            groups[-1].append(cur)
        else:
            groups.append([cur])

    intervals: list[tuple[float, float]] = []
    for g in groups:
        a = max(0.0, g[0].start - pad)
        b = min(duration, g[-1].end + pad)
        if b <= a:
            continue
        if intervals and a <= intervals[-1][1]:
            intervals[-1] = (intervals[-1][0], max(intervals[-1][1], b))
        else:
            intervals.append((a, b))
    if not intervals:
        return []

    kept = sum(b - a for a, b in intervals)
    if duration - kept < 1.5:
        return []            # nothing meaningful to cut
    if kept < duration * 0.55:
        logger.info("silence cut skipped: would keep only %.0f%% of the clip",
                    100 * kept / duration)
        return []
    return _cap_intervals(intervals, _MAX_INTERVALS)


def _cap_intervals(intervals: list[tuple[float, float]],
                   limit: int) -> list[tuple[float, float]]:
    """Merge across the smallest gaps until at most ``limit`` intervals."""
    ivs = list(intervals)
    while len(ivs) > limit:
        gaps = [(ivs[i + 1][0] - ivs[i][1], i) for i in range(len(ivs) - 1)]
        _, i = min(gaps)
        ivs[i] = (ivs[i][0], ivs[i + 1][1])
        del ivs[i + 1]
    return ivs


def remap_words(words, intervals) -> list[Word]:
    """Map clip-local word times through the cut to the new timeline."""
    if not intervals:
        return list(words or [])
    offsets = []
    ofs = 0.0
    for a, b in intervals:
        offsets.append((a, b, ofs))
        ofs += b - a
    total = ofs

    def map_t(t: float) -> float:
        for a, b, o in offsets:
            if t < a:
                return o           # inside a removed gap -> snap to next start
            if t <= b:
                return (t - a) + o
        return total

    out: list[Word] = []
    for w in words or []:
        center = (w.start + w.end) / 2.0
        ns = map_t(w.start)
        ne = map_t(w.end)
        if ne - ns < 0.05:  # word swallowed by a cut edge — keep it readable
            ns = min(map_t(center), total - 0.05)
            ne = ns + max(0.05, w.end - w.start)
        out.append(Word(max(0.0, ns), min(total, max(ne, ns + 0.05)), w.text))
    return out


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------
def _probe(path: str) -> tuple[int, int, bool]:
    """(width, height, has_audio) via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
         path], capture_output=True, text=True, check=True, timeout=60)
    width = height = 0
    has_audio = False
    for s in json.loads(out.stdout).get("streams", []):
        if s.get("codec_type") == "video" and not width:
            width, height = int(s.get("width", 0)), int(s.get("height", 0))
        elif s.get("codec_type") == "audio":
            has_audio = True
    if not width or not height:
        raise RuntimeError(f"no video stream in {path}")
    return width, height, has_audio


def cut_video(src: str, out: str, intervals: list[tuple[float, float]], *,
              zoom_alternate: bool = True, zoom: float = 1.06) -> float:
    """Cut ``src`` down to ``intervals`` (one encode pass). Returns the new
    duration. Raises on ffmpeg failure — callers catch and keep the original."""
    if not intervals:
        raise ValueError("no intervals to keep")
    width, height, has_audio = _probe(src)

    # Odd intervals get a punch-in: crop the center then scale back to the
    # exact original WxH (integers computed here so concat inputs always match).
    cw = int(width / zoom) // 2 * 2
    ch = int(height / zoom) // 2 * 2
    cx, cy = (width - cw) // 2, (height - ch) // 2

    parts: list[str] = []
    concat_in: list[str] = []
    for i, (a, b) in enumerate(intervals):
        zf = (f",crop={cw}:{ch}:{cx}:{cy},scale={width}:{height}"
              if zoom_alternate and i % 2 == 1 else "")
        # setsar=1 on EVERY segment: scale adjusts SAR to preserve DAR after
        # the crop's even-rounding, and concat rejects mismatched SARs.
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},"
                     f"setpts=PTS-STARTPTS{zf},setsar=1[v{i}]")
        concat_in.append(f"[v{i}]")
        if has_audio:
            fade_out = max(0.0, (b - a) - _FADE)
            parts.append(
                f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
                f"afade=t=in:st=0:d={_FADE},"
                f"afade=t=out:st={fade_out:.3f}:d={_FADE}[a{i}]")
            concat_in.append(f"[a{i}]")

    n = len(intervals)
    if has_audio:
        parts.append("".join(concat_in) + f"concat=n={n}:v=1:a=1[v][a]")
    else:
        parts.append("".join(concat_in) + f"concat=n={n}:v=1:a=0[v]")

    cmd = ["ffmpeg", "-y", "-i", src, "-filter_complex", ";".join(parts),
           "-map", "[v]"]
    if has_audio:
        cmd += ["-map", "[a]", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-movflags", "+faststart", out]
    total = sum(b - a for a, b in intervals)
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=max(300.0, total * 8))
    logger.info("silence cut: %d joints, %.1fs kept", n, total)
    return total
