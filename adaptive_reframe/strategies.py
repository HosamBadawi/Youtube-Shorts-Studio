"""Reframing strategies — one class per supported mode.

Architecture (Strategy pattern + a small registry):

* :class:`ReframeStrategy` is the ABC. Each strategy precomputes whatever it
  needs in :meth:`prepare` and produces an exact ``out_h x out_w`` BGR frame in
  :meth:`render_frame`.
* Focus strategies (Face Focus / Active Speaker / Smart Crop) share a smoothed
  crop-trajectory implementation so framing glides instead of snapping.
* Preservation strategies (Blur / Mirror / Dynamic Canvas / No Crop) delegate to
  :mod:`.imaging`.
* :class:`SceneAwareStrategy` is a meta-strategy: it splits the clip at scene
  boundaries and dispatches each scene to a child strategy chosen for that scene.

The renderer only ever calls ``prepare`` once then ``render_frame`` per frame, so
strategies stay decoupled from video I/O.
"""

from __future__ import annotations

import dataclasses
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import geometry as geo
from . import imaging
from .types import ClipAnalysis, FaceObservation, ReframeMode, ReframeParams

logger = logging.getLogger(__name__)


@dataclass
class RenderContext:
    analysis: ClipAnalysis
    params: ReframeParams
    src_w: int
    src_h: int
    fps: float
    duration: float
    video_path: str


class ReframeStrategy(ABC):
    mode: ReframeMode

    def __init__(self, ctx: RenderContext) -> None:
        self.ctx = ctx
        self.p = ctx.params

    def prepare(self) -> None:  # optional override
        return None

    @abstractmethod
    def render_frame(self, frame: np.ndarray, t: float) -> np.ndarray:
        ...


# --------------------------------------------------------------------------- #
# Focus strategies (shared smoothed crop trajectory)
# --------------------------------------------------------------------------- #
@dataclass
class _Targets:
    times: list[float] = field(default_factory=list)
    cx: list[float] = field(default_factory=list)
    cy: list[float] = field(default_factory=list)
    zoom: list[float] = field(default_factory=list)


class _TrajectoryStrategy(ReframeStrategy):
    """Base for crop-and-zoom strategies with One-Euro smoothed trajectories."""

    def prepare(self) -> None:
        ctx = self.ctx
        self._base_cw, self._base_ch = geo.max_crop_for_aspect(
            ctx.src_w, ctx.src_h, self.p.out_w, self.p.out_h
        )
        targets = self._build_targets()
        if not targets.times:
            # No information: hold the centre at zoom 1.
            cx, cy = ctx.src_w / 2.0, ctx.src_h / 2.0
            targets = _Targets([0.0, ctx.duration], [cx, cx], [cy, cy], [1.0, 1.0])
        self._times = targets.times
        self._cx = geo.smooth_path(targets.times, targets.cx, self.p.min_cutoff,
                                   self.p.beta)
        self._cy = geo.smooth_path(targets.times, targets.cy, self.p.min_cutoff,
                                   self.p.beta)
        # Zoom is smoothed more gently so it never pumps.
        self._zoom = geo.smooth_path(targets.times, targets.zoom,
                                     self.p.min_cutoff * 0.5, self.p.beta * 0.5)

    @abstractmethod
    def _build_targets(self) -> _Targets:
        ...

    def _zoom_for(self, face_h: float) -> float:
        return geo.zoom_for_face(face_h, self._base_ch, self.p.target_face_frac,
                                 self.p.max_zoom)

    def render_frame(self, frame: np.ndarray, t: float) -> np.ndarray:
        z = max(1.0, geo.interp_series(self._times, self._zoom, t))
        cw = self._base_cw / z
        ch = self._base_ch / z
        cx = geo.interp_series(self._times, self._cx, t)
        cy = geo.interp_series(self._times, self._cy, t)
        cx, cy = geo.clamp_center(cx, cy, cw, ch, self.ctx.src_w, self.ctx.src_h)
        return imaging.crop_and_resize(frame, cx, cy, cw, ch, self.p.out_w,
                                       self.p.out_h)


class FaceFocusStrategy(_TrajectoryStrategy):
    mode = ReframeMode.FACE_FOCUS

    def _build_targets(self) -> _Targets:
        by_time = _group_faces_by_time(self.ctx.analysis.faces)
        tg = _Targets()
        for t in sorted(by_time):
            obs = by_time[t]
            dom = max(obs, key=lambda o: o.box.area)
            tg.times.append(t)
            tg.cx.append(dom.box.cx)
            tg.cy.append(dom.box.cy - dom.box.h * 0.08)  # bias slightly up
            tg.zoom.append(self._zoom_for(dom.box.h))
        return tg


