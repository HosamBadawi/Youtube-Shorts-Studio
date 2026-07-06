"""Unit tests for the semantic segmenter (no network, no Ollama)."""

from __future__ import annotations

import json

from studio.segmenter import (SegmentPick, _dedup, build_sentences,
                              select_segments)
from studio.sponsorblock import extract_video_id
from studio.transcribe import Word


def words_from(spec):
    """spec: list of (start, end, text)."""
    return [Word(s, e, t) for s, e, t in spec]


def make_words(n, t0=0.0, dur=2.0, gap=0.1, punct_every=5):
    """n sentences of `punct_every` words each, back to back."""
    out, t = [], t0
    for i in range(n * punct_every):
        text = f"w{i}"
        if (i + 1) % punct_every == 0:
            text += "."
        out.append(Word(t, t + dur, text))
        t += dur + gap
    return out


class FakeLLM:
    """Routes by schema shape: clips / junk_idx / order."""

    def __init__(self, clips=None, junk=None, order=None):
        self.clips = clips if clips is not None else []
        self.junk = junk or []
        self.order = order
        self.map_calls = 0

    def available(self):
        return True

    def generate_json(self, prompt, schema=None):
        props = (schema or {}).get("properties", {})
        if "clips" in props:
            self.map_calls += 1
            return {"clips": self.clips}
        if "junk_idx" in props:
            return {"junk_idx": self.junk}
        if "order" in props:
            return {"order": self.order or []}
        return None


class Transcript:
    def __init__(self, words):
        self.words = words


# --- build_sentences ---------------------------------------------------------
def test_sentences_split_on_punctuation():
    ws = words_from([(0, 1, "hello"), (1.1, 2, "world."),
                     (2.1, 3, "next"), (3.1, 4, "one؟"),
                     (4.1, 5, "tail")])
    sents = build_sentences(ws)
    assert [s.text for s in sents] == ["hello world.", "next one؟", "tail"]
    assert sents[0].start == 0 and sents[0].end == 2


def test_sentences_split_on_gap_and_cap():
    # 1.5s gap forces a split without punctuation
    ws = words_from([(0, 1, "a"), (1.1, 2, "b"), (3.6, 4, "c")])
    assert len(build_sentences(ws, max_gap=0.7)) == 2
    # word cap forces splits in punctuation-free streams
    ws = words_from([(i, i + 0.5, f"w{i}") for i in range(70)])
    sents = build_sentences(ws, max_gap=99, max_words=30)
    assert len(sents) == 3


# --- select_segments ---------------------------------------------------------
def test_short_clip_extends_to_sentence_boundaries():
    words = make_words(20)  # 20 sentences, each ~10.4s
    sents = build_sentences(words)
    # model picks a single sentence (~10s) — must grow to >= 20s
    llm = FakeLLM(clips=[{"start_idx": 5, "end_idx": 5,
                          "hook_quote": sents[5].text, "topic": "t",
                          "score": 80, "reason": "r"}])
    picks = select_segments(llm, Transcript(words), duration=300.0, count=1,
                            min_s=20, max_s=40)
    assert len(picks) == 1
    d = picks[0].end - picks[0].start
    assert 20 <= d <= 41
    assert picks[0].start == sents[5].start  # hook kept


def test_junk_overlap_rejected():
    words = make_words(20)
    sents = build_sentences(words)
    llm = FakeLLM(clips=[{"start_idx": 2, "end_idx": 4,
                          "hook_quote": sents[2].text, "topic": "t",
                          "score": 90, "reason": "r"}])
    junk = [(sents[3].start, sents[3].end)]  # middle sentence is sponsor
    picks = select_segments(llm, Transcript(words), duration=300.0, count=1,
                            min_s=20, max_s=40, junk=junk)
    assert picks == []


def test_dedup_keeps_higher_score():
    a = SegmentPick(0, 30, score=90)
    b = SegmentPick(10, 40, score=50)  # 20s overlap of a 30s clip -> >50%
    c = SegmentPick(100, 130, score=70)
    kept = _dedup([a, b, c])
    assert a in kept and c in kept and b not in kept


