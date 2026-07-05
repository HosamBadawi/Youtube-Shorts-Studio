"""Semantic segment selection: map -> validate -> reduce over the transcript.

Core rule: **the LLM never emits timestamps.** It sees a numbered sentence
table and returns sentence *indices*; times are resolved from Whisper word
timestamps in code, so every clip boundary lands exactly on a sentence
boundary. Small local models degrade on long inputs, so the transcript is
processed in overlapping windows (map), each pick is validated and snapped in
code, and a final ranking pass (reduce) chooses the best ``count`` clips.

Honesty over padding: when fewer than ``count`` genuinely strong,
self-contained clips exist, fewer are returned — the caller surfaces the
reason instead of filling with blind cuts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Sentence-ending punctuation (Latin + Arabic). The Arabic comma ، is NOT a
# sentence break.
_SENTENCE_END = (".", "!", "?", "…", "؟")


@dataclass
class Sentence:
    idx: int
    start: float
    end: float
    text: str


@dataclass
class SegmentPick:
    start: float
    end: float
    topic: str = ""
    score: float = 0.0
    reason: str = ""


# ---------------------------------------------------------------------------
# sentence table
# ---------------------------------------------------------------------------
def build_sentences(words, max_gap: float = 0.7, max_words: int = 30
                    ) -> list[Sentence]:
    """Split flat Whisper words into sentences on punctuation, long pauses, or
    a hard word cap (protects against transcripts with no punctuation)."""
    sentences: list[Sentence] = []
    cur: list = []

    def flush() -> None:
        if not cur:
            return
        text = " ".join(w.text for w in cur).strip()
        if text:
            sentences.append(Sentence(len(sentences), cur[0].start,
                                      cur[-1].end, text))
        cur.clear()

    for i, w in enumerate(words or []):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt.start - w.end) if nxt else 0.0
        if (w.text.rstrip().endswith(_SENTENCE_END)
                or gap > max_gap or len(cur) >= max_words):
            flush()
    flush()
    return sentences


def _mask_indices(sentences: list[Sentence],
                  junk: list[tuple[float, float]],
                  min_start: float) -> set[int]:
    """Sentences that overlap a junk range or end before the intro floor."""
    masked: set[int] = set()
    for s in sentences:
        if s.end <= min_start:
            masked.add(s.idx)
            continue
        for a, b in junk:
            if s.start < b and s.end > a:
                masked.add(s.idx)
                break
    return masked


def _fmt_time(t: float) -> str:
    m, s = divmod(int(t), 60)
    return f"{m:02d}:{s:02d}"


def _table(sentences: list[Sentence]) -> str:
    return "\n".join(
        f"[{s.idx}] {_fmt_time(s.start)}-{_fmt_time(s.end)} {s.text}"
        for s in sentences)


def _fuzz(a: str, b: str) -> float:
    """0..1 similarity; rapidfuzz when installed, difflib otherwise."""
    a, b = a.strip(), b.strip()
    if not a or not b:
        return 0.0
    try:
        from rapidfuzz import fuzz  # type: ignore
        return fuzz.ratio(a, b) / 100.0
    except Exception:
        import difflib
        return difflib.SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# LLM schemas + prompts
# ---------------------------------------------------------------------------
_CLIPS_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_idx": {"type": "integer"},
                    "end_idx": {"type": "integer"},
                    "hook_quote": {"type": "string"},
                    "topic": {"type": "string"},
                    "score": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["start_idx", "end_idx", "hook_quote", "topic",
                             "score", "reason"],
            },
        },
    },
    "required": ["clips"],
}

_JUNK_SCHEMA = {
    "type": "object",
    "properties": {"junk_idx": {"type": "array", "items": {"type": "integer"}}},
    "required": ["junk_idx"],
}

_RANK_SCHEMA = {
    "type": "object",
    "properties": {"order": {"type": "array", "items": {"type": "integer"}}},
    "required": ["order"],
}

_MAP_PROMPT = """You are selecting clips for viral vertical Shorts from one \
window of a video transcript. Below is a numbered sentence table: \
[index] start-end text.

