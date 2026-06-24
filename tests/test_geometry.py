"""Unit tests for the pure geometry/signal helpers (no CV deps needed)."""

from __future__ import annotations

import math

from adaptive_reframe import geometry as geo


def test_max_crop_landscape_is_vertical_slice():
    cw, ch = geo.max_crop_for_aspect(1920, 1080, 9, 16)
    assert math.isclose(ch, 1080.0)
    assert math.isclose(cw, 1080.0 * 9 / 16)
    assert math.isclose(cw / ch, 9 / 16, rel_tol=1e-6)


def test_max_crop_portrait_limited_by_width():
    cw, ch = geo.max_crop_for_aspect(720, 1600, 9, 16)
    assert math.isclose(cw, 720.0)
    assert ch <= 1600.0
    assert math.isclose(cw / ch, 9 / 16, rel_tol=1e-6)


def test_clamp_center_keeps_box_inside():
    cx, cy = geo.clamp_center(0, 0, 200, 400, 1920, 1080)
    assert cx >= 100 and cy >= 200
    cx, cy = geo.clamp_center(5000, 5000, 200, 400, 1920, 1080)
    assert cx <= 1920 - 100 and cy <= 1080 - 200


def test_clamp_center_oversized_box_is_centered():
    cx, cy = geo.clamp_center(10, 10, 4000, 4000, 1920, 1080)
    assert math.isclose(cx, 960.0) and math.isclose(cy, 540.0)


def test_zoom_for_face_bounds():
    base_ch = 1080.0
    # Tiny face -> wants to punch in but capped at max_zoom.
    z = geo.zoom_for_face(60, base_ch, 0.34, 1.6)
    assert math.isclose(z, 1.6)
    # Huge face -> never below 1.0.
    z = geo.zoom_for_face(2000, base_ch, 0.34, 1.6)
    assert math.isclose(z, 1.0)


def test_interp_series_basic_and_clamped():
    ts = [0.0, 1.0, 2.0]
    vs = [0.0, 10.0, 20.0]
    assert math.isclose(geo.interp_series(ts, vs, 0.5), 5.0)
    assert math.isclose(geo.interp_series(ts, vs, 1.5), 15.0)
    assert math.isclose(geo.interp_series(ts, vs, -5), 0.0)  # clamp low
    assert math.isclose(geo.interp_series(ts, vs, 99), 20.0)  # clamp high


def test_one_euro_filter_reduces_jitter():
    f = geo.OneEuroFilter(freq=30.0, min_cutoff=0.5, beta=0.0)
    noisy = [0.0, 5.0, -5.0, 5.0, -5.0, 5.0, -5.0]
    out = [f(x, t / 30.0) for t, x in enumerate(noisy)]
    # Filtered signal must have smaller peak-to-peak than the noisy input.
    assert (max(out) - min(out)) < (max(noisy) - min(noisy))


def test_smooth_path_preserves_length():
    ts = [i / 10.0 for i in range(10)]
    vs = [float(i % 2) for i in range(10)]
    sm = geo.smooth_path(ts, vs, 0.5, 0.02)
    assert len(sm) == len(vs)


def test_smoothstep_endpoints():
    assert math.isclose(geo.smoothstep(0.0), 0.0)
    assert math.isclose(geo.smoothstep(1.0), 1.0)
    assert 0.0 < geo.smoothstep(0.5) < 1.0