def test_no_padding_when_llm_finds_nothing():
    words = make_words(20)
    llm = FakeLLM(clips=[])
    picks = select_segments(llm, Transcript(words), duration=300.0, count=5,
                            min_s=20, max_s=40)
    assert picks == []


def test_hook_mismatch_shifts_to_best_sentence():
    # Distinct sentence texts so fuzzy matching can discriminate.
    texts = ["the sky is blue today.", "cats chase red lasers.",
             "quantum tunneling is weird.", "bread needs slow yeast.",
             "volcanoes sleep for centuries.", "sharks predate the trees.",
             "honey never spoils at all.", "octopuses have three hearts.",
             "glass is an amorphous solid.", "maps distort every landmass."]
    words, t = [], 0.0
    for txt in texts:
        for w in txt.split():
            words.append(Word(t, t + 1.4, w))
            t += 1.5
    sents = build_sentences(words)
    # model says idx 6 but quotes sentence 7's text -> re-anchor to 7
    llm = FakeLLM(clips=[{"start_idx": 6, "end_idx": 9,
                          "hook_quote": sents[7].text, "topic": "t",
                          "score": 75, "reason": "r"}])
    picks = select_segments(llm, Transcript(words), duration=300.0, count=1,
                            min_s=10, max_s=45)
    assert len(picks) == 1
    assert abs(picks[0].start - sents[7].start) < 0.01


def test_intro_floor_masks_early_sentences():
    words = make_words(20)
    sents = build_sentences(words)
    llm = FakeLLM(clips=[{"start_idx": 0, "end_idx": 3,
                          "hook_quote": sents[0].text, "topic": "t",
                          "score": 95, "reason": "r"}])
    picks = select_segments(llm, Transcript(words), duration=300.0, count=1,
                            min_s=20, max_s=45, min_start=60.0,
                            junk=[(0.0, 0.0)])  # junk given -> no LLM classify
    assert picks == []  # clip entirely inside the intro floor


def test_snap_grows_start_when_end_overshoots():
    # Sentences: 0-13, 13-26, 26-39 then a LONG one 39-85 (46s). A pick on
    # [26,39] (13s) with min=50/max=60 cannot extend forward (39->85 = 59s?
    # no: 26->85 = 59 fits! use a longer tail) — craft: tail 39-100 (61s
    # overshoot). Growth must fall back to extending the START.
    spans = [(0, 13), (13, 26), (26, 39), (39, 100)]
    words = []
    for a, b in spans:
        n = 5
        step = (b - a) / n
        for i in range(n):
            t = a + step * i
            txt = f"s{a}w{i}" + ("." if i == n - 1 else "")
            words.append(Word(t, t + step, txt))  # contiguous: no gap splits
    sents = build_sentences(words)
    assert len(sents) == 4
    llm = FakeLLM(clips=[{"start_idx": 2, "end_idx": 2,
                          "hook_quote": sents[2].text, "topic": "t",
                          "score": 60, "reason": "r"}])
    picks = select_segments(llm, Transcript(words), duration=100.0, count=1,
                            min_s=30, max_s=45)
    # forward growth would be 26->100 (74s, overshoot); start-growth gives
    # 13->39 (26s, still short) then 0->39 (39s) — valid.
    assert len(picks) == 1
    assert picks[0].start == sents[0].start
    assert 30 <= picks[0].end - picks[0].start <= 46


# --- sponsorblock ------------------------------------------------------------
def test_extract_video_id_forms():
    vid = "dQw4w9WgXcQ"
    assert extract_video_id(f"https://www.youtube.com/watch?v={vid}") == vid
    assert extract_video_id(f"https://youtu.be/{vid}?t=1") == vid
    assert extract_video_id(f"https://youtube.com/shorts/{vid}") == vid
    assert extract_video_id("https://example.com/nope") is None
