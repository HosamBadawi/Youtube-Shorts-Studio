"""Pure-Python geometry and signal-smoothing helpers.

Everything here uses only :mod:`math` so the math can be unit-tested without
NumPy/OpenCV installed. Pixel coordinates are floats; callers round at the very
last moment (when slicing an array).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def max_crop_for_aspect(
    width: float, height: float, aspect_w: int, aspect_h: int
) -> tuple[float, float]:
    """Return the size ``(w, h)`` of the largest centred crop with the target
    aspect ratio that still fits inside a ``width x height`` frame.

    For a landscape source and a 9:16 target this yields a tall, narrow crop
    spanning the full source height.
    """

    if width <= 0 or height <= 0:
        return 0.0, 0.0
    target = aspect_w / aspect_h
    if width / height > target:
        # Source is wider than target -> limited by height.
        ch = height
        cw = height * target
    else:
        # Source is taller/narrower than target -> limited by width.
        cw = width
        ch = width / target
    return cw, ch


def clamp_center(
    cx: float, cy: float, cw: float, ch: float, width: float, height: float
) -> tuple[float, float]:
    """Clamp a crop *centre* so a ``cw x ch`` box stays inside the frame.

    If the crop is larger than the frame on an axis it is simply centred.
    """

    if cw >= width:
        cx = width / 2.0
    else:
        half = cw / 2.0
        cx = min(max(cx, half), width - half)
    if ch >= height:
        cy = height / 2.0
    else:
        half = ch / 2.0
        cy = min(max(cy, half), height - half)
    return cx, cy


def zoom_for_face(
    face_h: float,
    base_crop_h: float,
    target_face_frac: float,
    max_zoom: float,
) -> float:
    """Compute a zoom factor so a face of height ``face_h`` occupies roughly
    ``target_face_frac`` of the crop height. Clamped to ``[1, max_zoom]``.
    """

    if face_h <= 0 or base_crop_h <= 0 or target_face_frac <= 0:
        return 1.0
    desired_crop_h = face_h / target_face_frac
    zoom = base_crop_h / desired_crop_h
    return float(min(max(zoom, 1.0), max_zoom))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def smoothstep(t: float) -> float:
    """Classic Hermite smoothstep on ``[0, 1]`` for gentle ease-in/out."""

    t = min(max(t, 0.0), 1.0)
    return t * t * (3.0 - 2.0 * t)


def interp_series(times: list[float], values: list[float], query: float) -> float:
    """Linearly interpolate a ``(times, values)`` series at ``query``.

    ``times`` must be sorted ascending. Queries outside the range are clamped
    to the nearest endpoint (constant extrapolation).
    """

    n = len(times)
    if n == 0:
        return 0.0
    if n == 1 or query <= times[0]:
        return values[0]
    if query >= times[-1]:
        return values[-1]
    # Binary search for the right interval.
    lo, hi = 0, n - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if times[mid] <= query:
            lo = mid
        else:
            hi = mid
    span = times[hi] - times[lo]
    frac = 0.0 if span <= 0 else (query - times[lo]) / span
    return lerp(values[lo], values[hi], frac)


@dataclass
class _LowPass:
    alpha: float
    _y: float | None = None

    def filter(self, x: float, alpha: float | None = None) -> float:
        a = self.alpha if alpha is None else alpha
        if self._y is None:
            self._y = x
        else:
            self._y = a * x + (1.0 - a) * self._y
        return self._y


class OneEuroFilter:
    """The "1€" filter (Casiez et al., 2012).

    Adaptive low-pass that trades latency against jitter based on signal speed.
    Used to smooth the crop-centre and zoom trajectories so the framing glides
    instead of snapping frame-to-frame.
    """

    def __init__(
        self,
        freq: float,
        min_cutoff: float = 1.0,
        beta: float = 0.0,
        d_cutoff: float = 1.0,
    ) -> None:
        if freq <= 0:
            raise ValueError("freq must be > 0")
        self.freq = float(freq)
        self.min_cutoff = float(min_cutoff)
        self.beta = float(beta)
        self.d_cutoff = float(d_cutoff)
        self._x = _LowPass(self._alpha(min_cutoff))
        self._dx = _LowPass(self._alpha(d_cutoff))
        self._last_t: float | None = None
        self._last_x: float | None = None

    def _alpha(self, cutoff: float) -> float:
        te = 1.0 / self.freq
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / te)

    def __call__(self, x: float, t: float | None = None) -> float:
        if t is not None and self._last_t is not None:
            dt = t - self._last_t
            if dt > 0:
                self.freq = 1.0 / dt
        self._last_t = t

        prev = self._last_x if self._last_x is not None else x
        dx = (x - prev) * self.freq
        edx = self._dx.filter(dx, self._alpha(self.d_cutoff))
        cutoff = self.min_cutoff + self.beta * abs(edx)
        y = self._x.filter(x, self._alpha(cutoff))
        self._last_x = x
        return y


def smooth_path(
    times: list[float],
    values: list[float],
    min_cutoff: float,
    beta: float,
) -> list[float]:
    """Smooth a value series with a One-Euro filter, preserving sample times."""

    if not values:
        return []
    freq = 30.0
    if len(times) >= 2:
        dts = [b - a for a, b in zip(times, times[1:]) if b > a]
        if dts:
            freq = 1.0 / (sum(dts) / len(dts))
    f = OneEuroFilter(freq=freq, min_cutoff=min_cutoff, beta=beta)
    return [f(v, t) for v, t in zip(values, times)]
