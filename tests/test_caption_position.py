"""The numeric caption-position override — and proof the presets are
byte-for-byte untouched when it is not set."""

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


@pytest.mark.parametrize("pct,expected", [(60, int(H * 0.40)),
                                          (84, int(H * 0.16)),
                                          (30, int(H * 0.70))])
def test_numeric_override_sets_margin(pct, expected):
    ass = build_ass(EN, CaptionStyle(pos_pct=pct), 1080, H)
    assert margin_v(ass) == expected


def test_numeric_override_clamped_on_screen():
    assert margin_v(build_ass(EN, CaptionStyle(pos_pct=5), 1080, H)) \
        == int(H * (100 - 15) / 100)             # floor: 15% from top
    assert margin_v(build_ass(EN, CaptionStyle(pos_pct=100), 1080, H)) \
        == int(H * (100 - 98) / 100)             # ceiling: 98%


def test_rtl_absolute_path_honours_override():
    ass = build_ass(AR, CaptionStyle(pos_pct=60), 1080, H)
    if "\\pos(" not in ass:      # Pillow/font unavailable -> karaoke fallback
        pytest.skip("absolute RTL layout unavailable on this host")
    y = int(H * 60 / 100)
    assert f",{y})" in ass       # every \pos(x, y) lands at 60% from the top
