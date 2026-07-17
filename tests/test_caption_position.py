"""The numeric caption-position override (percent UP from the bottom —
bigger = higher) — and proof the presets are byte-for-byte untouched when it
is not set."""

from __future__ import annotations

import pytest

from studio.captions import CaptionStyle, build_ass
from studio.transcribe import Word

H = 1920
EN = [Word(0.0, 0.5, "hello"), Word(0.5, 1.0, "world")]
AR = [Word(0.0, 0.5, "مرحبا"), Word(0.5, 1.0, "بالعالم")]


def margin_v(ass: str) -> int:
    style_line = next(ln for ln in ass.splitlines() if ln.startswith("Style:"))
    return int(style_line.split(",")[-2])


def test_presets_unchanged_without_override():
    # identical output whether the field is left default or passed as None
    a = build_ass(EN, CaptionStyle(), 1080, H)
    b = build_ass(EN, CaptionStyle(pos_pct=None), 1080, H)
    assert a == b
    assert margin_v(a) == int(H * 0.16)          # "lower" preset, as always
    assert margin_v(build_ass(EN, CaptionStyle(position="bottom"),
                              1080, H)) == int(H * 0.06)


@pytest.mark.parametrize("pct,expected", [(60, int(H * 0.60)),
                                          (16, int(H * 0.16)),
                                          (30, int(H * 0.30))])
def test_numeric_override_lifts_from_bottom(pct, expected):
    # MarginV IS the lift from the bottom edge: bigger slider = higher text
    ass = build_ass(EN, CaptionStyle(pos_pct=pct), 1080, H)
    assert margin_v(ass) == expected


def test_numeric_override_clamped_on_screen():
    assert margin_v(build_ass(EN, CaptionStyle(pos_pct=1), 1080, H)) \
        == int(H * 0.02)                         # floor: 2% up
    assert margin_v(build_ass(EN, CaptionStyle(pos_pct=100), 1080, H)) \
        == int(H * 0.85)                         # ceiling: 85% up


def test_rtl_absolute_path_honours_override():
    ass = build_ass(AR, CaptionStyle(pos_pct=60), 1080, H)
    if "\\pos(" not in ass:      # Pillow/font unavailable -> karaoke fallback
        pytest.skip("absolute RTL layout unavailable on this host")
    y = H - int(H * 60 / 100)
    assert f",{y})" in ass       # lifted 60% up from the bottom


def test_both_paths_land_at_the_same_height():
    """The LTR MarginV lift and the RTL absolute y must be two views of the
    same position: MarginV == H - y."""
    pct = 42
    en = build_ass(EN, CaptionStyle(pos_pct=pct), 1080, H)
    ar = build_ass(AR, CaptionStyle(pos_pct=pct), 1080, H)
    if "\\pos(" not in ar:
        pytest.skip("absolute RTL layout unavailable on this host")
    lift = margin_v(en)
    assert lift == int(H * pct / 100)
    assert f",{H - lift})" in ar
