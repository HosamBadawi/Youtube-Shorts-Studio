"""Unit tests for the silence-cut planner + word remapping (no ffmpeg)."""

from __future__ import annotations

import pytest

from studio.montage import _cap_intervals, remap_words, speech_intervals
from studio.transcribe import Word


def w(s, e, t="x"):
    return Word(s, e, t)


def test_groups_and_pads():
    # two dense speech bursts separated by a 2s dead gap
    burst1 = [w(1.0 + i * 0.6, 1.5 + i * 0.6) for i in range(5)]   # 1.0-4.1
    burst2 = [w(6.0 + i * 0.6, 6.5 + i * 0.6) for i in range(5)]   # 6.0-9.1
    ivs = speech_intervals(burst1 + burst2, duration=10.0,
                           min_gap=0.45, pad=0.12)
    assert len(ivs) == 2
    assert ivs[0] == (pytest.approx(0.88), pytest.approx(4.02))
    assert ivs[1] == (pytest.approx(5.88), pytest.approx(9.02))


def test_small_gaps_not_cut():
    words = [w(0.0, 1.0), w(1.2, 2.0), w(2.3, 3.0)]  # gaps < 0.45
    # only ~0.5s of leading/trailing silence removable -> not worth it
    assert speech_intervals(words, duration=3.5) == []


def test_empty_words_means_no_cut():
    assert speech_intervals([], duration=30.0) == []


def test_over_cutting_guard():
    # one 3s burst in a 60s clip -> keeping <55% -> don't cut
    words = [w(10.0, 13.0)]
    assert speech_intervals(words, duration=60.0) == []


def test_interval_cap_merges_smallest_gaps():
    ivs = [(float(i), float(i) + 0.4) for i in range(0, 200, 2)]
    capped = _cap_intervals(ivs, 80)
    assert len(capped) == 80
    assert capped[0][0] == 0.0 and capped[-1][1] == ivs[-1][1]


def test_remap_words_monotonic_zero_based():
    words = [w(1.0, 1.5), w(1.6, 2.0), w(4.0, 4.5), w(4.6, 5.0)]
    ivs = [(0.88, 2.12), (3.88, 5.12)]
    remapped = remap_words(words, ivs)
    assert remapped[0].start == pytest.approx(1.0 - 0.88)
    # second burst starts right after the first interval's length
    assert remapped[2].start == pytest.approx((2.12 - 0.88) + (4.0 - 3.88))
    times = [r.start for r in remapped]
    assert times == sorted(times)
    assert all(r.end > r.start for r in remapped)


def test_remap_word_in_removed_gap_snaps():
    words = [w(2.5, 3.0)]  # centered inside the removed gap
    ivs = [(0.0, 2.0), (4.0, 6.0)]
    r = remap_words(words, ivs)[0]
    assert 0.0 <= r.start <= 4.0  # lands on the joined timeline
    assert r.end > r.start


def test_no_intervals_passthrough():
    words = [w(0.0, 1.0)]
    assert remap_words(words, []) == words
