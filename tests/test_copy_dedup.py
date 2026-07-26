"""Two shorts from the same video must never get the same title/headline."""

from __future__ import annotations

import json

from studio.llm import _BaseLLM, _is_duplicate, _norm_title


def reply(title, description="وصف مختلف تماما", headline="عنوان مصغرة",
          hashtags=("#تجربة",)):
    return json.dumps({"title": title, "description": description,
                       "headline": headline, "hashtags": list(hashtags)},
                      ensure_ascii=False)


class FakeLLM(_BaseLLM):
    """Replays canned JSON replies and records the sampling temperatures."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.temps: list[float | None] = []

    def available(self):
        return True

    def resolve_model(self):
        return "fake"

    def _generate(self, prompt, want_json=True, schema=None, temperature=None):
        self.temps.append(temperature)
        return self.replies.pop(0) if self.replies else None


# --- normalization / comparison ---------------------------------------------
def test_norm_ignores_harakat_and_alef_variants():
    assert _norm_title("لَنْ تُصَدِّق") == _norm_title("لن تصدق")
    assert _norm_title("أغرب حقيقة") == _norm_title("اغرب حقيقة")


def test_duplicate_detection():
    used = ["لن تصدق ماذا حدث بعد هذا"]
    assert _is_duplicate("لن تصدق ماذا حدث بعد هذا", used)      # exact
    assert _is_duplicate("لن تصدق ماذا حدث بعد هذا!", used)     # punctuation
    assert _is_duplicate("لن تصدق ماذا حدث بعد ذلك", used)      # near-identical
    assert not _is_duplicate("هل تعلم السر الحقيقي وراء النجاح", used)
    assert not _is_duplicate("", used)


# --- generate_copy ------------------------------------------------------------
def test_retries_until_the_title_is_unique():
    dup = "لن تصدق ماذا حدث"
    llm = FakeLLM([reply(dup), reply("هل تعلم السر وراء هذا الاكتشاف")])
    meta = llm.generate_copy("نص المقطع الثاني", avoid_titles=[dup])
    assert meta is not None
    assert meta.title == "هل تعلم السر وراء هذا الاكتشاف"
    assert len(llm.temps) == 2 and llm.temps[1] > llm.temps[0]  # hotter retry


def test_headline_collision_also_retried():
    llm = FakeLLM([reply("عنوان جديد", headline="نفس المصغرة"),
                   reply("عنوان اخر", headline="مصغرة مختلفة")])
    meta = llm.generate_copy("نص", avoid_headlines=["نفس المصغرة"])
    assert meta.thumbnail_headline == "مصغرة مختلفة"


def test_falls_back_to_the_headline_when_the_model_keeps_repeating():
    dup = "لن تصدق ماذا حدث"
    llm = FakeLLM([reply(dup, headline="سر الكون الغامض")] * 3)
    meta = llm.generate_copy("نص", avoid_titles=[dup])
    assert meta is not None
    # never ships the duplicate — promotes this clip's own headline instead
    assert not _is_duplicate(meta.title, [dup])
    assert meta.title == "سر الكون الغامض"


def test_headline_falls_back_to_the_description_lead():
    """When the model repeats BOTH fields, the headline must still end up
    unique — otherwise two shorts get the same thumbnail text."""
    dup_t, dup_h = "عنوان مكرر", "مصغرة مكررة"
    llm = FakeLLM([reply(dup_t, description="سر الجاذبية في الكون. تفاصيل",
                         headline=dup_h)] * 3)
    meta = llm.generate_copy("نص", avoid_titles=[dup_t],
                             avoid_headlines=[dup_h])
    assert not _is_duplicate(meta.title, [dup_t])
    assert not _is_duplicate(meta.thumbnail_headline, [dup_h])
    assert meta.thumbnail_headline == "سر الجاذبية في الكون"


def test_single_short_makes_one_warm_call():
    llm = FakeLLM([reply("عنوان وحيد")])
    meta = llm.generate_copy("نص")
    assert meta.title == "عنوان وحيد"
    assert llm.temps == [0.7]      # creative, not the 0.2 schema default


def test_invalid_json_still_retried():
    llm = FakeLLM(["not json at all", reply("عنوان صالح")])
    assert llm.generate_copy("نص").title == "عنوان صالح"
