"""Video renderer.

Decodes the source with OpenCV, transforms every frame with the chosen
:class:`~adaptive_reframe.strategies.ReframeStrategy`, and pipes the raw output
frames into FFmpeg for H.264 encoding while muxing the *original* audio back in.

Piping ``rawvideo`` to FFmpeg (rather than using ``cv2.VideoWriter``) gives us
deterministic, high-quality encoding, correct ``yuv420p`` for social platforms,
and the original audio track without a second decode pass.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np

from .strategies import ReframeStrategy
from .types import ReframeParams

logger = logging.getLogger(__name__)


class FFmpegNotFoundError(RuntimeError):
    pass


def _ffmpeg_bin() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise FFmpegNotFoundError("ffmpeg executable not found on PATH")
    return exe


def _has_audio(video_path: str) -> bool:
    exe = shutil.which("ffprobe")
    if not exe:
        return True  # assume yes; FFmpeg will simply find no stream to map
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=index", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=30,
        )
        return bool(out.stdout.strip())
    except Exception:  # pragma: no cover
        return True


class Renderer:
    """Drive the frame loop and encode the final vertical clip."""

    def __init__(self, params: ReframeParams) -> None:
        self.p = params

    def render(
        self,
        video_path: str,
        strategy: ReframeStrategy,
        out_path: str,
        progress_every: int = 60,
    ) -> str:
        ffmpeg = _ffmpeg_bin()
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise FileNotFoundError(f"Cannot open video: {video_path}")
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            # Input 0: raw frames from stdin.
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{self.p.out_w}x{self.p.out_h}", "-r", f"{fps}", "-i", "-",
        ]
        audio = _has_audio(video_path)
        if audio:
            cmd += ["-i", video_path]
        cmd += [
            "-map", "0:v:0",
            *(["-map", "1:a:0?"] if audio else []),
            "-c:v", "libx264", "-preset", self.p.preset, "-crf", str(self.p.crf),
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-movflags", "+faststart",
            "-r", f"{fps}",
        ]
        if audio:
            cmd += ["-c:a", "aac", "-b:a", self.p.audio_bitrate, "-shortest"]
        cmd += [out_path]

        logger.info("Encoding -> %s (%dx%d @ %.2ffps, audio=%s)",
                    out_path, self.p.out_w, self.p.out_h, fps, audio)
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        assert proc.stdin is not None

        written = 0
        try:
            idx = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                t = idx / fps
                out = strategy.render_frame(frame, t)
                out = self._coerce(out)
                proc.stdin.write(out.tobytes())
                written += 1
                idx += 1
                if progress_every and written % progress_every == 0 and n_frames:
                    logger.info("  %d/%d frames (%.0f%%)", written, n_frames,
                                100.0 * written / n_frames)
        except BrokenPipeError:  # pragma: no cover
            pass
        finally:
            cap.release()
            try:
                proc.stdin.close()
            except Exception:
                pass
            # stdin is already closed above, so we must NOT call
            # proc.communicate() (it would try to flush the closed pipe and
            # raise "flush of closed file"). Drain stderr and wait directly.
            err = proc.stderr.read() if proc.stderr is not None else b""
            proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg failed ({proc.returncode}): "
                    f"{err.decode('utf-8', 'ignore')[-800:]}"
                )
        logger.info("Wrote %d frames to %s", written, out_path)
        return out_path

    def _coerce(self, frame: np.ndarray) -> np.ndarray:
        """Guarantee the frame is exactly out_h x out_w x 3 uint8 BGR."""

        if frame.dtype != np.uint8:
            frame = np.clip(frame, 0, 255).astype(np.uint8)
        if frame.shape[:2] != (self.p.out_h, self.p.out_w):
            frame = cv2.resize(frame, (self.p.out_w, self.p.out_h))
        if frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        return np.ascontiguousarray(frame)
