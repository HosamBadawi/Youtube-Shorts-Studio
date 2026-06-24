"""Low-level image compositing helpers (NumPy + OpenCV).

These build the exact ``out_h x out_w`` canvases used by the preservation
strategies, and the crop/resize used by the focus strategies. All functions are
distortion-free: source pixels are never anisotropically stretched.
"""

from __future__ import annotations

import cv2
import numpy as np

BGRFrame = np.ndarray  # H x W x 3, uint8


def crop_and_resize(
    frame: BGRFrame,
    cx: float,
    cy: float,
    cw: float,
    ch: float,
    out_w: int,
    out_h: int,
) -> BGRFrame:
    """Crop a ``cw x ch`` window centred at ``(cx, cy)`` and resize to output.

    The crop is clamped to the frame; integer rounding is handled so the result
    is exactly ``out_h x out_w``.
    """

    h, w = frame.shape[:2]
    x1 = int(round(cx - cw / 2.0))
    y1 = int(round(cy - ch / 2.0))
    x2 = int(round(x1 + cw))
    y2 = int(round(y1 + ch))
    x1 = max(0, min(x1, w - 1))
    y1 = max(0, min(y1, h - 1))
    x2 = max(x1 + 1, min(x2, w))
    y2 = max(y1 + 1, min(y2, h))
    crop = frame[y1:y2, x1:x2]
    interp = cv2.INTER_AREA if crop.shape[1] > out_w else cv2.INTER_CUBIC
    return cv2.resize(crop, (out_w, out_h), interpolation=interp)


def _scaled_to_width(frame: BGRFrame, out_w: int) -> BGRFrame:
    h, w = frame.shape[:2]
    scale = out_w / w
    new_h = max(1, int(round(h * scale)))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    return cv2.resize(frame, (out_w, new_h), interpolation=interp)


