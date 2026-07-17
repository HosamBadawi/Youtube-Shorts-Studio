"""The never-overlap guarantee: spans of previously generated shorts from the
same source video are excluded from every later generation."""

from __future__ import annotations

from studio.jobs import (STATUS_DONE, STATUS_ERROR, STATUS_READY, JobStore)
from studio.pipeline import _first_free_window, _overlaps


def _job(store, source, seg, status):
    j = store.create(source)
    j.segment = seg
    j.status = status
    store.update(j)
    return j


def test_used_segments_only_rendered_jobs(tmp_path):
    store = JobStore(tmp_path / "t.db")
    src = str(tmp_path / "4.mp4")
    _job(store, src, (10.0, 60.0), STATUS_READY)
    _job(store, src, (100.0, 150.0), STATUS_DONE)
    _job(store, src, (200.0, 250.0), STATUS_ERROR)   # never rendered -> free
    _job(store, str(tmp_path / "5.mp4"), (300.0, 350.0), STATUS_READY)  # other video
    used = store.used_segments_for_source(src)
    assert used == [(10.0, 60.0), (100.0, 150.0)]


def test_used_segments_match_resolved_paths(tmp_path):
    store = JobStore(tmp_path / "t.db")
    src = tmp_path / "4.mp4"
    _job(store, str(src), (10.0, 60.0), STATUS_READY)
    # relative-ish / differently-cased query for the same file still matches
    alt = str(tmp_path / "." / "4.mp4")
    assert store.used_segments_for_source(alt) == [(10.0, 60.0)]


def test_overlaps_tolerance():
    used = [(100.0, 150.0)]
    assert _overlaps(120.0, 170.0, used)          # 30s overlap
    assert not _overlaps(150.2, 200.0, used)      # touches within tolerance
    assert not _overlaps(0.0, 99.0, used)


def test_first_free_window_skips_used():
    used = [(45.0, 100.0), (130.0, 180.0)]
    win = _first_free_window(duration=600.0, floor=45.0, target_len=25.0,
                             used=used)
    assert win is not None
    s, e = win
    assert not _overlaps(s, e, used)
    assert s >= 45.0 and e - s >= 5.0


def test_first_free_window_full_video():
    used = [(0.0, 600.0)]
    assert _first_free_window(600.0, 45.0, 25.0, used) is None