class ActiveSpeakerStrategy(_TrajectoryStrategy):
    mode = ReframeMode.ACTIVE_SPEAKER

    def _build_targets(self) -> _Targets:
        by_time = _group_faces_by_time(self.ctx.analysis.faces)
        times = sorted(by_time)
        tg = _Targets()
        active_id: int | None = None
        hold_until = -1.0
        for t in times:
            obs = by_time[t]
            # Candidate = loudest mouth; switch only with margin + hold.
            cand = max(obs, key=lambda o: o.lip_activity)
            cur = next((o for o in obs if o.track_id == active_id), None)
            switch = (
                active_id is None
                or cur is None
                or (cand.lip_activity > (cur.lip_activity + self.p.speaker_switch_margin)
                    and t >= hold_until)
            )
            if switch and cand.track_id != active_id:
                active_id = cand.track_id
                hold_until = t + self.p.speaker_min_hold_s
            chosen = next((o for o in obs if o.track_id == active_id), cand)
            tg.times.append(t)
            tg.cx.append(chosen.box.cx)
            tg.cy.append(chosen.box.cy - chosen.box.h * 0.08)
            tg.zoom.append(self._zoom_for(chosen.box.h))
        return tg


class SmartCropStrategy(_TrajectoryStrategy):
    """Saliency-driven crop for footage with a subject but unreliable faces."""

    mode = ReframeMode.SMART_CROP

    def _build_targets(self) -> _Targets:
        # Prefer faces if we have any; else sample visual saliency.
        faces = self.ctx.analysis.faces
        if faces:
            by_time = _group_faces_by_time(faces)
            tg = _Targets()
            for t in sorted(by_time):
                dom = max(by_time[t], key=lambda o: o.box.area)
                tg.times.append(t)
                tg.cx.append(dom.box.cx)
                tg.cy.append(dom.box.cy)
                tg.zoom.append(min(1.25, self._zoom_for(dom.box.h)))
            return tg
        return self._saliency_targets()

    def _saliency_targets(self) -> _Targets:
        tg = _Targets()
        saliency = None
        if hasattr(cv2, "saliency"):
            try:
                saliency = cv2.saliency.StaticSaliencySpectralResidual_create()
            except Exception:  # pragma: no cover
                saliency = None
        cap = cv2.VideoCapture(self.ctx.video_path)
        fps = self.ctx.fps
        stride = max(1, int(round(fps / 4.0)))
        idx = 0
        cxc, cyc = self.ctx.src_w / 2.0, self.ctx.src_h / 2.0
        while True:
            ok = cap.grab()
            if not ok:
                break
            if idx % stride == 0:
                ok, frame = cap.retrieve()
                if not ok:
                    break
                cx, cy = cxc, cyc
                if saliency is not None:
                    ok2, sal = saliency.computeSaliency(frame)
                    if ok2:
                        m = (sal * 255).astype(np.uint8)
                        mom = cv2.moments(m)
                        if mom["m00"] > 1e-3:
                            cx = mom["m10"] / mom["m00"]
                            cy = mom["m01"] / mom["m00"]
                tg.times.append(idx / fps)
                tg.cx.append(cx)
                tg.cy.append(cy)
                tg.zoom.append(1.0)  # smart crop frames, doesn't punch in hard
            idx += 1
        cap.release()
        return tg


# --------------------------------------------------------------------------- #
# Preservation strategies
# --------------------------------------------------------------------------- #
class NoCropStrategy(ReframeStrategy):
    mode = ReframeMode.NO_CROP

    def render_frame(self, frame: np.ndarray, t: float) -> np.ndarray:
        return imaging.letterbox(frame, self.p.out_w, self.p.out_h,
                                 self.p.letterbox_color)


class BlurBackgroundStrategy(ReframeStrategy):
    mode = ReframeMode.BLUR_BACKGROUND

    def render_frame(self, frame: np.ndarray, t: float) -> np.ndarray:
        return imaging.blur_background(frame, self.p.out_w, self.p.out_h,
                                       self.p.blur_ksize, self.p.bg_zoom)