def _cover_resize(frame: BGRFrame, out_w: int, out_h: int) -> BGRFrame:
    """Resize so the frame fully covers the canvas (then we centre-crop)."""

    h, w = frame.shape[:2]
    scale = max(out_w / w, out_h / h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    resized = cv2.resize(frame, (nw, nh), interpolation=interp)
    x0 = (nw - out_w) // 2
    y0 = (nh - out_h) // 2
    return resized[y0 : y0 + out_h, x0 : x0 + out_w]


def _paste_center(canvas: BGRFrame, fg: BGRFrame) -> BGRFrame:
    """Paste ``fg`` centred onto ``canvas`` (both must fit)."""

    ch, cw = canvas.shape[:2]
    fh, fw = fg.shape[:2]
    if fh > ch:  # too tall: centre-crop the foreground vertically
        y0 = (fh - ch) // 2
        fg = fg[y0 : y0 + ch]
        fh = ch
    if fw > cw:
        x0 = (fw - cw) // 2
        fg = fg[:, x0 : x0 + cw]
        fw = cw
    y = (ch - fh) // 2
    x = (cw - fw) // 2
    canvas[y : y + fh, x : x + fw] = fg
    return canvas


def letterbox(
    frame: BGRFrame, out_w: int, out_h: int, color: tuple[int, int, int]
) -> BGRFrame:
    """Fit the *entire* frame inside the canvas with solid bars (No-Crop)."""

    h, w = frame.shape[:2]
    scale = min(out_w / w, out_h / h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    fg = cv2.resize(frame, (nw, nh), interpolation=interp)
    canvas = np.empty((out_h, out_w, 3), dtype=np.uint8)
    canvas[:] = np.array(color, dtype=np.uint8)
    return _paste_center(canvas, fg)


def blur_background(
    frame: BGRFrame,
    out_w: int,
    out_h: int,
    ksize: int,
    bg_zoom: float,
) -> BGRFrame:
    """Foreground fitted to width over a blurred, over-scaled copy of itself.

    The background blur is computed at quarter resolution with a proportionally
    smaller kernel, then upscaled. The result is visually identical to a
    full-res Gaussian blur but ~10-20x cheaper - which matters because this runs
    on every frame of every rendered short.
    """

    scale = 0.25
    sw, sh = max(1, int(out_w * scale)), max(1, int(out_h * scale))
    bg = _cover_resize(frame, sw, sh)
    k = max(3, int(ksize * scale)) | 1  # GaussianBlur needs an odd kernel
    bg = cv2.GaussianBlur(bg, (k, k), 0)
    bg = (bg.astype(np.float32) * 0.82).astype(np.uint8)  # darken slightly
    bg = cv2.resize(bg, (out_w, out_h), interpolation=cv2.INTER_LINEAR)
    fg = _scaled_to_width(frame, out_w)
    return _paste_center(bg, fg)


def mirror_background(frame: BGRFrame, out_w: int, out_h: int) -> BGRFrame:
    """Fill the top/bottom by vertically mirroring the foreground edges."""

    fg = _scaled_to_width(frame, out_w)
    fh = fg.shape[0]
    canvas = np.empty((out_h, out_w, 3), dtype=np.uint8)
    if fh >= out_h:
        return _paste_center(canvas, fg)

    y = (out_h - fh) // 2
    canvas[y : y + fh] = fg

    flip = cv2.flip(fg, 0)  # vertical mirror
    # Fill upward.
    cursor = y
    src_band = flip
    toggle = True
    while cursor > 0:
        band = src_band[-min(cursor, fh):]
        canvas[cursor - band.shape[0] : cursor] = band
        cursor -= band.shape[0]
        toggle = not toggle
        src_band = fg if toggle else flip
    # Fill downward.
    cursor = y + fh
    src_band = flip
    toggle = True
    while cursor < out_h:
        remaining = out_h - cursor
        band = src_band[: min(remaining, fh)]
        canvas[cursor : cursor + band.shape[0]] = band
        cursor += band.shape[0]
        toggle = not toggle
        src_band = fg if toggle else flip
    return canvas


def _dominant_colors(frame: BGRFrame, k: int = 2) -> list[tuple[int, int, int]]:
    small = cv2.resize(frame, (32, 32), interpolation=cv2.INTER_AREA)
    data = small.reshape(-1, 3).astype(np.float32)
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 8, 1.0)
    _, labels, centers = cv2.kmeans(
        data, k, None, crit, 2, cv2.KMEANS_PP_CENTERS
    )
    counts = np.bincount(labels.flatten(), minlength=k)
    order = np.argsort(-counts)
    return [tuple(int(c) for c in centers[i]) for i in order]


def dynamic_canvas(
    frame: BGRFrame, out_w: int, out_h: int, margin: float
) -> BGRFrame:
    """A designed canvas: vertical gradient from the frame's dominant colours
    with the content inset and given a soft shadow + thin border.
    """

    cols = _dominant_colors(frame, k=2)
    top = np.array(cols[0], dtype=np.float32)
    bot = np.array(cols[-1], dtype=np.float32) * 0.55
    ramp = np.linspace(0.0, 1.0, out_h, dtype=np.float32)[:, None]
    grad = (top[None, :] * (1 - ramp) + bot[None, :] * ramp).astype(np.uint8)
    canvas = np.repeat(grad[:, None, :], out_w, axis=1)
    canvas = cv2.GaussianBlur(canvas, (0, 0), sigmaX=out_w * 0.04)

    inset = int(out_w * margin)
    content_w = out_w - 2 * inset
    fg = _scaled_to_width(frame, content_w)
    fh = fg.shape[0]
    if fh > out_h - 2 * inset:
        fg = letterbox(frame, content_w, out_h - 2 * inset, tuple(cols[-1]))
        fh = fg.shape[0]
    y = (out_h - fh) // 2
    x = inset
    # Soft shadow.
    shadow = canvas.copy()
    cv2.rectangle(
        shadow, (x - 6, y - 6), (x + content_w + 6, y + fh + 6), (0, 0, 0), -1
    )
    shadow = cv2.GaussianBlur(shadow, (0, 0), sigmaX=12)
    canvas = cv2.addWeighted(canvas, 0.7, shadow, 0.3, 0)
    canvas[y : y + fh, x : x + content_w] = fg
    cv2.rectangle(
        canvas,
        (x, y),
        (x + content_w - 1, y + fh - 1),
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return canvas