Select AT MOST 3 clips. Every clip MUST:
- START at the exact sentence where a strong hook lands — a bold claim, a \
surprising fact, a provocative question, or an emotional beat. NEVER start \
mid-setup or on a calm lead-in.
- be COMPLETELY understandable with zero outside context (a viewer sees only \
this clip),
- END right after its payoff / punchline / answer sentence — never leave an \
explanation unfinished,
- span between {min_s} and {max_s} seconds (each sentence's times are shown),
- contain NO greetings, channel branding, sponsor reads, ads, or \
"like & subscribe" filler.

For each clip give: start_idx and end_idx (sentence numbers from the table), \
hook_quote (the EXACT text of the first sentence), topic (2-5 words), score \
(0-100 — how strongly the first two seconds stop a scroller), reason (one \
short sentence).

If this window has no genuinely strong self-contained clip, return \
{{"clips": []}} — do NOT invent weak clips.

Sentence table:
{table}

Respond with ONLY a JSON object: {{"clips": [{{"start_idx": ..., \
"end_idx": ..., "hook_quote": "...", "topic": "...", "score": ..., \
"reason": "..."}}]}}"""

_JUNK_PROMPT = """Below are numbered sentences from the OPENING and ENDING of \
a video. Identify which sentence indices are junk that must never appear in a \
clip: greetings ("hi everyone…"), channel intros/branding, sponsor reads, \
ads, self-promo, "like & subscribe" reminders, or outro/goodbye filler. \
Sentences with real content are NOT junk.

{table}

Respond with ONLY a JSON object: {{"junk_idx": [numbers]}}"""

_RANK_PROMPT = """Rank these candidate Shorts clips from MOST to LEAST \
scroll-stopping. Judge by: hook strength in the first sentence, emotional \
punch, and how self-contained the idea is.

{listing}

Respond with ONLY a JSON object listing every clip number exactly once, best \
first: {{"order": [numbers]}}"""


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------
def select_segments(llm, transcript, duration: float, count: int,
                    min_s: float, max_s: float, *, min_start: float = 0.0,
                    junk: list[tuple[float, float]] | None = None,
                    window: int = 32, overlap: int = 8,
                    on_stage=None) -> list[SegmentPick]:
    """Pick up to ``count`` sentence-snapped clips from ``transcript``."""

    def stage(msg: str) -> None:
        if on_stage:
            try:
                on_stage(msg)
            except Exception:
                pass

    sentences = build_sentences(getattr(transcript, "words", None) or [])
    if len(sentences) < 3 or count < 1 or not llm.available():
        return []
    logger.info("segmenter: %d sentences over %.0fs", len(sentences), duration)

    masked = _mask_indices(sentences, junk or [], min_start)
    if not junk:
        masked |= _classify_junk(llm, sentences)
    logger.info("segmenter: %d sentence(s) masked as junk/intro", len(masked))

    # --- MAP: overlapping windows -> candidate clips -------------------------
    step = max(1, window - overlap)
    starts = list(range(0, max(1, len(sentences) - overlap), step))
    candidates: list[SegmentPick] = []
    for n, w0 in enumerate(starts, 1):
        stage(f"analyzing part {n}/{len(starts)}")
        win = sentences[w0:w0 + window]
        if all(s.idx in masked for s in win):
            continue
        prompt = _MAP_PROMPT.format(min_s=int(min_s), max_s=int(max_s),
                                    table=_table(win))
        for _attempt in range(2):
            obj = llm.generate_json(prompt, schema=_CLIPS_SCHEMA)
            clips = obj.get("clips") if isinstance(obj, dict) else None
            if isinstance(clips, list):
                candidates.extend(_validate(clips, sentences, win, masked,
                                            min_s, max_s, duration))
                break
    logger.info("segmenter: %d validated candidate(s)", len(candidates))
    if not candidates:
        return []

    # --- REDUCE: dedup overlaps, rank, take the best `count` -----------------
    deduped = _dedup(candidates)
    if len(deduped) > count:
        stage("ranking the best clips")
        deduped = _rank(llm, deduped)
    picked = sorted(deduped[:count], key=lambda p: p.start)
    logger.info("segmenter: returning %d/%d clip(s)", len(picked), count)
    return picked


def _classify_junk(llm, sentences: list[Sentence],
                   edge_seconds: float = 90.0) -> set[int]:
    """One cheap LLM pass over the first/last ~90s when SponsorBlock had no
    data. Failure -> empty set (the intro floor still protects)."""
    try:
        head = [s for s in sentences if s.start < edge_seconds]
        tail_from = sentences[-1].end - edge_seconds
        tail = [s for s in sentences if s.end > tail_from and s not in head]
        edge = head + tail
        if not edge:
            return set()
        obj = llm.generate_json(_JUNK_PROMPT.format(table=_table(edge)),
                                schema=_JUNK_SCHEMA)
        idx = obj.get("junk_idx") if isinstance(obj, dict) else None
        valid = {s.idx for s in edge}
        return {int(i) for i in idx if int(i) in valid} if isinstance(
            idx, list) else set()
    except Exception:
        return set()


def _validate(clips: list, sentences: list[Sentence], win: list[Sentence],
              masked: set[int], min_s: float, max_s: float,
              duration: float) -> list[SegmentPick]:
    """Ground each LLM pick: indices in range, hook verified, boundaries
    snapped to whole sentences, duration forced into [min_s, max_s]."""
    lo, hi = win[0].idx, win[-1].idx
    out: list[SegmentPick] = []
    for c in clips:
        try:
            si, ei = int(c["start_idx"]), int(c["end_idx"])
            hook = str(c.get("hook_quote", ""))
            score = max(0.0, min(100.0, float(c.get("score", 0))))
        except (KeyError, TypeError, ValueError):
            continue
        if not (lo <= si <= ei <= hi):
            continue
        # Hook sanity: the model sometimes off-by-ones the index. Re-anchor to
        # the best-matching sentence in the window before trusting it.
        if _fuzz(hook, sentences[si].text) < 0.55:
            best, best_r = si, 0.0
            for s in win:
                r = _fuzz(hook, s.text)
                if r > best_r:
                    best, best_r = s.idx, r
            if best_r < 0.55:
                continue
            si = best
            ei = max(ei, si)

        si, ei = _snap_duration(sentences, si, ei, masked, min_s, max_s)
        if si is None:
            continue
        if any(i in masked for i in range(si, ei + 1)):
            continue
        start = sentences[si].start
        end = min(duration, sentences[ei].end + 0.15)
        out.append(SegmentPick(start=start, end=end,
                               topic=str(c.get("topic", "")).strip(),
                               score=score,
                               reason=str(c.get("reason", "")).strip()))
    return out


def _snap_duration(sentences: list[Sentence], si: int, ei: int,
                   masked: set[int], min_s: float, max_s: float):
    """Grow/shrink [si, ei] by whole sentences until the span duration fits
    [min_s, max_s]. Growth prefers extending the END (keeps the hook opening);
    shrink always trims the tail. Returns (si, ei) or (None, None)."""
    last = len(sentences) - 1

    def dur(a: int, b: int) -> float:
        return sentences[b].end - sentences[a].start

    guard = 0
    while dur(si, ei) < min_s and guard < 500:
        guard += 1
        if ei < last and (ei + 1) not in masked:
            ei += 1
        elif si > 0 and (si - 1) not in masked:
            si -= 1
        else:
            return None, None
    while dur(si, ei) > max_s and ei > si:
        ei -= 1
    if not (min_s <= dur(si, ei) <= max_s):
        return None, None
    return si, ei


def _dedup(cands: list[SegmentPick]) -> list[SegmentPick]:
    """Drop the lower-scored clip of any pair overlapping >50% of the shorter."""
    kept: list[SegmentPick] = []
    for c in sorted(cands, key=lambda p: -p.score):
        clash = False
        for k in kept:
            inter = min(c.end, k.end) - max(c.start, k.start)
            shorter = min(c.end - c.start, k.end - k.start)
            if inter > 0 and shorter > 0 and inter / shorter > 0.5:
                clash = True
                break
        if not clash:
            kept.append(c)
    return kept


def _rank(llm, cands: list[SegmentPick]) -> list[SegmentPick]:
    """Relative ranking (far more reliable at 8B size than absolute scores).
    Falls back to score order on any model failure."""
    by_score = sorted(cands, key=lambda p: -p.score)
    short = by_score[:10]
    listing = "\n".join(
        f"[{i}] ({p.end - p.start:.0f}s) {p.topic} — {p.reason[:120]}"
        for i, p in enumerate(short))
    try:
        obj = llm.generate_json(_RANK_PROMPT.format(listing=listing),
                                schema=_RANK_SCHEMA)
        order = obj.get("order") if isinstance(obj, dict) else None
        if isinstance(order, list):
            seen: list[int] = []
            for i in order:
                if isinstance(i, int) and 0 <= i < len(short) and i not in seen:
                    seen.append(i)
            if seen:
                ranked = [short[i] for i in seen]
                ranked += [p for j, p in enumerate(short) if j not in seen]
                return ranked + by_score[10:]
    except Exception:
        pass
    return by_score