class CropBlurStrategy(ReframeStrategy):
    """Hybrid: a subject-tracked crop to an intermediate aspect (so the subject
    is enlarged) composited over a blurred fill of the leftover top/bottom.

    It reuses the focus trajectory's face centring for the horizontal crop
    position, then hands the cropped window to :func:`imaging.blur_background`,
    which scales it to full width and blur-fills the smaller remaining margins.
    """

    mode = ReframeMode.CROP_BLUR

    def prepare(self) -> None:
        ctx = self.ctx
        self._aspect = max(0.4, min(0.95, getattr(self.p, "combine_crop_aspect",
                                                  0.75)))
        times: list[float] = []
        xs: list[float] = []
        if ctx.analysis.faces:
            by_time = _group_faces_by_time(ctx.analysis.faces)
            for t in sorted(by_time):
                dom = max(by_time[t], key=lambda o: o.box.area)
                times.append(t)
                xs.append(dom.box.cx)
        if not times:  # no faces -> hold centre
            times = [0.0, max(ctx.duration, 0.1)]
            xs = [ctx.src_w / 2.0, ctx.src_w / 2.0]
        self._times = times
        self._cx = geo.smooth_path(times, xs, self.p.min_cutoff, self.p.beta)

    def render_frame(self, frame: np.ndarray, t: float) -> np.ndarray:
        h, w = frame.shape[:2]
        cw = min(w, int(round(h * self._aspect)))
        cx = geo.interp_series(self._times, self._cx, t)
        x1 = int(round(cx - cw / 2.0))
        x1 = max(0, min(x1, w - cw))
        crop = frame[:, x1:x1 + cw]
        return imaging.blur_background(crop, self.p.out_w, self.p.out_h,
                                       self.p.blur_ksize, self.p.bg_zoom)


class MirrorBackgroundStrategy(ReframeStrategy):
    mode = ReframeMode.MIRROR_BACKGROUND

    def render_frame(self, frame: np.ndarray, t: float) -> np.ndarray:
        return imaging.mirror_background(frame, self.p.out_w, self.p.out_h)


class DynamicCanvasStrategy(ReframeStrategy):
    mode = ReframeMode.DYNAMIC_CANVAS

    def render_frame(self, frame: np.ndarray, t: float) -> np.ndarray:
        return imaging.dynamic_canvas(frame, self.p.out_w, self.p.out_h,
                                      self.p.canvas_margin)


# --------------------------------------------------------------------------- #
# Scene-aware meta-strategy
# --------------------------------------------------------------------------- #
class SceneAwareStrategy(ReframeStrategy):
    mode = ReframeMode.SCENE_AWARE

    def prepare(self) -> None:
        a = self.ctx.analysis
        self._children: list[tuple[float, float, ReframeStrategy]] = []
        for scene in a.scenes:
            faces = a.faces_in(scene.start, scene.end)
            present = len({round(f.t, 2) for f in faces})
            ratio = present / max(1, int(scene.duration * 5)) if scene.duration else 0
            stable_face = ratio > 0.5 and bool(faces)
            child_cls = FaceFocusStrategy if stable_face else BlurBackgroundStrategy
            scoped = dataclasses.replace(a, faces=faces, scenes=[scene])
            child_ctx = dataclasses.replace(self.ctx, analysis=scoped)
            child = child_cls(child_ctx)
            child.prepare()
            self._children.append((scene.start, scene.end, child))
            logger.info("Scene %.1f-%.1fs -> %s", scene.start, scene.end,
                        child.mode.value)
        if not self._children:
            fallback = BlurBackgroundStrategy(self.ctx)
            fallback.prepare()
            self._children.append((0.0, self.ctx.duration, fallback))

    def render_frame(self, frame: np.ndarray, t: float) -> np.ndarray:
        for start, end, child in self._children:
            if start <= t < end:
                return child.render_frame(frame, t)
        return self._children[-1][2].render_frame(frame, t)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
STRATEGY_REGISTRY: dict[ReframeMode, type[ReframeStrategy]] = {
    ReframeMode.FACE_FOCUS: FaceFocusStrategy,
    ReframeMode.ACTIVE_SPEAKER: ActiveSpeakerStrategy,
    ReframeMode.SMART_CROP: SmartCropStrategy,
    ReframeMode.SCENE_AWARE: SceneAwareStrategy,
    ReframeMode.BLUR_BACKGROUND: BlurBackgroundStrategy,
    ReframeMode.CROP_BLUR: CropBlurStrategy,
    ReframeMode.MIRROR_BACKGROUND: MirrorBackgroundStrategy,
    ReframeMode.DYNAMIC_CANVAS: DynamicCanvasStrategy,
    ReframeMode.NO_CROP: NoCropStrategy,
}


def build_strategy(mode: ReframeMode, ctx: RenderContext) -> ReframeStrategy:
    strategy = STRATEGY_REGISTRY[mode](ctx)
    strategy.prepare()
    return strategy


def _group_faces_by_time(
    faces: list[FaceObservation],
) -> dict[float, list[FaceObservation]]:
    grouped: dict[float, list[FaceObservation]] = {}
    for f in faces:
        grouped.setdefault(round(f.t, 3), []).append(f)
    return grouped
