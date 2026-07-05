"""Light smoke tests for thumbnail composition (no rembg, no video IO)."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PIL")
np = pytest.importorskip("numpy")

from studio.thumbnails.compose import CANVAS, make_thumbnail
from studio.thumbnails.headline import render_headline, shape


def _frame():
    f = np.zeros((1080, 1920, 3), dtype=np.uint8)
    f[:, :, 2] = 90
    f[200:600, 700:1200, :] = 180
    return f


def test_headline_renders_shaped_arabic():
    img = render_headline("لن تصدق ما حدث", 1800)
    assert img is not None and img.mode == "RGBA"
    assert img.width > img.height  # a headline, not a column
    # some ink actually landed
    assert img.getbbox() is not None


def test_shape_avoids_isolated_presentation_forms():
    shaped = shape("لن تصدق ما حدث في النهاية")
    # isolated forms missing from modern Arabic fonts must not appear
    assert not any(0xFE8D <= ord(c) <= 0xFEF4 and c in "ﺍﺙﻕ"
                   for c in shaped)


@pytest.mark.parametrize("template", ["blur", "burst", "flat"])
def test_make_thumbnail_all_templates(tmp_path, template):
    out = str(tmp_path / f"{template}.jpg")
    make_thumbnail(_frame(), None, "عنوان تجريبي كبير", template, out)
    from PIL import Image
    im = Image.open(out)
    assert im.size == CANVAS
    assert os.path.getsize(out) < 2_000_000


def test_empty_headline_is_skipped(tmp_path):
    out = str(tmp_path / "no_head.jpg")
    make_thumbnail(_frame(), None, "", "flat", out)
    assert os.path.exists(out)
