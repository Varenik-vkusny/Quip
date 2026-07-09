"""PURE-тесты для конфигурируемой длины клипов (clip_min_sec / clip_max_sec).

Проверяем валидацию на уровне select_segments, динамический промпт, пост-процесс гейт.
"""

from __future__ import annotations

import pytest

from app.errors import JobError
from app.models import Transcript, Word
from app.pipeline.stage2_select import (
    build_indexed_transcript,
    build_user_prompt,
    postprocess,
    select_segments,
)


def _words(n: int, sentence_ends: set[int] | None = None) -> list[Word]:
    ends = sentence_ends or set()
    return [
        Word(text=(f"w{k}." if k in ends else f"w{k}"), start=float(k), end=float(k) + 0.8)
        for k in range(n)
    ]


def _raw_segment(start_word: int, end_word: int, **kw: float) -> dict:
    return {
        "start_word_index": start_word,
        "end_word_index": end_word,
        "score": kw.get("score", 0.8),
        "type": "hook",
    }


class TestSelectSegmentsValidation:
    def test_raises_when_min_exceeds_duration(self) -> None:
        tr = Transcript(language="en", duration=10.0, words=_words(10))
        with pytest.raises(JobError, match="no segment can satisfy"):
            select_segments(tr, "title", clip_min_sec=30, clip_max_sec=60)

    def test_raises_when_min_exceeds_max(self) -> None:
        tr = Transcript(language="en", duration=120.0, words=_words(100))
        with pytest.raises(JobError, match="clip length range is inverted"):
            select_segments(tr, "title", clip_min_sec=60, clip_max_sec=20)


class TestPostprocessDynamicLimits:
    def test_drops_too_short_for_custom_min(self) -> None:
        words = _words(30)
        raw = [_raw_segment(5, 8)]  # ~2.4s < 10s
        result = postprocess(raw, words, min_sec=10, max_sec=60)
        assert len(result) == 0

    def test_keeps_segment_within_custom_range(self) -> None:
        words = _words(100)
        raw = [_raw_segment(10, 50)]  # ~32s, внутри [15, 60]
        result = postprocess(raw, words, min_sec=15, max_sec=60)
        assert len(result) == 1
        assert result[0].end > result[0].start

    def test_allows_very_short_clips_with_low_min(self) -> None:
        words = _words(60, sentence_ends={18})
        raw = [_raw_segment(5, 18)]  # ~10.4s, внутри [4, 180]
        result = postprocess(raw, words, min_sec=4, max_sec=180)
        assert len(result) == 1


class TestBuildUserPromptDynamic:
    def test_custom_values_appear_in_prompt(self) -> None:
        words = _words(80)
        tr = Transcript(language="en", duration=60.0, words=words)
        indexed = build_indexed_transcript(words)
        prompt = build_user_prompt("test", tr, indexed, max_clips=5, min_sec=4, max_sec=90)
        assert "4" in prompt
        assert "90" in prompt

    def test_defaults_appear_in_prompt(self) -> None:
        words = _words(120)
        tr = Transcript(language="en", duration=120.0, words=words)
        indexed = build_indexed_transcript(words)
        prompt = build_user_prompt("test", tr, indexed, max_clips=5)
        assert "20" in prompt
        assert "60" in prompt
